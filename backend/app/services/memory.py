"""
Per-session conversation memory.

Matches the proposal exactly: "Each user gets a UUID on first request. A
ConversationBufferMemory object is stored in a server-side dict keyed by
UUID... Memory is cleared after 30 minutes of inactivity."

`InMemoryChatMessageHistory` (langchain_core) is the direct modern
equivalent of the old `ConversationBufferMemory` — a plain in-memory
message list — and is what `RunnableWithMessageHistory`-style LCEL chains
expect, so we use it directly rather than reinventing message storage.

LIMITATION (documented, not hidden): this is a single-process in-memory
store. It does not survive a restart and is not shared across multiple
uvicorn workers. That's the correct tradeoff for a single-container HF
Spaces deployment (see proposal §4.2, Memory row) — a multi-worker
production deployment would swap this for a Redis-backed store behind the
same `get_history()` / `touch()` interface.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from langchain_core.chat_history import InMemoryChatMessageHistory

from app.config.logging import get_logger

logger = get_logger(__name__)


@dataclass
class _SessionEntry:
    history: InMemoryChatMessageHistory = field(default_factory=InMemoryChatMessageHistory)
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionStore:
    """Thread-safe (for a single-process asyncio app) session memory store."""

    def __init__(self, ttl_minutes: int):
        self._ttl = timedelta(minutes=ttl_minutes)
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = threading.Lock()

    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = _SessionEntry()
        logger.info("session_created", session_id=session_id)
        return session_id

    def get_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """
        Returns the message history for a session, transparently creating
        one if the id is unknown or expired (e.g. the frontend's
        localStorage UUID outlived the server's in-memory record). This
        favors availability over strict session validation — a legal Q&A
        chatbot losing prior turn context is a minor UX blip, not a
        correctness or security issue worth hard-failing the request over.
        """
        self._evict_expired()
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                logger.info("session_not_found_creating_new", requested_session_id=session_id)
                entry = _SessionEntry()
                self._sessions[session_id] = entry
            entry.last_active = datetime.now(timezone.utc)
            return entry.history

    def touch(self, session_id: str) -> None:
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is not None:
                entry.last_active = datetime.now(timezone.utc)

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _evict_expired(self) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = [sid for sid, entry in self._sessions.items() if now - entry.last_active > self._ttl]
            for sid in expired:
                del self._sessions[sid]
        if expired:
            logger.info("sessions_expired", count=len(expired))

    @property
    def active_session_count(self) -> int:
        self._evict_expired()
        with self._lock:
            return len(self._sessions)
