"""
Offline retrieval evaluation for CI (no model servers).

`export_fixture` (run where the real index and embedder are) saves:
- every chunk's payload, dense vector (float16), BM25 vector, and the build metadata;
- for each item of the smoke set, its standalone query (follow-ups rewritten
  by the real model) and that query's embedding.

`load_fixture` rebuilds the index in an in-memory Qdrant and returns an
embedder that looks query vectors up instead of computing them. Retrieval then
runs through the same router, correspondence and context assembly as the live
API, so a change to any of them shows up in the metrics. Changes to the
embedding model, the chunker or the BM25 tokenizer do not: those need a new
fixture (a BM25 version mismatch is refused).
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from qdrant_client import QdrantClient, models

from lexai.core.settings import (
    QdrantSettings,
    QuerySettings,
    RerankSettings,
    RetrievalSettings,
    Settings,
)
from lexai.domain.models import Chunk
from lexai.evaluation.dataset import GoldenItem
from lexai.retrieval.qdrant_store import (
    DENSE,
    SPARSE,
    BuildMeta,
    QdrantStore,
    chunk_from_payload,
    new_collection_name,
)
from lexai.retrieval.sparse import SPARSE_VERSION

SMOKE_SIZE = 50
VECTORS = "smoke_index.npz"
PAYLOADS = "smoke_payloads.json.gz"
META = "smoke_meta.json"
QUERIES = "smoke_queries.json"
SETTINGS = "smoke_settings.json"  # retrieval/query/rerank settings the fixture was exported with


def smoke_items(items: list[GoldenItem]) -> list[GoldenItem]:
    """The PR-gate subset: dev items with gold or an abstention label, chosen by hash."""
    pool = [i for i in items if i.split == "dev" and (i.has_gold or i.should_abstain)]
    pool.sort(key=lambda i: hashlib.sha256(i.id.encode()).hexdigest())
    return sorted(pool[:SMOKE_SIZE], key=lambda i: i.id)


async def export_index(store: QdrantStore, collection: str, meta: BuildMeta, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - one-off CLI
    records: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    offset: Any = None
    while True:
        points, offset = await store._run(  # export needs raw vectors
            store._client.scroll,
            collection,
            limit=512,
            offset=offset,
            with_payload=True,
            with_vectors=[DENSE, SPARSE],
        )
        for p in points:
            assert isinstance(p.vector, dict), "expected named dense + sparse vectors"
            sparse = p.vector[SPARSE]
            assert isinstance(sparse, models.SparseVector)
            records.append(
                {
                    "payload": p.payload or {},
                    "sparse": [sparse.indices, [round(v, 4) for v in sparse.values]],
                }
            )
            vectors.append(list(p.vector[DENSE]))  # type: ignore[arg-type]
        if offset is None:
            break
    np.savez_compressed(out_dir / VECTORS, dense=np.asarray(vectors, dtype=np.float16))
    with gzip.open(out_dir / PAYLOADS, "wt", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False)
    (out_dir / META).write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    return len(records)


def write_queries(out_dir: Path, queries: dict[str, dict[str, Any]]) -> None:
    (out_dir / QUERIES).write_text(json.dumps(queries, ensure_ascii=False), encoding="utf-8")


def write_settings(out_dir: Path, settings: Settings) -> None:
    pinned = {
        "retrieval": settings.retrieval.model_dump(mode="json"),
        "query": settings.query.model_dump(mode="json"),
        "rerank": settings.rerank.model_dump(mode="json"),
    }
    (out_dir / SETTINGS).write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")


def pinned_settings(directory: Path) -> Settings:
    """Settings for an offline run: the exporting machine's retrieval, query and rerank
    settings, so the gate gives the same numbers on every machine whatever its .env says."""
    settings = Settings()
    path = directory / SETTINGS
    if path.exists():
        pinned = json.loads(path.read_text(encoding="utf-8"))
        settings.retrieval = RetrievalSettings(**pinned["retrieval"])
        settings.query = QuerySettings(**pinned["query"])
        settings.rerank = RerankSettings(**pinned["rerank"])
    return settings


def fixture_present(directory: Path) -> bool:
    return all((directory / name).exists() for name in (VECTORS, PAYLOADS, META, QUERIES))


class FixtureEmbedder:
    """Looks query vectors up by text (the fixture's standalone queries)."""

    def __init__(self, queries: dict[str, dict[str, Any]]) -> None:
        self._by_text = {q["standalone"]: q["vector"] for q in queries.values()}

    async def embed_query(self, query: str) -> list[float]:
        try:
            return list(self._by_text[query])
        except KeyError as exc:
            raise KeyError(f"no fixture vector for query {query!r}; re-export the fixture") from exc

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("the offline fixture has no document embedder (HyDE is not available)")

    async def aclose(self) -> None:
        return None


async def load_fixture(directory: Path) -> tuple[QdrantStore, str, BuildMeta, dict[str, dict[str, Any]]]:
    meta = BuildMeta.model_validate_json((directory / META).read_text(encoding="utf-8"))
    if meta.sparse_version != SPARSE_VERSION:
        raise ValueError(
            f"fixture BM25 version {meta.sparse_version} != {SPARSE_VERSION}: re-export the fixture"
        )
    dense = np.load(directory / VECTORS)["dense"].astype(np.float32)
    with gzip.open(directory / PAYLOADS, "rt", encoding="utf-8") as f:
        records: list[dict[str, Any]] = json.load(f)
    queries: dict[str, dict[str, Any]] = json.loads((directory / QUERIES).read_text(encoding="utf-8"))

    store = QdrantStore(QdrantClient(location=":memory:"), QdrantSettings(path=directory / "unused"))
    name = new_collection_name()
    await store.create_collection(name, int(dense.shape[1]))
    # The index's own sparse vectors are stored: chunk payloads do not keep the embed text.
    chunks: list[Chunk] = [chunk_from_payload(r["payload"]) for r in records]
    sparse = [(r["sparse"][0], r["sparse"][1]) for r in records]
    await store.upsert_chunks(name, chunks, dense.tolist(), sparse, batch=512)
    meta = meta.model_copy(update={"collection": name})
    await store.write_meta(meta)
    await store.set_live(name)
    return store, name, meta, queries
