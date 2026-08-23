from __future__ import annotations

import re
import secrets
from typing import Any, Dict, Tuple

from app.auth.jwt_utils import UserContext
from app.documents.registry import DocumentRegistry

_DOCUMENT_REFERENCE_PATTERN = re.compile(r"\[\[DOCUMENT:([A-Za-z0-9_\-]+)\]\]")
_OPEN, _CLOSE = "\u27e6", "\u27e7"


def generate_request_token() -> str:
    return secrets.token_hex(8)


def payload_references_documents(payload: Dict[str, Any]) -> bool:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str) and _DOCUMENT_REFERENCE_PATTERN.search(content):
            return True
    return False


def _sentinel_wrap(token: str, name: str, content: str) -> str:
    return f"{_OPEN}DOC:{token}:{name}{_CLOSE}{content}{_OPEN}/DOC:{token}:{name}{_CLOSE}"


def _build_sentinel_strip_pattern(token: str) -> "re.Pattern[str]":
    escaped_token = re.escape(token)
    return re.compile(
        rf"{_OPEN}DOC:{escaped_token}:([^{_CLOSE}]+){_CLOSE}.*?{_OPEN}/DOC:{escaped_token}:\1{_CLOSE}",
        re.DOTALL,
    )


STRUCTURAL_SENTINEL_PATTERN = re.compile(
    rf"{_OPEN}DOC:[0-9a-f]+:[^{_CLOSE}]+{_CLOSE}.*?{_OPEN}/DOC:[0-9a-f]+:[^{_CLOSE}]+{_CLOSE}",
    re.DOTALL,
)


def resolve_document_references(
    payload: Dict[str, Any], user: UserContext, registry: DocumentRegistry, token: str
) -> Tuple[Dict[str, Any], bool]:
    mutated = dict(payload)
    messages = mutated.get("messages")
    any_resolved = False

    if isinstance(messages, list):
        new_messages = []
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or "[[DOCUMENT:" not in content:
                new_messages.append(message)
                continue

            def _replace(match: "re.Match[str]") -> str:
                nonlocal any_resolved
                doc_id = match.group(1)
                record = registry.get(doc_id)
                if record is None:
                    return match.group(0)
                if not registry.is_authorized(record, user):
                    return f"[Document: {record.name} \u2014 access not authorized for your clearance/department]"
                any_resolved = True
                return _sentinel_wrap(token, record.name, record.content)

            new_content = _DOCUMENT_REFERENCE_PATTERN.sub(_replace, content)
            new_messages.append({**message, "content": new_content} if new_content != content else message)
        mutated["messages"] = new_messages

    return mutated, any_resolved


def strip_document_content(text: str, token: str) -> str:
    pattern = _build_sentinel_strip_pattern(token)
    return pattern.sub(lambda m: f"[Document: {m.group(1)}]", text)