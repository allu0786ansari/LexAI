"""
On-disk search index used until Phase 2 moves to Qdrant.

Layout of an index directory:
    index.faiss       FAISS inner-product index over L2-normalised vectors (= cosine)
    chunks.jsonl      one Chunk per line, in the same order as the FAISS vectors
    index_meta.json   embedding model, dimension, counts, build time

No pickle: chunks are plain JSON, and FAISS uses its own file format. The
metadata file records which embedding model built the index, so the API can
refuse to serve queries embedded by a different model (previously vectors of
the wrong size were silently padded or truncated).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import faiss
import numpy as np

from lexai.core.errors import IndexMismatchError, IndexNotReady
from lexai.domain.models import Chunk
from lexai.retrieval.bm25 import BM25Index

INDEX_FILE = "index.faiss"
CHUNKS_FILE = "chunks.jsonl"
META_FILE = "index_meta.json"
SCHEMA_VERSION = 3


@dataclass(frozen=True)
class IndexMeta:
    schema_version: int
    embedding_model: str
    dimension: int
    document_prefix: str
    chunk_count: int
    source_files: list[str]
    built_at: str

    @classmethod
    def read(cls, directory: Path) -> IndexMeta:
        data = json.loads((directory / META_FILE).read_text(encoding="utf-8"))
        return cls(**data)


def _normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Zero-length embedding vector; refusing to index it.")
    normalized: np.ndarray = (vectors / norms).astype(np.float32)
    return normalized


class LocalIndex:
    def __init__(self, chunks: list[Chunk], faiss_index: faiss.Index, meta: IndexMeta) -> None:
        if faiss_index.ntotal != len(chunks):
            raise IndexMismatchError(
                f"FAISS holds {faiss_index.ntotal} vectors but there are {len(chunks)} chunks."
            )
        self.chunks = chunks
        self.meta = meta
        self._faiss = faiss_index
        self._bm25 = BM25Index([c.text for c in chunks])

    def __len__(self) -> int:
        return len(self.chunks)

    # ---- build / persist ---------------------------------------------------

    @classmethod
    def build(
        cls,
        chunks: list[Chunk],
        vectors: list[list[float]],
        embedding_model: str,
        document_prefix: str = "",
    ) -> LocalIndex:
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        if not chunks:
            raise ValueError("Cannot build an empty index")
        matrix = _normalize(np.asarray(vectors, dtype=np.float32))
        dim = int(matrix.shape[1])
        index = faiss.IndexFlatIP(dim)
        index.add(matrix)
        meta = IndexMeta(
            schema_version=SCHEMA_VERSION,
            embedding_model=embedding_model,
            dimension=dim,
            document_prefix=document_prefix,
            chunk_count=len(chunks),
            source_files=sorted({c.metadata.source_file for c in chunks}),
            built_at=datetime.now(UTC).isoformat(),
        )
        return cls(chunks, index, meta)

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._faiss, str(directory / INDEX_FILE))
        with open(directory / CHUNKS_FILE, "w", encoding="utf-8") as f:
            for chunk in self.chunks:
                f.write(chunk.model_dump_json() + "\n")
        (directory / META_FILE).write_text(json.dumps(self.meta.__dict__, indent=2), encoding="utf-8")

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        expected_model: str,
        expected_dimension: int,
        expected_document_prefix: str = "",
    ) -> LocalIndex:
        if not (directory / META_FILE).exists():
            raise IndexNotReady(f"No index at {directory}. Run `lexai-ingest` first.")
        meta = IndexMeta.read(directory)
        if meta.schema_version != SCHEMA_VERSION:
            raise IndexMismatchError(
                f"Index schema v{meta.schema_version} is not supported "
                f"(need v{SCHEMA_VERSION}). Re-run ingestion."
            )
        if meta.embedding_model != expected_model or meta.dimension != expected_dimension:
            raise IndexMismatchError(
                f"Index was built with {meta.embedding_model} ({meta.dimension}-dim), but the API is "
                f"configured for {expected_model} ({expected_dimension}-dim). Re-run ingestion."
            )
        if meta.document_prefix != expected_document_prefix:
            raise IndexMismatchError(
                f"Index was embedded with document prefix {meta.document_prefix!r}, but the API is "
                f"configured with {expected_document_prefix!r}. Re-run ingestion."
            )
        faiss_index = faiss.read_index(str(directory / INDEX_FILE))
        with open(directory / CHUNKS_FILE, encoding="utf-8") as f:
            chunks = [Chunk.model_validate_json(line) for line in f if line.strip()]
        return cls(chunks, faiss_index, meta)

    # ---- search (CPU-bound; callers run these in a worker thread) ------------

    def dense_search(self, vector: list[float], k: int) -> list[tuple[int, float]]:
        query = np.asarray([vector], dtype=np.float32)
        if query.shape[1] != self.meta.dimension:
            raise IndexMismatchError(
                f"Query vector has {query.shape[1]} dims, index has {self.meta.dimension}."
            )
        scores, ids = self._faiss.search(_normalize(query), min(k, len(self.chunks)))
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0], strict=True) if i != -1]

    def sparse_search(self, query: str, k: int) -> list[tuple[int, float]]:
        return self._bm25.search(query, k)
