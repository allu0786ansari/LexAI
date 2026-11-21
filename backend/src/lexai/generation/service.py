"""
Chat orchestration (SSE contract v1).

    question ─▶ condense (follow-ups only) ─▶ plan (references, intent, applicable code)
             ─▶ guardrails (refuse / redirect without searching)
             ─▶ cache ─▶ routed retrieval ─▶ abstain? ─▶ context ─▶ stream ─▶ verify

Events, in order: `meta`, `citations`, `token`*, `verification`, `done`; or a
single `error` with a stable code and the request id (details only in logs).

Each request is one trace: a `lexai.chat` span with a child per stage, and the
same stage timings go to Prometheus (`lexai_stage_duration_seconds`).
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import Span, Status, StatusCode

from lexai.clients.llm import LLMClient
from lexai.core.errors import IndexNotReady, LexAIError, RetrievalError
from lexai.core.logging import get_logger, mask_pii
from lexai.core.metrics import (
    ANSWERS,
    CACHE_REQUESTS,
    STAGE_DURATION,
    TIME_TO_FIRST_TOKEN,
    TOKENS,
    UPSTREAM_ERRORS,
    VERIFICATION_FAILURES,
)
from lexai.core.settings import Settings
from lexai.core.telemetry import tracer
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
    applicable_law_text,
    format_context,
    render_system_prompt,
    status_text,
)
from lexai.generation.verify import Verification, verify
from lexai.query.condense import condense
from lexai.query.understand import QueryPlan, plan_query
from lexai.retrieval.qdrant_store import QdrantStore, SearchFilter
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
            doc_type=meta.doc_type,
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

    # ------------------------------------------------------------------ tracing helpers

    def _text(self, text: str) -> str:
        """Question/answer text for traces: as is only when the operator opted in."""
        return text if self.settings.obs.capture_text else mask_pii(text)

    @contextmanager
    def _stage(
        self, name: str, ctx: Context, latency: dict[str, Any] | None = None, **attrs: Any
    ) -> Iterator[Span]:
        """A child span + stage timing for a block that does not yield events."""
        t0 = time.perf_counter()
        with tracer.start_as_current_span(f"lexai.{name}", context=ctx, attributes=attrs) as span:
            yield span
        ms = _ms(t0)
        if latency is not None:
            latency[f"{name}_ms" if name != "retrieve" else "retrieval_ms"] = ms
        STAGE_DURATION.labels(stage=name).observe(ms / 1000)

    # ------------------------------------------------------------------ chat

    async def stream_answer(
        self,
        question: str,
        session_id: str,
        request_id: str,
        filters: SearchFilter | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        root = tracer.start_span(
            "lexai.chat",
            attributes={
                "lexai.request_id": request_id,
                "lexai.session_id": session_id,
                "lexai.prompt_version": self.settings.llm.prompt_version,
                "lexai.index_version": self.index_version,
                "lexai.question": self._text(question),
                "lexai.filters": repr(filters) if filters else "",
            },
        )
        ctx = trace.set_span_in_context(root)
        state: dict[str, Any] = {"outcome": "error"}
        try:
            async for event in self._stream(question, session_id, request_id, filters, ctx, state):
                yield event
        except LexAIError as exc:
            logger.error("chat_failed", code=exc.code, error=str(exc))
            root.record_exception(exc)
            root.set_status(Status(StatusCode.ERROR, exc.code))
            UPSTREAM_ERRORS.labels(service=exc.service or "lexai", code=exc.code).inc()
            yield StreamEvent(
                event="error",
                data=ErrorPayload(
                    code=exc.code, message=exc.public_message, request_id=request_id
                ).model_dump(),
            )
        except Exception as exc:
            logger.exception("chat_failed_unexpected", error=repr(exc))
            root.record_exception(exc)
            root.set_status(Status(StatusCode.ERROR, "internal_error"))
            UPSTREAM_ERRORS.labels(service="lexai", code="internal_error").inc()
            yield StreamEvent(
                event="error",
                data=ErrorPayload(
                    code="internal_error", message="An unexpected error occurred.", request_id=request_id
                ).model_dump(),
            )
        finally:
            ANSWERS.labels(outcome=state["outcome"]).inc()
            root.set_attribute("lexai.outcome", state["outcome"])
            root.end()

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

    async def _retrieve(
        self, plan: QueryPlan, latency: dict[str, Any], ctx: Context, filters: SearchFilter | None = None
    ) -> Retrieved:
        assert self.router is not None
        extra: list[str] = []
        if self.settings.retrieval.hyde_enabled:
            with self._stage("hyde", ctx, latency):
                hyde_text = await self._hyde(plan.standalone)
            if hyde_text:
                extra.append(hyde_text)
        with self._stage(
            "retrieve", ctx, latency, **{"lexai.standalone_query": self._text(plan.standalone)}
        ) as span:
            try:
                retrieved = await self.router.retrieve(plan, extra, filters)
            except LexAIError:
                raise
            except Exception as exc:
                raise RetrievalError(repr(exc), service="qdrant") from exc
            ranked = [*retrieved.pinned, *retrieved.ranked]
            span.set_attributes(
                {
                    "lexai.retrieval.chunk_ids": [r.chunk.chunk_id for r in ranked],
                    "lexai.retrieval.labels": [r.chunk.metadata.label for r in ranked],
                    "lexai.retrieval.scores": [round(r.score, 4) for r in ranked],
                    "lexai.retrieval.pinned": len(retrieved.pinned),
                    "lexai.retrieval.best_dense": retrieved.best_dense
                    if retrieved.best_dense is not None
                    else -1.0,
                }
            )
        return retrieved

    def _should_abstain(self, retrieved: Retrieved) -> bool:
        if retrieved.found_exact:
            return False
        q = self.settings.query
        if retrieved.best_rerank is not None and q.abstain_min_rerank is not None:
            return retrieved.best_rerank < q.abstain_min_rerank
        if retrieved.best_dense is not None and q.abstain_min_dense is not None:
            return retrieved.best_dense < q.abstain_min_dense
        return False

    async def _plan(
        self, question: str, history: list[ChatMessage], latency: dict[str, Any], ctx: Context
    ) -> QueryPlan:
        own = plan_query(question)
        if own.intent == "refused":  # decided on the user's own words: no rewrite, no model call
            return own
        standalone = question
        if self.settings.query.condense_enabled and history:
            with self._stage("condense", ctx, latency) as span:
                standalone = await condense(
                    self.llm,
                    question,
                    history,
                    turns=self.settings.query.condense_turns,
                    max_tokens=self.settings.query.condense_max_tokens,
                )
                span.set_attribute("lexai.standalone_query", self._text(standalone))
        return plan_query(question, standalone)

    async def _stream(
        self,
        question: str,
        session_id: str,
        request_id: str,
        filters: SearchFilter | None,
        ctx: Context,
        state: dict[str, Any],
    ) -> AsyncIterator[StreamEvent]:
        t_start = time.perf_counter()
        latency: dict[str, Any] = {}
        history = await self.sessions.history(session_id)
        plan = await self._plan(question, history, latency, ctx)
        root = trace.get_current_span(ctx)
        root.set_attributes({"lexai.intent": plan.intent, "lexai.applicable_code": plan.era.era})
        span_ctx = root.get_span_context()
        meta = MetaPayload(
            request_id=request_id,
            session_id=session_id,
            trace_id=f"{span_ctx.trace_id:032x}" if span_ctx.is_valid else "",
            **plan.summary(),  # type: ignore[arg-type]
        )
        yield StreamEvent(event="meta", data=meta.model_dump())

        if plan.reply is not None:  # refused or out of scope: no search, no model call
            state["outcome"] = "refused" if plan.intent == "refused" else "out_of_scope"
            async for event in self._fixed_reply(plan, plan.reply, session_id, request_id, latency, t_start):
                yield event
            return
        if self.router is None or self.store is None or self.collection is None:
            raise IndexNotReady()

        key = cache_key(
            normalise(plan.standalone) + (f"|{filters!r}" if filters else ""),
            self.index_version,
            self.settings.llm.prompt_version,
            self.settings.llm.model,
        )
        if self.cache is not None:
            hit = await self.cache.get(key)
            CACHE_REQUESTS.labels(result="hit" if hit else "miss").inc()
            if hit:
                state["outcome"] = "cached"
                root.set_attribute("lexai.cached", True)
                async for event in self._replay(hit, question, session_id, request_id, latency, t_start):
                    yield event
                return

        retrieved = await self._retrieve(plan, latency, ctx, filters)

        if not retrieved.pinned and not retrieved.ranked:
            state["outcome"] = "abstained"
            async for event in self._fixed_reply(
                plan, NO_RESULTS_ANSWER, session_id, request_id, latency, t_start, abstained=True
            ):
                yield event
            return
        if self._should_abstain(retrieved):
            state["outcome"] = "abstained"
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

        exact_parents = {p.chunk.metadata.parent_id for p in retrieved.pinned}
        with self._stage("context", ctx, latency) as span:
            passages = await assemble(
                self.store,
                self.collection,
                retrieved,
                char_budget=self.settings.retrieval.context_char_budget,
                max_passages=len(exact_parents) + self.settings.rerank.top_n,
            )
            span.set_attributes(
                {
                    "lexai.context.passages": [p.chunk.metadata.label for p in passages],
                    "lexai.context.chunk_ids": [p.chunk.chunk_id for p in passages],
                    "lexai.context.chars": sum(len(p.chunk.text) for p in passages),
                }
            )
        citations = build_citations(passages, exact_parents)
        yield StreamEvent(event="citations", data=[c.model_dump() for c in citations])

        system = render_system_prompt(
            format_context(passages),
            applicable_law_text(plan.era.era, plan.era.reason),
            retrieved.notes,
            self.settings.llm.prompt_version,
        )
        keep = self.settings.api.history_messages_in_prompt
        messages = [ChatMessage(role="system", content=system)]
        messages += history[-keep:] if keep else []
        messages.append(ChatMessage(role="user", content=plan.standalone))

        parts: list[str] = []
        usage: dict[str, int] = {}
        t0 = time.perf_counter()
        # Not a "current" span: the block yields, and context must not be held across yields.
        gen = tracer.start_span(
            "lexai.generate",
            context=ctx,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": self.settings.llm.model,
                "lexai.prompt_version": self.settings.llm.prompt_version,
            },
        )
        try:
            async for delta in self.llm.stream_chat(messages, usage=usage):
                if "time_to_first_token_ms" not in latency:
                    latency["time_to_first_token_ms"] = _ms(t_start)
                    TIME_TO_FIRST_TOKEN.observe(latency["time_to_first_token_ms"] / 1000)
                    gen.add_event("first_token")
                parts.append(delta)
                yield StreamEvent(event="token", data={"text": delta})
        finally:
            latency["generation_ms"] = _ms(t0)
            STAGE_DURATION.labels(stage="generate").observe(latency["generation_ms"] / 1000)
            gen.set_attributes(
                {
                    "gen_ai.usage.input_tokens": usage.get("prompt_tokens", 0),
                    "gen_ai.usage.output_tokens": usage.get("completion_tokens", 0),
                    "lexai.time_to_first_token_ms": latency.get("time_to_first_token_ms", -1.0),
                    "lexai.answer": self._text("".join(parts)),
                }
            )
            gen.end()
        for kind in ("prompt_tokens", "completion_tokens"):
            if usage.get(kind):
                TOKENS.labels(kind=kind.split("_")[0]).inc(usage[kind])

        with self._stage("verify", ctx, latency) as span:
            checked = verify("".join(parts), passages)
            span.set_attributes(
                {
                    "lexai.verification.valid": checked.valid,
                    "lexai.verification.coverage": checked.coverage if checked.coverage is not None else -1.0,
                    "lexai.verification.flags": [f"{f['kind']}: {f['detail']}" for f in checked.flags],
                }
            )
        yield StreamEvent(event="verification", data=checked.payload())
        if checked.flags:
            logger.warning("answer_flagged", flags=checked.flags, standalone_query=plan.standalone)
            for f in checked.flags:
                VERIFICATION_FAILURES.labels(kind=f["kind"]).inc()
        state["outcome"] = "answered"

        await self.sessions.append_turn(session_id, question, checked.answer)
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

    async def search(
        self, question: str, session_id: str | None, filters: SearchFilter | None = None
    ) -> dict[str, Any]:
        """
        Retrieval only (no answer, nothing added to the session): the plan, the passages
        the model would see, correspondence notes and the abstention signal. For
        debugging and evaluation.
        """
        with tracer.start_as_current_span(
            "lexai.search", attributes={"lexai.question": self._text(question)}
        ) as root:
            ctx = trace.set_span_in_context(root)
            history = await self.sessions.history(session_id) if session_id else []
            latency: dict[str, Any] = {}
            plan = await self._plan(question, history, latency, ctx)
            result: dict[str, Any] = {"plan": plan.summary(), "reply": plan.reply}
            if plan.reply is not None:
                return result | {"passages": [], "notes": [], "abstain": False}
            if self.router is None or self.store is None or self.collection is None:
                raise IndexNotReady()
            retrieved = await self._retrieve(plan, latency, ctx, filters)
            exact_parents = {p.chunk.metadata.parent_id for p in retrieved.pinned}
            with self._stage("context", ctx, latency):
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
                "latency": latency,
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
        await self.sessions.append_turn(session_id, plan.question, text)
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
        await self.sessions.append_turn(session_id, question, hit["answer"])
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
            prompt_version=self.settings.llm.prompt_version,
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
            prompt_version=self.settings.llm.prompt_version,
            index_version=self.index_version,
            usage=usage,
            cached=cached,
            abstained=abstained,
            notes=notes or [],
            verification=verification.payload() if verification else None,
        )
        return StreamEvent(event="done", data=payload.model_dump())
