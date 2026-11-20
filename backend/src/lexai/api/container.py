"""
Builds and owns every long-lived object (HTTP clients, Qdrant, models).

Start-up rules:
- No live index does not stop the API; `/readyz` reports it and chat returns an
  `index_not_ready` error until a build has been published.
- A live index built with a different embedding model, dimension, document
  prefix or sparse encoding stops start-up: serving would give wrong results.
- An embedder returning vectors of the wrong size stops start-up.
- An unreachable model server only logs a warning; `/readyz` stays red until
  it comes up, so the API can start before Ollama/vLLM.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from lexai.clients.embedder import EmbeddingClient
from lexai.clients.llm import LLMClient
from lexai.clients.reranker import CrossEncoderReranker, Reranker
from lexai.core.errors import ConfigurationError, IndexMismatchError, UpstreamUnavailable
from lexai.core.logging import get_logger
from lexai.core.settings import Settings
from lexai.generation.service import ChatService
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore
from lexai.retrieval.sparse import SPARSE_VERSION
from lexai.services.sessions import SessionStore

logger = get_logger(__name__)


def check_compatible(meta: BuildMeta, settings: Settings) -> None:
    wanted = {
        "embedding model": (meta.embedding_model, settings.embed.model),
        "dimension": (meta.dimension, settings.embed.dimension),
        "document prefix": (meta.document_prefix, settings.embed.document_prefix),
        "sparse encoding": (meta.sparse_version, SPARSE_VERSION),
    }
    diffs = [f"{k}: index has {a!r}, API configured {b!r}" for k, (a, b) in wanted.items() if a != b]
    if diffs:
        raise IndexMismatchError(
            f"The live index {meta.collection} does not match this configuration ({'; '.join(diffs)}). "
            "Rebuild it with the ingestion build command."
        )


@dataclass
class Container:
    settings: Settings
    llm: LLMClient
    embedder: EmbeddingClient
    reranker: Reranker | None
    store: QdrantStore | None
    collection: str | None
    meta: BuildMeta | None
    sessions: SessionStore
    chat: ChatService
    notes: list[str] = field(default_factory=list)

    @classmethod
    async def build(cls, settings: Settings) -> Container:
        notes: list[str] = []
        llm = LLMClient(settings.llm)
        embedder = EmbeddingClient(settings.embed)

        try:
            probe = await embedder.health()
            if not probe["dimension_ok"]:
                raise ConfigurationError(
                    f"Embedder '{settings.embed.model}' returns {probe['dimension']}-dim vectors; "
                    f"LEXAI_EMBED_DIMENSION is {settings.embed.dimension}."
                )
        except UpstreamUnavailable as exc:
            logger.warning("embedder_unreachable_at_startup", error=str(exc))
            notes.append("embedder unreachable at start-up")

        store = QdrantStore.connect(settings.qdrant)
        collection = await store.live_collection()
        meta = await store.read_meta(collection) if collection else None
        if collection is None or meta is None:
            logger.warning("index_missing", qdrant=str(settings.qdrant.url or settings.qdrant.path))
            notes.append("no live index: run the ingestion build")
            collection = None
        else:
            check_compatible(meta, settings)
            logger.info(
                "index_ready", collection=collection, chunks=meta.total_chunks, built_at=meta.built_at
            )

        reranker: Reranker | None = None
        if settings.rerank.enabled:
            reranker = await CrossEncoderReranker.load(settings.rerank)

        retriever = HybridRetriever(store, collection, embedder, settings.retrieval) if collection else None
        sessions = SessionStore(
            ttl_minutes=settings.api.session_ttl_minutes,
            max_messages=settings.api.session_max_messages,
            max_sessions=settings.api.max_sessions,
        )
        chat = ChatService(settings, llm, retriever, reranker, sessions)
        return cls(settings, llm, embedder, reranker, store, collection, meta, sessions, chat, notes)

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.embedder.aclose()
        if self.store is not None:
            await self.store.close()

    async def _index_health(self) -> dict[str, Any]:
        if self.store is None or self.collection is None or self.meta is None:
            raise RuntimeError("no live index: run the ingestion build")
        count = await self.store.count(self.collection)
        return {
            "collection": self.collection,
            "chunks": count,
            "documents": len(self.meta.docs),
            "built_at": self.meta.built_at,
        }

    async def readiness(self) -> tuple[bool, dict[str, Any]]:
        async def check(name: str, coro: Any) -> tuple[str, dict[str, Any]]:
            try:
                result = await asyncio.wait_for(coro, timeout=5.0)
                return name, {"ok": True, **result}
            except Exception as exc:
                return name, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}

        results = dict(
            await asyncio.gather(
                check("llm", self.llm.health()),
                check("embedder", self.embedder.health()),
                check("index", self._index_health()),
            )
        )
        llm_ok = results["llm"]["ok"] and results["llm"].get("model_available", False)
        if results["llm"]["ok"] and not llm_ok:
            results["llm"]["ok"] = False
            results["llm"]["detail"] = f"model '{self.settings.llm.model}' is not served"
        results["embedder"]["ok"] = results["embedder"]["ok"] and results["embedder"].get(
            "dimension_ok", False
        )
        results["reranker"] = {
            "ok": True,
            "enabled": self.reranker is not None,
            "model": self.reranker.name if self.reranker else None,
        }
        ready = all(v["ok"] for v in results.values())
        return ready, results
