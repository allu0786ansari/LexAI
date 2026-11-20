"""FastAPI application factory. Run with: `uvicorn lexai.api.app:create_app --factory`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from lexai import __version__
from lexai.api.container import Container
from lexai.api.middleware import RequestContextMiddleware
from lexai.api.routes import build_router
from lexai.core.errors import LexAIError
from lexai.core.logging import configure_logging, get_logger
from lexai.core.settings import Settings, get_settings

logger = get_logger(__name__)


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """
    `container` lets tests inject fakes; in normal runs it is built during
    start-up from `settings`.
    """
    settings = settings or get_settings()
    configure_logging(settings.api)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = container is None
        app.state.container = container or await Container.build(settings)
        logger.info(
            "startup_complete",
            version=__version__,
            llm=settings.llm.model,
            embedder=settings.embed.model,
            notes=app.state.container.notes,
        )
        try:
            yield
        finally:
            if owned:
                await app.state.container.aclose()

    app = FastAPI(title="LexAI API", version=__version__, lifespan=lifespan)
    if container is not None:
        app.state.container = container  # also available without running lifespan (tests)

    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    @app.exception_handler(LexAIError)
    async def lexai_error_handler(request: Request, exc: LexAIError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error("request_failed", code=exc.code, error=str(exc))
        return JSONResponse(
            status_code=503,
            content={"error": exc.code, "detail": exc.public_message, "request_id": request_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception("unhandled_exception", error=repr(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": "An unexpected error occurred.",
                "request_id": request_id,
            },
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"name": "LexAI API", "version": __version__, "docs": "/docs"}

    app.include_router(build_router(limiter, settings.api.rate_limit_query, settings.api.rate_limit_session))
    return app
