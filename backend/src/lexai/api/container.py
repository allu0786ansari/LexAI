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
from pathlib import Path
from typing import Any

from lexai.clients.embedder import EmbeddingClient
from lexai.clients.llm import LLMClient
from lexai.clients.reranker import CrossEncoderReranker, Reranker
from lexai.core.errors import (
    ConfigurationError,
    IndexMismatchError,
    IndexNotReady,
    LexAIError,
    UpstreamUnavailable,
)
from lexai.core.logging import get_logger
from lexai.core.settings import Settings
from lexai.domain.models import ChunkResponse, DocumentInfo
from lexai.generation.context import join_pieces
from lexai.generation.prompts import answer_template, status_text
from lexai.generation.service import ChatService
from lexai.ingestion.indexer import build_index
from lexai.ingestion.manifest import expand, load_manifest, source_path
from lexai.query.references import NEW_CODES, OLD_CODES
from lexai.retrieval.correspondence import Correspondence, sections_from_chunks
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore
from lexai.retrieval.router import Router
from lexai.retrieval.sparse import SPARSE_VERSION
from lexai.services.cache import AnswerCache, build_cache
from lexai.services.feedback import FeedbackStore
from lexai.services.jobs import JobRunner
from lexai.services.ratelimit import RateLimiter
from lexai.services.sessions import MemorySessionStore, RedisSessionStore, SessionStore

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


async def load_correspondence(store: QdrantStore, collection: str) -> Correspondence:
    """Derive the old/new code correspondence from the live index (~1 s for ~3,400 chunks)."""
    chunks = await store.statute_sections(collection, [*OLD_CODES, *NEW_CODES])
    table = await asyncio.to_thread(Correspondence.build, sections_from_chunks(chunks))
    logger.info("correspondence_ready", sections=len(table))
    return table


def make_sessions(settings: Settings) -> SessionStore:
    api = settings.api
    if api.redis_url:
        return RedisSessionStore.from_url(api.redis_url, api.session_ttl_minutes, api.session_max_messages)
    return MemorySessionStore(api.session_ttl_minutes, api.session_max_messages, api.max_sessions)


