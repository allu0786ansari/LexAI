"""
Chat orchestration: (optional HyDE) -> hybrid retrieval -> rerank -> streamed generation.

Emits `StreamEvent`s in this order: `citations`, then `token`*, then `done`.
Any failure ends the stream with a single `error` event carrying a stable
code and the request id; details go to the logs only.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, AsyncIterator

from lexai.clients.llm import LLMClient
from lexai.clients.reranker import Reranker
from lexai.core.errors import IndexNotReady, LexAIError, RetrievalError
from lexai.core.logging import get_logger
from lexai.core.settings import Settings
from lexai.domain.models import (
    ChatMessage,
    Citation,
    DonePayload,
    ErrorPayload,
    LatencyBreakdown,
    ScoredChunk,
    StreamEvent,
)
from lexai.generation.prompts import HYDE_PROMPT, PROMPT_VERSION, SYSTEM_PROMPT, format_context
from lexai.retrieval.hybrid import HybridRetriever
from lexai.services.sessions import SessionStore

logger = get_logger(__name__)

NO_RESULTS_ANSWER = (
    "I couldn't find anything relevant to that question in the indexed sources. "
    "Try rephrasing, or ask about a specific statute, section or case."
)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def build_citations(chunks: list[ScoredChunk]) -> list[Citation]:
    return [
        Citation(
            ref=n,
            chunk_id=item.chunk.chunk_id,
            source_file=item.chunk.metadata.source_file,
            page=item.chunk.metadata.page,
            law_type=item.chunk.metadata.law_type,
            sections=item.chunk.metadata.sections,
            relevance_score=round(item.score, 4),
        )
        for n, item in enumerate(chunks, start=1)
    ]


class ChatService:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        retriever: HybridRetriever | None,
        reranker: Reranker | None,
        sessions: SessionStore,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.retriever = retriever
        self.reranker = reranker
        self.sessions = sessions

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

    async def _stream(self, question: str, session_id: str, request_id: str) -> AsyncIterator[StreamEvent]:
        if self.retriever is None:
            raise IndexNotReady()
        t_start = time.perf_counter()
        history = self.sessions.history(session_id)

        hyde_ms = None
        extra: list[str] = []
        if self.settings.retrieval.hyde_enabled:
            t0 = time.perf_counter()
            hyde_text = await self._hyde(question)
            hyde_ms = _ms(t0)
            if hyde_text:
                extra.append(hyde_text)

        t0 = time.perf_counter()
        try:
            candidates = await self.retriever.retrieve(question, extra)
        except LexAIError:
            raise
        except Exception as exc:
            raise RetrievalError(repr(exc)) from exc
        retrieval_ms = _ms(t0)

        if not candidates:
            yield StreamEvent(event="citations", data=[])
            yield StreamEvent(event="token", data={"text": NO_RESULTS_ANSWER})
            self.sessions.append_turn(session_id, question, NO_RESULTS_ANSWER)
            yield self._done(
                NO_RESULTS_ANSWER, session_id, request_id, [], hyde_ms, retrieval_ms, None, None, t_start
            )
            return

        rerank_ms = None
        top_n = self.settings.rerank.top_n
        if self.reranker is not None:
            t0 = time.perf_counter()
            try:
                ranked = await self.reranker.rerank(question, [c.chunk.text for c in candidates], top_n)
            except Exception as exc:
                raise RetrievalError(f"rerank failed: {exc!r}") from exc
            context = [
                ScoredChunk(
                    chunk=candidates[i].chunk,
                    score=score,
                    matched_dense=candidates[i].matched_dense,
                    matched_sparse=candidates[i].matched_sparse,
                )
                for i, score in ranked
            ]
            rerank_ms = _ms(t0)
        else:
            context = candidates[:top_n]

        citations = build_citations(context)
        yield StreamEvent(event="citations", data=[c.model_dump() for c in citations])

        keep = self.settings.api.history_messages_in_prompt
        messages = [ChatMessage(role="system", content=SYSTEM_PROMPT.format(context=format_context(context)))]
        messages += history[-keep:] if keep else []
        messages.append(ChatMessage(role="user", content=question))

        parts: list[str] = []
        ttft_ms: float | None = None
        async for delta in self.llm.stream_chat(messages):
            if ttft_ms is None:
                ttft_ms = _ms(t_start)
            parts.append(delta)
            yield StreamEvent(event="token", data={"text": delta})

        answer = "".join(parts)
        self.sessions.append_turn(session_id, question, answer)
        yield self._done(
            answer, session_id, request_id, citations, hyde_ms, retrieval_ms, rerank_ms, ttft_ms, t_start
        )

    def _done(
        self,
        answer: str,
        session_id: str,
        request_id: str,
        citations: list[Citation],
        hyde_ms: float | None,
        retrieval_ms: float,
        rerank_ms: float | None,
        ttft_ms: float | None,
        t_start: float,
    ) -> StreamEvent:
        latency = LatencyBreakdown(
            hyde_ms=hyde_ms,
            retrieval_ms=retrieval_ms,
            rerank_ms=rerank_ms,
            time_to_first_token_ms=ttft_ms,
            total_ms=_ms(t_start),
        )
        logger.info(
            "query_complete",
            prompt_version=PROMPT_VERSION,
            context_chunks=len(citations),
            answer_chars=len(answer),
            **latency.model_dump(),
        )
        payload = DonePayload(
            answer=answer, session_id=session_id, request_id=request_id, citations=citations, latency=latency
        )
        return StreamEvent(event="done", data=payload.model_dump())
