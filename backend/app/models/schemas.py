"""
Pydantic v2 request/response models for the API layer.

Kept separate from the SSE event payloads emitted by qa_service (those are
plain dicts, documented in app/services/qa_service.py) — these models are
for the two plain JSON endpoints (`/api/session`, `/api/health`) and for
validating the incoming `/api/query` request body.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(
        default=None,
        description="UUID from a prior POST /api/session call. If omitted or unknown, a new session is created transparently.",
    )


class Citation(BaseModel):
    source_file: str
    page: int | None = None
    law_type: str
    sections: list[str] = Field(default_factory=list)
    relevance_score: float


class LatencyBreakdown(BaseModel):
    hyde_ms: float | None = None
    retrieval_ms: float
    rerank_ms: float | None = None
    time_to_first_token_ms: float
    total_ms: float


class QueryResponse(BaseModel):
    """Shape of the final SSE 'done' event's data payload — documented here
    for API consumers even though it's transmitted as an SSE frame, not a
    plain JSON response body."""

    answer: str
    session_id: str
    citations: list[Citation]
    latency: LatencyBreakdown


class SessionResponse(BaseModel):
    session_id: str


class HealthResponse(BaseModel):
    status: str
    faiss_documents: int
    bm25_documents: int
    embedding_model: str
    llm_model: str
    reranker_enabled: bool
    hyde_enabled: bool
    active_sessions: int


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
    request_id: str | None = None