async def build_chat(
    settings: Settings,
    llm: LLMClient,
    embedder: EmbeddingClient,
    reranker: Reranker | None,
    store: QdrantStore | None,
    collection: str | None,
    sessions: SessionStore,
    cache: AnswerCache | None,
) -> ChatService:
    answer_template(settings.llm.prompt_version)  # fail at start-up on an unknown prompt version
    router = None
    if store is not None and collection is not None:
        router = Router(
            store,
            collection,
            embedder,
            HybridRetriever(store, collection, embedder, settings.retrieval),
            await load_correspondence(store, collection),
            settings.retrieval,
            settings.query,
            reranker,
            settings.rerank.top_n,
        )
    return ChatService(settings, llm, router, sessions, store=store, collection=collection, cache=cache)


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
    cache: AnswerCache | None = None
    limiter: RateLimiter = field(default_factory=RateLimiter)
    feedback: FeedbackStore | None = None
    jobs: JobRunner = field(default_factory=JobRunner)
    documents: list[DocumentInfo] = field(default_factory=list)
    _pdfs: dict[str, Path] = field(default_factory=dict)

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

        sessions = make_sessions(settings)
        if settings.api.redis_url and not settings.cache.redis_url:
            settings.cache.redis_url = settings.api.redis_url
        cache = build_cache(settings.cache)
        chat = await build_chat(settings, llm, embedder, reranker, store, collection, sessions, cache)
        container = cls(
            settings, llm, embedder, reranker, store, collection, meta, sessions, chat, notes, cache,
            limiter=RateLimiter(settings.api.redis_url),
            feedback=FeedbackStore(settings.api.feedback_db),
        )  # fmt: skip
        container.load_documents()
        logger.info(
            "state_backends",
            sessions=type(sessions).__name__,
            rate_limits=container.limiter.backend,
            cache=type(cache).__name__ if cache else None,
        )
        return container

    # ------------------------------------------------------------------ corpus

    def load_documents(self) -> None:
        """The corpus manifest with build counts and PDF availability (for /documents and /chunks)."""
        corpus = self.settings.corpus
        try:
            specs = expand(load_manifest(corpus.manifest), corpus.bundled_dir)
        except (OSError, LexAIError, ValueError) as exc:
            logger.warning("manifest_unavailable", error=str(exc))
            return
        built = self.meta.docs if self.meta else {}
        docs, pdfs = [], {}
        for spec in specs:
            path = source_path(spec, corpus.bundled_dir, corpus.raw_dir)
            if path.is_file():
                pdfs[spec.id] = path
            docs.append(
                DocumentInfo(
                    doc_id=spec.id,
                    title=built[spec.id].title if spec.id in built and built[spec.id].title else spec.title,
                    short_name=spec.short_name,
                    doc_type=spec.doc_type,
                    status=spec.status,
                    repealed_on=spec.repealed_on.isoformat() if spec.repealed_on else None,
                    replaced_by=spec.replaced_by,
                    chunks=built[spec.id].chunks if spec.id in built else 0,
                    source_url=spec.urls[0] if spec.urls else None,
                    has_pdf=path.is_file(),
                )
            )
        self.documents, self._pdfs = docs, pdfs

    def pdf_path(self, doc_id: str) -> Path | None:
        """The source PDF of a manifest document (never a client-supplied path)."""
        return self._pdfs.get(doc_id)

    async def chunk(self, chunk_id: str) -> ChunkResponse | None:
        if self.store is None or self.collection is None:
            raise IndexNotReady()
        found = await self.store.get_chunk(self.collection, chunk_id)
        if found is None:
            return None
        meta = found.metadata
        text = found.text
        if meta.unit and meta.doc_type != "judgment":  # the whole section, not just this piece
            parts = await self.store.parts_of(self.collection, meta.parent_id)
            text = join_pieces([p.text for p in parts]) if parts else text
        page = meta.page_start
        return ChunkResponse(
            chunk_id=found.chunk_id,
            label=meta.label,
            doc_id=meta.doc_id,
            doc_title=meta.doc_title,
            doc_type=meta.doc_type,
            unit_label=meta.unit_label,
            unit=meta.unit,
            unit_title=meta.unit_title,
            status=status_text(meta),
            text=text,
            page_start=meta.page_start,
            page_end=meta.page_end,
            source_file=meta.source_file,
            pdf_url=(
                f"/api/v1/documents/{meta.doc_id}/pdf" + (f"#page={page}" if page else "")
                if meta.doc_id in self._pdfs
                else None
            ),
        )

    # ------------------------------------------------------------------ reindex

    async def reindex(self, only: list[str] | None = None) -> str:
        """Build a new index in-process (same store) and switch to it when it is live."""
        if self.store is None:
            raise IndexNotReady()
        report = await build_index(self.settings, only=only, store=self.store)
        if report.status.startswith("live"):
            await self.reload()
        return report.status

    async def refresh_index(self) -> bool:
        """Switch to the live index if another process published one (ingest job, other replica)."""
        if self.store is None:
            return False
        live = await self.store.live_collection()
        if live is None or live == self.collection:
            return False
        logger.info("new_live_index_detected", previous=self.collection, live=live)
        await self.reload()
        return True

    async def reload(self) -> None:
        assert self.store is not None
        collection = await self.store.live_collection()
        meta = await self.store.read_meta(collection) if collection else None
        if collection is None or meta is None:
            raise IndexNotReady()
        check_compatible(meta, self.settings)
        chat = await build_chat(
            self.settings,
            self.llm,
            self.embedder,
            self.reranker,
            self.store,
            collection,
            self.sessions,
            self.cache,
        )
        # Swap in one step; requests already running keep the old service (its build is kept).
        self.collection, self.meta, self.chat = collection, meta, chat
        self.load_documents()
        logger.info("index_reloaded", collection=collection, chunks=meta.total_chunks)

    async def aclose(self) -> None:
        await self.jobs.aclose()
        await self.sessions.aclose()
        if self.cache is not None:
            await self.cache.aclose()
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
