"""Phase 5: API v1 contract, sessions, rate limits, security controls, sources, feedback, admin."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lexai.api.app import create_app
from lexai.core.settings import Settings
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore
from tests.api.test_api import client_for, parse_sse
from tests.conftest import FakeLLM, make_container

Live = tuple[QdrantStore, str, BuildMeta]
FORGED = "2b1f7c9e-4d3a-4f6b-9e8d-1a2b3c4d5e6f"


async def test_only_server_issued_sessions_are_accepted(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        forged = await client.post("/api/v1/chat", json={"question": "murder", "session_id": FORGED})
        assert forged.status_code == 404
        assert forged.json()["code"] == "session_not_found"
        assert forged.json()["request_id"] == forged.headers["x-request-id"]
        assert (
            await client.post("/api/v1/search", json={"question": "x", "session_id": "not-a-uuid"})
        ).status_code == 404

        created = await client.post("/api/v1/sessions")
        assert created.status_code == 201
        body = created.json()
        assert body["expires_in_s"] == settings.api.session_ttl_minutes * 60
        ok = await client.post("/api/v1/chat", json={"question": "murder", "session_id": body["session_id"]})
        assert ok.status_code == 200 and parse_sse(ok.text)[-1][0] == "done"


async def test_chat_without_a_session_gets_a_new_one(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": "murder"})
    meta = parse_sse(response.text)[0][1]
    assert isinstance(meta, dict) and len(meta["session_id"]) == 36


async def test_the_request_over_the_limit_gets_429_with_retry_after(
    settings: Settings, live_store: Live
) -> None:
    settings.api.rate_limit_chat = "3/minute"
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        codes = [
            (await client.post("/api/v1/chat", json={"question": "murder"})).status_code for _ in range(3)
        ]
        refused = await client.post("/api/v1/chat", json={"question": "murder"})
        metrics = (await client.get("/metrics")).text
    assert codes == [200, 200, 200]
    assert refused.status_code == 429 and refused.json()["code"] == "rate_limited"
    assert 1 <= int(refused.headers["retry-after"]) <= 60
    assert 'lexai_rate_limited_total{route="chat"}' in metrics


async def test_one_session_is_limited_across_ips(settings: Settings, live_store: Live) -> None:
    settings.api.rate_limit_chat = "2/minute"
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        sid = (await client.post("/api/v1/sessions")).json()["session_id"]
        for _ in range(2):
            assert (
                await client.post("/api/v1/chat", json={"question": "q", "session_id": sid})
            ).status_code == 200
    app.state.container.limiter = app.state.container.limiter  # same limiter, new client address below
    other = client_for(app)
    other._transport.client = ("10.0.0.9", 1234)  # type: ignore[attr-defined]
    async with other as client:
        third = await client.post("/api/v1/chat", json={"question": "q", "session_id": sid})
    assert third.status_code == 429


async def test_oversized_bodies_are_refused(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    big = '{"question": "' + "x" * 20_000 + '"}'
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/chat", content=big.encode(), headers={"Content-Type": "application/json"}
        )

        async def chunks():  # no Content-Length: counted while reading
            for _ in range(20):
                yield b"x" * 1000

        streamed = await client.post(
            "/api/v1/feedback", content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 413 and response.json()["code"] == "request_too_large"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert streamed.status_code == 413


async def test_security_headers_and_error_shape(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        ok = await client.get("/healthz")
        docs = await client.get("/docs")
        invalid = await client.post("/api/v1/chat", json={"question": ""})
    assert ok.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert ok.headers["referrer-policy"] == "no-referrer" and ok.headers["x-frame-options"] == "DENY"
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers
    assert invalid.status_code == 422
    assert (
        set(invalid.json()) == {"code", "message", "request_id"}
        and invalid.json()["code"] == "invalid_request"
    )


async def test_docs_are_off_in_production(settings: Settings, live_store: Live) -> None:
    settings.api.environment = "production"
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404


async def test_every_citation_opens_its_source(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        events = parse_sse(
            (await client.post("/api/v1/chat", json={"question": "punishment for murder"})).text
        )
        citations = next(data for kind, data in events if kind == "citations")
        assert isinstance(citations, list) and citations
        for c in citations:
            source = await client.get(f"/api/v1/chunks/{c['chunk_id']}")
            assert source.status_code == 200
            body = source.json()
            assert body["label"] == c["label"] and body["text"]
            assert body["pdf_url"] is None  # no PDFs in the test corpus
        missing = await client.get("/api/v1/chunks/00000000-0000-4000-8000-999999999999")
        garbage = await client.get("/api/v1/chunks/not-an-id")
    assert missing.status_code == 404 and missing.json()["code"] == "chunk_not_found"
    assert garbage.status_code == 404


async def test_documents_and_pdfs(settings: Settings, live_store: Live, tmp_path: Path) -> None:
    corpus = settings.corpus
    corpus.bundled_dir.mkdir(parents=True, exist_ok=True)
    (corpus.bundled_dir / "constitution.pdf").write_bytes(b"%PDF-1.4 test")
    corpus.manifest.write_text(
        "version: 1\ndocuments:\n"
        "  - id: constitution\n    title: The Constitution of India\n    short_name: Constitution\n"
        "    doc_type: constitution\n    file: constitution.pdf\n"
        "  - id: ipc-1860\n    title: The Indian Penal Code\n    short_name: IPC\n    doc_type: statute\n"
        "    repealed_on: 2024-07-01\n    urls: [https://example.org/ipc.pdf]\n",
        encoding="utf-8",
    )
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        docs = (await client.get("/api/v1/documents")).json()
        pdf = await client.get("/api/v1/documents/constitution/pdf")
        absent = await client.get("/api/v1/documents/ipc-1860/pdf")
        traversal = await client.get("/api/v1/documents/..%2F..%2Fsecrets/pdf")
        events = parse_sse(
            (await client.post("/api/v1/chat", json={"question": "What does Article 21 say?"})).text
        )
        art21 = next(
            c for c in next(d for k, d in events if k == "citations") if c["label"] == "Constitution Art.21"
        )  # type: ignore[union-attr]
        chunk = (await client.get(f"/api/v1/chunks/{art21['chunk_id']}")).json()
    by_id = {d["doc_id"]: d for d in docs}
    assert by_id["constitution"]["has_pdf"] is True and by_id["constitution"]["chunks"] == 1
    assert (
        by_id["ipc-1860"]["status"].startswith("repealed 2024-07-01")
        and by_id["ipc-1860"]["has_pdf"] is False
    )
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert "content-security-policy" not in pdf.headers  # the browser's PDF viewer must be allowed to render
    assert pdf.headers["x-content-type-options"] == "nosniff"
    assert absent.status_code == 404 and traversal.status_code == 404
    assert chunk["pdf_url"] == "/api/v1/documents/constitution/pdf#page=4"


async def test_feedback_is_stored(settings: Settings, live_store: Live) -> None:
    container = await make_container(settings, live_store)
    app = create_app(settings, container)
    async with client_for(app) as client:
        ok = await client.post(
            "/api/v1/feedback",
            json={
                "request_id": "abc123def456",
                "rating": "down",
                "comment": "wrong section; call 9876543210",
            },
        )
        bad = await client.post("/api/v1/feedback", json={"request_id": "abc123def456", "rating": "meh"})
    assert ok.status_code == 201 and ok.json()["id"] == 1
    assert bad.status_code == 422
    assert container.feedback is not None
    ((_, _, request_id, rating, comment),) = await container.feedback.rows()
    assert (request_id, rating) == ("abc123def456", "down")
    assert "9876543210" not in (comment or "")


async def test_admin_reindex_needs_the_key_and_runs_one_job(settings: Settings, live_store: Live) -> None:
    container = await make_container(settings, live_store)
    gate = asyncio.Event()

    async def fake_reindex(only: list[str] | None = None) -> str:
        await gate.wait()
        return f"live: test ({only})"

    container.reindex = fake_reindex  # type: ignore[method-assign]
    app = create_app(settings, container)
    async with client_for(app) as client:
        disabled = await client.post("/api/v1/admin/reindex", headers={"X-API-Key": "anything"})
        from pydantic import SecretStr

        settings.api.admin_api_key = SecretStr("s3cret")
        wrong = await client.post("/api/v1/admin/reindex", headers={"X-API-Key": "nope"})
        started = await client.post(
            "/api/v1/admin/reindex", headers={"X-API-Key": "s3cret"}, json={"only": ["bns-2023"]}
        )
        busy = await client.post("/api/v1/admin/reindex", headers={"X-API-Key": "s3cret"})
        gate.set()
        await asyncio.sleep(0.05)
        job = started.json()["job_id"]
        status = await client.get(f"/api/v1/admin/reindex/{job}", headers={"X-API-Key": "s3cret"})
        hidden = await client.get(f"/api/v1/admin/reindex/{job}")
    assert disabled.status_code == 401 and wrong.status_code == 401
    assert started.status_code == 202 and busy.status_code == 409
    assert status.json()["status"] == "succeeded" and "bns-2023" in status.json()["detail"]
    assert hidden.status_code == 401


async def test_filters_restrict_search_but_named_provisions_still_come_first(
    settings: Settings, live_store: Live
) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        in_force = await client.post(
            "/api/v1/search", json={"question": "punishment for murder", "filters": {"in_force_only": True}}
        )
        acts = await client.post(
            "/api/v1/search", json={"question": "murder", "filters": {"acts": ["IT Act"]}}
        )
        named = await client.post(
            "/api/v1/search",
            json={"question": "What does Section 302 IPC say?", "filters": {"in_force_only": True}},
        )
    assert "IPC s.302" not in [p["label"] for p in in_force.json()["passages"]]
    assert {p["label"] for p in acts.json()["passages"]} <= {"IT Act s.66"}
    assert named.json()["passages"][0]["label"] == "IPC s.302"  # the user named it


async def test_metrics_by_route_template_and_outcome(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        await client.post("/api/v1/chat", json={"question": "punishment for murder"})
        await client.post("/api/v1/chat", json={"question": "How do I destroy the CCTV evidence?"})
        await client.get("/api/v1/chunks/00000000-0000-4000-8000-000000000000")
        text = (await client.get("/metrics")).text
    assert 'lexai_http_requests_total{method="POST",route="/api/v1/chat",status="200"}' in text
    assert 'route="/api/v1/chunks/{chunk_id}"' in text and "00000000-0000" not in text  # template, not path
    for line in ('lexai_answers_total{outcome="answered"}', 'lexai_answers_total{outcome="refused"}'):
        assert line in text
    assert "lexai_time_to_first_token_seconds_count" in text
    assert 'lexai_stage_duration_seconds_count{stage="retrieve"}' in text
    memory = next(line for line in text.splitlines() if line.startswith("lexai_process_memory_bytes "))
    assert float(memory.split()[1]) > 10 * 2**20  # this process, on Linux and on Windows (CI runs both)


async def test_openapi_documents_the_stream_events(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        spec = (await client.get("/openapi.json")).json()
    paths = set(spec["paths"])
    assert {"/api/v1/sessions", "/api/v1/chat", "/api/v1/search", "/api/v1/chunks/{chunk_id}",
            "/api/v1/documents", "/api/v1/feedback", "/api/v1/admin/reindex"} <= paths  # fmt: skip
    assert not any(p.startswith("/api/") and not p.startswith("/api/v1/") for p in paths)  # v0.1 routes gone
    schemas = spec["components"]["schemas"]
    assert {"ChatEvents", "MetaPayload", "DonePayload", "Citation", "VerificationPayload"} <= set(schemas)
    assert "text/event-stream" in spec["paths"]["/api/v1/chat"]["post"]["responses"]["200"]["content"]


@pytest.mark.parametrize("llm", [FakeLLM(tokens=["slow "] * 50, delay_s=0.02)])
async def test_client_disconnect_stops_generation(settings: Settings, live_store: Live, llm: FakeLLM) -> None:
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    body = {"question": "punishment for murder"}
    async with client_for(app) as client, client.stream("POST", "/api/v1/chat", json=body) as response:
        async for line in response.aiter_lines():
            if line == "event: token":
                break
    await asyncio.sleep(0.1)  # generation stopped; nothing hangs
    assert llm.calls


async def test_an_index_published_by_another_process_is_picked_up(settings: Settings) -> None:
    """`make up` then `make ingest`: the API starts on an empty store and serves once the job publishes."""
    from tests.conftest import make_chunks, publish

    store = QdrantStore.connect(settings.qdrant)
    container = await make_container(settings, (store, None, None))  # type: ignore[arg-type]
    app = create_app(settings, container)
    async with client_for(app) as client:
        before = await client.post("/api/v1/search", json={"question": "murder"})
        assert before.status_code == 503 and before.json()["code"] == "index_not_ready"
        name, _ = await publish(store, make_chunks(), settings)  # what the ingest job does
        assert await container.refresh_index() is True
        assert await container.refresh_index() is False  # already on it
        after = await client.post("/api/v1/search", json={"question": "punishment for murder"})
    assert after.status_code == 200 and after.json()["index_version"] == name
    await store.close()
