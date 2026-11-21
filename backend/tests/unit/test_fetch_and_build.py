from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from lexai.core.errors import ConfigurationError
from lexai.core.settings import Settings
from lexai.ingestion import indexer
from lexai.ingestion.fetch import fetch_documents
from lexai.ingestion.indexer import build_index
from lexai.ingestion.manifest import DocumentSpec, read_lock
from lexai.ingestion.parsing import DocumentText
from lexai.retrieval.qdrant_store import QdrantStore
from tests.conftest import FakeEmbedder
from tests.unit.test_ingestion import STATUTE

PDF_V1 = b"%PDF-1.4 version one"
PDF_V2 = b"%PDF-1.4 version two"


def downloaded_spec(*urls: str) -> DocumentSpec:
    return DocumentSpec(id="bns-2023", title="BNS", short_name="BNS", doc_type="statute", urls=list(urls))


# ---------------------------------------------------------------- fetch


@respx.mock
async def test_fetch_downloads_pins_and_then_uses_the_cache(settings: Settings) -> None:
    route = respx.get("https://gov.example/bns.pdf").mock(return_value=httpx.Response(200, content=PDF_V1))
    first = await fetch_documents([downloaded_spec("https://gov.example/bns.pdf")], settings.corpus)
    assert first[0].status == "downloaded"
    pin = read_lock(settings.corpus.lock_file)["bns-2023"]
    assert pin.sha256 == hashlib.sha256(PDF_V1).hexdigest() and pin.url == "https://gov.example/bns.pdf"

    second = await fetch_documents([downloaded_spec("https://gov.example/bns.pdf")], settings.corpus)
    assert second[0].status == "cached" and route.call_count == 1


@respx.mock
async def test_fetch_falls_back_to_the_next_url_when_one_is_not_a_pdf(settings: Settings) -> None:
    respx.get("https://a.example/x.pdf").mock(return_value=httpx.Response(200, text="<html>login</html>"))
    respx.get("https://b.example/x.pdf").mock(return_value=httpx.Response(200, content=PDF_V1))
    result = await fetch_documents(
        [downloaded_spec("https://a.example/x.pdf", "https://b.example/x.pdf")], settings.corpus
    )
    assert result[0].status == "downloaded" and "b.example" in result[0].detail
    assert "not a PDF" in result[0].errors[0]


@respx.mock
async def test_fetch_refuses_a_changed_upstream_file_unless_updating(settings: Settings) -> None:
    route = respx.get("https://gov.example/bns.pdf")
    route.mock(return_value=httpx.Response(200, content=PDF_V1))
    await fetch_documents([downloaded_spec("https://gov.example/bns.pdf")], settings.corpus)
    (settings.corpus.raw_dir / "bns-2023.pdf").unlink()

    route.mock(return_value=httpx.Response(200, content=PDF_V2))
    refused = await fetch_documents([downloaded_spec("https://gov.example/bns.pdf")], settings.corpus)
    assert refused[0].status == "failed" and "pinned" in refused[0].errors[0]

    accepted = await fetch_documents(
        [downloaded_spec("https://gov.example/bns.pdf")], settings.corpus, update=True
    )
    assert accepted[0].status == "downloaded"
    assert read_lock(settings.corpus.lock_file)["bns-2023"].sha256 == hashlib.sha256(PDF_V2).hexdigest()


@respx.mock
async def test_fetch_retries_gateway_timeouts_then_succeeds(settings: Settings) -> None:
    route = respx.get("https://gov.example/bns.pdf").mock(
        side_effect=[httpx.Response(504), httpx.Response(503), httpx.Response(200, content=PDF_V1)]
    )
    result = await fetch_documents([downloaded_spec("https://gov.example/bns.pdf")], settings.corpus)
    assert result[0].status == "downloaded" and route.call_count == 3


@respx.mock
async def test_fetch_gives_up_on_a_url_after_its_attempts_and_uses_the_mirror(settings: Settings) -> None:
    primary = respx.get("https://gov.example/bns.pdf").mock(return_value=httpx.Response(504))
    respx.get("https://mirror.example/bns.pdf").mock(return_value=httpx.Response(200, content=PDF_V1))
    result = await fetch_documents(
        [downloaded_spec("https://gov.example/bns.pdf", "https://mirror.example/bns.pdf")], settings.corpus
    )
    assert primary.call_count == settings.corpus.download_attempts
    assert result[0].status == "downloaded" and "mirror.example" in result[0].detail
    assert result[0].errors == [
        f"https://gov.example/bns.pdf: HTTP 504 Gateway Timeout after {settings.corpus.download_attempts}"
        " attempts"
    ]


@respx.mock
async def test_fetch_does_not_retry_a_missing_file(settings: Settings) -> None:
    route = respx.get("https://gov.example/bns.pdf").mock(return_value=httpx.Response(404))
    result = await fetch_documents([downloaded_spec("https://gov.example/bns.pdf")], settings.corpus)
    assert result[0].status == "failed" and route.call_count == 1
    assert result[0].errors == ["https://gov.example/bns.pdf: HTTP 404 Not Found"]


