from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as redis

from app.config import Settings

logger = logging.getLogger(__name__)

class RedisClientManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: Optional[redis.ConnectionPool] = None
        self._client: Optional[redis.Redis] = None

    async def start(self) -> None:
        self._pool = redis.ConnectionPool.from_url(
            self._settings.redis_url,
            max_connections=self._settings.redis_max_connections,
            decode_responses=True,
        )
        self._client = redis.Redis(connection_pool=self._pool)
        await self._client.ping()
        logger.info("Redis client connected (url=%s)", self._settings.redis_url)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        if self._pool is not None:
            await self._pool.disconnect()
        self._client = None
        self._pool = None
        logger.info("Redis client closed.")

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise RuntimeError("RedisClientManager used before start(). Check app lifespan wiring.")
        return self._client