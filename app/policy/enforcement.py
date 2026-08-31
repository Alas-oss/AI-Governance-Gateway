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

def _redact_tags(content: str, tags: List[str]) -> tuple[str, int]:
    """Shared redaction pass: given a set of restricted-doc-tags, strip any
    tagged block or line from content. Returns the redacted text and how
    many blocks/lines were redacted (0 means nothing in this tag set was
    present in the content)."""
    redacted = content
    total = 0
    for tag in tags:
        tag_body = re.escape(tag.strip("[]"))

        block_pattern = _TAG_BLOCK_TEMPLATE.format(tag_body=tag_body)
        redacted, block_count = re.subn(
            block_pattern, "[REDACTED: insufficient clearance]", redacted, flags=re.DOTALL
        )

        line_pattern = re.escape(tag) + r".*?(\n|$)"
        redacted, line_count = re.subn(line_pattern, "[REDACTED: insufficient clearance]\n", redacted)

        total += block_count + line_count
    return redacted, total


def most_restrictive_doc_tags(settings: Settings) -> List[str]:
    """The union of every restricted_doc_tags list across the *entire*
    permission matrix -- every clearance level, the default policy, and
    every department-specific override's additional tags.

    This is the tag set to use for anything that must be safe to persist
    or log regardless of who actually made the request. A specific user's
    own exemptions (e.g. a senior employee legitimately being allowed to
    see [INTERNAL-SENIOR-ONLY] content live) must never leak into what's
    written to the audit trail -- the persisted view has to reflect what
    the most restricted possible viewer in the whole organization would be
    allowed to see, not what this particular requester was allowed to see.
    """
    matrix = get_permission_matrix(settings.permissions_file_path)
    tags: set[str] = set(matrix.default_policy.restricted_doc_tags)
    for policy in matrix.policies.values():
        tags.update(policy.restricted_doc_tags)
    for department_overrides in matrix.department_policies.values():
        for override in department_overrides.values():
            tags.update(override.additional_restricted_doc_tags)
    return sorted(tags)


def redact_system_prompt(content: str, user: UserContext, settings: Settings) -> str:
    matrix = get_permission_matrix(settings.permissions_file_path)
    policy = matrix.policy_for(user.clearance_level, user.department)

    redacted, count = _redact_tags(content, policy.restricted_doc_tags)
    if count:
        logger.info(
            "Redacted %d restricted block(s)/line(s) for user_id=%s",
            count,
            user.user_id,
            )
    return redacted

def redact_payload_for_persisted_view(
    payload: Dict[str, Any], settings: Settings
) -> "tuple[Dict[str, Any], bool]":
    """Redact restricted-tag blocks from every system message in a request
    payload using the most-restrictive baseline (most_restrictive_doc_tags),
    regardless of which user actually made the request.

    This is what the persisted view / audit log / semantic cache must be
    built from -- never the requester's own permission-filtered payload,
    since that reflects what THEY were exempted from seeing, not what's
    safe to log for everyone.

    Returns (redacted_payload, had_restricted_content). had_restricted_content
    is True if anything was actually redacted here, meaning this request
    touched restricted internal content -- callers should treat the
    corresponding persisted RESPONSE as tainted too (see
    build_persisted_view's guidance: prefer omitting it entirely rather than
    attempting to scrub the model's free-form paraphrase of that content).
    """
    tags = most_restrictive_doc_tags(settings)
    mutated = dict(payload)
    had_restricted_content = False

    messages = mutated.get("messages")
    if isinstance(messages, list):
        new_messages = []
        for message in messages:
            if (
                isinstance(message, dict)
                and message.get("role") == "system"
                and isinstance(message.get("content"), str)
            ):
                redacted, count = _redact_tags(message["content"], tags)
                if count:
                    had_restricted_content = True
                    message = {**message, "content": redacted}
            new_messages.append(message)
        mutated["messages"] = new_messages

    return mutated, had_restricted_content

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