"""
API routes: POST /api/query (SSE), POST /api/session, GET /api/health.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config.settings import get_settings
from app.models.schemas import HealthResponse, QueryRequest, SessionResponse
from app.services.qa_service import LegalQAService, get_qa_service

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def _sse_format(event: dict) -> str:
    """Wire-format a single SSE frame: `event: <type>\\ndata: <json>\\n\\n`."""
    return f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"


async def _sse_stream(qa_service: LegalQAService, question: str, session_id: str):
    async for event in qa_service.stream_answer(question, session_id):
        yield _sse_format(event)


@router.post("/query")
@limiter.limit(get_settings().rate_limit_query)
async def query(
    request: Request,
    body: QueryRequest,
    qa_service: LegalQAService = Depends(get_qa_service),
):
    """
    Streams a Server-Sent Events response. See app/services/qa_service.py
    module docstring for the event contract (citations -> token* -> done,
    or an error event if generation fails mid-stream).

    session_id: if omitted or unrecognised, a fresh session is created
    transparently (see SessionStore.get_history) and its id is included in
    the final 'done' event so the frontend can persist it for future turns.
    """
    session_id = body.session_id or qa_service.sessions.create_session()
    return StreamingResponse(
        _sse_stream(qa_service, body.question, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx response buffering, if fronted by nginx
        },
    )


@router.post("/session", response_model=SessionResponse)
@limiter.limit(get_settings().rate_limit_session)
async def create_session(request: Request, qa_service: LegalQAService = Depends(get_qa_service)):
    return SessionResponse(session_id=qa_service.sessions.create_session())


@router.get("/health", response_model=HealthResponse)
async def health(qa_service: LegalQAService = Depends(get_qa_service)):
    return HealthResponse(**qa_service.health_snapshot())
