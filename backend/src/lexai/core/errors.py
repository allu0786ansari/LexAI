"""Typed errors. Each carries a stable `code` that is sent to clients; details stay in the logs."""

from __future__ import annotations


class LexAIError(Exception):
    code = "internal_error"
    public_message = "An unexpected error occurred."

    def __init__(self, message: str | None = None, *, service: str | None = None) -> None:
        super().__init__(message or self.public_message)
        self.service = service  # the upstream that failed (llm, embedder, qdrant), for metrics


class ConfigurationError(LexAIError):
    code = "configuration_error"
    public_message = "The service is misconfigured."


class UpstreamUnavailable(LexAIError):
    """A model server (LLM, embedder) could not be reached or kept failing."""

    code = "upstream_unavailable"
    public_message = "A model server is unavailable. Please try again shortly."


class IndexNotReady(LexAIError):
    code = "index_not_ready"
    public_message = "The search index has not been built yet. Run the ingestion job first."


class IndexMismatchError(ConfigurationError):
    code = "index_mismatch"
    public_message = "The search index was built with a different embedding model. Re-run ingestion."


class RetrievalError(LexAIError):
    code = "retrieval_failed"
    public_message = "Retrieval failed. Please try again."


class GenerationError(LexAIError):
    code = "generation_failed"
    public_message = "Answer generation failed. Please retry."


class SessionNotFound(LexAIError):
    """Only server-issued session ids are accepted; an unknown or expired id is refused."""

    code = "session_not_found"
    public_message = "Unknown or expired session. Create a new session and retry."


class Forbidden(LexAIError):
    code = "forbidden"
    public_message = "A valid API key is required."
