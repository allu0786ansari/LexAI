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
from lexai.retrieval.qdrant_store import BuildMeta, QdrantStore
from tests.conftest import FakeEmbedder, FakeLLM, make_container

Live = tuple[QdrantStore, str, BuildMeta]


def parse_sse(body: str) -> list[tuple[str, object]]:
    events = []
    for frame in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # type: ignore[arg-type]


async def test_query_streams_meta_citations_tokens_verification_done(
    settings: Settings, live_store: Live
) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": "What is the punishment for murder?"})
    assert response.status_code == 200
    events = parse_sse(response.text)
    kinds = [kind for kind, _ in events]
    assert kinds[:2] == ["meta", "citations"]
    assert kinds[-2:] == ["verification", "done"]
    assert set(kinds[2:-2]) == {"token"}

    meta = events[0][1]
    assert isinstance(meta, dict)
    assert meta["intent"] == "concept" and meta["applicable_code"] == "new"
    assert meta["request_id"] == response.headers["x-request-id"]

    citations = events[1][1]
    assert isinstance(citations, list)
    assert {c["label"] for c in citations[:2]} == {"IPC s.302", "BNS s.103"}
    assert citations[0]["ref"] == 1
    ipc = next(c for c in citations if c["label"] == "IPC s.302")
    assert ipc["unit_title"] == "Punishment for murder"
    assert ipc["status"].startswith("repealed 2024-07-01; replaced by BNS")
    assert ipc["sections"][0] == "IPC s.302" and ipc["page"] == 1

    done = events[-1][1]
    assert isinstance(done, dict)
    tokens = "".join(data["text"] for kind, data in events if kind == "token")  # type: ignore[index]
    assert done["answer"] == tokens
    assert done["request_id"] == response.headers["x-request-id"]
    assert done["latency"]["time_to_first_token_ms"] is not None


async def test_prompt_contains_numbered_passages_and_history(settings: Settings, live_store: Live) -> None:
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        session = (await client.post("/api/v1/sessions")).json()["session_id"]
        await client.post("/api/v1/chat", json={"question": "murder punishment", "session_id": session})
        await client.post("/api/v1/chat", json={"question": "and for cheating?", "session_id": session})
    system, *rest = llm.calls[1]
    assert system.role == "system" and "<passages>" in system.content and "[1]" in system.content
    assert [m.role for m in rest] == ["user", "assistant", "user"]


async def test_llm_outage_becomes_an_error_event(settings: Settings, live_store: Live) -> None:
    llm = FakeLLM(error=UpstreamUnavailable("connection refused"))
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": "murder"})
    kind, data = parse_sse(response.text)[-1]
    assert kind == "error"
    assert data == {
        "code": "upstream_unavailable",
        "message": UpstreamUnavailable.public_message,
        "request_id": response.headers["x-request-id"],
    }


async def test_embedder_outage_is_an_error_not_garbage(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store, embedder=FakeEmbedder(fail=True)))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": "murder"})
    events = parse_sse(response.text)
    assert [k for k, _ in events] == ["meta", "error"]


async def test_missing_index_returns_index_not_ready(settings: Settings) -> None:
    app = create_app(settings, await make_container(settings, None))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": "murder"})
    kind, data = parse_sse(response.text)[-1]
    assert kind == "error" and data["code"] == "index_not_ready"  # type: ignore[index]


