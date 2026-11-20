"""
BM25 sparse vectors for Qdrant.

Qdrant computes the IDF part of BM25 itself (sparse vector `modifier=IDF`); we
send the term-frequency part per document and a weight of 1 per query term:

    doc weight(t)  = tf * (k1 + 1) / (tf + k1 * (1 - b + b * len / avg_len))
    query weight(t) = 1

Terms are mapped to indices with CRC32, which is stable across processes and
Python versions (unlike `hash()`), so an index built today matches queries
made next month.
"""

from __future__ import annotations

import re
import zlib
from collections import Counter

SPARSE_VERSION = "bm25-crc32-v1"
K1 = 1.2
B = 0.75

_TOKEN = re.compile(r"[a-z0-9]+")

# Common English function words. Without them, words like "the", "shall" and
# "under" dominate BM25 on statute text, where they occur in almost every chunk.
_STOPWORDS = frozenset(
    """
    a an the and or but if then else when while of at by for with about against between into
    through during before after above below to from up down in out on off over under again
    further here there all any both each few more most other some such no nor not only own same
    so than too very s t can will just don should now is are was were be been being have has had
    having do does did doing this that these those i me my myself we our ours ourselves you your
    yours yourself yourselves he him his himself she her hers herself it its itself they them
    their theirs themselves what which who whom as until because
    """.split()  # noqa: SIM905
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def term_index(term: str) -> int:
    return zlib.crc32(term.encode("utf-8")) & 0x7FFFFFFF


def doc_vector(text: str, avg_len: float) -> tuple[list[int], list[float]]:
    tokens = tokenize(text)
    if not tokens:
        return [], []
    length = len(tokens)
    norm = K1 * (1 - B + B * length / max(avg_len, 1.0))
    weights: dict[int, float] = {}
    for term, tf in Counter(tokens).items():
        idx = term_index(term)
        weights[idx] = weights.get(idx, 0.0) + tf * (K1 + 1) / (tf + norm)
    indices = sorted(weights)
    return indices, [weights[i] for i in indices]


def query_vector(text: str) -> tuple[list[int], list[float]]:
    indices = sorted({term_index(t) for t in tokenize(text)})
    return indices, [1.0] * len(indices)


def average_length(texts: list[str]) -> float:
    lengths = [len(tokenize(t)) for t in texts]
    return sum(lengths) / len(lengths) if lengths else 1.0
