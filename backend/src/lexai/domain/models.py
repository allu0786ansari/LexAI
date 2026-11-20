"""Domain models shared by ingestion, retrieval, generation and the API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DocType = Literal["statute", "constitution", "amendment", "judgment", "manual"]
Role = Literal["system", "user", "assistant"]


class ChunkMetadata(BaseModel):
    """Everything needed to cite, filter and explain a passage. Stored as the Qdrant payload."""

    doc_id: str
    doc_title: str
    short_name: str
    doc_type: DocType
    source_file: str
    unit_label: str = Field(description='"Section", "Article", "Schedule", "Preamble", "Judgment", ...')
    unit: str | None = Field(default=None, description='e.g. "302", "21A"; None for unnumbered text')
    unit_title: str | None = None
    hierarchy: str | None = Field(
        default=None, description='e.g. "Chapter XVI: Of offences affecting the human body"'
    )
    parent_id: str = Field(description="Shared by all pieces of one section, for re-assembly.")
    part: int = 1
    parts: int = 1
    page_start: int | None = None
    page_end: int | None = None
    year: int | None = None
    in_force_from: str | None = None
    repealed_on: str | None = None
    replaced_by: str | None = None
    case_name: str | None = None
    decision_date: str | None = None
    citation: str | None = None
    sections: list[str] = Field(
        default_factory=list, description="Sections/Articles referred to in the text."
    )
    doc_sha256: str = ""
    chunker_version: str = ""

    @property
    def label(self) -> str:
        """Short human reference, e.g. "IPC s.302" or "Constitution Art.21"."""
        if self.unit is None:
            return self.case_name or self.short_name
        abbrev = {"Section": "s.", "Article": "Art."}.get(self.unit_label, f"{self.unit_label} ")
        return f"{self.short_name} {abbrev}{self.unit}"


class Chunk(BaseModel):
    """One retrievable unit of text."""

    chunk_id: str = Field(description="UUID (Qdrant point id), stable for the same document and text.")
    text: str = Field(description="The passage as shown to the model and the user.")
    embed_text: str = Field(description="Header + text; what is embedded and indexed for keyword search.")
    metadata: ChunkMetadata


class ScoredChunk(BaseModel):
    chunk: Chunk
    score: float


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
    label: str = Field(description='Short reference, e.g. "IPC s.302".')
    doc_id: str
    doc_title: str
    unit_label: str
    unit: str | None = None
    unit_title: str | None = None
    status: str = Field(
        description=(
            '"in force" or "repealed 2024-07-01; replaced by BNS; applies to earlier offences" for'
            " legislation; empty for judgments and manuals"
        )
    )
    source_file: str
    page: int | None = None
    law_type: str = Field(description="Document type (kept for the v0.1 frontend).")
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