@respx.mock
async def test_fetch_tries_the_pinned_source_first(settings: Settings) -> None:
    primary = respx.get("https://gov.example/bns.pdf").mock(return_value=httpx.Response(504))
    mirror = respx.get("https://mirror.example/bns.pdf").mock(
        return_value=httpx.Response(200, content=PDF_V1)
    )
    spec = downloaded_spec("https://gov.example/bns.pdf", "https://mirror.example/bns.pdf")
    await fetch_documents([spec], settings.corpus)  # primary down: pins the mirror
    (settings.corpus.raw_dir / "bns-2023.pdf").unlink()

    primary.mock(return_value=httpx.Response(200, content=PDF_V2))  # primary back, different file
    calls_before = primary.call_count
    again = await fetch_documents([spec], settings.corpus)
    assert again[0].status == "downloaded" and "mirror.example" in again[0].detail
    assert primary.call_count == calls_before and mirror.call_count == 2


# ---------------------------------------------------------------- build


def write_corpus(settings: Settings, expect: str = "whoever commits murder shall be punished") -> None:
    statutes = settings.corpus.bundled_dir / "statutes"
    statutes.mkdir(parents=True, exist_ok=True)
    pdf = statutes / "example.pdf"
    pdf.write_bytes(b"%PDF-1.4 placeholder; text comes from the patched loader")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    (settings.corpus.bundled_dir / "SHA256SUMS").write_text(f"{digest}  statutes/example.pdf\n")
    manifest = {
        "version": 1,
        "documents": [
            {
                "id": "example-2023",
                "title": "The Example Code, 2023",
                "short_name": "EC",
                "doc_type": "statute",
                "file": "statutes/example.pdf",
                "expect": [expect],
                "min_units": 4,
            }
        ],
    }
    settings.corpus.manifest.write_text(json.dumps(manifest))  # JSON is valid YAML


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    embedder = FakeEmbedder()

    def fake_load(path: Path) -> DocumentText:
        return DocumentText(STATUTE, [0, len(STATUTE) // 2])

    monkeypatch.setattr(indexer, "load_document", fake_load)
    monkeypatch.setattr(indexer, "EmbeddingClient", lambda settings: embedder)
    return embedder


async def live_info(settings: Settings) -> tuple[str | None, int]:
    store = QdrantStore.connect(settings.qdrant)
    try:
        live = await store.live_collection()
        return live, (await store.count(live) if live else 0)
    finally:
        await store.close()


async def test_build_publishes_then_rebuild_reuses_unchanged_documents(
    settings: Settings, patched: Any
) -> None:
    write_corpus(settings)
    first = await build_index(settings)
    assert first.status.startswith("live"), first
    assert first.embedded_docs == 1 and first.copied_docs == 0
    assert all(c["ok"] for c in first.checks)
    live, count = await live_info(settings)
    assert live == first.collection and count == first.chunks

    calls_before = patched.calls
    second = await build_index(settings)
    assert second.status.startswith("live")
    assert second.embedded_docs == 0 and second.copied_docs == 1
    assert patched.calls - calls_before == len(first.checks) - 1  # only the check queries were embedded
    live, count = await live_info(settings)
    assert live == second.collection and count == first.chunks


async def test_a_failed_build_leaves_the_live_index_serving(settings: Settings, patched: Any) -> None:
    write_corpus(settings)
    good = await build_index(settings)
    write_corpus(settings, expect="text that is not in the statute")
    bad = await build_index(settings)
    assert bad.status.startswith("failed")
    assert any("expected text not found" in p for p in bad.problems)
    live, _ = await live_info(settings)
    assert live == good.collection


async def test_dry_run_makes_no_model_or_database_calls(settings: Settings, patched: Any) -> None:
    write_corpus(settings)
    report = await build_index(settings, dry_run=True)
    assert report.status == "dry run ok" and report.chunks > 0
    assert patched.calls == 0
    assert not settings.qdrant.path.exists()


def write_two_doc_corpus(settings: Settings) -> None:
    statutes = settings.corpus.bundled_dir / "statutes"
    statutes.mkdir(parents=True, exist_ok=True)
    sums = []
    docs = []
    for doc_id, short in (("example-2023", "EC"), ("other-2023", "OC")):
        pdf = statutes / f"{doc_id}.pdf"
        pdf.write_bytes(f"%PDF-1.4 {doc_id}".encode())
        sums.append(f"{hashlib.sha256(pdf.read_bytes()).hexdigest()}  statutes/{doc_id}.pdf")
        docs.append({"id": doc_id, "title": f"The {short} Code, 2023", "short_name": short, "doc_type": "statute",
                     "file": f"statutes/{doc_id}.pdf", "expect": ["whoever commits murder shall be punished"], "min_units": 4})  # fmt: skip
    (settings.corpus.bundled_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    settings.corpus.manifest.write_text(json.dumps({"version": 1, "documents": docs}))


async def test_a_partial_build_keeps_every_other_document(settings: Settings, patched: Any) -> None:
    write_two_doc_corpus(settings)
    full = await build_index(settings)
    assert full.status.startswith("live") and full.embedded_docs == 2
    partial = await build_index(settings, only=["other-2023"])
    assert partial.status.startswith("live"), partial
    assert (
        partial.copied_docs == 2 and partial.embedded_docs == 0
    )  # selected one unchanged + one carried over
    store = QdrantStore.connect(settings.qdrant)
    try:
        live = await store.live_collection()
        meta = await store.read_meta(live) if live else None
        assert live == partial.collection and meta is not None
        assert set(meta.docs) == {"example-2023", "other-2023"}
        assert await store.count(live) == full.chunks
    finally:
        await store.close()


async def test_a_partial_build_without_a_live_index_is_refused(settings: Settings, patched: Any) -> None:
    write_two_doc_corpus(settings)
    with pytest.raises(ConfigurationError, match="partial rebuild"):
        await build_index(settings, only=["other-2023"])
