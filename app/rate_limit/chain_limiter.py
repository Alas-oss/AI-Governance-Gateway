from __future__ import annotations

from app.rate_limit.limiter import RateLimitResult, SlidingWindowRateLimiter

def chain_rate_limit_key(trace_id: str) -> str:
    return f"chain:{trace_id}"

class ChainRateLimiter:

    def __init__(self, limiter: SlidingWindowRateLimiter) -> None:
        self._limiter = limiter

    async def check_and_record_chain(self, trace_id: str) -> RateLimitResult:
        return await self._limiter.check_and_record(chain_rate_limit_key(trace_id))