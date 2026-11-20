"""BM25 sparse scoring. The same `tokenize` is used for indexing and querying."""

from __future__ import annotations

import re

import numpy as np
from rank_bm25 import BM25Okapi

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


class BM25Index:
    def __init__(self, texts: list[str]) -> None:
        tokens = [tokenize(t) for t in texts]
        self._size = len(tokens)
        self._bm25 = BM25Okapi(tokens) if tokens else None

    def __len__(self) -> int:
        return self._size

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        """Return (position, score) for the top `k` positive-scoring documents."""
        if self._bm25 is None:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = np.asarray(self._bm25.get_scores(q))
        k = min(k, len(scores))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(int(i), float(scores[i])) for i in top if scores[i] > 0]
