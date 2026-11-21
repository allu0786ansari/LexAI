"""
HTTP API v1 (`/api/v1/...`) plus health and metrics.

Every error is JSON `{code, message, request_id}` with a meaningful status:
404 unknown session/chunk/document, 401 bad admin key, 409 job running,
413 body too large (middleware), 422 invalid request, 429 rate limited (with
`Retry-After`), 503 model server or index unavailable.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from lexai.api.container import Container
from lexai.core.errors import Forbidden, LexAIError, SessionNotFound
from lexai.core.logging import bind_request_context, get_logger
from lexai.core.metrics import FEEDBACK, RATE_LIMITED, render
from lexai.domain.models import (
    ChatEvents,
    ChatFilters,
    ChatRequest,
    ChunkResponse,
    DocumentInfo,
    ErrorResponse,
    FeedbackRequest,
    FeedbackResponse,
    ReindexResponse,
    SearchRequest,
    SessionResponse,
    StreamEvent,
)
from lexai.retrieval.qdrant_store import SearchFilter
from lexai.services.jobs import JobBusy

logger = get_logger(__name__)

ApiKey = Annotated[str | None, Header(description="Admin API key (LEXAI_API_ADMIN_API_KEY)")]
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class RateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after


class Conflict(Exception):
    pass


def sse_frame(event: StreamEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def _container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def _client_ip(request: Request) -> str:
    # Behind a proxy, run uvicorn with --proxy-headers --forwarded-allow-ips so this is the client.
    return request.client.host if request.client else "unknown"


async def _limit(request: Request, route: str, limit: str, *keys: str) -> None:
    wait = await _container(request).limiter.hit(limit, *(f"{route}:{k}" for k in keys))
    if wait is not None:
        RATE_LIMITED.labels(route=route).inc()
        raise RateLimited(wait)


def _filters(f: ChatFilters | None) -> SearchFilter | None:
    if f is None or not (f.acts or f.in_force_only):
        return None
    return SearchFilter(short_names=f.acts or None, in_force_only=f.in_force_only)


async def _session(container: Container, session_id: str | None) -> str:
    """The client's server-issued session, or a new one when none is given."""
    if session_id is None:
        return await container.sessions.create()
    if not await container.sessions.exists(session_id):
        raise SessionNotFound()
    return session_id


_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (404, 422, 429, 503)
}