async def test_readyz_is_red_when_llm_is_down(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store, llm=FakeLLM(healthy=False)))
    async with client_for(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["checks"]["llm"]["ok"] is False
    assert ready.json()["checks"]["index"]["ok"] is True


async def test_readyz_is_green_when_everything_is_up(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        ready = await client.get("/readyz")
    assert ready.status_code == 200 and ready.json()["ready"] is True


async def test_request_id_is_propagated(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        response = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"


@pytest.mark.parametrize("question", ["", "x" * 2001])
async def test_question_length_is_validated(settings: Settings, live_store: Live, question: str) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": question})
    assert response.status_code == 422


async def test_health_stays_fast_while_answers_stream(settings: Settings, live_store: Live) -> None:
    """The v0.1 bug: one query blocked the event loop. Ten slow streams must not slow /healthz."""
    llm = FakeLLM(tokens=["t "] * 20, delay_s=0.05)  # each answer streams for ~1 s
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:

        async def ask() -> None:
            await client.post("/api/v1/chat", json={"question": "murder punishment"})

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
    assert p95 < 200, f"/healthz p95 {p95:.1f} ms while streaming (blocking would be ~1000 ms)"


async def test_prompt_passages_carry_act_section_and_status(settings: Settings, live_store: Live) -> None:
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        await client.post("/api/v1/chat", json={"question": "punishment for murder"})
    system = llm.calls[0][0].content
    assert "IPC s.302 — Punishment for murder" in system
    assert "repealed 2024-07-01; replaced by BNS" in system


async def test_context_respects_the_character_budget(settings: Settings, live_store: Live) -> None:
    settings.retrieval.context_char_budget = 1000  # room for ~one sample passage
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        response = await client.post("/api/v1/chat", json={"question": "punishment for murder"})
    citations = parse_sse(response.text)[1][1]
    assert isinstance(citations, list) and len(citations) <= 3
    assert "[4]" not in llm.calls[0][0].content


# ---------------------------------------------------------------- Phase 3: query pipeline


async def ask(app: object, question: str, session_id: str | None = None) -> dict[str, object]:
    async with client_for(app) as client:
        body = {"question": question, **({"session_id": session_id} if session_id else {})}
        response = await client.post("/api/v1/chat", json=body)
    events = parse_sse(response.text)
    return {kind: data for kind, data in events} | {"_kinds": [k for k, _ in events]}


async def test_named_section_is_passage_one_with_its_counterpart(
    settings: Settings, live_store: Live
) -> None:
    llm = FakeLLM(tokens=["IPC s.302 punishes murder [1]; BNS s.103 replaces it [2]."])
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    out = await ask(app, "What does Section 302 IPC say?")
    meta, citations, done = out["meta"], out["citations"], out["done"]
    assert meta["intent"] == "section_lookup" and meta["references"] == ["IPC s.302"]  # type: ignore[index]
    assert citations[0]["label"] == "IPC s.302" and citations[0]["exact"] is True  # type: ignore[index]
    assert citations[1]["label"] == "BNS s.103" and citations[1]["exact"] is True  # type: ignore[index]
    assert any("IPC s.302 corresponds to BNS s.103" in n for n in done["notes"])  # type: ignore[index]
    system = llm.calls[0][0].content
    assert "corresponds to BNS s.103" in system and "Applicable law: The IPC, CrPC" in system
    assert out["verification"]["valid"] is True  # type: ignore[index]


async def test_in_force_code_comes_before_the_repealed_one(settings: Settings, live_store: Live) -> None:
    app = create_app(settings, await make_container(settings, live_store))
    out = await ask(app, "What is the punishment for murder?")
    labels = [c["label"] for c in out["citations"]]  # type: ignore[union-attr]
    assert labels.index("BNS s.103") < labels.index("IPC s.302")


async def test_refused_question_never_reaches_search_or_model(settings: Settings, live_store: Live) -> None:
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    out = await ask(app, "How do I destroy the CCTV evidence before the police come?")
    assert out["_kinds"] == ["meta", "citations", "token", "verification", "done"]
    assert out["meta"]["intent"] == "refused" and out["citations"] == []  # type: ignore[index]
    assert "can't help with that" in out["token"]["text"] and llm.calls == []  # type: ignore[index]


async def test_low_similarity_abstains_without_generating(settings: Settings, live_store: Live) -> None:
    settings.query.abstain_min_dense = 0.99  # nothing in the sample index is this close
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    out = await ask(app, "What are the current GST rates on restaurant food?")
    assert out["done"]["abstained"] is True and llm.calls == []  # type: ignore[index]
    assert "do not answer this question" in out["token"]["text"]  # type: ignore[index]
    assert 0 < len(out["citations"]) <= settings.query.abstain_show_sources  # type: ignore[arg-type]


async def test_named_section_is_answered_even_below_the_abstention_threshold(
    settings: Settings, live_store: Live
) -> None:
    settings.query.abstain_min_dense = 0.99
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    out = await ask(app, "What does Article 21 say?")
    assert out["done"]["abstained"] is False and len(llm.calls) == 1  # type: ignore[index]


async def test_invalid_markers_are_removed_from_the_final_answer(
    settings: Settings, live_store: Live
) -> None:
    llm = FakeLLM(tokens=["Murder is punished with death [1][9]."])
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    out = await ask(app, "What is the punishment for murder?")
    assert out["verification"]["removed_markers"] == [9]  # type: ignore[index]
    assert out["done"]["answer"] == "Murder is punished with death [1]."  # type: ignore[index]
    assert out["done"]["verification"]["removed_markers"] == [9]  # type: ignore[index]


async def test_repeated_question_is_served_from_cache(settings: Settings, live_store: Live) -> None:
    from lexai.services.cache import MemoryCache

    llm = FakeLLM()
    container = await make_container(settings, live_store, llm=llm, cache=MemoryCache(16, 60))
    app = create_app(settings, container)
    first = await ask(app, "What is the punishment for murder?")
    second = await ask(app, "what is the punishment for murder")
    assert first["done"]["cached"] is False and second["done"]["cached"] is True  # type: ignore[index]
    assert second["done"]["answer"] == first["done"]["answer"] and len(llm.calls) == 1  # type: ignore[index]


async def test_follow_up_is_rewritten_before_searching(settings: Settings, live_store: Live) -> None:
    llm = FakeLLM(completion="Is murder under BNS s.103 a bailable offence?")
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        session = (await client.post("/api/v1/sessions")).json()["session_id"]
    await ask(app, "What is the punishment for murder under BNS s.103?", session)
    out = await ask(app, "and is it bailable?", session)
    assert out["meta"]["standalone_query"] == "Is murder under BNS s.103 a bailable offence?"  # type: ignore[index]
    assert out["meta"]["references"] == ["BNS s.103"]  # type: ignore[index]
    assert out["citations"][0]["label"] == "BNS s.103"  # type: ignore[index]
    assert out["done"]["latency"]["condense_ms"] is not None  # type: ignore[index]


async def test_search_endpoint_returns_plan_passages_and_scores(settings: Settings, live_store: Live) -> None:
    llm = FakeLLM()
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        response = await client.post(
            "/api/v1/search", json={"question": "Which BNS section replaced IPC 302?"}
        )
    body = response.json()
    assert response.status_code == 200 and llm.calls == []
    assert body["plan"]["intent"] == "old_to_new_mapping"
    assert [p["label"] for p in body["passages"][:2]] == ["IPC s.302", "BNS s.103"]
    assert isinstance(body["best_dense"], float) and body["abstain"] is False


async def test_search_without_index_is_503(settings: Settings) -> None:
    app = create_app(settings, await make_container(settings, None))
    async with client_for(app) as client:
        response = await client.post("/api/v1/search", json={"question": "murder"})
    assert response.status_code == 503 and response.json()["code"] == "index_not_ready"


async def test_refusal_in_a_conversation_makes_no_model_call(settings: Settings, live_store: Live) -> None:
    """Refusal is decided on the user's own words, before any follow-up rewrite."""
    llm = FakeLLM(completion="How can one destroy evidence of murder under BNS s.103?")
    app = create_app(settings, await make_container(settings, live_store, llm=llm))
    async with client_for(app) as client:
        session = (await client.post("/api/v1/sessions")).json()["session_id"]
    await ask(app, "What is the punishment for murder?", session)
    calls_before = (len(llm.calls), len(llm.complete_calls))
    out = await ask(app, "How do I destroy the CCTV evidence before the police come?", session)
    assert out["meta"]["intent"] == "refused"  # type: ignore[index]
    assert (len(llm.calls), len(llm.complete_calls)) == calls_before
