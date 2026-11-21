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


class ChatFilters(BaseModel):
    """Optional restrictions on what is searched. Provisions the question names are still looked up."""

    acts: list[str] = Field(
        default_factory=list, max_length=30, description='Short names, e.g. ["BNS", "BNSS"]; empty = all.'
    )
    in_force_only: bool = Field(default=False, description="Exclude repealed codes (IPC, CrPC, IEA).")


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(
        default=None, max_length=64, description="From POST /api/v1/sessions; omitted = a new session."
    )
    filters: ChatFilters | None = None


class SearchRequest(ChatRequest):
    pass


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
    doc_type: str = Field(description="statute, constitution, judgment or manual.")
    sections: list[str] = Field(default_factory=list)
    relevance_score: float
    snippet: str = Field(default="", description="First ~300 characters of the passage.")
    exact: bool = Field(default=False, description="Looked up because the question named it.")


class LatencyBreakdown(BaseModel):
    condense_ms: float | None = None
    hyde_ms: float | None = None
    retrieval_ms: float | None = None  # search, exact lookups and (if enabled) reranking
    context_ms: float | None = None
    time_to_first_token_ms: float | None = None
    generation_ms: float | None = None
    verify_ms: float | None = None
    total_ms: float


class MetaPayload(BaseModel):
    request_id: str
    session_id: str
    trace_id: str = Field(default="", description="OpenTelemetry trace id (empty when tracing is off).")
    standalone_query: str
    intent: str
    references: list[str]
    applicable_code: str
    applicable_code_reason: str


class DonePayload(BaseModel):
    answer: str = Field(description="Final answer; invalid [n] markers removed.")
    session_id: str
    request_id: str
    citations: list[Citation]
    latency: LatencyBreakdown
    prompt_version: str = ""
    index_version: str = ""
    usage: dict[str, int] | None = None
    cached: bool = False
    abstained: bool = False
    notes: list[str] = Field(default_factory=list, description="Correspondences and lookup notes.")
    verification: dict[str, Any] | None = None


class SessionResponse(BaseModel):
    session_id: str
    expires_in_s: int = Field(description="Idle time after which the session expires.")


class VerificationFlag(BaseModel):
    kind: str = Field(description="invalid_citation, unsupported_provision or no_citations.")
    detail: str


class VerificationPayload(BaseModel):
    valid: bool
    coverage: float | None = Field(description="Share of claim sentences with a citation.")
    flags: list[VerificationFlag] = Field(default_factory=list)
    removed_markers: list[int] = Field(default_factory=list)


class TokenPayload(BaseModel):
    text: str


class ChatEvents(BaseModel):
    """
    Payload of each Server-Sent Event of POST /api/v1/chat (documentation and client types;
    the stream itself is `event: <name>` + `data: <json>`). Order: meta, citations,
    token*, verification, done; or error.
    """

    meta: MetaPayload
    citations: list[Citation]
    token: TokenPayload
    verification: VerificationPayload
    done: DonePayload
    error: ErrorPayload


class ChunkResponse(BaseModel):
    chunk_id: str
    label: str
    doc_id: str
    doc_title: str
    doc_type: str
    unit_label: str
    unit: str | None = None
    unit_title: str | None = None
    status: str
    text: str = Field(description="The whole provision (all its pieces) or, for judgments, this passage.")
    page_start: int | None = None
    page_end: int | None = None
    source_file: str
    pdf_url: str | None = Field(
        description="Opens the source PDF at the passage's page; None if unavailable."
    )


class DocumentInfo(BaseModel):
    doc_id: str
    title: str
    short_name: str | None = None
    doc_type: str
    status: str
    repealed_on: str | None = None
    replaced_by: str | None = None
    chunks: int
    source_url: str | None = None
    has_pdf: bool


class FeedbackRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=64)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    id: int


class ReindexResponse(BaseModel):
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    detail: str = ""
    started_at: str | None = None
    finished_at: str | None = None


class ErrorPayload(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """Every non-2xx JSON response."""

    code: str
    message: str
    request_id: str


class StreamEvent(BaseModel):
    """
    One Server-Sent Event (contract v1):
    `meta` -> `citations` -> `token`* -> `verification` -> `done`, or `error`.
    """

    event: Literal["meta", "citations", "token", "verification", "done", "error"]
    data: Any
