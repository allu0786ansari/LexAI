"""
Builds and owns every long-lived object (HTTP clients, index, models).

Start-up rules:
- A missing index does not stop the API; `/readyz` reports it and chat returns
  an `index_not_ready` error until ingestion has run.
- An index built with a different embedding model, or an embedder returning
  vectors of the wrong size, stops start-up: serving would give wrong results.
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
from lexai.core.errors import ConfigurationError, IndexNotReady, UpstreamUnavailable
from lexai.core.logging import get_logger
from lexai.core.settings import Settings
from lexai.generation.service import ChatService
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.index import LocalIndex
from lexai.services.sessions import SessionStore

logger = get_logger(__name__)


@dataclass
class Container:
    settings: Settings
    llm: LLMClient
    embedder: EmbeddingClient
    reranker: Reranker | None
    index: LocalIndex | None
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

        index: LocalIndex | None
        try:
            index = await asyncio.to_thread(
                LocalIndex.load,
                settings.retrieval.index_dir,
                expected_model=settings.embed.model,
                expected_dimension=settings.embed.dimension,
                expected_document_prefix=settings.embed.document_prefix,
            )
            logger.info("index_loaded", chunks=len(index), built_at=index.meta.built_at)
        except IndexNotReady as exc:
            logger.warning("index_missing", error=str(exc))
            notes.append("index missing: run ingestion")
            index = None

        reranker: Reranker | None = None
        if settings.rerank.enabled:
            reranker = await CrossEncoderReranker.load(settings.rerank)

        retriever = HybridRetriever(index, embedder, settings.retrieval) if index is not None else None
        sessions = SessionStore(
            ttl_minutes=settings.api.session_ttl_minutes,
            max_messages=settings.api.session_max_messages,
            max_sessions=settings.api.max_sessions,
        )
        chat = ChatService(settings, llm, retriever, reranker, sessions)
        return cls(settings, llm, embedder, reranker, index, sessions, chat, notes)

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.embedder.aclose()

    async def readiness(self) -> tuple[bool, dict[str, Any]]:
        async def check(name: str, coro: Any) -> tuple[str, dict[str, Any]]:
            try:
                result = await asyncio.wait_for(coro, timeout=5.0)
                return name, {"ok": True, **result}
            except Exception as exc:
                return name, {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}

        results = dict(
            await asyncio.gather(check("llm", self.llm.health()), check("embedder", self.embedder.health()))
        )
        llm_ok = results["llm"]["ok"] and results["llm"].get("model_available", False)
        if results["llm"]["ok"] and not llm_ok:
            results["llm"]["ok"] = False
            results["llm"]["detail"] = f"model '{self.settings.llm.model}' is not served"
        embed_ok = results["embedder"]["ok"] and results["embedder"].get("dimension_ok", False)
        results["embedder"]["ok"] = embed_ok
        results["index"] = (
            {"ok": True, "chunks": len(self.index), "built_at": self.index.meta.built_at}
            if self.index is not None
            else {"ok": False, "detail": "no index: run ingestion"}
        )
        results["reranker"] = {
            "ok": True,
            "enabled": self.reranker is not None,
            "model": self.reranker.name if self.reranker else None,
        }
        ready = all(v["ok"] for v in results.values())
        return ready, results
