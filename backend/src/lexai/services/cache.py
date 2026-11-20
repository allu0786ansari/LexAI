"""
Answer cache.

Keyed by the normalised standalone question plus everything that changes the
answer: index version, prompt version and model. Only answers that passed
verification are stored. In-process (LRU with TTL) by default; Redis when
LEXAI_CACHE_REDIS_URL is set, so several API workers share it (Phase 6).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Protocol

from lexai.core.logging import get_logger
from lexai.core.settings import CacheSettings

logger = get_logger(__name__)


class AnswerCache(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any]) -> None: ...
    async def aclose(self) -> None: ...


def normalise(question: str) -> str:
    text = question.lower().strip()
    text = re.sub(r"[\s]+", " ", text)
    return re.sub(r"[?.!\s]+$", "", text)


def cache_key(*parts: str) -> str:
    return "lexai:answer:" + hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class MemoryCache:
    def __init__(self, max_entries: int, ttl_s: int) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    async def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires, raw = item
            if expires <= time.monotonic():
                del self._items[key]
                return None
            self._items.move_to_end(key)
        value: dict[str, Any] = json.loads(raw)
        return value

    async def set(self, key: str, value: dict[str, Any]) -> None:
        if self._max == 0 or self._ttl <= 0:  # disabled
            return
        raw = json.dumps(value, ensure_ascii=False)
        with self._lock:
            self._items[key] = (time.monotonic() + self._ttl, raw)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    async def aclose(self) -> None:
        return None


class RedisCache:
    def __init__(self, url: str, ttl_s: int) -> None:
        import redis.asyncio as redis  # optional dependency path; imported only when configured

        self._client = redis.from_url(url, decode_responses=True)
        self._ttl = ttl_s

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(key)
        except Exception as exc:  # a cache outage must not fail the answer
            logger.warning("cache_get_failed", error=repr(exc))
            return None
        if not raw:
            return None
        value: dict[str, Any] = json.loads(raw)
        return value

    async def set(self, key: str, value: dict[str, Any]) -> None:
        try:
            await self._client.set(key, json.dumps(value, ensure_ascii=False), ex=self._ttl)
        except Exception as exc:
            logger.warning("cache_set_failed", error=repr(exc))

    async def aclose(self) -> None:
        await self._client.aclose()


def build_cache(settings: CacheSettings) -> AnswerCache | None:
    if not settings.enabled:
        return None
    if settings.redis_url:
        return RedisCache(settings.redis_url, settings.ttl_s)
    return MemoryCache(settings.max_entries, settings.ttl_s)
