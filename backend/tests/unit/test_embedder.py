from __future__ import annotations

import httpx
import pytest
import respx

from lexai.clients import embedder as embedder_module
from lexai.clients.embedder import EmbeddingClient
from lexai.core.errors import ConfigurationError, UpstreamUnavailable
from lexai.core.settings import EmbedSettings

BASE = "http://embed.test/v1"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_: float) -> None:
        return None

    monkeypatch.setattr(embedder_module.asyncio, "sleep", instant)


def client(dim: int = 3, batch: int = 2) -> EmbeddingClient:
    return EmbeddingClient(EmbedSettings(base_url=BASE, model="m", dimension=dim, batch_size=batch))


def reply(request: httpx.Request) -> httpx.Response:
    import json

    inputs = json.loads(request.content)["input"]
    # Deliberately return items out of order: the client must sort by index.
    data = [{"index": i, "embedding": [float(i), 1.0, 0.0]} for i in range(len(inputs))][::-1]
    return httpx.Response(200, json={"data": data})


@respx.mock
async def test_batches_and_keeps_input_order() -> None:
    route = respx.post(f"{BASE}/embeddings").mock(side_effect=reply)
    vectors = await client().embed_documents(["a", "b", "c"])
    assert route.call_count == 2  # batch size 2
    assert [v[0] for v in vectors] == [0.0, 1.0, 0.0]


@respx.mock
async def test_failure_raises_instead_of_falling_back() -> None:
    respx.post(f"{BASE}/embeddings").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(UpstreamUnavailable):
        await client().embed_documents(["a"])


@respx.mock
async def test_wrong_dimension_is_a_configuration_error() -> None:
    respx.post(f"{BASE}/embeddings").mock(side_effect=reply)
    with pytest.raises(ConfigurationError):
        await client(dim=1024).embed_documents(["a"])


def test_prefixes_are_applied_to_queries_and_documents_separately() -> None:
    c = EmbeddingClient(
        EmbedSettings(base_url=BASE, query_prefix="search_query: ", document_prefix="search_document: ")
    )
    assert c.format_query("bail?") == "search_query: bail?"
    assert c.format_document("Section 436") == "search_document: Section 436"


@respx.mock
async def test_document_prefix_is_sent_to_the_server() -> None:
    import json

    route = respx.post(f"{BASE}/embeddings").mock(side_effect=reply)
    c = EmbeddingClient(EmbedSettings(base_url=BASE, model="m", dimension=3, document_prefix="doc: "))
    await c.embed_documents(["a"])
    assert json.loads(route.calls[0].request.content)["input"] == ["doc: a"]


@respx.mock
async def test_health_probe_uses_the_query_prefix() -> None:
    """nomic-embed-text and similar models expect a prefix on every input, probes included."""
    import json

    route = respx.post(f"{BASE}/embeddings").mock(side_effect=reply)
    c = EmbeddingClient(EmbedSettings(base_url=BASE, model="m", dimension=3, query_prefix="search_query: "))
    assert (await c.health())["dimension_ok"] is True
    assert json.loads(route.calls[0].request.content)["input"] == ["search_query: health probe"]
