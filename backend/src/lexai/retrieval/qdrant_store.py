"""
Qdrant access: collections, hybrid search, build metadata and alias swaps.

Layout:
- `lexai_chunks_<timestamp>` collections, one per build, each with a named
  dense vector ("dense", cosine) and a named sparse vector ("bm25", IDF modifier);
- the alias `lexai_chunks` points at the live build (blue/green: a new build
  becomes live only after its checks pass, in one alias update);
- `lexai_meta` holds one record per build: embedding model, dimension, prefix,
  chunker and sparse versions, and per-document checksums used for incremental
  rebuilds and for the API's start-up compatibility check.

Works against a Qdrant server (`LEXAI_QDRANT_URL`) or embedded on disk
(`LEXAI_QDRANT_PATH`; one process at a time).

Every client call runs in a worker thread. The embedded mode does its search
work in Python on the calling thread, even through Qdrant's async client, which
blocked the event loop; calls are also serialised there, since the embedded
store is not designed for concurrent use.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from lexai.core.errors import ConfigurationError, UpstreamUnavailable
from lexai.core.logging import get_logger
from lexai.core.settings import QdrantSettings
from lexai.domain.models import Chunk, ChunkMetadata, ScoredChunk

logger = get_logger(__name__)

DENSE = "dense"
SPARSE = "bm25"
META_COLLECTION = "lexai_meta"
_META_NS = uuid.UUID("0f4b6c1e-2d3a-4e5f-8a9b-7c6d5e4f3a2b")


class DocBuildInfo(BaseModel):
    sha256: str
    chunks: int
    title: str


class BuildMeta(BaseModel):
    collection: str
    embedding_model: str
    dimension: int
    document_prefix: str
    chunker_version: str
    sparse_version: str
    avg_len: float
    built_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    docs: dict[str, DocBuildInfo] = Field(default_factory=dict)

    @property
    def total_chunks(self) -> int:
        return sum(d.chunks for d in self.docs.values())


@dataclass
class SearchFilter:
    doc_ids: list[str] | None = None
    doc_types: list[str] | None = None
    short_names: list[str] | None = None
    in_force_only: bool = False

    def to_qdrant(self) -> models.Filter | None:
        must: list[Any] = []
        if self.doc_ids:
            must.append(models.FieldCondition(key="doc_id", match=models.MatchAny(any=self.doc_ids)))
        if self.doc_types:
            must.append(models.FieldCondition(key="doc_type", match=models.MatchAny(any=self.doc_types)))
        if self.short_names:
            must.append(models.FieldCondition(key="short_name", match=models.MatchAny(any=self.short_names)))
        if self.in_force_only:
            must.append(models.IsNullCondition(is_null=models.PayloadField(key="repealed_on")))
        return models.Filter(must=must) if must else None


def new_collection_name() -> str:
    return "lexai_chunks_" + datetime.now(UTC).strftime("%Y%m%dt%H%M%S%f")


def _payload(chunk: Chunk) -> dict[str, Any]:
    return {**chunk.metadata.model_dump(), "chunk_id": chunk.chunk_id, "text": chunk.text}


def chunk_from_payload(payload: dict[str, Any]) -> Chunk:
    meta = ChunkMetadata.model_validate({k: v for k, v in payload.items() if k not in ("text", "chunk_id")})
    return Chunk(chunk_id=payload["chunk_id"], text=payload["text"], embed_text="", metadata=meta)


T = TypeVar("T")


class QdrantStore:
    def __init__(self, client: QdrantClient, settings: QdrantSettings) -> None:
        self._client = client
        self.settings = settings
        self._lock = threading.Lock() if not settings.url else None

    async def _run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        def call() -> T:
            if self._lock is None:
                return fn(*args, **kwargs)
            with self._lock:
                return fn(*args, **kwargs)

        return await asyncio.to_thread(call)

    @classmethod
    def connect(cls, settings: QdrantSettings) -> QdrantStore:
        try:
            if settings.url:
                client = QdrantClient(url=settings.url, api_key=settings.api_key, timeout=30)
            else:
                settings.path.mkdir(parents=True, exist_ok=True)
                client = QdrantClient(path=str(settings.path))
        except RuntimeError as exc:
            if "already accessed" in str(exc):
                raise ConfigurationError(
                    f"The embedded Qdrant store at {settings.path} is in use by another process "
                    "(is the API running?). Stop it first, or use a Qdrant server via LEXAI_QDRANT_URL."
                ) from exc
            raise
        return cls(client, settings)

    async def close(self) -> None:
        await self._run(self._client.close)

    # ---- collections -------------------------------------------------------

    async def create_collection(self, name: str, dimension: int) -> None:
        await self._run(
            self._client.create_collection,
            collection_name=name,
            vectors_config={DENSE: models.VectorParams(size=dimension, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        if self.settings.url:  # payload indexes only matter on a server
            for key in ("doc_id", "doc_type", "short_name", "unit", "unit_label", "parent_id"):
                await self._run(
                    self._client.create_payload_index, name, key, models.PayloadSchemaType.KEYWORD
                )

    async def delete_collection(self, name: str) -> None:
        if await self._run(self._client.collection_exists, name):
            await self._run(self._client.delete_collection, name)
        if await self._run(self._client.collection_exists, META_COLLECTION):
            await self._run(
                self._client.delete,
                META_COLLECTION,
                points_selector=models.PointIdsList(points=[self._meta_id(name)]),
            )

    async def count(self, name: str, flt: models.Filter | None = None) -> int:
        return (await self._run(self._client.count, name, count_filter=flt, exact=True)).count

    async def build_collections(self) -> list[str]:
        response = await self._run(
            self._client.get_collections,
        )
        return sorted(c.name for c in response.collections if c.name.startswith("lexai_chunks_"))

    # ---- writing -----------------------------------------------------------

    async def upsert_chunks(
        self,
        name: str,
        chunks: Sequence[Chunk],
        dense: Sequence[Sequence[float]],
        sparse: Sequence[tuple[list[int], list[float]]],
        batch: int = 128,
    ) -> None:
        if not (len(chunks) == len(dense) == len(sparse)):
            raise ValueError("chunks, dense and sparse vectors must have the same length")
        for start in range(0, len(chunks), batch):
            points = [
                models.PointStruct(
                    id=c.chunk_id,
                    vector={DENSE: list(d), SPARSE: models.SparseVector(indices=s[0], values=s[1])},
                    payload=_payload(c),
                )
                for c, d, s in zip(
                    chunks[start : start + batch],
                    dense[start : start + batch],
                    sparse[start : start + batch],
                    strict=True,
                )
            ]
            await self._run(self._client.upsert, name, points=points, wait=True)

    async def copy_document(self, source: str, target: str, doc_id: str) -> int:
        """Copy one document's points (vectors and payload) between builds, without re-embedding."""
        flt = models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))])
        copied = 0
        offset: Any = None
        while True:
            points, offset = await self._run(
                self._client.scroll,
                source,
                scroll_filter=flt,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if points:
                await self._run(
                    self._client.upsert,
                    target,
                    points=[
                        # vectors come back from scroll exactly as they were stored
                        models.PointStruct(id=p.id, vector=p.vector or {}, payload=p.payload)  # type: ignore[arg-type]
                        for p in points
                    ],
                    wait=True,
                )
                copied += len(points)
            if offset is None:
                return copied

    # ---- build metadata and the live alias ------------------------------------

    def _meta_id(self, collection: str) -> str:
        return str(uuid.uuid5(_META_NS, collection))

    async def write_meta(self, meta: BuildMeta) -> None:
        if not await self._run(self._client.collection_exists, META_COLLECTION):
            await self._run(
                self._client.create_collection,
                META_COLLECTION,
                vectors_config=models.VectorParams(size=1, distance=models.Distance.DOT),
            )
        await self._run(
            self._client.upsert,
            META_COLLECTION,
            points=[
                models.PointStruct(id=self._meta_id(meta.collection), vector=[1.0], payload=meta.model_dump())
            ],
            wait=True,
        )

    async def read_meta(self, collection: str) -> BuildMeta | None:
        if not await self._run(self._client.collection_exists, META_COLLECTION):
            return None
        found = await self._run(
            self._client.retrieve, META_COLLECTION, ids=[self._meta_id(collection)], with_payload=True
        )
        return BuildMeta.model_validate(found[0].payload) if found else None

    async def live_collection(self) -> str | None:
        aliases = await self._run(
            self._client.get_aliases,
        )
        return next(
            (a.collection_name for a in aliases.aliases if a.alias_name == self.settings.collection_alias),
            None,
        )

    async def set_live(self, collection: str) -> None:
        alias = self.settings.collection_alias
        ops: list[Any] = []
        if await self.live_collection() is not None:
            ops.append(models.DeleteAliasOperation(delete_alias=models.DeleteAlias(alias_name=alias)))
        ops.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(collection_name=collection, alias_name=alias)
            )
        )
        await self._run(self._client.update_collection_aliases, change_aliases_operations=ops)

    async def incomplete_builds(self, exclude: str | None = None) -> list[str]:
        """Collections without build metadata: left by a build that failed or was interrupted."""
        out = []
        for name in await self.build_collections():
            if name != exclude and await self.read_meta(name) is None:
                out.append(name)
        return out

    async def prune(self, keep: str) -> list[str]:
        """
        Delete every incomplete build, and complete builds older than the live one beyond
        `keep_previous`. Only complete builds are kept for rollback: a build interrupted
        half-way is newer than the live one but must never become the rollback target.
        """
        incomplete = await self.incomplete_builds(exclude=keep)
        complete = [c for c in await self.build_collections() if c != keep and c not in incomplete]
        older = [c for c in complete if c < keep]
        newer = [c for c in complete if c > keep]
        doomed = incomplete + older[: max(0, len(older) - self.settings.keep_previous)] + newer
        for name in doomed:
            await self.delete_collection(name)
        return sorted(doomed)

    # ---- reading -----------------------------------------------------------

    async def hybrid_search(
        self,
        collection: str,
        dense_queries: Sequence[Sequence[float]],
        sparse_query: tuple[list[int], list[float]],
        *,
        k_dense: int,
        k_sparse: int,
        limit: int,
        flt: SearchFilter | None = None,
        mode: str = "hybrid",
    ) -> list[ScoredChunk]:
        qfilter = flt.to_qdrant() if flt else None
        prefetch = []
        if mode in ("hybrid", "dense"):
            prefetch = [
                models.Prefetch(query=list(v), using=DENSE, limit=k_dense, filter=qfilter)
                for v in dense_queries
            ]
        if sparse_query[0] and mode in ("hybrid", "bm25"):
            prefetch.append(
                models.Prefetch(
                    query=models.SparseVector(indices=sparse_query[0], values=sparse_query[1]),
                    using=SPARSE,
                    limit=k_sparse,
                    filter=qfilter,
                )
            )
        try:
            result = await self._run(
                self._client.query_points,
                collection,
                prefetch=prefetch,
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            raise UpstreamUnavailable(f"Qdrant search failed: {exc!r}", service="qdrant") from exc
        return [ScoredChunk(chunk=chunk_from_payload(p.payload or {}), score=p.score) for p in result.points]

    async def lookup_unit(self, collection: str, short_name: str, unit: str) -> list[Chunk]:
        """Exact fetch of a numbered unit, e.g. ("IPC", "302"); pieces returned in order."""
        flt = models.Filter(
            must=[
                models.FieldCondition(key="short_name", match=models.MatchValue(value=short_name)),
                models.FieldCondition(key="unit", match=models.MatchValue(value=unit)),
                models.FieldCondition(key="unit_label", match=models.MatchAny(any=["Section", "Article"])),
            ]
        )
        points, _ = await self._run(
            self._client.scroll, collection, scroll_filter=flt, limit=50, with_payload=True
        )
        chunks = [chunk_from_payload(p.payload or {}) for p in points]
        return sorted(chunks, key=lambda c: c.metadata.part)

    async def scroll_all(self, collection: str, flt: models.Filter) -> list[Chunk]:
        """Every chunk matching `flt` (paged)."""
        chunks: list[Chunk] = []
        offset: Any = None
        while True:
            points, offset = await self._run(
                self._client.scroll,
                collection,
                scroll_filter=flt,
                limit=512,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            chunks.extend(chunk_from_payload(p.payload or {}) for p in points)
            if offset is None:
                return chunks

    async def statute_sections(self, collection: str, short_names: Sequence[str]) -> list[Chunk]:
        """All section chunks of the given acts (for the old/new code correspondence)."""
        flt = models.Filter(
            must=[
                models.FieldCondition(key="short_name", match=models.MatchAny(any=list(short_names))),
                models.FieldCondition(key="unit_label", match=models.MatchValue(value="Section")),
            ]
        )
        return await self.scroll_all(collection, flt)

    async def get_chunk(self, collection: str, chunk_id: str) -> Chunk | None:
        try:
            points = await self._run(
                self._client.retrieve, collection, ids=[chunk_id], with_payload=True, with_vectors=False
            )
        except (ValueError, UnexpectedResponse):  # not a valid point id
            return None
        return chunk_from_payload(points[0].payload or {}) if points else None

    async def parts_of(self, collection: str, parent_id: str) -> list[Chunk]:
        """All pieces of one section or unit, in order."""
        flt = models.Filter(
            must=[models.FieldCondition(key="parent_id", match=models.MatchValue(value=parent_id))]
        )
        return sorted(await self.scroll_all(collection, flt), key=lambda c: c.metadata.part)

    async def best_dense_score(
        self, collection: str, vector: Sequence[float], flt: SearchFilter | None = None
    ) -> float | None:
        """Cosine similarity of the closest passage: the abstention signal without a reranker."""
        try:
            result = await self._run(
                self._client.query_points,
                collection,
                query=list(vector),
                using=DENSE,
                limit=1,
                query_filter=flt.to_qdrant() if flt else None,
                with_payload=False,
            )
        except Exception as exc:
            raise UpstreamUnavailable(f"Qdrant search failed: {exc!r}", service="qdrant") from exc
        return float(result.points[0].score) if result.points else None
