from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from lexai.clients import llm as llm_module
from lexai.clients.llm import LLMClient
from lexai.core.errors import GenerationError, UpstreamUnavailable
from lexai.core.settings import LLMSettings
from lexai.domain.models import ChatMessage

BASE = "http://llm.test/v1"
URL = f"{BASE}/chat/completions"
MESSAGES = [ChatMessage(role="user", content="hi")]


def sse(*deltas: str, done: bool = True) -> bytes:
    lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": d}, "index": 0}]}) + "\n\n" for d in deltas
    ]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


@pytest.fixture
def client() -> LLMClient:
    return LLMClient(LLMSettings(base_url=BASE, model="qwen2.5-7b-instruct", max_retries=2))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(llm_module.asyncio, "sleep", instant)


async def collect(client: LLMClient) -> list[str]:
    return [t async for t in client.stream_chat(MESSAGES)]


@respx.mock
async def test_streams_deltas_in_order_and_sends_sampling_params(client: LLMClient) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(200, content=sse("Hel", "lo", " world")))
    assert await collect(client) == ["Hel", "lo", " world"]
    body = json.loads(route.calls[0].request.content)
    assert body["stream"] is True
    assert body["model"] == "qwen2.5-7b-instruct"
    assert {"temperature", "top_p", "max_tokens"} <= body.keys()


@respx.mock
async def test_retries_server_error_before_first_token(client: LLMClient) -> None:
    route = respx.post(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, content=sse("ok"))])
    assert await collect(client) == ["ok"]
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_retries_when_unreachable(client: LLMClient) -> None:
    route = respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamUnavailable):
        await collect(client)
    assert route.call_count == 3  # first try + 2 retries


@respx.mock
async def test_does_not_retry_client_errors(client: LLMClient) -> None:
    route = respx.post(URL).mock(return_value=httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(GenerationError):
        await collect(client)
    assert route.call_count == 1


@respx.mock
async def test_never_retries_after_first_token(client: LLMClient) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield sse("partial", done=False)
        raise httpx.ReadError("connection dropped")

    route = respx.post(URL).mock(return_value=httpx.Response(200, content=broken()))
    received: list[str] = []
    with pytest.raises(GenerationError):
        async for token in client.stream_chat(MESSAGES):
            received.append(token)
    assert received == ["partial"]
    assert route.call_count == 1


@respx.mock
async def test_health_reports_whether_model_is_served(client: LLMClient) -> None:
    respx.get(f"{BASE}/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "qwen2.5-7b-instruct:latest"}]})
    )
    health = await client.health()
    assert health["model_available"] is True
