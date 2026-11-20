"""
Which section of the new code corresponds to a section of the old one (IPC ↔ BNS,
CrPC ↔ BNSS, IEA ↔ BSA), derived from the indexed text itself.

Most BNS/BNSS/BSA sections re-enact an old section with the same title and
largely the same words under a new number, so each section is matched to the
section of the other code with the most similar title and text (TF-IDF cosine,
title weighted). This is a derived table, not an official one: every match
carries its score, low-confidence matches are dropped, and answers present it
as "corresponds to", never as a legal determination. Sections that were split or
merged map to their closest counterpart, plus close runners-up.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np

from lexai.domain.models import Chunk
from lexai.query.references import OLD_CODES

MIN_SCORE = 0.35  # below this a "match" is noise
RUNNER_UP = 0.9  # also keep matches scoring at least 90% of the best
TITLE_WEIGHT = 3  # title words count three times

_STOP = {
    "the",
    "of",
    "and",
    "or",
    "to",
    "in",
    "a",
    "an",
    "by",
    "for",
    "on",
    "with",
    "any",
    "such",
    "as",
    "be",
    "is",
    "are",
    "was",
    "shall",
    "may",
    "which",
    "that",
    "this",
    "under",
    "who",
    "whom",
    "whose",
    "from",
    "at",
    "it",
    "its",
    "other",
    "than",
    "not",
    "no",
    "all",
    "where",
    "when",
    "if",
    "into",
    "upon",
    "being",
    "been",
    "has",
    "have",
    "had",
    "person",
    "persons",
    "section",
    "sections",
    "sub",
    "clause",
    "provided",
    "act",
    "code",
    "thereof",
    "herein",
}
_WORD = re.compile(r"[a-z]{3,}")


@dataclass(frozen=True)
class SectionText:
    act: str
    unit: str
    title: str | None
    text: str


@dataclass(frozen=True)
class Match:
    act: str
    unit: str
    title: str | None
    score: float

    @property
    def label(self) -> str:
        return f"{self.act} s.{self.unit}"


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def sections_from_chunks(chunks: list[Chunk]) -> list[SectionText]:
    """Join the pieces of each numbered section, in order."""
    grouped: dict[tuple[str, str], list[Chunk]] = {}
    for c in chunks:
        m = c.metadata
        if m.unit and m.unit_label == "Section":
            grouped.setdefault((m.short_name, m.unit), []).append(c)
    out = []
    for (act, unit), pieces in grouped.items():
        pieces.sort(key=lambda c: c.metadata.part)
        out.append(SectionText(act, unit, pieces[0].metadata.unit_title, "\n".join(p.text for p in pieces)))
    return out


def _vectors(docs: list[list[str]], vocab: dict[str, int], idf: np.ndarray) -> np.ndarray:
    mat = np.zeros((len(docs), len(vocab)), dtype=np.float32)
    for i, toks in enumerate(docs):
        for w, n in Counter(toks).items():
            j = vocab.get(w)
            if j is not None:
                mat[i, j] = (1 + math.log(n)) * idf[j]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.maximum(norms, 1e-9)


def _match(a: list[SectionText], b: list[SectionText]) -> dict[tuple[str, str], list[Match]]:
    """For each section in `a`, its best matches in `b`."""
    docs_a = [_tokens(s.title or "") * TITLE_WEIGHT + _tokens(s.text) for s in a]
    docs_b = [_tokens(s.title or "") * TITLE_WEIGHT + _tokens(s.text) for s in b]
    df: Counter[str] = Counter()
    for toks in docs_a + docs_b:
        df.update(set(toks))
    vocab = {w: i for i, w in enumerate(sorted(df))}
    n_docs = len(docs_a) + len(docs_b)
    idf = np.array([math.log((1 + n_docs) / (1 + df[w])) + 1 for w in sorted(df)], dtype=np.float32)
    sims = _vectors(docs_a, vocab, idf) @ _vectors(docs_b, vocab, idf).T
    result: dict[tuple[str, str], list[Match]] = {}
    for i, s in enumerate(a):
        order = np.argsort(-sims[i])[:3]
        best = float(sims[i, order[0]]) if len(order) else 0.0
        matches = [
            Match(b[j].act, b[j].unit, b[j].title, round(float(sims[i, j]), 3))
            for j in order
            if sims[i, j] >= MIN_SCORE and sims[i, j] >= RUNNER_UP * best
        ]
        if matches:
            result[(s.act, s.unit)] = matches
    return result


class Correspondence:
    def __init__(self, table: dict[tuple[str, str], list[Match]]) -> None:
        self._table = table

    @classmethod
    def build(cls, sections: list[SectionText]) -> Correspondence:
        by_act: dict[str, list[SectionText]] = {}
        for s in sections:
            by_act.setdefault(s.act, []).append(s)
        table: dict[tuple[str, str], list[Match]] = {}
        for old, new in OLD_CODES.items():
            if by_act.get(old) and by_act.get(new):
                table.update(_match(by_act[old], by_act[new]))
                table.update(_match(by_act[new], by_act[old]))
        return cls(table)

    def equivalents(self, act: str | None, unit: str) -> list[Match]:
        """Counterparts of a section in the other code (empty if none is confident)."""
        return list(self._table.get((act or "", unit.upper()), []))

    def __len__(self) -> int:
        return len(self._table)
