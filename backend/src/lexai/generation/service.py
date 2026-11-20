"""
Chat orchestration (SSE contract v1).

    question ─▶ condense (follow-ups only) ─▶ plan (references, intent, applicable code)
             ─▶ guardrails (refuse / redirect without searching)
             ─▶ cache ─▶ routed retrieval ─▶ abstain? ─▶ context ─▶ stream ─▶ verify

Events, in order: `meta`, `citations`, `token`*, `verification`, `done`; or a
single `error` with a stable code and the request id (details only in logs).
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

from lexai.clients.llm import LLMClient
from lexai.core.errors import IndexNotReady, LexAIError, RetrievalError
from lexai.core.logging import get_logger
from lexai.core.settings import Settings
from lexai.domain.models import (
    ChatMessage,
    Citation,
    DonePayload,
    ErrorPayload,
    LatencyBreakdown,
    MetaPayload,
    ScoredChunk,
    StreamEvent,
)
from lexai.generation.context import assemble
from lexai.generation.prompts import (
    HYDE_PROMPT,
    PROMPT_VERSION,
    applicable_law_text,
    format_context,
    render_system_prompt,
    status_text,
)
from lexai.generation.verify import Verification, verify
from lexai.query.condense import condense
from lexai.query.understand import QueryPlan, plan_query
from lexai.retrieval.qdrant_store import QdrantStore
from lexai.retrieval.router import Retrieved, Router
from lexai.services.cache import AnswerCache, cache_key, normalise
from lexai.services.sessions import SessionStore

logger = get_logger(__name__)

ABSTAIN_ANSWER = (
    "The indexed sources do not answer this question. The closest passages are listed below; "
    "they may help you rephrase it or narrow it to a specific act or section."
)
NO_RESULTS_ANSWER = (
    "I couldn't find anything relevant to that question in the indexed sources. "
    "Try rephrasing, or ask about a specific statute, section or case."
)
SNIPPET_CHARS = 300


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def build_citations(chunks: list[ScoredChunk], exact_parents: set[str] | None = None) -> list[Citation]:
    exact_parents = exact_parents or set()
    return [
        Citation(
            ref=n,
            chunk_id=item.chunk.chunk_id,
            label=meta.label,
            doc_id=meta.doc_id,
            doc_title=meta.doc_title,
            unit_label=meta.unit_label,
            unit=meta.unit,
            unit_title=meta.unit_title,
            status=status_text(meta),
            source_file=meta.source_file,
            page=meta.page_start,
            law_type=meta.doc_type,
            sections=[meta.label, *meta.sections] if meta.unit else meta.sections,
            relevance_score=round(item.score, 4),
            snippet=re.sub(r"\s+", " ", item.chunk.text[:SNIPPET_CHARS]).strip(),
            exact=meta.parent_id in exact_parents,
        )
        for n, item in enumerate(chunks, start=1)
        for meta in [item.chunk.metadata]
    ]


class ChatService:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        router: Router | None,
        sessions: SessionStore,
        *,
        store: QdrantStore | None = None,
        collection: str | None = None,
        cache: AnswerCache | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.router = router
        self.sessions = sessions
        self.store = store
        self.collection = collection
        self.cache = cache

    @property
    def index_version(self) -> str:
        return self.collection or ""

    async def stream_answer(
        self, question: str, session_id: str, request_id: str
    ) -> AsyncGenerator[StreamEvent, None]:
        try:
            async for event in self._stream(question, session_id, request_id):
                yield event
        except LexAIError as exc:
            logger.error("chat_failed", code=exc.code, error=str(exc))
            yield StreamEvent(
                event="error",
                data=ErrorPayload(
                    code=exc.code, message=exc.public_message, request_id=request_id
                ).model_dump(),
            )
        except Exception as exc:
            logger.exception("chat_failed_unexpected", error=repr(exc))
            yield StreamEvent(
                event="error",
                data=ErrorPayload(
                    code="internal_error", message="An unexpected error occurred.", request_id=request_id
                ).model_dump(),
            )

    async def _hyde(self, question: str) -> str | None:
        try:
            text = await self.llm.complete(
                [ChatMessage(role="user", content=HYDE_PROMPT.format(question=question))],
                temperature=0.5,
                max_tokens=256,
            )
            return text.strip() or None
        except LexAIError as exc:  # experimental feature: log and continue without it
            logger.warning("hyde_failed", error=str(exc))
            return None

    def _should_abstain(self, retrieved: Retrieved) -> bool:
        if retrieved.found_exact:
            return False
        q = self.settings.query
        if retrieved.best_rerank is not None and q.abstain_min_rerank is not None:
            return retrieved.best_rerank < q.abstain_min_rerank
        if retrieved.best_dense is not None and q.abstain_min_dense is not None:
            return retrieved.best_dense < q.abstain_min_dense
        return False

    async def _stream(self, question: str, session_id: str, request_id: str) -> AsyncIterator[StreamEvent]:
        t_start = time.perf_counter()
        latency: dict[str, Any] = {}
        history = self.sessions.history(session_id)

        standalone = question
        if self.settings.query.condense_enabled and history:
            t0 = time.perf_counter()
            standalone = await condense(
                self.llm,
                question,
                history,
                turns=self.settings.query.condense_turns,
                max_tokens=self.settings.query.condense_max_tokens,
            )
            latency["condense_ms"] = _ms(t0)
        plan = plan_query(question, standalone)
        meta = MetaPayload(request_id=request_id, session_id=session_id, **plan.summary())  # type: ignore[arg-type]
        yield StreamEvent(event="meta", data=meta.model_dump())

        if plan.reply is not None:  # refused or out of scope: no search, no model call
            async for event in self._fixed_reply(plan, plan.reply, session_id, request_id, latency, t_start):
                yield event
            return
        if self.router is None or self.store is None or self.collection is None:
            raise IndexNotReady()

        key = cache_key(
            normalise(plan.standalone), self.index_version, PROMPT_VERSION, self.settings.llm.model
        )
        if self.cache is not None and (hit := await self.cache.get(key)):
            async for event in self._replay(hit, question, session_id, request_id, latency, t_start):
                yield event
            return

        extra: list[str] = []
        if self.settings.retrieval.hyde_enabled:
            t0 = time.perf_counter()
            hyde_text = await self._hyde(plan.standalone)
            latency["hyde_ms"] = _ms(t0)
            if hyde_text:
                extra.append(hyde_text)

        t0 = time.perf_counter()
        try:
            retrieved = await self.router.retrieve(plan, extra)
        except LexAIError:
            raise
        except Exception as exc:
            raise RetrievalError(repr(exc)) from exc
        latency["retrieval_ms"] = _ms(t0)

        if not retrieved.pinned and not retrieved.ranked:
            async for event in self._fixed_reply(
                plan, NO_RESULTS_ANSWER, session_id, request_id, latency, t_start, abstained=True
            ):
                yield event
            return
        if self._should_abstain(retrieved):
            closest = [*retrieved.pinned, *retrieved.ranked][: self.settings.query.abstain_show_sources]
            logger.info("abstained", best_dense=retrieved.best_dense, best_rerank=retrieved.best_rerank)
            async for event in self._fixed_reply(
                plan,
                ABSTAIN_ANSWER,
                session_id,
                request_id,
                latency,
                t_start,
                abstained=True,
                sources=closest,
            ):
                yield event
            return

        t0 = time.perf_counter()
        exact_parents = {p.chunk.metadata.parent_id for p in retrieved.pinned}
        passages = await assemble(
            self.store,
            self.collection,
            retrieved,
            char_budget=self.settings.retrieval.context_char_budget,
            max_passages=len(exact_parents) + self.settings.rerank.top_n,
        )
        latency["context_ms"] = _ms(t0)
        citations = build_citations(passages, exact_parents)
        yield StreamEvent(event="citations", data=[c.model_dump() for c in citations])

        system = render_system_prompt(
            format_context(passages), applicable_law_text(plan.era.era, plan.era.reason), retrieved.notes
        )
        keep = self.settings.api.history_messages_in_prompt
        messages = [ChatMessage(role="system", content=system)]
        messages += history[-keep:] if keep else []
        messages.append(ChatMessage(role="user", content=plan.standalone))

        parts: list[str] = []
        usage: dict[str, int] = {}
        t0 = time.perf_counter()
        async for delta in self.llm.stream_chat(messages, usage=usage):
            if "time_to_first_token_ms" not in latency:
                latency["time_to_first_token_ms"] = _ms(t_start)
            parts.append(delta)
            yield StreamEvent(event="token", data={"text": delta})
        latency["generation_ms"] = _ms(t0)

        t0 = time.perf_counter()
        checked = verify("".join(parts), passages)
        latency["verify_ms"] = _ms(t0)
        yield StreamEvent(event="verification", data=checked.payload())
        if checked.flags:
            logger.warning("answer_flagged", flags=checked.flags, standalone_query=plan.standalone)

        self.sessions.append_turn(session_id, question, checked.answer)
        done = self._done(
            checked.answer, session_id, request_id, citations, latency, t_start,
            usage=usage or None, notes=retrieved.notes, verification=checked,
        )  # fmt: skip
        if self.cache is not None and checked.valid:
            await self.cache.set(
                key,
                {
                    "answer": checked.answer,
                    "citations": [c.model_dump() for c in citations],
                    "verification": checked.payload(),
                    "notes": retrieved.notes,
                },
            )
        yield done

    async def search(self, question: str, session_id: str | None) -> dict[str, Any]:
        """
        Retrieval only (no answer, nothing added to the session): the plan, the passages
        the model would see, correspondence notes and the abstention signal. For
        debugging and evaluation.
        """
        history = self.sessions.history(session_id) if session_id else []
        standalone = question
        if self.settings.query.condense_enabled and history:
            standalone = await condense(
                self.llm, question, history,
                turns=self.settings.query.condense_turns, max_tokens=self.settings.query.condense_max_tokens,
            )  # fmt: skip
        plan = plan_query(question, standalone)
        result: dict[str, Any] = {"plan": plan.summary(), "reply": plan.reply}
        if plan.reply is not None:
            return result | {"passages": [], "notes": [], "abstain": False}
        if self.router is None or self.store is None or self.collection is None:
            raise IndexNotReady()
        retrieved = await self.router.retrieve(plan)
        exact_parents = {p.chunk.metadata.parent_id for p in retrieved.pinned}
        passages = await assemble(
            self.store,
            self.collection,
            retrieved,
            char_budget=self.settings.retrieval.context_char_budget,
            max_passages=len(exact_parents) + self.settings.rerank.top_n,
        )
        return result | {
            "passages": [c.model_dump() for c in build_citations(passages, exact_parents)],
            "notes": retrieved.notes,
            "best_dense": retrieved.best_dense,
            "best_rerank": retrieved.best_rerank,
            "abstain": self._should_abstain(retrieved),
            "index_version": self.index_version,
        }

    async def _fixed_reply(
        self,
        plan: QueryPlan,
        text: str,
        session_id: str,
        request_id: str,
        latency: dict[str, Any],
        t_start: float,
        *,
        abstained: bool = False,
        sources: list[ScoredChunk] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        citations = build_citations(sources or [])
        yield StreamEvent(event="citations", data=[c.model_dump() for c in citations])
        yield StreamEvent(event="token", data={"text": text})
        checked = Verification(valid=True, coverage=None, answer=text)
        yield StreamEvent(event="verification", data=checked.payload())
        self.sessions.append_turn(session_id, plan.question, text)
        yield self._done(
            text,
            session_id,
            request_id,
            citations,
            latency,
            t_start,
            abstained=abstained,
            verification=checked,
        )

    async def _replay(
        self,
        hit: dict[str, Any],
        question: str,
        session_id: str,
        request_id: str,
        latency: dict[str, Any],
        t_start: float,
    ) -> AsyncIterator[StreamEvent]:
        citations = [Citation(**c) for c in hit["citations"]]
        yield StreamEvent(event="citations", data=hit["citations"])
        yield StreamEvent(event="token", data={"text": hit["answer"]})
        yield StreamEvent(event="verification", data=hit["verification"])
        self.sessions.append_turn(session_id, question, hit["answer"])
        checked = Verification(**{**hit["verification"], "answer": hit["answer"]})
        yield self._done(
            hit["answer"], session_id, request_id, citations, latency, t_start,
            cached=True, notes=hit.get("notes", []), verification=checked,
        )  # fmt: skip

    def _done(
        self,
        answer: str,
        session_id: str,
        request_id: str,
        citations: list[Citation],
        latency: dict[str, Any],
        t_start: float,
        *,
        usage: dict[str, int] | None = None,
        cached: bool = False,
        abstained: bool = False,
        notes: list[str] | None = None,
        verification: Verification | None = None,
    ) -> StreamEvent:
        breakdown = LatencyBreakdown(total_ms=_ms(t_start), **latency)
        logger.info(
            "query_complete",
            prompt_version=PROMPT_VERSION,
            index_version=self.index_version,
            context_chunks=len(citations),
            answer_chars=len(answer),
            cached=cached,
            abstained=abstained,
            verified=verification.valid if verification else None,
            coverage=verification.coverage if verification else None,
            usage=usage,
            **breakdown.model_dump(exclude_none=True),
        )
        payload = DonePayload(
            answer=answer,
            session_id=session_id,
            request_id=request_id,
            citations=citations,
            latency=breakdown,
            prompt_version=PROMPT_VERSION,
            index_version=self.index_version,
            usage=usage,
            cached=cached,
            abstained=abstained,
            notes=notes or [],
            verification=verification.payload() if verification else None,
        )
        return StreamEvent(event="done", data=payload.model_dump())
