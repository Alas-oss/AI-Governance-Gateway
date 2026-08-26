from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import ValidationError

from app.auth.jwt_utils import UserContext
from app.config import Settings
from app.policy.schema import PermissionMatrix

logger = logging.getLogger(__name__)

_TAG_BLOCK_TEMPLATE = r"\[{tag_body}\](.*?)\[/{tag_body}\]"

class PolicyLoadError(Exception):
    """Is raised when permissions.yaml is missing, malformed, or fails schema validation."""

def _load_permission_matrix(path: str) -> PermissionMatrix:
    file_path = Path(path)
    if not file_path.exists():
        raise PolicyLoadError(f"Permission matrix file not found at '{path}'.")

    try:
        raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PolicyLoadError(f"Failed to parse permissions YAML: {exc}") from exc

    try: 
        return PermissionMatrix.model_validate(raw)
    except ValidationError as exc:
        raise PolicyLoadError(f"permissions.yaml failed schema validation: {exc}") from exc

@lru_cache
def get_permission_matrix(permissions_file_path: str) -> PermissionMatrix:
    return _load_permission_matrix(permissions_file_path)

def _tool_name(tool_def: Dict[str, Any]) -> Optional[str]:
    if isinstance(tool_def.get("function"), dict):
        return tool_def["function"].get("name")
    return tool_def.get("name")

def filter_tools(tools: List[Dict[str, Any]], user: UserContext, settings: Settings) -> List[Dict[str, Any]]:
    matrix = get_permission_matrix(settings.permissions_file_path)
    policy = matrix.policy_for(user.clearance_level, user.department)

    if policy.allow_all_tools:
        return tools

    allowed_exact = set(policy.allowed_tools)
    allowed_prefixes = tuple(policy.allowed_tool_prefixes)

    filtered: List[Dict[str, Any]] = []
    for tool in tools:
        name = _tool_name(tool)
        if not name:
            logger.warning("Encountered a tool definition with no resolvable name; dropping it defensively.")
            continue
        if name in allowed_exact or (allowed_prefixes and name.startswith(allowed_prefixes)):
            filtered.append(tool)
        else:
            logger.info(
                "Stripped unauthorized tool '%s' for user_id=%s clearance=%s",
                name,
                user.user_id,
                user.clearance_level.value,
            )
    return filtered

def redact_system_prompt(content: str, user: UserContext, settings: Settings) -> str:

    matrix = get_permission_matrix(settings.permissions_file_path)
    policy = matrix.policy_for(user.clearance_level, user.department)

    redacted = content
    for tag in policy.restricted_doc_tags:
        tag_body = re.escape(tag.strip("[]"))

        block_pattern = _TAG_BLOCK_TEMPLATE.format(tag_body=tag_body)
        redacted, block_count = re.subn(
            block_pattern, "[REDACTED: insufficient clearance]", redacted, flags=re.DOTALL
        )

        line_pattern = re.escape(tag) + r".*?(\n|$)"
        redacted, line_count = re.subn(line_pattern, "[REDACTED: insufficient clearance]\n", redacted)

        if block_count or line_count:
            logger.info(
                "Redacted %d block(s) / %d line(s) tagged '%s' for user_id=%s",
                block_count,
                line_count,
                tag,
                user.user_id,
            )
    return redacted

def get_masking_exempt_entities(user: UserContext, settings: Settings) -> List[str]:
    matrix = get_permission_matrix(settings.permissions_file_path)
    policy = matrix.policy_for(user.clearance_level, user.department)
    return policy.masking_exempt_entities

def enforce_policy_on_payload(payload: Dict[str, Any], user: UserContext, settings: Settings) -> Dict[str, Any]:
    mutated = dict(payload)

    if isinstance(mutated.get("tools"), list):
        mutated["tools"] = filter_tools(mutated["tools"], user, settings)

    messages = mutated.get("messages")
    if isinstance(messages, list):
        new_messages = []
        for message in messages:
            if not isinstance(message, dict):
                new_messages.append(message)
                continue
            if message.get("role") == "system" and isinstance(message.get("content"), str):
                message = {**message, "content": redact_system_prompt(message["content"], user, settings)}
            new_messages.append(message)
        mutated["messages"] = new_messages

    return mutated