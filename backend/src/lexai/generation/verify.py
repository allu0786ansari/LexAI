"""
Check an answer against the passages it was given, after the stream ends.

- Every [n] must name a passage that exists; markers that do not are removed
  from the final answer (the `done` event carries the cleaned text).
- Every section or article the answer mentions must appear in the passages,
  either as a passage's own provision or referred to in a passage's text.
  Anything else may come from the model's memory and is flagged.
- Coverage is the share of claim sentences carrying at least one [n].

The result goes to the client as the `verification` event and to the logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lexai.domain.models import ScoredChunk
from lexai.query.references import extract_references

_MARKER = re.compile(r"\[(\s*\d{1,3}(?:\s*(?:,|\u2013|-)\s*\d{1,3})*\s*)\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_DISCLAIMER = re.compile(r"legal information, not legal advice|^provisions\s*:", re.I)
_PROVISION_IN_TEXT = (
    r"\b(?:sections?|secs?\.?|s\.|ss\.|articles?|art\.)\s*(?:\d{{1,3}}[A-Z]{{0,3}}\s*(?:,|and|or)\s*)*{num}\b"
)


@dataclass
class Verification:
    valid: bool
    coverage: float | None
    flags: list[dict[str, str]] = field(default_factory=list)
    removed_markers: list[int] = field(default_factory=list)
    answer: str = ""

    def payload(self) -> dict[str, object]:
        return {
            "valid": self.valid,
            "coverage": self.coverage,
            "flags": self.flags,
            "removed_markers": self.removed_markers,
        }


def _expand(body: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"\s*,\s*", body.strip()):
        if re.fullmatch(r"\d{1,3}\s*[\u2013-]\s*\d{1,3}", part):
            a, b = (int(x) for x in re.split(r"\s*[\u2013-]\s*", part))
            numbers.extend(range(min(a, b), min(max(a, b), min(a, b) + 20) + 1))
        elif part.isdigit():
            numbers.append(int(part))
    return numbers


def clean_markers(answer: str, n_passages: int) -> tuple[str, list[int]]:
    removed: list[int] = []

    def fix(m: re.Match[str]) -> str:
        nums = _expand(m.group(1))
        good = [n for n in nums if 1 <= n <= n_passages]
        removed.extend(n for n in nums if not 1 <= n <= n_passages)
        return "".join(f"[{n}]" for n in dict.fromkeys(good))

    cleaned = _MARKER.sub(fix, answer)
    cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)
    return cleaned, removed


def _supported(act: str | None, unit: str, passages: list[ScoredChunk]) -> bool:
    pattern = re.compile(_PROVISION_IN_TEXT.format(num=re.escape(unit)), re.I)
    for p in passages:
        meta = p.chunk.metadata
        if meta.unit and meta.unit.upper() == unit and (act is None or act == meta.short_name):
            return True
        if pattern.search(p.chunk.text) or any(
            re.search(rf"\b{re.escape(unit)}\b", s, re.I) for s in meta.sections
        ):
            return True
    return False


ABSTENTION = re.compile(r"indexed sources do not (?:answer|cover)", re.I)
_TRAILING_MARKERS = re.compile(r"([.!?])[ \t]*((?:\[[^\]\n]{1,20}\][ \t]*)+)")


def claim_sentences(answer: str) -> list[str]:
    # Models often cite after the full stop ("... fine. [1]"): keep those markers with
    # their sentence before splitting.
    attached = _TRAILING_MARKERS.sub(lambda m: m.group(2).strip() + m.group(1), answer)
    return [
        s.strip()
        for s in _SENTENCE.split(attached)
        if len(s.strip()) >= 25 and not _DISCLAIMER.search(s.strip())
    ]


def verify(answer: str, passages: list[ScoredChunk]) -> Verification:
    cleaned, removed = clean_markers(answer, len(passages))
    flags: list[dict[str, str]] = []
    if removed:
        flags.append(
            {
                "kind": "invalid_citation",
                "detail": f"removed [{'], ['.join(map(str, sorted(set(removed))))}]: no such passage",
            }
        )
    for ref in extract_references(cleaned):
        if not _supported(ref.act, ref.unit, passages):
            flags.append({"kind": "unsupported_provision", "detail": f"{ref.label} is not in the passages"})

    claims = claim_sentences(cleaned)
    cited = [s for s in claims if _MARKER.search(s)]
    coverage = round(len(cited) / len(claims), 2) if claims else None
    if claims and not cited and not ABSTENTION.search(cleaned):
        # An abstention ("The indexed sources do not answer this.") makes no claim to cite.
        flags.append({"kind": "no_citations", "detail": "the answer cites no passage"})
    valid = not any(f["kind"] in ("unsupported_provision", "no_citations") for f in flags)
    return Verification(
        valid=valid, coverage=coverage, flags=flags, removed_markers=sorted(set(removed)), answer=cleaned
    )
