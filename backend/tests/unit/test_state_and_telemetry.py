"""Sessions and rate limits (memory and Redis), and the trace of one chat request."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lexai.api.app import create_app
from lexai.core.errors import SessionNotFound
from lexai.core.settings import Settings
from lexai.core.telemetry import JsonlSpanExporter
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore
from lexai.services.ratelimit import RateLimiter
from lexai.services.sessions import MemorySessionStore, RedisSessionStore
from tests.api.test_api import client_for, parse_sse
from tests.conftest import FakeLLM, make_container

Live = tuple[QdrantStore, str, BuildMeta]


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    """A real Redis: TEST_REDIS_URL (CI service container), else a throwaway redis-server, else skip."""
    if url := os.environ.get("TEST_REDIS_URL"):
        yield url
        return
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("no Redis available (set TEST_REDIS_URL or install redis-server)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--save", "", "--appendonly", "no", "--bind", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
    proc.wait(timeout=5)


# ---------------------------------------------------------------- sessions


async def _contract(store: MemorySessionStore | RedisSessionStore) -> None:
    sid = await store.create()
    assert await store.exists(sid) and await store.history(sid) == []
    for n in range(4):
        await store.append_turn(sid, f"q{n}", f"a{n}")
    history = await store.history(sid)
    assert [m.content for m in history] == ["q2", "a2", "q3", "a3"]  # last 4 messages kept
    for forged in ("2b1f7c9e-4d3a-4f6b-9e8d-1a2b3c4d5e6f", "not-a-uuid", ""):
        assert not await store.exists(forged)
        with pytest.raises(SessionNotFound):
            await store.history(forged)
        with pytest.raises(SessionNotFound):
            await store.append_turn(forged, "q", "a")
    assert await store.count() >= 1


async def test_memory_sessions() -> None:
    store = MemorySessionStore(ttl_minutes=30, max_messages=4, max_sessions=3)
    await _contract(store)
    ids = [await store.create() for _ in range(5)]
    assert await store.count() == 3 and not await store.exists(ids[0])  # oldest evicted at the cap


async def test_redis_sessions_share_state_and_expire(redis_url: str) -> None:
    store = RedisSessionStore.from_url(redis_url, ttl_minutes=30, max_messages=4)
    await _contract(store)
    other_worker = RedisSessionStore.from_url(redis_url, ttl_minutes=30, max_messages=4)
    sid = await store.create()
    await store.append_turn(sid, "q", "a")
    assert [m.content for m in await other_worker.history(sid)] == ["q", "a"]
    ttl = await store._r.ttl(f"lexai:session:{sid}:alive")
    assert 0 < ttl <= 30 * 60
    await store._r.delete(f"lexai:session:{sid}:alive")  # what expiry does
    with pytest.raises(SessionNotFound):
        await other_worker.history(sid)
    await store.aclose()
    await other_worker.aclose()


# ---------------------------------------------------------------- rate limits


@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_rate_limiter(backend: str, redis_url: str) -> None:
    limiter = RateLimiter(redis_url if backend == "redis" else None)
    key = f"test-{backend}-{id(limiter)}"
    assert [await limiter.hit("3/minute", key) for _ in range(3)] == [None, None, None]
    wait = await limiter.hit("3/minute", key)
    assert wait is not None and 1 <= wait <= 60
    assert await limiter.hit("3/minute", key + "-other") is None  # keys are independent


async def test_api_on_redis_end_to_end(settings: Settings, live_store: Live, redis_url: str) -> None:
    """Two API instances (workers) share sessions and limits through Redis."""
    settings.api.rate_limit_chat = "3/minute"
    sessions = RedisSessionStore.from_url(redis_url, 30, 20)
    apps = []
    for _ in range(2):
        container = await make_container(settings, live_store)
        container.sessions = sessions
        container.chat.sessions = sessions
        container.limiter = RateLimiter(redis_url)
        apps.append(create_app(settings, container))
    async with client_for(apps[0]) as a, client_for(apps[1]) as b:
        sid = (await a.post("/api/v1/sessions")).json()["session_id"]
        codes = [
            (await client.post("/api/v1/chat", json={"question": "murder", "session_id": sid})).status_code
            for client in (a, b, a, b)
        ]
    assert codes == [200, 200, 200, 429]
    assert len(await sessions.history(sid)) == 6  # three answered turns, written by both workers
    await sessions.aclose()


# ---------------------------------------------------------------- tracing


@pytest.fixture(scope="module")
def spans() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)  # once per process; the module tracer is a proxy to it
    return exporter


async def test_one_question_is_one_trace_with_a_span_per_stage(
    settings: Settings, live_store: Live, spans: InMemorySpanExporter
) -> None:
    spans.clear()
    llm = FakeLLM(completion="Is murder under BNS s.103 bailable?")
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        sid = (await client.post("/api/v1/sessions")).json()["session_id"]
        await client.post(
            "/api/v1/chat", json={"question": "Punishment for murder under BNS?", "session_id": sid}
        )
        spans.clear()
        response = await client.post(
            "/api/v1/chat", json={"question": "and is it bailable? call me on 9876543210", "session_id": sid}
        )
    meta = parse_sse(response.text)[0][1]
    assert isinstance(meta, dict)
    finished = spans.get_finished_spans()
    by_name = {s.name: s for s in finished}
    stages = ["lexai.condense", "lexai.retrieve", "lexai.context", "lexai.generate", "lexai.verify"]
    assert set(stages) <= set(by_name) and "POST /api/v1/chat" in by_name
    assert {f"{s.context.trace_id:032x}" for s in finished} == {meta["trace_id"]}  # one trace, id in meta
    chat = by_name["lexai.chat"]
    assert chat.parent is not None and chat.parent.span_id == by_name["POST /api/v1/chat"].context.span_id
    for stage in stages:
        assert by_name[stage].parent is not None and by_name[stage].parent.span_id == chat.context.span_id
    attrs = {name: dict(s.attributes or {}) for name, s in by_name.items()}
    assert attrs["lexai.chat"]["lexai.outcome"] == "answered"
    assert attrs["lexai.chat"]["lexai.prompt_version"] == settings.llm.prompt_version
    assert "9876543210" not in attrs["lexai.chat"]["lexai.question"]  # masked unless capture_text
    assert (
        attrs["lexai.retrieve"]["lexai.retrieval.chunk_ids"]
        and attrs["lexai.retrieve"]["lexai.retrieval.scores"]
    )
    assert attrs["lexai.generate"]["gen_ai.request.model"] == settings.llm.model
    assert "lexai.verification.valid" in attrs["lexai.verify"]
    assert attrs["POST /api/v1/chat"]["http.route"] == "/api/v1/chat"


async def test_errors_are_marked_on_the_trace(
    settings: Settings, live_store: Live, spans: InMemorySpanExporter
) -> None:
    from lexai.core.errors import UpstreamUnavailable

    spans.clear()
    llm = FakeLLM(error=UpstreamUnavailable("down", service="llm"))
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        await client.post("/api/v1/chat", json={"question": "punishment for murder"})
        metrics = (await client.get("/metrics")).text
    chat = next(s for s in spans.get_finished_spans() if s.name == "lexai.chat")
    assert chat.status.status_code.name == "ERROR" and chat.attributes["lexai.outcome"] == "error"  # type: ignore[index]
    assert 'lexai_upstream_errors_total{code="upstream_unavailable",service="llm"}' in metrics


def test_jsonl_exporter(tmp_path: Path) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("parent"), tracer.start_as_current_span("child") as child:
        child.set_attribute("lexai.retrieval.labels", ["BNS s.103"])
    path = tmp_path / "traces.jsonl"
    JsonlSpanExporter(path).export(exporter.get_finished_spans())
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["name"] for r in records] == ["child", "parent"]
    assert records[0]["parent_id"] == records[1]["span_id"] and records[1]["parent_id"] is None
    assert records[0]["attributes"]["lexai.retrieval.labels"] == ["BNS s.103"]
