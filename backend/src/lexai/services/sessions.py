"""
Conversation sessions: server-issued ids only.

A client may only use a session id that this server created (POST /api/v1/sessions);
an unknown or expired id raises `SessionNotFound` instead of being created on the
fly, so a guessed or leaked id cannot be used to write into a new conversation and
clients cannot grow memory without limit. Each session keeps its last
`max_messages` messages and expires `ttl_minutes` after its last use.

Two stores behind one interface: in-process (default; one API process) and Redis
(`LEXAI_REDIS_URL`; shared by several workers, survives API restarts).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from lexai.core.errors import SessionNotFound
from lexai.domain.models import ChatMessage


class SessionStore(Protocol):
    ttl_s: int

    async def create(self) -> str: ...
    async def exists(self, session_id: str) -> bool: ...
    async def history(self, session_id: str) -> list[ChatMessage]: ...
    async def append_turn(self, session_id: str, question: str, answer: str) -> None: ...
    async def count(self) -> int: ...
    async def aclose(self) -> None: ...


def _valid_format(session_id: str) -> bool:
    try:
        return str(uuid.UUID(session_id, version=4)) == session_id
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass
class _Session:
    messages: list[ChatMessage] = field(default_factory=list)
    last_active: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemorySessionStore:
    """In-process store with TTL and caps. Not shared between processes."""

    def __init__(self, ttl_minutes: int, max_messages: int, max_sessions: int) -> None:
        self.ttl_s = ttl_minutes * 60
        self._ttl = timedelta(minutes=ttl_minutes)
        self._max_messages = max_messages
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._lock = asyncio.Lock()

    async def create(self) -> str:
        session_id = str(uuid.uuid4())
        async with self._lock:
            self._evict_expired()
            self._sessions[session_id] = _Session()
            while len(self._sessions) > self._max_sessions:  # oldest-used first
                self._sessions.popitem(last=False)
        return session_id

    def _get(self, session_id: str) -> _Session:
        self._evict_expired()
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFound(f"session {session_id[:8]}... not found")
        session.last_active = datetime.now(UTC)
        self._sessions.move_to_end(session_id)
        return session

    async def exists(self, session_id: str) -> bool:
        async with self._lock:
            self._evict_expired()
            return session_id in self._sessions

    async def history(self, session_id: str) -> list[ChatMessage]:
        async with self._lock:
            return list(self._get(session_id).messages)

    async def append_turn(self, session_id: str, question: str, answer: str) -> None:
        async with self._lock:
            session = self._get(session_id)
            session.messages.extend(
                [ChatMessage(role="user", content=question), ChatMessage(role="assistant", content=answer)]
            )
            del session.messages[: -self._max_messages]

    async def count(self) -> int:
        async with self._lock:
            self._evict_expired()
            return len(self._sessions)

    async def aclose(self) -> None:
        return None

    def _evict_expired(self) -> None:
        cutoff = datetime.now(UTC) - self._ttl
        for sid in [s for s, v in self._sessions.items() if v.last_active < cutoff]:
            del self._sessions[sid]


class RedisSessionStore:
    """
    One Redis list per session (`lexai:session:<id>`, JSON messages) plus a marker key
    that exists from creation, so an empty new session is still known. Both keys get
    the sliding TTL on every use; LTRIM keeps the last `max_messages`.
    """

    PREFIX = "lexai:session:"

    def __init__(self, client: Any, ttl_minutes: int, max_messages: int) -> None:
        self._r = client
        self.ttl_s = ttl_minutes * 60
        self._max_messages = max_messages

    @classmethod
    def from_url(cls, url: str, ttl_minutes: int, max_messages: int) -> RedisSessionStore:
        import redis.asyncio as redis

        return cls(redis.from_url(url, decode_responses=True), ttl_minutes, max_messages)

    def _keys(self, session_id: str) -> tuple[str, str]:
        return f"{self.PREFIX}{session_id}:alive", f"{self.PREFIX}{session_id}:messages"

    async def create(self) -> str:
        session_id = str(uuid.uuid4())
        alive, _ = self._keys(session_id)
        await self._r.set(alive, "1", ex=self.ttl_s)
        return session_id

    async def exists(self, session_id: str) -> bool:
        if not _valid_format(session_id):
            return False
        return bool(await self._r.exists(self._keys(session_id)[0]))

    async def _touch(self, session_id: str) -> None:
        alive, messages = self._keys(session_id)
        if not _valid_format(session_id) or not await self._r.expire(alive, self.ttl_s):
            raise SessionNotFound(f"session {session_id[:8]}... not found")
        await self._r.expire(messages, self.ttl_s)

    async def history(self, session_id: str) -> list[ChatMessage]:
        await self._touch(session_id)
        raw = await self._r.lrange(self._keys(session_id)[1], 0, -1)
        return [ChatMessage.model_validate(json.loads(m)) for m in raw]

    async def append_turn(self, session_id: str, question: str, answer: str) -> None:
        await self._touch(session_id)
        _, messages = self._keys(session_id)
        turn = [ChatMessage(role="user", content=question), ChatMessage(role="assistant", content=answer)]
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.rpush(messages, *(m.model_dump_json() for m in turn))
            pipe.ltrim(messages, -self._max_messages, -1)
            pipe.expire(messages, self.ttl_s)
            await pipe.execute()

    async def count(self) -> int:
        n = 0
        async for _ in self._r.scan_iter(match=f"{self.PREFIX}*:alive", count=500):
            n += 1
        return n

    async def aclose(self) -> None:
        await self._r.aclose()
