from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)

_FALLBACK_CHARS_PER_TOKEN = 4

class TokenCounter:

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self._encoding = None
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(encoding_name)
            logger.info("TokenCounter using tiktoken encoding '%s'.", encoding_name)
        except Exception as exc: # noqa: BLE001 - failure->fallback, not crash
            logger.warning(
                "tiktoken encoding '%s' unavailable (%s); falling back to a %d-charse-per-token " \
                "heuristic. Token accounting will be approximate until network access is restored.",
                encoding_name,
                exc,
                _FALLBACK_CHARS_PER_TOKEN,
            )

    @property
    def is_exact(self) -> bool:
        return self._encoding is not None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return max(1, len(text) // _FALLBACK_CHARS_PER_TOKEN)

class TokenAccountingEngine:

    def __init__(self, client: redis.Redis, counter: TokenCounter) -> None:
        self._client = client
        self._counter = counter

    def count_prompt_tokens(self, payload: Optional[Dict[str, Any]]) -> int:
        if not isinstance(payload, dict):
            return 0
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return 0
        total = 0
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                total += self._counter.count(message["content"])
        return total

    def count_completion_tokens(self, response_payload: Optional[Dict[str, Any]]) -> int:
        if not isinstance(response_payload, dict):
            return 0 
        choices = response_payload.get("choices")
        if not isinstance(choices, list):
            return 0
        total = 0
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                total += self._counter.count(message["content"])
            elif isinstance(choice.get("text"), str):
                total += self._counter.count(choice["text"])
        return total

    async def record_usage(self, department: str, prompt_tokens: int, completion_tokens: int) -> None:
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        pipe = self._client.pipeline(transaction=True)
        if prompt_tokens:
            pipe.incrby(f"tokens:{department}:prompt", prompt_tokens)
        if completion_tokens:
            pipe.incrby(f"tokens:{department}:completion", completion_tokens)
        pipe.incrby(f"tokens:{department}:total", prompt_tokens + completion_tokens)
        await pipe.execute()

    async def get_usage(self, department: str) -> Dict[str, int]:
        keys = [f"tokens:{department}:prompt", f"tokens:{department}:completion", f"tokens:{department}:total"]
        values = await self._client.mget(keys)
        return {
            "prompt_tokens": int(values[0] or 0),
            "completion_tokens": int(values[1] or 0),
            "total_tokens": int(values[2] or 0),
        }