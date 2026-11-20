from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lexai.api.container import Container
from lexai.core.errors import UpstreamUnavailable
from lexai.core.settings import (
    APISettings,
    EmbedSettings,
    LLMSettings,
    RerankSettings,
    RetrievalSettings,
    Settings,
)
from lexai.domain.models import ChatMessage, Chunk, ChunkMetadata
from lexai.generation.service import ChatService
from lexai.retrieval.bm25 import tokenize
from lexai.retrieval.hybrid import HybridRetriever
from lexai.retrieval.index import LocalIndex
from lexai.services.sessions import SessionStore

DIM = 64
EMBED_MODEL = "fake-embedder"


class FakeEmbedder:
    """Deterministic bag-of-words vectors: texts sharing words get similar vectors."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def format_query(self, query: str) -> str:
        return query

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(DIM, dtype=np.float32)
        for tok in tokenize(text) or ["empty"]:
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
        return v.tolist()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise UpstreamUnavailable("embedder down")
        return [self._vec(t) for t in texts]

    async def embed_query(self, query: str) -> list[float]:
        return (await self.embed_documents([query]))[0]

    async def health(self) -> dict[str, Any]:
        if self.fail:
            raise UpstreamUnavailable("embedder down")
        return {"reachable": True, "model": EMBED_MODEL, "dimension": DIM, "dimension_ok": True}

    async def aclose(self) -> None:
        return None


class FakeLLM:
    def __init__(
        self,
        tokens: list[str] | None = None,
        delay_s: float = 0.0,
        error: Exception | None = None,
        healthy: bool = True,
    ) -> None:
        self.tokens = tokens or ["Murder ", "is ", "punishable ", "with ", "death [1]."]
        self.delay_s = delay_s
        self.error = error
        self.healthy = healthy
        self.calls: list[list[ChatMessage]] = []

    async def stream_chat(self, messages: Sequence[ChatMessage], **_: Any) -> AsyncIterator[str]:
        self.calls.append(list(messages))
        if self.error is not None:
            raise self.error
        for token in self.tokens:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield token

    async def complete(self, messages: Sequence[ChatMessage], **_: Any) -> str:
        return "hypothetical passage"

    async def health(self) -> dict[str, Any]:
        if not self.healthy:
            raise UpstreamUnavailable("llm down")
        return {"reachable": True, "model": "fake-llm", "model_available": True}

    async def aclose(self) -> None:
        return None


SAMPLE_TEXTS = [
    (
        "IPC_1860.pdf",
        "statute",
        "Section 302. Punishment for murder. Whoever commits murder shall be "
        "punished with death or imprisonment for life, and shall also be liable to fine.",
    ),
    ("IPC_1860.pdf", "statute", "Section 420. Cheating and dishonestly inducing delivery of property."),
    ("IT_Act_2000.pdf", "statute", "Section 66. Computer related offences, hacking with computer system."),
    ("Constitution_of_India.pdf", "statute", "Article 21. Protection of life and personal liberty."),
]


def make_chunks() -> list[Chunk]:
    return [
        Chunk(
            chunk_id=f"c{i}",
            text=text,
            metadata=ChunkMetadata(source_file=src, law_type=lt, page=i + 1, chunk_index=i),  # type: ignore[arg-type]
        )
        for i, (src, lt, text) in enumerate(SAMPLE_TEXTS)
    ]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm=LLMSettings(model="fake-llm", max_retries=2),
        embed=EmbedSettings(model=EMBED_MODEL, dimension=DIM),
        rerank=RerankSettings(enabled=False, top_n=3),
        retrieval=RetrievalSettings(index_dir=tmp_path / "index", data_dir=tmp_path / "data"),
        api=APISettings(environment="ci", rate_limit_query="1000/minute", rate_limit_session="1000/minute"),
    )


@pytest.fixture
async def index() -> LocalIndex:
    chunks = make_chunks()
    vectors = await FakeEmbedder().embed_documents([c.text for c in chunks])
    return LocalIndex.build(chunks, vectors, EMBED_MODEL)


def make_container(
    settings: Settings,
    index: LocalIndex | None,
    llm: FakeLLM | None = None,
    embedder: FakeEmbedder | None = None,
) -> Container:
    llm = llm or FakeLLM()
    embedder = embedder or FakeEmbedder()
    sessions = SessionStore(ttl_minutes=30, max_messages=20, max_sessions=100)
    retriever = HybridRetriever(index, embedder, settings.retrieval) if index else None  # type: ignore[arg-type]
    chat = ChatService(settings, llm, retriever, None, sessions)  # type: ignore[arg-type]
    return Container(settings, llm, embedder, None, index, sessions, chat)  # type: ignore[arg-type]
