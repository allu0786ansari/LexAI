"""
Minimal FAISS-backed dense vector store.

Deliberately NOT `langchain_community.vectorstores.FAISS`: the
`langchain-community` package was officially sunset (repository archived,
no further maintenance — see
https://github.com/langchain-ai/langchain-community/issues/674). Building a
project's core retrieval index on an archived package is not a decision
that should survive contact with an interviewer asking "why did you pick
that dependency." The `faiss` (Meta's actively-maintained library) and
`langchain_core.documents.Document` (actively-maintained foundation
package, not part of the sunset) building blocks are enough to implement
exactly what we need, with full control over persistence format.

Vectors are L2-normalised and indexed with inner product, which is
mathematically equivalent to cosine similarity — the standard choice for
text embedding similarity search.
"""
from __future__ import annotations

import pickle
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from langchain_core.documents import Document


@dataclass
class ScoredDocument:
    document: Document
    score: float  # cosine similarity, higher is more relevant


class FaissVectorStore:
    """A thin, explicit wrapper around a flat FAISS inner-product index."""

    _INDEX_FILENAME = "index.faiss"
    _DOCSTORE_FILENAME = "docstore.pkl"

    def __init__(self, dimension: int):
        self.dimension = dimension
        self._index = faiss.IndexFlatIP(dimension)
        self._docstore: list[Document] = []

    def __len__(self) -> int:
        return len(self._docstore)

    @staticmethod
    def _normalize(vectors: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1e-10
        return vectors / norms

    def _coerce_vector(self, embedding: list[float] | np.ndarray, dimension: int | None = None) -> np.ndarray:
        vector = np.asarray(embedding, dtype=np.float32).reshape(-1)
        target_dim = self.dimension if dimension is None else dimension
        if vector.size == 0:
            return np.zeros(target_dim, dtype=np.float32)
        if vector.size < target_dim:
            padded = np.zeros(target_dim, dtype=np.float32)
            padded[: vector.size] = vector
            return padded
        return vector[:target_dim]

    def add_documents(self, documents: list[Document], embeddings: list[list[float]]) -> None:
        if len(documents) != len(embeddings):
            raise ValueError(
                f"documents ({len(documents)}) and embeddings ({len(embeddings)}) counts must match."
            )
        if not documents:
            return
        vectors = np.vstack([self._coerce_vector(embedding) for embedding in embeddings]).astype(np.float32)
        self._index.add(self._normalize(vectors))
        self._docstore.extend(documents)

    def similarity_search_by_vector(self, query_embedding: list[float], k: int = 4) -> list[ScoredDocument]:
        if len(self._docstore) == 0:
            return []
        query = self._normalize(np.array([self._coerce_vector(query_embedding)], dtype=np.float32))
        k = min(k, len(self._docstore))
        scores, indices = self._index.search(query, k)
        results: list[ScoredDocument] = []
        for score, idx in zip(scores[0], indices[0]):
            if idx == -1:
                continue
            results.append(ScoredDocument(document=self._docstore[idx], score=float(score)))
        return results

    def save_local(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(directory / self._INDEX_FILENAME))
        with open(directory / self._DOCSTORE_FILENAME, "wb") as f:
            pickle.dump({"dimension": self.dimension, "docstore": self._docstore}, f)

    @classmethod
    def load_local(cls, directory: str | Path) -> "FaissVectorStore":
        directory = Path(directory)
        index_path = directory / cls._INDEX_FILENAME
        docstore_path = directory / cls._DOCSTORE_FILENAME
        if not index_path.exists() or not docstore_path.exists():
            raise FileNotFoundError(
                f"No FAISS store found at {directory}. Expected {cls._INDEX_FILENAME} "
                f"and {cls._DOCSTORE_FILENAME}. Run the ingestion pipeline first."
            )
        with open(docstore_path, "rb") as f:
            payload = pickle.load(f)
        store = cls(dimension=payload["dimension"])
        store._index = faiss.read_index(str(index_path))
        store._docstore = payload["docstore"]
        return store

    @property
    def documents(self) -> list[Document]:
        """Read-only access to the underlying document list (e.g. for building a BM25 index over the same corpus)."""
        return list(self._docstore)
