from langchain_core.documents import Document

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import ingestion.ingest as ingest
from app.services.providers import build_embeddings_client
from ingestion.chunker import SemanticChunker, SemanticChunkerConfig
from ingestion.ingest import select_pdf_files_to_process


def test_select_pdf_files_to_process_skips_completed(tmp_path: Path) -> None:
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    for name in ["a.pdf", "b.pdf", "c.pdf"]:
        (data_dir / name).write_bytes(b"pdf")

    checkpoint = tmp_path / "ingestion_checkpoint.json"
    checkpoint.write_text('{"completed_files": ["a.pdf", "c.pdf"]}', encoding="utf-8")

    pending = select_pdf_files_to_process(data_dir, checkpoint)

    assert [path.name for path in pending] == ["b.pdf"]


def test_build_retrying_embed_fn_waits_minute_after_quota(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr(ingest.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    class FakeClient:
        calls = 0

        def embed_documents(self, texts):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 12s.")
            return [[0.1, 0.2]]

    settings = SimpleNamespace(
        embedding_max_retries=2,
        embedding_retry_min_seconds=1,
        embedding_retry_max_seconds=5,
        embedding_quota_wait_seconds=60,
    )

    embed_fn = ingest.build_retrying_embed_fn(settings)
    vectors = embed_fn(FakeClient(), ["hello"])

    assert vectors == [[0.1, 0.2]]
    assert sleep_calls == [60.0]


def test_semantic_chunker_waits_before_retrying_quota(monkeypatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr("ingestion.chunker.time.sleep", lambda seconds: sleep_calls.append(seconds))

    class FakeEmbeddings:
        calls = 0

        def embed_documents(self, texts):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 12s.")
            return [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]]

    chunker = SemanticChunker(
        embeddings=FakeEmbeddings(),
        config=SemanticChunkerConfig(
            buffer_size=0,
            breakpoint_percentile=100.0,
            min_chunk_chars=1,
            max_chunk_chars=2000,
            hard_split_overlap=0,
            embedding_max_retries=2,
            embedding_retry_min_seconds=1,
            embedding_retry_max_seconds=5,
            embedding_quota_wait_seconds=60,
        ),
    )

    chunks = chunker.split_text("First sentence. Second sentence. Third sentence.")

    assert chunks
    assert sleep_calls == [60.0]


def test_build_embeddings_client_uses_ollama_when_enabled(monkeypatch) -> None:
    class FakeOllamaEmbeddings:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    fake_module = types.SimpleNamespace(OllamaEmbeddings=FakeOllamaEmbeddings)
    monkeypatch.setitem(sys.modules, "langchain_ollama", fake_module)

    settings = SimpleNamespace(
        embedding_provider="ollama",
        embedding_model="nomic-embed-text",
        ollama_base_url="http://localhost:11434",
        embedding_output_dimensionality=768,
    )

    client = build_embeddings_client(settings, task_type="retrieval_query")

    assert isinstance(client, FakeOllamaEmbeddings)
    assert client.kwargs["model"] == "nomic-embed-text"
    assert client.kwargs["base_url"] == "http://localhost:11434"


def test_run_ingestion_reports_stats_when_embedding_is_skipped(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "Data"
    data_dir.mkdir()
    (data_dir / "a.pdf").write_bytes(b"pdf")
    database_dir = tmp_path / "Database"
    database_dir.mkdir()

    class DummyChunker:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def split_documents(self, pages):
            return [Document(page_content="chunk", metadata={"page": 1})]

    class DummyVectorStore:
        def __init__(self, dimension: int = 0) -> None:
            self.dimension = dimension

        def add_documents(self, chunks, vectors) -> None:
            pass

        def save_local(self, path) -> None:
            pass

        def __len__(self) -> int:
            return 0

    class DummyBm25Store:
        def __init__(self, docs) -> None:
            self.docs = docs

        def add_documents(self, chunks) -> None:
            pass

        def save_local(self, path) -> None:
            pass

    monkeypatch.setattr(ingest, "SemanticChunker", DummyChunker)
    monkeypatch.setattr(ingest, "FaissVectorStore", DummyVectorStore)
    monkeypatch.setattr(ingest, "Bm25Store", DummyBm25Store)
    monkeypatch.setattr(
        ingest,
        "process_document",
        lambda pdf_path, chunker, dry_run_splitter: ([Document(page_content="chunk", metadata={"page": 1})], 1),
    )
    monkeypatch.setattr(ingest, "embed_all", lambda texts, settings: (_ for _ in ()).throw(RuntimeError("quota")))
    monkeypatch.setattr(ingest, "backup_existing_database", lambda database_dir: None)

    settings = SimpleNamespace(
        chunk_max_chars=200,
        chunk_hard_overlap=20,
        chunk_buffer_size=2,
        chunk_breakpoint_percentile=80,
        chunk_min_chars=50,
        embedding_model="model",
        embedding_output_dimensionality=64,
        embedding_batch_size=2,
        embedding_retry_min_seconds=1,
        embedding_retry_max_seconds=5,
        embedding_pause_seconds=0,
        embedding_max_retries=1,
        embedding_task_type="semantic_similarity",
        embedding_quota_wait_seconds=60,
        require_google_api_key=lambda: "dummy-key",
    )

    stats = ingest.run_ingestion(data_dir, database_dir, dry_run=False, no_backup=True, settings=settings)

    assert stats["total_chunks"] == 1