def build_router(metrics_enabled: bool = True) -> APIRouter:
    router = APIRouter()
    v1 = APIRouter(prefix="/api/v1", responses=_ERRORS)

    # ------------------------------------------------------------------ health and metrics

    @router.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up and its event loop is responsive."""
        return {"status": "ok"}

    @router.get("/readyz", tags=["health"])
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: model servers reachable, models served, index loaded."""
        ready, checks = await _container(request).readiness()
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    if metrics_enabled:

        @router.get("/metrics", tags=["health"], include_in_schema=False)
        async def metrics() -> Response:
            body, content_type = render()
            return Response(body, media_type=content_type)

    # ------------------------------------------------------------------ sessions and chat

    @v1.post("/sessions", status_code=201, tags=["chat"])
    async def create_session(request: Request) -> SessionResponse:
        """A new conversation. Only ids issued here are accepted by /chat and /search."""
        container = _container(request)
        await _limit(request, "sessions", container.settings.api.rate_limit_sessions, _client_ip(request))
        return SessionResponse(
            session_id=await container.sessions.create(), expires_in_s=container.sessions.ttl_s
        )

    @v1.post(
        "/chat",
        tags=["chat"],
        response_model=ChatEvents,
        responses={
            200: {
                "description": "Server-Sent Events; each payload is described in ChatEvents.",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def chat(request: Request, body: ChatRequest) -> StreamingResponse:
        """Ask a question; the answer streams as Server-Sent Events (contract v1)."""
        container = _container(request)
        limit = container.settings.api.rate_limit_chat
        session_id = await _session(container, body.session_id)
        await _limit(request, "chat", limit, f"ip:{_client_ip(request)}", f"session:{session_id}")
        request_id: str = request.state.request_id
        bind_request_context(session_id=session_id)
        chat_service = container.chat  # read now: a reindex swaps it for later requests

        async def stream() -> AsyncIterator[str]:
            events = chat_service.stream_answer(body.question, session_id, request_id, _filters(body.filters))
            try:
                async for event in events:
                    if await request.is_disconnected():
                        logger.info("client_disconnected", request_id=request_id)
                        break
                    yield sse_frame(event)
            finally:
                await events.aclose()  # stops generation upstream if the client left

        return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    @v1.post("/search", tags=["chat"])
    async def search(request: Request, body: SearchRequest) -> dict[str, Any]:
        """Retrieval only: the plan and the passages the model would be given (no answer)."""
        container = _container(request)
        if body.session_id is not None:
            await _session(container, body.session_id)
        await _limit(request, "search", container.settings.api.rate_limit_search, f"ip:{_client_ip(request)}")
        return await container.chat.search(body.question, body.session_id, _filters(body.filters))

    # ------------------------------------------------------------------ sources

    @v1.get("/chunks/{chunk_id}", tags=["sources"])
    async def get_chunk(request: Request, chunk_id: str) -> ChunkResponse:
        """A cited passage: the whole provision's text, its metadata and a link to the PDF page."""
        found = await _container(request).chunk(chunk_id)
        if found is None:
            raise _NotFound("chunk_not_found", "No passage with this id in the live index.")
        return found

    @v1.get("/documents", tags=["sources"])
    async def documents(request: Request) -> list[DocumentInfo]:
        """The corpus: every document, its type, legal status and number of indexed passages."""
        return _container(request).documents

    @v1.get(
        "/documents/{doc_id}/pdf",
        tags=["sources"],
        response_class=FileResponse,
        responses={200: {"content": {"application/pdf": {}}}},
    )
    async def document_pdf(request: Request, doc_id: str) -> FileResponse:
        """The source PDF (append `#page=N` in the browser to open a page)."""
        path = _container(request).pdf_path(doc_id)
        if path is None:
            raise _NotFound("document_not_found", "No PDF for this document.")
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{doc_id}.pdf"'},
        )

    # ------------------------------------------------------------------ feedback

    @v1.post("/feedback", status_code=201, tags=["feedback"])
    async def feedback(request: Request, body: FeedbackRequest) -> FeedbackResponse:
        """Thumbs up/down on an answer, by its request id, with an optional comment."""
        container = _container(request)
        await _limit(request, "feedback", container.settings.api.rate_limit_feedback, _client_ip(request))
        assert container.feedback is not None
        row = await container.feedback.add(body.request_id, body.rating, body.comment)
        FEEDBACK.labels(rating=body.rating).inc()
        logger.info("feedback", feedback_request_id=body.request_id, rating=body.rating)
        return FeedbackResponse(id=row)

    # ------------------------------------------------------------------ admin

    def _admin(request: Request, key: str | None) -> Container:
        container = _container(request)
        expected = container.settings.api.admin_api_key
        if (
            expected is None
            or not key
            or not hmac.compare_digest(key.encode(), expected.get_secret_value().encode())
        ):
            raise Forbidden()
        return container

    @v1.post("/admin/reindex", status_code=202, tags=["admin"], responses={401: {"model": ErrorResponse}})
    async def reindex(
        request: Request,
        x_api_key: ApiKey = None,
        only: Annotated[list[str] | None, Body(embed=True, description="Document ids; omitted = all")] = None,
    ) -> ReindexResponse:
        """Start an ingestion build in the background; the API switches to the new index when it goes live."""
        container = _admin(request, x_api_key)
        try:
            return container.jobs.start(lambda: container.reindex(only))
        except JobBusy as exc:
            raise Conflict() from exc

    @v1.get("/admin/reindex/{job_id}", tags=["admin"], responses={401: {"model": ErrorResponse}})
    async def reindex_status(request: Request, job_id: str, x_api_key: ApiKey = None) -> ReindexResponse:
        container = _admin(request, x_api_key)
        job = container.jobs.jobs.get(job_id)
        if job is None:
            raise _NotFound("job_not_found", "No such job.")
        return job

    router.include_router(v1)
    return router


class _NotFound(LexAIError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
