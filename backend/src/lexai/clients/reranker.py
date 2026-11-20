"""
Cross-encoder reranking.

Phase 1 runs the model in-process, but always in a worker thread so the event
loop never blocks on it. Phase 6 moves it to its own inference container
behind the same `Reranker` protocol.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from lexai.core.errors import ConfigurationError
from lexai.core.logging import get_logger
from lexai.core.settings import RerankSettings

logger = get_logger(__name__)


class Reranker(Protocol):
    name: str

    async def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        """Return (index into `texts`, score) pairs, best first, at most `top_n`."""
        ...


class CrossEncoderReranker:
    def __init__(self, model: Any, name: str) -> None:
        self._model = model
        self.name = name

    @classmethod
    async def load(cls, settings: RerankSettings) -> CrossEncoderReranker:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ConfigurationError(
                "Reranking is enabled but sentence-transformers is not installed. "
                "Run `uv sync --extra rerank`, or set LEXAI_RERANK_ENABLED=false."
            ) from exc
        logger.info("reranker_loading", model=settings.model)
        model = await asyncio.to_thread(CrossEncoder, settings.model)
        logger.info("reranker_ready", model=settings.model)
        return cls(model, settings.model)

    async def rerank(self, query: str, texts: Sequence[str], top_n: int) -> list[tuple[int, float]]:
        if not texts:
            return []

        def _run() -> list[tuple[int, float]]:
            ranked = self._model.rank(query, list(texts), top_k=min(top_n, len(texts)))
            return [(int(r["corpus_id"]), float(r["score"])) for r in ranked]

        return await asyncio.to_thread(_run)
