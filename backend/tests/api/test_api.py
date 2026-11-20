from __future__ import annotations

import asyncio
import json
import statistics
import time

import httpx
import pytest

from lexai.api.app import create_app
from lexai.core.errors import UpstreamUnavailable
from lexai.core.settings import Settings
from lexai.retrieval.index import LocalIndex
from tests.conftest import FakeEmbedder, FakeLLM, make_container


def parse_sse(body: str) -> list[tuple[str, object]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # type: ignore[arg-type]


async def test_query_streams_citations_tokens_then_done(settings: Settings, index: LocalIndex) -> None:
    app = create_app(settings, make_container(settings, index))
    async with client_for(app) as client:
        response = await client.post("/api/query", json={"question": "What is the punishment for murder?"})
    assert response.status_code == 200
    events = parse_sse(response.text)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "citations"
    assert kinds[-1] == "done"
    assert set(kinds[1:-1]) == {"token"}

    citations = events[0][1]
    assert isinstance(citations, list) and citations[0]["source_file"] == "IPC_1860.pdf"
    assert citations[0]["ref"] == 1

    done = events[-1][1]
    assert isinstance(done, dict)
    tokens = "".join(data["text"] for kind, data in events if kind == "token")  # type: ignore[index]
    assert done["answer"] == tokens
    assert done["request_id"] == response.headers["x-request-id"]
    assert done["latency"]["time_to_first_token_ms"] is not None


async def test_prompt_contains_numbered_passages_and_history(settings: Settings, index: LocalIndex) -> None:
    llm = FakeLLM()
    app = create_app(settings, make_container(settings, index, llm=llm))
    async with client_for(app) as client:
        session = (await client.post("/api/session")).json()["session_id"]
        await client.post("/api/query", json={"question": "murder punishment", "session_id": session})
        await client.post("/api/query", json={"question": "and for cheating?", "session_id": session})
    system, *rest = llm.calls[1]
    assert system.role == "system" and "<passages>" in system.content and "[1]" in system.content
    assert [m.role for m in rest] == ["user", "assistant", "user"]


async def test_llm_outage_becomes_an_error_event(settings: Settings, index: LocalIndex) -> None:
    llm = FakeLLM(error=UpstreamUnavailable("connection refused"))
    app = create_app(settings, make_container(settings, index, llm=llm))
    async with client_for(app) as client:
        response = await client.post("/api/query", json={"question": "murder"})
    kind, data = parse_sse(response.text)[-1]
    assert kind == "error"
    assert data == {
        "code": "upstream_unavailable",
        "message": UpstreamUnavailable.public_message,
        "request_id": response.headers["x-request-id"],
    }


async def test_embedder_outage_is_an_error_not_garbage(settings: Settings, index: LocalIndex) -> None:
    app = create_app(settings, make_container(settings, index, embedder=FakeEmbedder(fail=True)))
    async with client_for(app) as client:
        response = await client.post("/api/query", json={"question": "murder"})
    events = parse_sse(response.text)
    assert [k for k, _ in events] == ["error"]


async def test_missing_index_returns_index_not_ready(settings: Settings) -> None:
    app = create_app(settings, make_container(settings, index=None))
    async with client_for(app) as client:
        response = await client.post("/api/query", json={"question": "murder"})
    kind, data = parse_sse(response.text)[-1]
    assert kind == "error" and data["code"] == "index_not_ready"  # type: ignore[index]


async def test_readyz_is_red_when_llm_is_down(settings: Settings, index: LocalIndex) -> None:
    app = create_app(settings, make_container(settings, index, llm=FakeLLM(healthy=False)))
    async with client_for(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["checks"]["llm"]["ok"] is False
    assert ready.json()["checks"]["index"]["ok"] is True


async def test_readyz_is_green_when_everything_is_up(settings: Settings, index: LocalIndex) -> None:
    app = create_app(settings, make_container(settings, index))
    async with client_for(app) as client:
        ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ready"] is True


async def test_request_id_is_propagated(settings: Settings, index: LocalIndex) -> None:
    app = create_app(settings, make_container(settings, index))
    async with client_for(app) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


@pytest.mark.parametrize("question", ["", "x" * 2001])
async def test_question_length_is_validated(settings: Settings, index: LocalIndex, question: str) -> None:
    app = create_app(settings, make_container(settings, index))
    async with client_for(app) as client:
        response = await client.post("/api/query", json={"question": question})
    assert response.status_code == 422


async def test_health_stays_fast_while_answers_stream(settings: Settings, index: LocalIndex) -> None:
    """The v0.1 bug: one query blocked the event loop. Ten slow streams must not slow /healthz."""
    llm = FakeLLM(tokens=["t "] * 20, delay_s=0.05)  # each answer streams for ~1 s
    app = create_app(settings, make_container(settings, index, llm=llm))
    async with client_for(app) as client:

        async def ask() -> None:
            await client.post("/api/query", json={"question": "murder punishment"})

        async def probe() -> list[float]:
            timings = []
            for _ in range(20):
                t0 = time.perf_counter()
                assert (await client.get("/healthz")).status_code == 200
                timings.append((time.perf_counter() - t0) * 1000)
                await asyncio.sleep(0.02)
            return timings

        streams = [asyncio.create_task(ask()) for _ in range(10)]
        await asyncio.sleep(0.05)
        timings = await probe()
        await asyncio.gather(*streams)
    p95 = statistics.quantiles(timings, n=20)[-1]
    assert p95 < 50, f"/healthz p95 {p95:.1f} ms while streaming"
