"""HTTP routes. Paths under /api are unchanged from v0.1 so the current frontend keeps working."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from slowapi import Limiter

from lexai.api.container import Container
from lexai.core.errors import LexAIError
from lexai.core.logging import bind_request_context, get_logger
from lexai.domain.models import QueryRequest, SearchRequest, SessionResponse, StreamEvent

logger = get_logger(__name__)


def sse_frame(event: StreamEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def _container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def build_router(limiter: Limiter, query_limit: str, session_limit: str) -> APIRouter:
    router = APIRouter()

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up and its event loop is responsive."""
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: model servers reachable, models served, index loaded."""
        ready, checks = await _container(request).readiness()
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    @router.get("/api/health")
    async def legacy_health(request: Request) -> JSONResponse:
        return await readyz(request)

    @router.post("/api/session", response_model=SessionResponse)
    @limiter.limit(session_limit)
    async def create_session(request: Request) -> SessionResponse:
        return SessionResponse(session_id=_container(request).sessions.create())

    @router.post("/api/query")
    @limiter.limit(query_limit)
    async def query(request: Request, body: QueryRequest) -> StreamingResponse:
        container = _container(request)
        session_id = body.session_id or container.sessions.create()
        request_id: str = request.state.request_id
        bind_request_context(session_id=session_id)

        async def stream() -> AsyncIterator[str]:
            events = container.chat.stream_answer(body.question, session_id, request_id)
            try:
                async for event in events:
                    if await request.is_disconnected():
                        logger.info("client_disconnected", request_id=request_id)
                        break
                    yield sse_frame(event)
            finally:
                await events.aclose()  # stops generation upstream if the client left

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/search")
    @limiter.limit(query_limit)
    async def search(request: Request, body: SearchRequest) -> JSONResponse:
        """Retrieval only: what the model would be given for this question (no answer)."""
        container = _container(request)
        try:
            result = await container.chat.search(body.question, body.session_id)
        except LexAIError as exc:
            status = 503 if exc.code in ("index_not_ready", "upstream_unavailable") else 500
            return JSONResponse(
                {"code": exc.code, "message": exc.public_message, "request_id": request.state.request_id},
                status_code=status,
            )
        return JSONResponse(result)

    return router
