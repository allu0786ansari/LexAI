"""
Async client for an OpenAI-compatible `/embeddings` endpoint (Ollama `/v1` or
Text Embeddings Inference).

There is deliberately no fallback: if embeddings cannot be computed, the call
raises. The previous hash-based fallback produced vectors that looked valid but
were meaningless, which silently ruined retrieval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import httpx

from lexai.core.errors import ConfigurationError, UpstreamUnavailable
from lexai.core.logging import get_logger
from lexai.core.settings import EmbedSettings

logger = get_logger(__name__)


class EmbeddingClient:
    def __init__(self, settings: EmbedSettings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(settings.timeout_s, connect=5.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def format_query(self, query: str) -> str:
        if not self.settings.query_instruction:
            return query
        return f"Instruct: {self.settings.query_instruction}\nQuery: {query}"

    async def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        attempt = 0
        while True:
            try:
                response = await self._client.post(
                    "embeddings", json={"model": self.settings.model, "input": list(texts)}
                )
                response.raise_for_status()
                data = sorted(response.json()["data"], key=lambda d: d["index"])
                vectors = [list(map(float, d["embedding"])) for d in data]
                if len(vectors) != len(texts):
                    raise UpstreamUnavailable(
                        f"Embedder returned {len(vectors)} vectors for {len(texts)} inputs"
                    )
                return vectors
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TransportError) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in {429, 500, 502, 503}
                )
                if retryable and attempt < self.settings.max_retries:
                    delay = min(10.0, 1.0 * (2**attempt))
                    logger.warning("embed_retry", attempt=attempt + 1, delay_s=delay, error=repr(exc))
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise UpstreamUnavailable(
                    f"Embedding failed ({self.settings.model} at {self.settings.base_url}): {exc!r}"
                ) from exc
            except (KeyError, ValueError, TypeError) as exc:
                raise UpstreamUnavailable(f"Malformed embedding response: {exc!r}") from exc

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        size = self.settings.batch_size
        for start in range(0, len(texts), size):
            vectors.extend(await self._embed_batch(texts[start : start + size]))
        self._check_dimension(vectors)
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        vectors = await self._embed_batch([self.format_query(query)])
        self._check_dimension(vectors)
        return vectors[0]

    def _check_dimension(self, vectors: list[list[float]]) -> None:
        for v in vectors:
            if len(v) != self.settings.dimension:
                raise ConfigurationError(
                    f"Embedding model '{self.settings.model}' returned {len(v)}-dim vectors, "
                    f"but LEXAI_EMBED_DIMENSION is {self.settings.dimension}."
                )

    async def health(self) -> dict[str, Any]:
        """Embed a probe string; verifies reachability and the vector dimension."""
        vectors = await self._embed_batch(["health probe"])
        dim = len(vectors[0])
        return {
            "reachable": True,
            "model": self.settings.model,
            "dimension": dim,
            "dimension_ok": dim == self.settings.dimension,
        }
