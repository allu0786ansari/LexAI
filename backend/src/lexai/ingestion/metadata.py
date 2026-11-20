"""
Per-document and per-chunk metadata.

Phase 1 keeps filename-based classification but fixes the rules that sent 53
of 59 files to "other". Phase 2 replaces this with `data/manifest.yaml`.
"""

from __future__ import annotations

import re
from pathlib import Path

from lexai.domain.models import LawType

# First match wins; matched case-insensitively against the filename stem
# with "_", "-" and "," turned into spaces.
LAW_TYPE_RULES: list[tuple[str, LawType]] = [
    ("judis", "case_law"),  # Supreme Court judgments downloaded from JUDIS
    ("vs", "case_law"),
    ("v s", "case_law"),
    ("constitution", "statute"),
    ("ipc", "statute"),
    ("indian penal code", "statute"),
    ("crpc", "statute"),
    ("criminal procedure", "statute"),
    ("criminal law", "statute"),
    ("it act", "statute"),
    ("information technology", "statute"),
    ("prison", "statute"),
    ("evidence act", "statute"),
    ("bns", "statute"),
    ("bnss", "statute"),
    ("bsa", "statute"),
    ("annual report", "report"),
    ("law commission", "report"),
    ("manual", "manual"),
    ("instructions", "manual"),
]

_SECTION = re.compile(
    r"\b(?:Section|Sec\.?|Article|Art\.?)\s+\d+[A-Za-z]*(?:\(\d+\))?(?:\([a-zA-Z]\))?",
    re.IGNORECASE,
)
_YEAR = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")


_RULE_PATTERNS = [(re.compile(rf"\b{re.escape(p)}"), t) for p, t in LAW_TYPE_RULES]


def _normalise_stem(filename: str) -> str:
    stem = Path(filename).stem.lower()
    return re.sub(r"[_\-,.\s]+", " ", stem).strip()


def classify_law_type(filename: str) -> LawType:
    """Rules match at the start of a word, so "prison" matches "Prisons" but "vs" does not match "advs"."""
    stem = _normalise_stem(filename)
    for pattern, law_type in _RULE_PATTERNS:
        if pattern.search(stem):
            return law_type
    return "other"


def extract_year(filename: str) -> int | None:
    stem = Path(filename).stem
    if "judis" in stem.lower():  # JUDIS ids look like years; they are not
        return None
    match = _YEAR.search(stem)
    return int(match.group(1)) if match else None


def extract_sections(text: str, limit: int = 8) -> list[str]:
    seen: dict[str, None] = {}
    for match in _SECTION.finditer(text):
        seen.setdefault(re.sub(r"\s+", " ", match.group(0)).strip(), None)
        if len(seen) >= limit:
            break
    return list(seen)
