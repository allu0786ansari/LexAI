"""
In-process session memory with TTL and size caps.

Single-process only: it does not survive restarts and is not shared between
workers. Phase 5 replaces it with Redis behind the same methods.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from lexai.domain.models import ChatMessage


@dataclass
class _Session:
    messages: list[ChatMessage] = field(default_factory=list)
    last_active: datetime = field(default_factory=lambda: datetime.now(UTC))


class SessionStore:
    def __init__(self, ttl_minutes: int, max_messages: int, max_sessions: int) -> None:
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_messages = max_messages
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.Lock()

    def create(self) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = _Session()
            self._enforce_cap()
        return session_id

    def history(self, session_id: str) -> list[ChatMessage]:
        self._evict_expired()
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._sessions[session_id] = _Session()
                self._enforce_cap()
            session.last_active = datetime.now(UTC)
            self._sessions.move_to_end(session_id)
            return list(session.messages)

    def append_turn(self, session_id: str, question: str, answer: str) -> None:
        with self._lock:
            session = self._sessions.setdefault(session_id, _Session())
            session.messages.extend(
                [ChatMessage(role="user", content=question), ChatMessage(role="assistant", content=answer)]
            )
            del session.messages[: -self._max_messages]
            session.last_active = datetime.now(UTC)

    @property
    def active_count(self) -> int:
        self._evict_expired()
        with self._lock:
            return len(self._sessions)

    def _evict_expired(self) -> None:
        cutoff = datetime.now(UTC) - self._ttl
        with self._lock:
            for sid in [s for s, v in self._sessions.items() if v.last_active < cutoff]:
                del self._sessions[sid]

    def _enforce_cap(self) -> None:
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
