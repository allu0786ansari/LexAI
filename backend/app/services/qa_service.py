"""
Core RAG orchestration: HyDE -> hybrid retrieval -> rerank -> generation.

This replaces the old `ConversationalRetrievalChain` (deprecated, and its
`[INST]`-tagged prompt template was written for a Llama model, not
Gemini) with an explicit async pipeline built from LangChain's
actively-maintained pieces (langchain-core's `ChatPromptTemplate`,
`MessagesPlaceholder`, and `ChatGoogleGenerativeAI.astream`), composed as
plain Python rather than forced through a generic chain abstraction —
every stage here (HyDE, retrieval, rerank) is a custom component with its
own error-handling and timing needs, so composing them explicitly is more
readable than hiding that behind `RunnableLambda | RunnableLambda | ...`
without gaining anything. This is the "LCEL chain" from the proposal in
spirit: LangChain's runtime primitives (streaming `.astream`, chat
history via `InMemoryChatMessageHistory`, prompt templates), not the
literal `|`-pipe operator syntax for its own sake.

SSE event contract (see app/api/routes.py for the wire format):
  {"event": "citations", "data": [Citation, ...]}   - sent once, before generation
  {"event": "token",     "data": {"text": "..."}}    - sent per streamed token
  {"event": "done",      "data": QueryResponse}       - sent once, at the end
  {"event": "error",     "data": {"message": "..."}}  - sent if generation fails mid-stream
"""
from __future__ import annotations

import time
from collections.abc import AsyncIterator
from functools import lru_cache

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config.logging import get_logger
from app.config.settings import Settings, get_settings
from app.services.memory import SessionStore
from app.services.reranker import CrossEncoderReranker, RerankedDocument
from app.services.retriever import FusedDocument, HybridRetriever

logger = get_logger(__name__)

HYDE_PROMPT = """You are a legal research assistant. Write a short hypothetical passage \
(3-5 sentences) that would be a strong, ideal answer to the legal question below, as it might \
appear in an Indian statute, judgment, or legal commentary. Do not hedge, do not mention that \
this is hypothetical - just write the passage itself, in a neutral legal-drafting register.

Question: {question}

Hypothetical passage:"""

SYSTEM_PROMPT = """You are LexAI, a legal research assistant specialised in Indian law \
(the Constitution, IPC, CrPC, IT Act, and related statutes, judgments, and reports).

Answer the user's question using ONLY the retrieved context below. Follow these rules strictly:
- If the context does not contain enough information to answer confidently, say so plainly \
instead of guessing or relying on outside knowledge.
- Cite the source inline where relevant, e.g. "(IPC 1860.pdf, p.45)".
- Be precise and concise. This is a professional legal-research tool, not a casual chat.
- Do not fabricate section numbers, case names, or citations that are not in the context.

Retrieved context:
{context}"""


def _format_context(documents) -> str:
    blocks = []
    for i, item in enumerate(documents, start=1):
        doc = item.document
        meta = doc.metadata
        header = f"[{i}] {meta.get('source_file', 'unknown')}"
        if meta.get("page") is not None:
            header += f", p.{meta['page']}"
        if meta.get("law_type"):
            header += f" ({meta['law_type']})"
        blocks.append(f"{header}\n{doc.page_content}")
    return "\n\n".join(blocks)


def _build_citations(documents) -> list[dict]:
    citations = []
    for item in documents:
        doc = item.document
        score = getattr(item, "rerank_score", None)
        if score is None:
            score = getattr(item, "rrf_score", 0.0)
        citations.append(
            {
                "source_file": doc.metadata.get("source_file", "unknown"),
                "page": doc.metadata.get("page"),
                "law_type": doc.metadata.get("law_type", "other"),
                "sections": doc.metadata.get("sections", []),
                "relevance_score": round(float(score), 4),
            }
        )
    return citations


