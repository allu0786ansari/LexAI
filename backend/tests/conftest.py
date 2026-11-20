from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from lexai.api.container import Container, build_chat
from lexai.core.errors import UpstreamUnavailable
from lexai.core.settings import (
    APISettings,
    CacheSettings,
    CorpusSettings,
    EmbedSettings,
    LLMSettings,
    QdrantSettings,
    QuerySettings,
    RerankSettings,
    RetrievalSettings,
    Settings,
)
from lexai.domain.models import ChatMessage, Chunk, ChunkMetadata
from lexai.retrieval.qdrant_store import BuildMeta, DocBuildInfo, QdrantStore, new_collection_name
from lexai.retrieval.sparse import SPARSE_VERSION, average_length, doc_vector, tokenize
from lexai.services.sessions import SessionStore

DIM = 64
EMBED_MODEL = "fake-embedder"


class FakeEmbedder:
    """Deterministic bag-of-words vectors: texts sharing words get similar vectors."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def format_query(self, query: str) -> str:
        return query

    def format_document(self, text: str) -> str:
        return text

    def _vec(self, text: str) -> list[float]:
        v = np.zeros(DIM, dtype=np.float32)
        for tok in tokenize(text) or ["empty"]:
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
        return v.tolist()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise UpstreamUnavailable("embedder down")
        self.calls += 1
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
        completion: str = "hypothetical passage",
    ) -> None:
        self.tokens = tokens or ["Murder ", "is ", "punishable ", "with ", "death [1]."]
        self.completion = completion
        self.complete_calls: list[list[ChatMessage]] = []
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
        self.complete_calls.append(list(messages))
        return self.completion

    async def health(self) -> dict[str, Any]:
        if not self.healthy:
            raise UpstreamUnavailable("llm down")
        return {"reachable": True, "model": "fake-llm", "model_available": True}

    async def aclose(self) -> None:
        return None


# (doc_id, short_name, unit, unit_title, repealed_on, text)
SAMPLE = [
    ("ipc-1860", "IPC", "302", "Punishment for murder", "2024-07-01",
     "Whoever commits murder shall be punished with death, or imprisonment for life, and shall also be liable to fine."),
    ("ipc-1860", "IPC", "420", "Cheating and dishonestly inducing delivery of property", "2024-07-01",
     "Whoever cheats and thereby dishonestly induces the person deceived to deliver any property shall be punished."),
    ("bns-2023", "BNS", "103", "Punishment for murder", None,
     "Whoever commits murder shall be punished with death or imprisonment for life, and shall also be liable to fine."),
    ("constitution", "Constitution", "21", "Protection of life and personal liberty", None,
     "No person shall be deprived of his life or personal liberty except according to procedure established by law."),
    ("it-act-2000", "IT Act", "66", "Computer related offences", None,
     "If any person, dishonestly or fraudulently, does any act referred to in section 43, hacking a computer system."),
]  # fmt: skip


def make_chunks() -> list[Chunk]:
    chunks = []
    for i, (doc_id, short, unit, title, repealed, text) in enumerate(SAMPLE):
        label = "Article" if doc_id == "constitution" else "Section"
        meta = ChunkMetadata(
            doc_id=doc_id,
            doc_title=f"Title of {doc_id}",
            short_name=short,
            doc_type="constitution" if doc_id == "constitution" else "statute",
            source_file=f"{doc_id}.pdf",
            unit_label=label,
            unit=unit,
            unit_title=title,
            parent_id=f"p{i}",
            page_start=i + 1,
            page_end=i + 1,
            repealed_on=repealed,
            replaced_by="bns-2023" if repealed else None,
        )
        header = f"{short} {label} {unit} — {title}"
        chunks.append(
            Chunk(
                chunk_id=f"00000000-0000-4000-8000-{i:012d}",
                text=text,
                embed_text=f"{header}\n{text}",
                metadata=meta,
            )
        )
    return chunks


_SETTINGS_GROUPS = (
    LLMSettings, EmbedSettings, RerankSettings, RetrievalSettings, APISettings,
    CorpusSettings, QdrantSettings, QuerySettings, CacheSettings,
)  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never read backend/.env or LEXAI_* variables of the machine running them."""
    for name in list(os.environ):
        if name.upper().startswith("LEXAI_"):
            monkeypatch.delenv(name)
    for group in _SETTINGS_GROUPS:
        monkeypatch.setitem(group.model_config, "env_file", None)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        llm=LLMSettings(model="fake-llm", max_retries=2),
        embed=EmbedSettings(model=EMBED_MODEL, dimension=DIM, query_prefix="", document_prefix=""),
        rerank=RerankSettings(enabled=False, top_n=3),
        retrieval=RetrievalSettings(),
        api=APISettings(environment="ci", rate_limit_query="1000/minute", rate_limit_session="1000/minute"),
        corpus=CorpusSettings(
            manifest=tmp_path / "manifest.yaml",
            bundled_dir=tmp_path / "bundled",
            raw_dir=tmp_path / "raw",
            lock_file=tmp_path / "manifest.lock.json",
            report_dir=tmp_path / "reports",
            download_backoff_s=0,
        ),
        qdrant=QdrantSettings(path=tmp_path / "qdrant"),
    )


async def publish(store: QdrantStore, chunks: list[Chunk], settings: Settings) -> tuple[str, BuildMeta]:
    name = new_collection_name()
    await store.create_collection(name, DIM)
    texts = [c.embed_text for c in chunks]
    avg = average_length(texts)
    dense = await FakeEmbedder().embed_documents(texts)
    await store.upsert_chunks(name, chunks, dense, [doc_vector(t, avg) for t in texts])
    meta = BuildMeta(
        collection=name,
        embedding_model=settings.embed.model,
        dimension=DIM,
        document_prefix=settings.embed.document_prefix,
        chunker_version="test",
        sparse_version=SPARSE_VERSION,
        avg_len=avg,
        docs={c.metadata.doc_id: DocBuildInfo(sha256="x", chunks=1, title="t") for c in chunks},
    )
    await store.write_meta(meta)
    await store.set_live(name)
    return name, meta


@pytest.fixture
async def live_store(settings: Settings) -> AsyncIterator[tuple[QdrantStore, str, BuildMeta]]:
    store = QdrantStore.connect(settings.qdrant)
    name, meta = await publish(store, make_chunks(), settings)
    yield store, name, meta
    await store.close()


async def make_container(
    settings: Settings,
    live: tuple[QdrantStore, str, BuildMeta] | None,
    llm: FakeLLM | None = None,
    embedder: FakeEmbedder | None = None,
    cache: Any = None,
) -> Container:
    llm = llm or FakeLLM()
    embedder = embedder or FakeEmbedder()
    sessions = SessionStore(ttl_minutes=30, max_messages=20, max_sessions=100)
    store, collection, meta = live if live else (None, None, None)
    chat = await build_chat(settings, llm, embedder, None, store, collection, sessions, cache)  # type: ignore[arg-type]
    return Container(settings, llm, embedder, None, store, collection, meta, sessions, chat, [], cache)  # type: ignore[arg-type]
