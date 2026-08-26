from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    request_count: int
    limit: int
    window_seconds: int
    retry_after_seconds: Optional[int] = None

class SlidingWindowRateLimiter:

    def __init__(self, client: redis.Redis, *, window_seconds: int, max_requests: int) -> None:
        self._client = client
        self._window_seconds = window_seconds
        self._max_requests = max_requests

    async def check_and_record(self, user_id: str) -> RateLimitResult:
        key = f"ratelimit:{user_id}"
        now = time.time()
        window_start = now - self._window_seconds
        member = f"{now}:{uuid.uuid4().hex}"

        pipe = self._client.pipeline(transaction=True)
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {member: now})
        pipe.zcard(key)
        pipe.expire(key, self._window_seconds * 2)
        _, _, request_count, _ = await pipe.execute()

        allowed = request_count <= self._max_requests
        if not allowed:
            logger.warning(
                "Rate limit exceeded for user_id=%s: %d requests in %ds window (limit=%d)",
                user_id,
                request_count,
                self._window_seconds,
                self._max_requests,
            )
        return RateLimitResult(
            allowed=allowed,
            request_count=request_count,
            limit=self._max_requests,
            window_seconds=self._window_seconds,
            retry_after_seconds=self._window_seconds if not allowed else None,
        )
