"""
FastAPI application entry point: CORS, rate limiting, structured error
handling, and startup preloading of the (expensive to build) QA service
singleton.
"""
from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes import limiter, router
from app.config.logging import configure_logging, get_logger
from app.config.settings import get_settings
from app.services.qa_service import get_qa_service

settings = get_settings()
configure_logging(settings)
logger = get_logger(__name__)

app = FastAPI(title="LexAI - Legal Chatbot API")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def preload_qa_service() -> None:
    """
    Build the QA service singleton once at startup rather than on the
    first request: it loads the FAISS index, the BM25 index, and (if
    enabled) the CrossEncoder model. Doing this lazily on first request
    would make one unlucky user eat all of that latency; doing it here
    means the health check only goes green once the service can actually
    serve a query.
    """
    logger.info("startup_preloading_qa_service")
    get_qa_service()
    logger.info("startup_qa_service_ready")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Unlike the old handler (which returned `str(exc)` directly to the
    client - an information-leak antipattern that can expose stack
    internals, file paths, or API error details), this logs the full
    exception server-side with a correlation id and returns only that id
    to the client.
    """
    request_id = str(uuid.uuid4())
    logger.error(
        "unhandled_exception",
        request_id=request_id,
        path=str(request.url.path),
        error=str(exc),
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": "An unexpected error occurred. If this persists, please report it.",
            "request_id": request_id,
        },
    )


@app.get("/")
def read_root():
    return {"message": "LexAI - Legal Chatbot API", "docs": "/docs"}


app.include_router(router, prefix="/api")
