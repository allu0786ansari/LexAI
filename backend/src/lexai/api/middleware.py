"""
Pure-ASGI middleware (safe with streamed responses):
- request id: assigned, bound to logs, returned as `X-Request-ID`;
- body size limit: 413 above `LEXAI_API_MAX_BODY_BYTES`;
- security headers on every response;
- observability: request metrics per route template; request id on the HTTP span.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from lexai.core.logging import bind_request_context, clear_request_context
from lexai.core.metrics import HTTP_DURATION, HTTP_REQUESTS

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        bind_request_context(request_id=request_id, path=scope.get("path", ""))

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            clear_request_context()


# --------------------------------------------------------------------------- body size


class BodySizeLimitMiddleware:
    """
    Refuse request bodies over `max_bytes` with 413. A declared Content-Length is
    checked up front; otherwise the body (small by definition) is read here first and
    replayed to the app, because errors raised while the framework reads the body
    would be turned into a 400.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit():
            if int(declared) > self.max_bytes:
                await _too_large(scope, send, self.max_bytes)
                return
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await _too_large(scope, send, self.max_bytes)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


async def _too_large(scope: Scope, send: Send, limit: int) -> None:
    request_id = (scope.get("state") or {}).get("request_id", "")
    body = json.dumps(
        {
            "code": "request_too_large",
            "message": f"Request body over {limit} bytes.",
            "request_id": request_id,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


# --------------------------------------------------------------------------- security headers

_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-site"),
]
# The API returns JSON, SSE and PDFs: nothing it serves needs scripts, styles or frames.
_API_CSP = (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'")
_DOCS = ("/docs", "/redoc", "/openapi.json")  # Swagger UI loads its own scripts


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        docs = str(scope.get("path", "")).startswith(_DOCS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_HEADERS)
                if not docs:
                    headers.append(_API_CSP)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# --------------------------------------------------------------------------- metrics + tracing


class ObservabilityMiddleware:
    """
    Request metrics per route template (never the raw path), and the request id on the
    HTTP server span that FastAPI's built-in OpenTelemetry support opens.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in ("/metrics", "/healthz"):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        status = {"code": 500}
        start = time.perf_counter()
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("lexai.request_id", (scope.get("state") or {}).get("request_id", ""))

        async def send_with_status(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status["code"])).inc()
            HTTP_DURATION.labels(method=method, route=route).observe(time.perf_counter() - start)
