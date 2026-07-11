"""
BM25 sparse index over the same chunk corpus as the FAISS dense index.

The tokenizer here MUST be the one used at query time too (Phase 2's
retriever imports `tokenize` from this module rather than rolling its
own) — BM25 scoring is meaningless if the index and the query aren't
tokenized the same way.
"""
from __future__ import annotations

import pickle
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# Standard English stopword list. Without this, BM25's classic IDF
# (log((N-n+0.5)/(n+0.5)), which goes negative and gets epsilon-floored
# for terms in >50% of documents — see rank_bm25's BM25Okapi._calc_idf)
# lets common connector words ("the", "under", "shall", "of") dominate
# rankings on any corpus small enough, or repetitive enough, for those
# words to appear in most documents. Legal statute text is exactly that
# kind of corpus (structural words like "shall", "under", "section"
# repeat constantly), so this isn't an edge case — verified empirically:
# removing this caused an irrelevant IT-Act chunk to outrank the actual
# murder-punishment chunk for the query "what is the punishment for
# murder", purely because of stopword term-frequency noise.
_STOPWORDS = frozenset(
    """
    a an the and or but if then else when while of at by for with about
    against between into through during before after above below to from
    up down in out on off over under again further here there all any
    both each few more most other some such no nor not only own same so
    than too very s t can will just don should now is are was were be
    been being have has had having do does did doing this that these
    those i me my myself we our ours ourselves you your yours yourself
    yourselves he him his himself she her hers herself it its itself
    they them their theirs themselves what which who whom as until
    because until while
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokenizer with stopword removal. Deliberately simple and deterministic."""
    return [t for t in _TOKEN_PATTERN.findall(text.lower()) if t not in _STOPWORDS]


@dataclass
class ScoredDocument:
    document: Document
    score: float


class Bm25Store:
    _FILENAME = "bm25.pkl"

    def __init__(self, documents: list[Document]):
        self._documents = documents
        self._corpus_tokens = [tokenize(doc.page_content) for doc in documents]
        self._bm25 = BM25Okapi(self._corpus_tokens) if self._corpus_tokens else None

    def __len__(self) -> int:
        return len(self._documents)

    def search(self, query: str, k: int = 4) -> list[ScoredDocument]:
        if self._bm25 is None or not self._documents:
            return []
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        k = min(k, len(self._documents))
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
        return [
            ScoredDocument(document=self._documents[i], score=float(scores[i]))
            for i in top_indices
            if scores[i] > 0
        ]

    def save_local(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / self._FILENAME, "wb") as f:
            pickle.dump({"documents": self._documents}, f)

    @classmethod
    def load_local(cls, directory: str | Path) -> "Bm25Store":
        path = Path(directory) / cls._FILENAME
        if not path.exists():
            raise FileNotFoundError(f"No BM25 store found at {path}. Run the ingestion pipeline first.")
        with open(path, "rb") as f:
            payload = pickle.load(f)
        return cls(documents=payload["documents"])

    @property
    def documents(self) -> list[Document]:
        return list(self._documents)
