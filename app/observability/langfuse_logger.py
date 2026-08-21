from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.config import Settings

logger = logging.getLogger(__name__)

def langfuse_mask(*, data: Any, **_: Any) -> Any:
    if isinstance(data, str):
        try:
            from app.documents.pipeline import STRUCTURAL_SENTINEL_PATTERM

            return STRUCTURAL_SENTINEL_PATTERM.sub("[DOCUMENT CONTENT REDACTED]", data)
        except ImportError:
            return data
    if isinstance(data, dict):
        return {key: _langfuse_mask(data=value) for key, value in data.items()}
    if isinstance(data, list):
        return [_langfuse_mask(data=item) for item in data]
    return data

class AuditLogger:
    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.langfuse_enabled
        self._client = None

        if not self._enabled:
            logger.info("Langfuse audit logging disabled (langfuse_enabled=False).")
            return

        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
                mask=_langfuse_mask,
            )
            logger.info("Langfuse audit logging enabled (host=%s, mask hook active).", settings.langfuse_host)
        except Exception as exc: # noqa: BLE001 -> observability shouldn't crash the gateway
            logger.warning("Langfuse client failed to initialize (%s); audit logging disabled.", exc)
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    def log_call(
            self, 
            *,
            user_id: str, 
            department: str,
            masked_request: Optional[Dict[str, Any]],
            masked_response: Optional[Dict[str, Any]],
            prompt_tokens: int,
            completion_tokens: int,
            cache_hit: bool,
    ) -> None:
        if not self.enabled:
            return

        try:
            observation = self._client.start_observation(
                name="governed_proxy_call",
                as_type="generation",
                input=masked_request,
                output=masked_response,
                metadata={"user_id": user_id, "department": department, "cache_hit": cache_hit},
                usage_details={
                    "input": prompt_tokens,
                    "output": completion_tokens,
                    "total": prompt_tokens + completion_tokens,
                },
            )
            observation.end()
        except Exception:
            logger.exception("Langfuse audit logging failed for user_id=%s; request was not blocked.", user_id)

    def shutdown(self) -> None:
        if self._client is None:
            return
        try:
            self._client.flush()
            self._client.shutdown()
        except Exception: # noqa: BLE001 -> clean-up
            logger.exception("Error shutting down Langfuse client.")