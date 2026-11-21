"""
Async client for any OpenAI-compatible chat endpoint (Ollama `/v1`, vLLM).

Written on `httpx` directly rather than the `openai` SDK: the surface we need is
two endpoints, and owning it keeps streaming, retries and timeouts explicit and
easy to test.

Retry rule: connection errors, timeouts, 429 and 5xx are retried with
exponential backoff, but only before the first token has been yielded. Once
text has reached the user, a retry would duplicate it, so a later failure is
raised as `GenerationError`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from lexai.core.errors import GenerationError, UpstreamUnavailable
from lexai.core.logging import get_logger
from lexai.core.settings import LLMSettings
from lexai.domain.models import ChatMessage

logger = get_logger(__name__)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):  # connect errors, read timeouts, dropped connections
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return False


class LLMClient:
    def __init__(self, settings: LLMSettings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_s,
                read=settings.read_timeout_s,
                write=30.0,
                pool=10.0,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _payload(
        self,
        messages: Sequence[ChatMessage],
        *,
        stream: bool,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        # Sampling parameters are always sent explicitly: vLLM's server defaults
        # are not tuned for Qwen2.5 and can cause repetition.
        return {
            "model": self.settings.model,
            "messages": [m.as_openai() for m in messages],
            "stream": stream,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "top_p": self.settings.top_p,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
        }

    async def _backoff(self, attempt: int, exc: BaseException) -> None:
        delay = min(8.0, 0.5 * (2**attempt))
        logger.warning("llm_retry", attempt=attempt + 1, delay_s=delay, error=repr(exc))
        await asyncio.sleep(delay)

    async def stream_chat(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        usage: dict[str, int] | None = None,
    ) -> AsyncIterator[str]:
        """
        Yield text deltas as the server produces them. If `usage` is given it is filled
        with the server's token counts (prompt/completion/total) when it reports them.
        """
        payload = self._payload(messages, stream=True, temperature=temperature, max_tokens=max_tokens)
        # OpenAI-style servers (vLLM, recent Ollama) then send a final chunk with token
        # counts; servers that do not support the option ignore it.
        payload["stream_options"] = {"include_usage": True}
        attempt = 0
        while True:
            emitted = False
            try:
                async with self._client.stream("POST", "chat/completions", json=payload) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        response.raise_for_status()
                    async for line in response.aiter_lines():
                        if usage is not None:
                            _capture_usage(line, usage)
                        delta = _parse_sse_line(line)
                        if delta is None:
                            continue
                        if delta is _DONE:
                            return
                        emitted = True
                        yield str(delta)
                return
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if emitted:
                    raise GenerationError(
                        f"stream interrupted after first token: {exc!r}", service="llm"
                    ) from exc
                if _is_retryable(exc) and attempt < self.settings.max_retries:
                    await self._backoff(attempt, exc)
                    attempt += 1
                    continue
                if isinstance(exc, httpx.HTTPStatusError) and not _is_retryable(exc):
                    raise GenerationError(
                        f"LLM rejected the request ({exc.response.status_code}): {exc.response.text[:300]}",
                        service="llm",
                    ) from exc
                raise UpstreamUnavailable(
                    f"LLM unreachable at {self.settings.base_url}: {exc!r}", service="llm"
                ) from exc

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Non-streaming call, used for short auxiliary prompts (e.g. HyDE)."""
        payload = self._payload(messages, stream=False, temperature=temperature, max_tokens=max_tokens)
        attempt = 0
        while True:
            try:
                response = await self._client.post("chat/completions", json=payload)
                response.raise_for_status()
                body = response.json()
                return str(body["choices"][0]["message"]["content"] or "")
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if _is_retryable(exc) and attempt < self.settings.max_retries:
                    await self._backoff(attempt, exc)
                    attempt += 1
                    continue
                raise UpstreamUnavailable(f"LLM call failed: {exc!r}", service="llm") from exc
            except (KeyError, IndexError, ValueError) as exc:
                raise GenerationError(f"Malformed LLM response: {exc!r}", service="llm") from exc

    async def health(self) -> dict[str, Any]:
        """Check the server is up and serves the configured model."""
        response = await self._client.get("models", timeout=5.0)
        response.raise_for_status()
        models = [m.get("id") for m in response.json().get("data", [])]
        served = self.settings.model in models or f"{self.settings.model}:latest" in models
        return {"reachable": True, "model": self.settings.model, "model_available": served}


_DONE = object()


def _capture_usage(line: str, sink: dict[str, int]) -> None:
    if '"usage"' not in line:
        return
    try:
        counts = json.loads(line.strip()[5:].strip()).get("usage") or {}
    except (json.JSONDecodeError, AttributeError):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if isinstance(counts.get(key), int):
            sink[key] = counts[key]


def _parse_sse_line(line: str) -> object | None:
    """Return the text delta in one `data:` line, `_DONE` at the end, or None to skip."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return _DONE
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        logger.warning("llm_bad_sse_line", line=line[:200])
        return None
    choices = chunk.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("delta") or {}).get("content")
    return content if content else None
