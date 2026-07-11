"""
Metadata tagging for ingested legal documents.

Every chunk that reaches the vector store carries structured metadata so
the retriever can filter by law type, and so the API can show a proper
citation (source file, page, law type) instead of a bare vector match.

Classification is filename-based (substring rules), which is intentionally
conservative: the source corpus filenames are inconsistent (spaces, commas,
inconsistent casing — see `IPC 1860.pdf` vs `criminal_procedure,_1973.pdf`),
so precise keyword matching is more reliable than trying to parse
inconsistently-formatted names into a rigid schema. New documents just need
one entry added to `LAW_TYPE_RULES`; unmatched files fall back to "other"
rather than crashing the pipeline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LawType = str  # "statute" | "case_law" | "report" | "manual" | "other"

# Exact-stem abbreviations, checked before the substring rules. These are
# short tokens (e.g. "coi") that would false-positive as substrings
# (e.g. matching inside unrelated words), so they require the filename
# stem to equal the abbreviation exactly, not merely contain it.
LAW_TYPE_ABBREVIATIONS: dict[str, LawType] = {
    "coi": "statute",  # Constitution of India
}

# Ordered list of (substring, law_type). First match wins, so put more
# specific patterns first. Matching is case-insensitive against the
# filename stem only (not the full path).
LAW_TYPE_RULES: list[tuple[str, LawType]] = [
    ("vs_state", "case_law"),
    ("vs state", "case_law"),
    (" vs ", "case_law"),
    ("_vs_", "case_law"),
    ("constitution", "statute"),
    ("ipc", "statute"),
    ("indian penal code", "statute"),
    ("crpc", "statute"),
    ("criminal_procedure", "statute"),
    ("criminal procedure", "statute"),
    ("criminallaw", "statute"),
    ("criminal law", "statute"),
    ("it act", "statute"),
    ("it code", "statute"),
    ("information technology act", "statute"),
    ("prison", "statute"),
    ("annualreport", "report"),
    ("annual report", "report"),
    ("law commission report", "report"),
    ("commission defamation", "report"),
    ("commission defafmation", "report"),  # matches the actual (misspelled) filename in the corpus
    ("citizenmanual", "manual"),
    ("citizen manual", "manual"),
    ("instructions_citizenreporting", "manual"),
    ("cyberfraud", "manual"),
    ("cybercrime", "manual"),
]

# Matches "Section 302", "Sec. 34", "Article 21", "Art. 19(1)(a)" etc.
# Deliberately broad: false positives are harmless extra metadata,
# false negatives just mean no section-level filter is possible for
# that chunk, which degrades gracefully.
_SECTION_PATTERN = re.compile(
    r"\b(?:Section|Sec\.?|Article|Art\.?)\s+\d+[A-Za-z]*(?:\(\d+\))?(?:\([a-zA-Z]\))?",
    re.IGNORECASE,
)

# A bare 4-digit year (1500-2099), used to tag statutes/reports with the
# year they were enacted/published when it appears in the filename.
# Uses digit-adjacent lookaround rather than \b: `\b` does not create a
# boundary between two word characters, so it fails to match a year glued
# to a preceding letter or underscore (e.g. "CriminalLaw2018",
# "criminal_procedure,_1973" — underscore counts as \w). Requiring
# "not preceded/followed by another digit" is what we actually mean.
_YEAR_PATTERN = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")


@dataclass(frozen=True)
class DocumentMetadata:
    """Document-level metadata, computed once per source file."""

    source_file: str
    law_type: LawType
    year: int | None = None
    extra: dict = field(default_factory=dict)


def classify_law_type(filename: str) -> LawType:
    """Classify a source filename into a coarse law_type bucket."""
    stem = Path(filename).stem.lower().strip()
    if stem in LAW_TYPE_ABBREVIATIONS:
        return LAW_TYPE_ABBREVIATIONS[stem]
    for pattern, law_type in LAW_TYPE_RULES:
        if pattern in stem:
            return law_type
    return "other"


def extract_year(filename: str) -> int | None:
    """Best-effort year extraction from a filename. Returns None if absent/ambiguous."""
    match = _YEAR_PATTERN.search(Path(filename).stem)
    return int(match.group(1)) if match else None


def extract_sections(text: str, limit: int = 8) -> list[str]:
    """
    Extract distinct statutory section/article references mentioned in a
    chunk of text (e.g. ["Section 302", "Article 21"]), capped at `limit`
    to keep metadata payloads small. Order of first appearance is preserved.
    """
    seen: dict[str, None] = {}
    for match in _SECTION_PATTERN.finditer(text):
        normalized = re.sub(r"\s+", " ", match.group(0)).strip()
        if normalized not in seen:
            seen[normalized] = None
        if len(seen) >= limit:
            break
    return list(seen.keys())


def build_document_metadata(source_path: Path) -> DocumentMetadata:
    """Compute the document-level metadata for a source PDF, once per file."""
    filename = source_path.name
    return DocumentMetadata(
        source_file=filename,
        law_type=classify_law_type(filename),
        year=extract_year(filename),
    )


def tag_chunk_metadata(
    chunk_text: str,
    doc_metadata: DocumentMetadata,
    page_number: int | None,
    chunk_index: int,
) -> dict:
    """
    Build the final per-chunk metadata dict attached to a LangChain
    Document before it's embedded and written to the vector store / BM25
    index. This is the metadata the API will surface as a citation.
    """
    return {
        "source_file": doc_metadata.source_file,
        "law_type": doc_metadata.law_type,
        "year": doc_metadata.year,
        "page": page_number,
        "chunk_index": chunk_index,
        "sections": extract_sections(chunk_text),
    }