class LegalQAService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.retriever = HybridRetriever(settings)
        self.reranker = CrossEncoderReranker(settings) if settings.reranker_enabled else None
        self.sessions = SessionStore(ttl_minutes=settings.session_ttl_minutes)

        self._llm = ChatGoogleGenerativeAI(
            model=settings.llm_model,
            google_api_key=settings.require_google_api_key(),
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout=settings.llm_request_timeout_seconds,
        )
        self._hyde_llm = ChatGoogleGenerativeAI(
            model=settings.llm_model,
            google_api_key=settings.require_google_api_key(),
            temperature=0.5,
            max_output_tokens=settings.hyde_max_output_tokens,
            timeout=settings.llm_request_timeout_seconds,
        )
        logger.info(
            "qa_service_ready",
            llm_model=settings.llm_model,
            reranker_enabled=settings.reranker_enabled,
            hyde_enabled=settings.hyde_enabled,
        )

    async def _generate_hyde_document(self, question: str) -> str | None:
        """
        Best-effort HyDE step. Failure here (rate limit, transient API
        error) degrades gracefully to plain-query retrieval rather than
        failing the whole request - a slightly worse retrieval signal
        beats no answer at all.
        """
        try:
            response = await self._hyde_llm.ainvoke(HYDE_PROMPT.format(question=question))
            text = response.content if isinstance(response.content, str) else str(response.content)
            return text.strip() or None
        except Exception as exc:
            logger.warning("hyde_generation_failed", error=str(exc))
            return None

    def health_snapshot(self) -> dict:
        retriever_health = self.retriever.health_snapshot()
        return {
            "status": "ok",
            "faiss_documents": retriever_health["faiss_documents"],
            "bm25_documents": retriever_health["bm25_documents"],
            "embedding_model": self.settings.embedding_model,
            "llm_model": self.settings.llm_model,
            "reranker_enabled": self.settings.reranker_enabled,
            "hyde_enabled": self.settings.hyde_enabled,
            "active_sessions": self.sessions.active_session_count,
        }

    async def stream_answer(self, question: str, session_id: str) -> AsyncIterator[dict]:
        t_start = time.perf_counter()
        history = self.sessions.get_history(session_id)

        # --- HyDE ---
        hyde_text = None
        hyde_ms = None
        if self.settings.hyde_enabled:
            t0 = time.perf_counter()
            hyde_text = await self._generate_hyde_document(question)
            hyde_ms = (time.perf_counter() - t0) * 1000

        # --- Hybrid retrieval ---
        t0 = time.perf_counter()
        try:
            fused, trace = self.retriever.retrieve(
                query=question,
                extra_query_texts=[hyde_text] if hyde_text else None,
            )
        except Exception as exc:
            logger.error("retrieval_failed", session_id=session_id, error=str(exc), exc_info=True)
            yield {"event": "error", "data": {"message": "Retrieval failed. Please try again."}}
            return
        retrieval_ms = (time.perf_counter() - t0) * 1000

        if not fused:
            yield {"event": "citations", "data": []}
            answer = (
                "I couldn't find anything relevant to that question in the knowledge base. "
                "Try rephrasing, or ask about a specific statute, section, or case."
            )
            for word in answer.split(" "):
                yield {"event": "token", "data": {"text": word + " "}}
            history.add_user_message(question)
            history.add_ai_message(answer)
            total_ms = (time.perf_counter() - t_start) * 1000
            yield {
                "event": "done",
                "data": {
                    "answer": answer,
                    "session_id": session_id,
                    "citations": [],
                    "latency": {
                        "hyde_ms": hyde_ms,
                        "retrieval_ms": retrieval_ms,
                        "rerank_ms": None,
                        "time_to_first_token_ms": total_ms,
                        "total_ms": total_ms,
                    },
                },
            }
            return

        # --- Rerank ---
        rerank_ms = None
        if self.reranker is not None:
            t0 = time.perf_counter()
            try:
                context_documents = self.reranker.rerank(
                    question, [f.document for f in fused], top_n=self.settings.rerank_top_n
                )
            except Exception as exc:
                logger.warning("rerank_failed_falling_back_to_rrf_order", error=str(exc))
                context_documents = fused[: self.settings.rerank_top_n]
            rerank_ms = (time.perf_counter() - t0) * 1000
        else:
            context_documents = fused[: self.settings.rerank_top_n]

        citations = _build_citations(context_documents)
        yield {"event": "citations", "data": citations}

        # --- Generation ---
        context_text = _format_context(context_documents)
        messages = [SystemMessage(content=SYSTEM_PROMPT.format(context=context_text))]
        messages.extend(history.messages[-8:])  # last 4 turns of prior context, kept small deliberately
        messages.append(HumanMessage(content=question))

        answer_chunks: list[str] = []
        first_token_ms: float | None = None
        try:
            async for chunk in self._llm.astream(messages):
                text = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                if not text:
                    continue
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - t_start) * 1000
                answer_chunks.append(text)
                yield {"event": "token", "data": {"text": text}}
        except Exception as exc:
            logger.error("generation_failed", session_id=session_id, error=str(exc), exc_info=True)
            yield {"event": "error", "data": {"message": "Answer generation failed partway through. Please retry."}}
            return

        answer = "".join(answer_chunks)
        history.add_user_message(question)
        history.add_ai_message(answer)

        total_ms = (time.perf_counter() - t_start) * 1000
        latency = {
            "hyde_ms": round(hyde_ms, 1) if hyde_ms is not None else None,
            "retrieval_ms": round(retrieval_ms, 1),
            "rerank_ms": round(rerank_ms, 1) if rerank_ms is not None else None,
            "time_to_first_token_ms": round(first_token_ms, 1) if first_token_ms is not None else round(total_ms, 1),
            "total_ms": round(total_ms, 1),
        }
        logger.info(
            "query_complete",
            session_id=session_id,
            fused_candidates=trace.fused_candidates,
            context_chunks=len(context_documents),
            **latency,
        )
        yield {
            "event": "done",
            "data": {
                "answer": answer,
                "session_id": session_id,
                "citations": citations,
                "latency": latency,
            },
        }


@lru_cache(maxsize=1)
def get_qa_service() -> LegalQAService:
    """
    Process-wide singleton. Building this loads the FAISS index, the BM25
    index, and (if enabled) downloads/loads the CrossEncoder model - all
    real startup cost, which is why this is built once at app startup
    (see app/main.py) rather than per-request.
    """
    return LegalQAService(get_settings())
