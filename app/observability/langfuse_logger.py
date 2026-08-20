from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, List

from app.config import Settings

logger = logging.getLogger(__name__)

def redact_sensitive_info(data: Any, keywords: List[str]) -> Any:
    if not keywords:
        return data

    pattern = re.compile(r"|".join(map(re.escape, keywords)), re.IGNORECASE)

    if isinstance(data, str):
        return pattern.sub("[REDACTED_AGENT_DATA]", data)
    elif isinstance(data, dict):
        return {k: redact_sensitive_info(v, keywords) for k, v in data.items()}
    elif isinstance(data, list):
        return [redact_sensitive_info(item, keywords) for item in data]
    return data

class AuditLogger:

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.langfuse_enabled
        self._client = None
        self._sensitive_keywords = getattr(settings, "sensitive_keywords", [])

        if not self.enabled:
            logger.info("Langfuse audit logging disabled (langfuse_enabled=False).")
            return

        try: 
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
                host=settings.langfuse_host,
            )
            logger.info("Langfuse audit logging enabled (host=%s).", settings.langfuse_host)
        except Exception as exc: #noqa: BLE001 observability shouldn't crash the gateway
            logger.warning("Langfuse client failed to initialize (%s); audit logging disabled.", exc)
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    def log_call(self, *, user_id: str, department: str, masked_request: Optional[Dict[str, Any]], masked_response: Optional[Dict[str, Any]],
                 prompt_tokens: int, completion_tokens: int, cache_hit: bool) -> None:
        if not self.enabled:
            return

        try:
            clean_request = redact_sensitive_info(masked_request, self._sensitive_keywords)
            clean_response = redact_sensitive_info(masked_response, self._sensitive_keywords)

            observation = self._client.start_observation(
                name="governance_proxy_call",
                as_type="generation",
                input=clean_request,
                output=clean_response,
                metadata={"user_id": user_id, "department": department, "cache_hit": cache_hit},
                usage_details={
                    "input": prompt_tokens,
                    "output": completion_tokens,
                    "total": prompt_tokens + completion_tokens,
                },
            )
            observation.end()
        except Exception: #noqa: BLE001 doesn't allow logging failure to break the actual response
            logger.exception("Langfuse audit logging failed for user_id=%s; request was not blocked.", user_id)

    def shutdown(self) -> None:
        if self._client is None:
            return
        try:
            self._client.flush()
            self._client.shutdown()
        except Exception: #noqa: BLE001 cleanup
            logger.exception("Error shutting down Lnagfuse client.")