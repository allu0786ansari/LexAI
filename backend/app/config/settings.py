"""
Centralised application configuration.

All configuration for the ingestion pipeline AND the FastAPI service is
defined here as a single `Settings` object, loaded once and imported
everywhere else. Values are sourced from environment variables / a `.env`
file, with sane defaults for local development.

Do not read `os.environ` directly anywhere else in the codebase — add a
field here instead.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Root of the `backend/` directory (parent of `app/`).
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # LLM / embedding provider
    # ------------------------------------------------------------------
    # NOTE: gemini-1.5-pro and text-embedding-004/embedding-001 are
    # retired (404 as of 2026). These are placeholders for models known
    # to be live at the time this project was built — swap freely via
    # env vars, nothing below should ever be hardcoded elsewhere.
    google_api_key: str = Field(default="", description="Google Generative AI API key")
    llm_provider: str = Field(default="ollama", description="Provider for chat generation: google or ollama")
    embedding_provider: str = Field(default="ollama", description="Provider for embeddings: google or ollama")
    ollama_base_url: str = Field(default="http://localhost:11434", description="Base URL for local Ollama service")
    llm_model: str = Field(
        default="qwen2.5:3b-instruct",
        description="Chat model id passed to the active chat provider.",
    )
    llm_temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=1024, gt=0)
    llm_request_timeout_seconds: float = Field(default=30.0, gt=0)

    embedding_model: str = Field(
        default="nomic-embed-text",
        description="Embedding model id passed to the active embedding provider.",
    )
    embedding_output_dimensionality: int | None = Field(
        default=768,
        description="Matryoshka output dimension for the embedding model. None uses the model default.",
    )

    # ------------------------------------------------------------------
    # Filesystem layout
    # ------------------------------------------------------------------
    data_dir: Path = Field(default=BACKEND_ROOT / "Data")
    database_dir: Path = Field(default=BACKEND_ROOT / "Database")

    # ------------------------------------------------------------------
    # Chunking (semantic chunker) — Phase 1
    # ------------------------------------------------------------------
    chunk_buffer_size: int = Field(default=2, ge=0)
    chunk_breakpoint_percentile: float = Field(default=90.0, gt=0.0, lt=100.0)
    chunk_min_chars: int = Field(default=400, gt=0)
    chunk_max_chars: int = Field(default=1800, gt=0)
    chunk_hard_overlap: int = Field(default=220, ge=0)

    # ------------------------------------------------------------------
    # Embedding batching / retry — Phase 1
    # ------------------------------------------------------------------
    embedding_batch_size: int = Field(default=50, gt=0, le=250)
    embedding_max_retries: int = Field(default=6, ge=1)
    embedding_retry_min_seconds: float = Field(default=2.0, gt=0)
    embedding_retry_max_seconds: float = Field(default=60.0, gt=0)
    embedding_quota_wait_seconds: int = Field(default=60, ge=1)
    embedding_task_type: str = Field(default="semantic_similarity")

    # ------------------------------------------------------------------
    # Retrieval — Phase 2
    # ------------------------------------------------------------------
    retrieval_k_dense: int = Field(default=40, gt=0, description="Candidates pulled per dense (FAISS) query.")
    retrieval_k_sparse: int = Field(default=40, gt=0, description="Candidates pulled from BM25.")
    retrieval_rrf_k: int = Field(default=50, gt=0, description="The k constant in RRF(d) = sum(1/(k+rank(d))).")
    retrieval_fused_top_n: int = Field(
        default=30, gt=0, description="How many fused RRF candidates are passed into the reranker."
    )

    # ------------------------------------------------------------------
    # HyDE — Phase 2
    # ------------------------------------------------------------------
    hyde_enabled: bool = Field(default=True)
    hyde_max_output_tokens: int = Field(default=256, gt=0)

    # ------------------------------------------------------------------
    # Reranking — Phase 2
    # ------------------------------------------------------------------
    reranker_enabled: bool = Field(default=True)
    reranker_model: str = Field(default="BAAI/bge-reranker-v2-m3")
    rerank_top_n: int = Field(default=8, gt=0, description="Final number of chunks used as generation context.")

    # ------------------------------------------------------------------
    # Session memory — Phase 2
    # ------------------------------------------------------------------
    session_ttl_minutes: int = Field(default=30, gt=0)

    # ------------------------------------------------------------------
    # API — Phase 2
    # ------------------------------------------------------------------
    cors_allowed_origins_raw: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000,http://localhost:8001,http://127.0.0.1:8001",
        alias="CORS_ALLOWED_ORIGINS",
        description="Comma-separated list of allowed CORS origins.",
    )
    rate_limit_query: str = Field(default="20/minute", description="slowapi rate limit string for POST /api/query.")
    rate_limit_session: str = Field(default="10/minute", description="slowapi rate limit string for POST /api/session.")

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    environment: Literal["local", "ci", "production"] = Field(default="local")

    @field_validator("data_dir", "database_dir", mode="before")
    @classmethod
    def _resolve_path(cls, v: str | Path) -> Path:
        return Path(v).expanduser().resolve()

    @property
    def cors_allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins_raw.split(",") if origin.strip()]

    def require_google_api_key(self) -> str:
        """Fail loudly and early instead of letting a 401 surface deep in a request."""
        if not self.google_api_key:
            raise RuntimeError(
                "GOOGLE_API_KEY is not set. Add it to backend/.env "
                "(see .env.example) before running ingestion or starting the API."
            )
        return self.google_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton — import and call this, don't instantiate Settings() directly."""
    return Settings()
