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
    # Qwen3-Embedding is instruction-aware: queries get a task instruction,
    # documents are embedded as-is. Set to "" for models without instructions.
    query_instruction: str = (
        "Given a question about Indian law, retrieve statute sections and judgment passages that answer it"
    )
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=6)


class RerankSettings(BaseSettings):
    model_config = _config("LEXAI_RERANK_")

    enabled: bool = True
    model: str = "BAAI/bge-reranker-v2-m3"
    top_n: int = Field(default=8, gt=0, description="Chunks passed to the LLM as context.")


class RetrievalSettings(BaseSettings):
    model_config = _config("LEXAI_RETRIEVAL_")

    index_dir: Path = BACKEND_ROOT / "var" / "index"
    data_dir: Path = BACKEND_ROOT / "Data"
    k_dense: int = Field(default=40, gt=0)
    k_sparse: int = Field(default=40, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    fused_top_n: int = Field(default=30, gt=0, description="Fused candidates sent to the reranker.")
    hyde_enabled: bool = Field(
        default=False,
        description="Experimental. Adds one full LLM call per query; off until Phase 4 shows a gain.",
    )
    chunk_size_chars: int = Field(default=1500, gt=100)
    chunk_overlap_chars: int = Field(default=200, ge=0)

    @field_validator("index_dir", "data_dir", mode="before")
    @classmethod
    def _resolve(cls, v: str | Path) -> Path:
        path = Path(v).expanduser()
        return path if path.is_absolute() else (BACKEND_ROOT / path).resolve()


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
    ) -> None:
        self.llm = llm or LLMSettings()
        self.embed = embed or EmbedSettings()
        self.rerank = rerank or RerankSettings()
        self.retrieval = retrieval or RetrievalSettings()
        self.api = api or APISettings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
