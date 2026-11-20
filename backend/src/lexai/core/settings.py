"""
Centralised, validated configuration.

Settings are split into small groups, each reading its own `LEXAI_<GROUP>_*`
environment variables (and `backend/.env` if present). Nothing else in the
codebase reads `os.environ` directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_ROOT.parent
ENV_FILE = BACKEND_ROOT / ".env"


def _config(prefix: str) -> SettingsConfigDict:
    return SettingsConfigDict(
        env_prefix=prefix,
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class LLMSettings(BaseSettings):
    """Chat model served over an OpenAI-compatible API (Ollama in dev, vLLM in prod)."""

    model_config = _config("LEXAI_LLM_")

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen2.5-7b-instruct"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=0.8, gt=0.0, le=1.0)
    max_tokens: int = Field(default=1024, gt=0)
    connect_timeout_s: float = Field(default=5.0, gt=0)
    # Max wait for the next byte. Generous because a 7B model on CPU can take a
    # while to process a long prompt before the first token appears.
    read_timeout_s: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=5)


class EmbedSettings(BaseSettings):
    """Embedding model served over an OpenAI-compatible `/embeddings` endpoint."""

    model_config = _config("LEXAI_EMBED_")

    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen3-embedding:0.6b"
    dimension: int = Field(default=1024, gt=0)
    batch_size: int = Field(default=32, gt=0, le=256)
    # Text put in front of queries and documents before embedding. Each model
    # family expects its own:
    #   qwen3-embedding:  query "Instruct: <task>\nQuery: ", document ""
    #   nomic-embed-text: query "search_query: ",           document "search_document: "
    #   bge-m3:           both ""
    # The document prefix is recorded in the index; changing it requires re-ingestion.
    query_prefix: str = (
        "Instruct: Given a question about Indian law, retrieve statute sections and judgment "
        "passages that answer it\nQuery: "
    )
    document_prefix: str = ""
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=6)


class RerankSettings(BaseSettings):
    model_config = _config("LEXAI_RERANK_")

    enabled: bool = True
    model: str = "BAAI/bge-reranker-v2-m3"
    top_n: int = Field(default=8, gt=0, description="Chunks passed to the LLM as context.")


def _resolve_path(v: str | Path, base: Path) -> Path:
    path = Path(v).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


class CorpusSettings(BaseSettings):
    """Where the corpus is described, downloaded and bundled, and how it is chunked."""

    model_config = _config("LEXAI_CORPUS_")

    manifest: Path = REPO_ROOT / "data" / "manifest.yaml"
    bundled_dir: Path = REPO_ROOT / "data" / "bundled"
    raw_dir: Path = REPO_ROOT / "data" / "raw"
    lock_file: Path = REPO_ROOT / "data" / "manifest.lock.json"
    report_dir: Path = BACKEND_ROOT / "var" / "reports"
    # Sections longer than this are split at paragraph boundaries; each piece repeats the
    # section header. Part of the chunker config: changing it re-chunks on the next build.
    max_chunk_chars: int = Field(default=2400, ge=600)
    chunk_overlap_chars: int = Field(default=200, ge=0)
    download_timeout_s: float = Field(default=120.0, gt=0)
    # Government sites often answer 502/503/504 under load: retry those, timeouts and
    # connection errors with backoff (base, base*3, ...) before moving to the next URL.
    download_attempts: int = Field(default=3, ge=1, le=10)
    download_backoff_s: float = Field(default=10.0, ge=0)
    verify_tls: bool = True

    @field_validator("manifest", "bundled_dir", "raw_dir", "lock_file", "report_dir", mode="before")
    @classmethod
    def _resolve(cls, v: str | Path) -> Path:
        return _resolve_path(v, REPO_ROOT)


class QdrantSettings(BaseSettings):
    """
    Vector database. Set `url` for a Qdrant server; otherwise Qdrant runs embedded
    in the process, storing data under `path` (no Docker needed; one process at a time).
    """

    model_config = _config("LEXAI_QDRANT_")

    url: str | None = None
    api_key: str | None = None
    path: Path = BACKEND_ROOT / "var" / "qdrant"
    collection_alias: str = "lexai_chunks"
    keep_previous: int = Field(default=1, ge=0, description="Older collections kept for rollback.")

    @field_validator("path", mode="before")
    @classmethod
    def _resolve(cls, v: str | Path) -> Path:
        return _resolve_path(v, BACKEND_ROOT)


class RetrievalSettings(BaseSettings):
    model_config = _config("LEXAI_RETRIEVAL_")

    k_dense: int = Field(default=40, gt=0)
    k_sparse: int = Field(default=40, gt=0)
    fused_top_n: int = Field(default=30, gt=0, description="Fused candidates sent to the reranker.")
    context_char_budget: int = Field(
        default=12_000,
        ge=1000,
        description="Max characters of passages put in the prompt (keeps small context windows safe).",
    )
    hyde_enabled: bool = Field(
        default=False,
        description="Experimental. Adds one full LLM call per query; off until Phase 4 shows a gain.",
    )
    mode: Literal["hybrid", "dense", "bm25"] = Field(
        default="hybrid", description="Search signals fused with RRF (dense / bm25 only for ablations)."
    )


class QuerySettings(BaseSettings):
    """Query understanding, abstention and answer checking (Phase 3)."""

    model_config = _config("LEXAI_QUERY_")

    # Rewrite follow-ups into standalone questions with one short model call.
    condense_enabled: bool = True
    condense_turns: int = Field(default=3, ge=1, le=10)
    condense_max_tokens: int = Field(default=96, gt=0)
    max_references: int = Field(default=4, ge=1, le=10, description="Exact lookups per question.")
    # Exact lookups, old/new counterparts and code preference (Phase 3). Off = plain
    # hybrid search, the Phase 2 behaviour (kept for the evaluation's ablation).
    routing_enabled: bool = True
    # Abstain (answer "not found", no generation) when the best passage scores below the
    # threshold. With a reranker the rerank score is used, otherwise the dense cosine
    # similarity. Unset = never abstain on score; calibrated per embedder/reranker.
    abstain_min_dense: float | None = Field(default=None, ge=-1.0, le=1.0)
    abstain_min_rerank: float | None = None
    abstain_show_sources: int = Field(default=3, ge=0, le=10)


class CacheSettings(BaseSettings):
    """Answer and query-embedding cache. In-process by default; Redis when `redis_url` is set."""

    model_config = _config("LEXAI_CACHE_")

    enabled: bool = True
    ttl_s: int = Field(default=86_400, ge=0)
    max_entries: int = Field(default=512, ge=0, description="In-process cache size.")
    redis_url: str | None = None


class APISettings(BaseSettings):
    model_config = _config("LEXAI_API_")

    environment: Literal["local", "ci", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    rate_limit_query: str = "20/minute"
    rate_limit_session: str = "10/minute"
    session_ttl_minutes: int = Field(default=30, gt=0)
    session_max_messages: int = Field(default=20, gt=0)
    max_sessions: int = Field(default=10_000, gt=0)
    history_messages_in_prompt: int = Field(default=6, ge=0)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


class Settings:
    """All settings groups, loaded once."""

    def __init__(
        self,
        llm: LLMSettings | None = None,
        embed: EmbedSettings | None = None,
        rerank: RerankSettings | None = None,
        retrieval: RetrievalSettings | None = None,
        api: APISettings | None = None,
        corpus: CorpusSettings | None = None,
        qdrant: QdrantSettings | None = None,
        query: QuerySettings | None = None,
        cache: CacheSettings | None = None,
    ) -> None:
        self.llm = llm or LLMSettings()
        self.embed = embed or EmbedSettings()
        self.rerank = rerank or RerankSettings()
        self.retrieval = retrieval or RetrievalSettings()
        self.api = api or APISettings()
        self.corpus = corpus or CorpusSettings()
        self.qdrant = qdrant or QdrantSettings()
        self.query = query or QuerySettings()
        self.cache = cache or CacheSettings()


def env_profile(path: Path = ENV_FILE) -> str:
    """Name of the profile in backend/.env (`# lexai-profile: <name>`), for logs and diagnostics."""
    if not path.exists():
        return "none (no backend/.env; built-in defaults)"
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().lower().startswith("# lexai-profile:"):
            return line.split(":", 1)[1].strip()
    return "unnamed (backend/.env has no lexai-profile line)"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
