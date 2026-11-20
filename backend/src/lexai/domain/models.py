"""Domain models shared by ingestion, retrieval, generation and the API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

LawType = Literal["statute", "case_law", "report", "manual", "other"]
Role = Literal["system", "user", "assistant"]


class ChunkMetadata(BaseModel):
    source_file: str
    law_type: LawType = "other"
    year: int | None = None
    page: int | None = None
    chunk_index: int
    sections: list[str] = Field(default_factory=list)


class Chunk(BaseModel):
    """One retrievable unit of text. Replaces LangChain's `Document`."""

    chunk_id: str
    text: str
    metadata: ChunkMetadata


class ScoredChunk(BaseModel):
    chunk: Chunk
    score: float
    matched_dense: bool = False
    matched_sparse: bool = False


class ChatMessage(BaseModel):
    role: Role
    content: str

    def as_openai(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


# ---------------------------------------------------------------------------
# API contract
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(default=None, max_length=64)


class Citation(BaseModel):
    ref: int = Field(description="The [n] marker used for this passage in the answer.")
    chunk_id: str
    source_file: str
    page: int | None = None
    law_type: str
    sections: list[str] = Field(default_factory=list)
    relevance_score: float


class LatencyBreakdown(BaseModel):
    hyde_ms: float | None = None
    retrieval_ms: float
    rerank_ms: float | None = None
    time_to_first_token_ms: float | None = None
    total_ms: float


class DonePayload(BaseModel):
    answer: str
    session_id: str
    request_id: str
    citations: list[Citation]
    latency: LatencyBreakdown


class SessionResponse(BaseModel):
    session_id: str


class ErrorPayload(BaseModel):
    code: str
    message: str
    request_id: str


class StreamEvent(BaseModel):
    """One Server-Sent Event: `citations` -> `token`* -> `done`, or `error`."""

    event: Literal["citations", "token", "done", "error"]
    data: Any
