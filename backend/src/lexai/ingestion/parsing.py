"""
PDF text extraction and cleaning.

India Code PDFs (and most government statute PDFs) carry noise that hurts both
search and answers:
- amendment markers glued to text: `2[21A.`, `1*[India]`;
- footnotes at the bottom of each page: `1. Subs. by Act 25 of 2005, s. 23 ...`;
- bare page numbers and running headers (e.g. JUDIS's `http://JUDIS.NIC.IN ... Page 1 of 4`).

Pages are cleaned one by one and joined into a single text with the start
offset of every page, so any span of the text can be mapped back to pages.
"""

from __future__ import annotations

import bisect
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from lexai.core.errors import ConfigurationError

# pypdf warns about every odd font or object it skips; keep only real errors.
logging.getLogger("pypdf").setLevel(logging.ERROR)

_FOOTNOTE_START = re.compile(
    r"^\s*(?:\d{1,2}|[*†‡§¶])\.?\s+(?:Subs|Ins|Omitted|Added|Rep|Renumbered|The\s|Cl\.|Clause|"
    r"S\.\s|Ss\.|Sub-|Vide|Inserted|Substituted|Section|Now|Came|Words|w\.e\.f|Amended|"
    r"Brought|Extended|This\s|See\s|For\s|Certain|Original|Re-numbered|Earlier)",
)
_AMENDING_INSTRUMENT = re.compile(r"\b(?:Act|Ordinance|Regulation|Order|w\.e\.f)\b")
_NUMBERED_NOTE = re.compile(r"^\s*(?:\d{1,2}|[*†‡§¶])\.?\s+\S")
_PAGE_NUMBER = re.compile(r"^\s*(?:-\s*)?\d{1,4}(?:\s*-)?\s*$")
_JUDIS_HEADER = re.compile(r"^\s*http://JUDIS\.NIC\.IN\s+SUPREME COURT OF INDIA\s+Page \d+ of \d+\s*$", re.I)
_MARKER = re.compile(r"(?<![\w\])])(\d{1,2})\*?\[")  # 2[21A.  1*[India]  ->  [21A.  [India]
_SECTION_REF = re.compile(
    r"\b(?:Section|Sec\.?|Article|Art\.?)\s+\d+[A-Za-z]*(?:\(\d+\))?(?:\([a-zA-Z]\))?",
    re.IGNORECASE,
)


@dataclass
class DocumentText:
    text: str
    page_starts: list[int]  # offset in `text` where each page begins; page n is index n-1

    def page_at(self, offset: int) -> int:
        return bisect.bisect_right(self.page_starts, offset)

    @property
    def pages(self) -> int:
        return len(self.page_starts)


def extract_pages(path: Path) -> list[str]:
    try:
        reader = PdfReader(str(path))
    except (PdfReadError, OSError) as exc:
        raise ConfigurationError(f"Cannot read PDF {path.name}: {exc}") from exc
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # pypdf raises many parser-specific errors on malformed pages
            pages.append("")
    return pages


def strip_footnotes(lines: list[str]) -> list[str]:
    """
    Drop the footnote block at the bottom of a page: from the last separator
    (a blank line of many spaces, or a rule of dashes/underscores) in the lower
    part of the page, when the next line looks like an amendment footnote.
    """
    cutoff = len(lines)
    lower_half = len(lines) // 3
    for i in range(len(lines) - 1, lower_half - 1, -1):
        raw = lines[i]
        is_rule = re.fullmatch(r"\s*[_\-—]{10,}\s*", raw) is not None
        is_blank_separator = raw.strip() == "" and len(raw) >= 10
        if not (is_rule or is_blank_separator):
            continue
        following = next((ln for ln in lines[i + 1 :] if ln.strip()), "")
        # After a drawn rule any numbered note is a footnote; after a blank line, only
        # notes that read like amendment footnotes ("Subs. by", "Ins. by", ...).
        if (is_rule and _NUMBERED_NOTE.match(following)) or (
            _FOOTNOTE_START.match(following) and _AMENDING_INSTRUMENT.search(following)
        ):
            cutoff = i
    return lines[:cutoff]


def clean_page(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace(" ", " ")
    lines = strip_footnotes(text.split("\n"))
    kept = [ln for ln in lines if not _PAGE_NUMBER.match(ln) and not _JUDIS_HEADER.match(ln)]
    cleaned = "\n".join(ln.rstrip() for ln in kept)
    cleaned = _MARKER.sub("[", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def load_document(path: Path) -> DocumentText:
    parts: list[str] = []
    starts: list[int] = []
    offset = 0
    for raw in extract_pages(path):
        starts.append(offset)
        page = clean_page(raw)
        parts.append(page)
        offset += len(page) + 1  # the joining newline
    return DocumentText("\n".join(parts), starts)


def normalize_for_match(text: str) -> str:
    """Lowercase, drop brackets/markers/punctuation spacing, unify quotes and dashes."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\[\]*†‡§¶]", "", text)
    text = re.sub(r"[‘’“”\"']", "", text)
    text = re.sub(r"[—–-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_sections(text: str, limit: int = 8) -> list[str]:
    seen: dict[str, None] = {}
    for match in _SECTION_REF.finditer(text):
        seen.setdefault(re.sub(r"\s+", " ", match.group(0)).strip(), None)
        if len(seen) >= limit:
            break
    return list(seen)


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """
    Recursive character splitter: split on the coarsest separator that keeps
    pieces under `size`, then merge neighbours back up to `size` with `overlap`.
    """
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    pieces = _split_recursive(text.strip(), size, 0)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > size:
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            space = tail.find(" ")
            current = (tail[space + 1 :] if space != -1 else tail) + piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


_SEPARATORS = ["\n\n", "\n", ". ", " "]


def _split_recursive(text: str, size: int, level: int) -> list[str]:
    if len(text) <= size:
        return [text]
    if level >= len(_SEPARATORS):
        return [text[i : i + size] for i in range(0, len(text), size)]
    sep = _SEPARATORS[level]
    parts = text.split(sep)
    out: list[str] = []
    for i, part in enumerate(parts):
        piece = part + (sep if i < len(parts) - 1 else "")
        if len(piece) <= size:
            out.append(piece)
        else:
            out.extend(_split_recursive(piece, size, level + 1))
    return out
