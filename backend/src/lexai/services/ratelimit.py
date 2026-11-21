"""
Request rate limits per client IP and per session, in memory or in Redis.

Uses the `limits` library's moving window. A refused request gets the number of
seconds until the window frees a slot, sent to the client as `Retry-After`.
"""

from __future__ import annotations

import math
import time

from limits import parse
from limits.aio.strategies import MovingWindowRateLimiter
from limits.storage import storage_from_string


def storage_uri(redis_url: str | None) -> str:
    if not redis_url:
        return "async+memory://"
    return redis_url if redis_url.startswith("async+") else f"async+{redis_url}"


class RateLimiter:
    def __init__(self, redis_url: str | None = None) -> None:
        self.backend = "redis" if redis_url else "memory"
        options = {"implementation": "redispy"} if redis_url else {}  # the `redis` package, not coredis
        self._limiter = MovingWindowRateLimiter(storage_from_string(storage_uri(redis_url), **options))

    async def hit(self, limit: str, *keys: str) -> int | None:
        """Count one request against `limit` for each key: None if allowed, else seconds to wait."""
        item = parse(limit)
        for key in keys:
            if not await self._limiter.hit(item, key):
                stats = await self._limiter.get_window_stats(item, key)
                return max(1, math.ceil(stats.reset_time - time.time()))
        return None
