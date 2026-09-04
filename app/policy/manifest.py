from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

from app.auth.jwt_utils import ClearanceLevel, UserContext
from app.config import Settings
from app.documents.pipeline import _DOCUMENT_REFERENCE_PATTERN
from app.policy.enforcement import _tool_name, get_permission_matrix

class DelegationDepthExceeded(Exception):
    """Raises a narrow() when the delegation chain tries to go deeper 
    than the manifest's max_delegation_depth allows."""

@dataclass(frozen=True)
class CapabilityManifest:
    origin_user_id: str
    department: str
    clearance_level: ClearanceLevel

    allow_all_tools: bool
    allowed_tools: List[str]
    allowed_tool_prefixes: List[str]
    restricted_doc_tags: List[str]
    masking_exempt_entities: List[str]

    max_delegate_depth: int
    depth: int = 0

    def can_use_tool(self, tool_name: str) -> bool:
        if self.allow_all_tools:
            return True
        if tool_name in self.allowed_tools:
            return True
        return any(tool_name.startswith(prefix) for prefix in self.allowed_tool_prefixes)

    def can_see_doc_tag(self, tag: str) -> bool:
        return tag not in self.restricted_doc_tags

    def can_delegate_further(self) -> bool:
        return self.depth < self.max_delegate_depth

    def narrow(
        self,
        *,
        allowed_tools: Optional[List[str]] = None,
        allowed_tool_prefixes: Optional[List[str]] = None,
        masking_exempt_entities: Optional[List[str]] = None,
    ) -> "CapabilityManifest":
        if not self.can_delegate_further():
            raise DelegationDepthExceeded(
                f"Delegation depth limit ({self.max_delegate_depth}) reach for "
                f"origin_user_id{self.origin_user_id!r}; refusing to delegate further."
            )

        next_tools = (
            list(self.allowed_tools)
            if allowed_tools is None
            else [t for t in allowed_tools if self.can_use_tool(t)]
        )
        next_prefixes = (
            list(self.allowed_tool_prefixes)
            if allowed_tool_prefixes is None
            else [p for p in allowed_tool_prefixes if p in self.allowed_tool_prefixes]
        )
        next_exempt = (
            list(self.masking_exempt_entities)
            if masking_exempt_entities is None
            else [e for e in masking_exempt_entities if e in self.masking_exempt_entities]
        )

        return replace(
            self,
            allowed_tools=next_tools,
            allowed_tool_prefixes=next_prefixes,
            masking_exempt_entities=next_exempt,
            depth=self.depth + 1,
        )

def build_capability_manifest(user: UserContext, settings: Settings, *,
            max_delegation_depth: int = 4) -> CapabilityManifest:
    matrix = get_permission_matrix(settings.permissions_file_path)
    policy = matrix.policy_for(user.clearance_level, user.department)

    return CapabilityManifest(
        origin_user_id=user.user_id,
        department=user.department,
        clearance_level=user.clearance_level,
        allow_all_tools=policy.allow_all_tools,
        allowed_tools=list(policy.allowed_tools),
        allowed_tool_prefixes=list(policy.allowed_tool_prefixes),
        restricted_doc_tags=list(policy.restricted_doc_tags),
        masking_exempt_entities=list(policy.masking_exempt_entities),
        max_delegate_depth=max_delegation_depth,
    )

@dataclass(frozen=True)
class PreflightResult:
    permitted: bool
    reason: Optional[str] = None
    missing_tools: List[str] = field(default_factory=list)
    missing_documents: List[str]=field(default_factory=list)

def requested_tool_names(payload: Dict[str, Any]) -> List[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    names = []
    for tool in tools: 
        if isinstance(tool, dict):
            name = _tool_name(tool)
            if name:
                names.append(name)
    return names

def referenced_document_ids(payload: Dict[str, Any]) -> List[str]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    ids: List[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            ids.extend(_DOCUMENT_REFERENCE_PATTERN.findall(content))
    return ids

def preflight_check(manifest: CapabilityManifest, payload: Dict[str, Any],
            document_registry: Optional[Any] = None) -> PreflightResult:
    requested_tools = requested_tool_names(payload)
    missing_tools = [t for t in requested_tools if not manifest.can_use_tool(t)]
    usable_tools = [t for t in requested_tools if manifest.can_use_tool(t)]

    doc_ids = referenced_document_ids(payload)
    missing_documents: List[str] = []
    usable_documents: List[str] = []
    if document_registry is not None:
        for doc_id in doc_ids:
            record = document_registry.get(doc_id)
            if record is None:
                continue
            if document_registry.is_authorized(record, manifest):
                usable_documents.append(doc_id)
            else:
                missing_documents.append(doc_id)

    nothing_requested = not requested_tools and not doc_ids
    has_comething_usable = bool(usable_tools) or bool(usable_documents)

    if nothing_requested or has_comething_usable:
        return PreflightResult(permitted=True)

    reasons = []
    if missing_tools:
        reasons.append(f"tools not permitted: {', '.join(missing_tools)}")
    if missing_documents:
        reasons.append(f"documents not permitted: {', '.join(missing_documents)}")
    return PreflightResult(
        permitted=False,
        reason="; ".join(reasons),
        missing_tools=missing_tools,
        missing_documents=missing_documents,
    )