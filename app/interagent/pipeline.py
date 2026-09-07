from __future__ import annotations

import json
import re
from typing import Any, List, Tuple

_OPEN, _CLOSE = "\u27e8", "\u27e9"


def wrap_interagent_payload(token: str, label: str, payload: Any) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return f"{_OPEN}IAP:{token}:{label}{_CLOSE}{text}{_OPEN}/IAP:{token}:{label}{_CLOSE}"


def _build_strip_pattern(token: str) -> "re.Pattern[str]":
    escaped_token = re.escape(token)
    return re.compile(
        rf"{_OPEN}IAP:{escaped_token}:([^{_CLOSE}]+){_CLOSE}.*?{_OPEN}/IAP:{escaped_token}:\1{_CLOSE}",
        re.DOTALL,
    )


def _build_capture_pattern(token: str) -> "re.Pattern[str]":
    escaped_token = re.escape(token)
    return re.compile(
        rf"{_OPEN}IAP:{escaped_token}:([^{_CLOSE}]+){_CLOSE}(.*?){_OPEN}/IAP:{escaped_token}:\1{_CLOSE}",
        re.DOTALL,
    )

STRUCTURAL_SENTINEL_PATTERN = re.compile(
    rf"{_OPEN}IAP:[0-9a-f]+:[^{_CLOSE}]+{_CLOSE}.*?{_OPEN}/IAP:[0-9a-f]+:[^{_CLOSE}]+{_CLOSE}",
    re.DOTALL,
)


def strip_interagent_payload(text: str, token: str) -> str:
    pattern = _build_strip_pattern(token)
    return pattern.sub(lambda m: f"[Inter-agent payload: {m.group(1)}]", text)


def unwrap_interagent_payload(text: str, token: str) -> List[Tuple[str, str]]:
    pattern = _build_capture_pattern(token)
    return [(match.group(1), match.group(2)) for match in pattern.finditer(text)]


def contains_interagent_payload(text: str, token: str) -> bool:
    return _build_strip_pattern(token).search(text) is not None
