"""FastAPI application factory. Run with: `uvicorn lexai.api.app:create_app --factory`."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from lexai import __version__
from lexai.api.container import Container
from lexai.api.middleware import (
    BodySizeLimitMiddleware,
    ObservabilityMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from lexai.api.routes import Conflict, RateLimited, build_router
from lexai.core.errors import LexAIError
from lexai.core.logging import configure_logging, get_logger
from lexai.core.metrics import ACTIVE_SESSIONS, READY
from lexai.core.settings import Settings, env_profile, get_settings
from lexai.core.telemetry import configure_tracing, flush

logger = get_logger(__name__)

_STATUS = {
    "session_not_found": 404,
    "chunk_not_found": 404,
    "document_not_found": 404,
    "job_not_found": 404,
    "forbidden": 401,
    "index_not_ready": 503,
    "upstream_unavailable": 503,
    "index_mismatch": 503,
}


def _error(
    request: Request, status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "request_id": request_id},
        headers=headers,
    )


async def _probe(container: Container, interval: float) -> None:
    """Keeps the lexai_ready and lexai_active_sessions gauges current for dashboards and alerts."""
    while True:
        try:
            ready, _ = await container.readiness()
            READY.set(1 if ready else 0)
            ACTIVE_SESSIONS.set(await container.sessions.count())
        except Exception as exc:  # never let the probe kill the app
            READY.set(0)
            logger.warning("readiness_probe_failed", error=repr(exc))
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """`container` lets tests inject fakes; in normal runs it is built during start-up."""
    settings = settings or get_settings()
    configure_logging(settings.api)
    configure_tracing(settings.obs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await Container.build(settings)
        probe = None
        if settings.api.readiness_probe_s > 0:
            probe = asyncio.create_task(_probe(app.state.container, settings.api.readiness_probe_s))
        logger.info(
            "startup_complete",
            version=__version__,
            profile=env_profile(),
            llm=settings.llm.model,
            embedder=settings.embed.model,
            prompt=settings.llm.prompt_version,
            notes=app.state.container.notes,
        )
        try:
            yield
        finally:
            if probe is not None:
                probe.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await probe
            flush()
            if owned:
                await app.state.container.aclose()

    docs = settings.api.environment != "production"
    app = FastAPI(
        title="LexAI API",
        version=__version__,
        description="Legal research assistant for Indian law: grounded, cited answers over a fixed corpus.",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
        # FastAPI's own OpenTelemetry HTTP server span is kept (standard attributes); its
        # per-dependency spans, OTel metrics and logs are off: Prometheus and structlog cover them.
        telemetry={
            "operation_spans": False,
            "metrics": False,
            "logs": False,
            "exclude": lambda scope: scope.get("path") in ("/healthz", "/metrics", "/readyz"),
        },
    )
    if container is not None:
        app.state.container = container  # also available without running lifespan (tests)

    # The last added runs first. Inbound order: request id -> security headers -> metrics/trace
    # -> CORS -> body size limit -> routes, so even a 413 carries the id and the headers.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.api.max_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )
    app.add_middleware(ObservabilityMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(LexAIError)
    async def lexai_error(request: Request, exc: LexAIError) -> JSONResponse:
        status = _STATUS.get(exc.code, 500)
        log = logger.warning if status < 500 else logger.error
        log("request_failed", code=exc.code, status=status, error=str(exc))
        return _error(request, status, exc.code, exc.public_message)

    @app.exception_handler(RateLimited)
    async def rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        return _error(
            request,
            429,
            "rate_limited",
            f"Too many requests. Retry in {exc.retry_after} s.",
            {"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return _error(request, 409, "job_running", "A reindex job is already running.")

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return _error(
            request, 422, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}".strip(": ")
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error=repr(exc))
        return _error(request, 500, "internal_error", "An unexpected error occurred.")

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "LexAI API", "version": __version__, "docs": "/docs" if docs else ""}

    app.include_router(build_router(settings.obs.metrics_enabled))
    return app
