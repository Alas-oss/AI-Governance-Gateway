from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from app.policy.enforcement import _tool_name
from app.policy.manifest import(
    CapabilityManifest,
    DelegationDepthExceeded,
    PreflightResult,
    preflight_check,
    requested_tool_names,
)

class DelegationAction(str, Enum):
    DELEGATE = "delegate"
    DELEGATE_NARROWED = "delegate_narrowed"
    DENY = "deny"

@dataclass(frozen=True)
class SubAgentSpec:
    name: str
    description: str
    required_tools: List[str] = field(default_factory=list)
    required_doc_tags: List[str] = field(default_factory=list)
    topic_keywords: List[str] = field(default_factory=list)

@dataclass(frozen=True)
class DelegationDecision:
    action: DelegationAction
    reason: str
    target: Optional[str] = None
    sub_agent_manifest: Optional[CapabilityManifest] = None
    payload_for_sub_agent: Optional[Dict[str, Any]] = None
    denial_message: Optional[str] = None

_DENIAL_MESSAGE_TEMPLATE = (
    "I'm not able to help with that request, it requires access this account doesn't " \
    "have ({reason}). Contact your administrator if you believe this is incorrect."
)

def _manifest_supports_spec(spec: SubAgentSpec, manifest: CapabilityManifest) -> bool:
    tools_ok = all(manifest.can_use_tool(t) for t in spec.required_tools)
    tags_ok = all(manifest.can_see_doc_tag(t) for t in spec.required_doc_tags)
    return tools_ok and tags_ok

def _last_user_message_text(payload: Dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""

def _matches_topic(spec: SubAgentSpec, query_text: str) -> bool:
    if not spec.topic_keywords:
        return True
    lowered = query_text.lower()
    return any(keyword.lower() in lowered for keyword in spec.topic_keywords)

def _strip_unpermitted_tools(payload: Dict[str, Any], manifest: CapabilityManifest) -> Dict[str, Any]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload
    kept = []
    for tool in tools:
        name = _tool_name(tool) if isinstance(tool, dict) else None
        if name and manifest.can_use_tool(name):
            kept.append(tool)
    mutated = dict(payload)
    mutated["tools"] = kept
    return mutated

def _try_narrow(spec: SubAgentSpec, manifest: CapabilityManifest) -> Optional[CapabilityManifest]:
    try:
        return manifest.narrow(allowed_tools=spec.required_tools or None)
    except DelegationDepthExceeded:
        return None

def route_delegation(
        manifest: CapabilityManifest,
        payload: Dict[str, Any],
        sub_agents: List[SubAgentSpec],
        document_registry: Optional[Any] = None,
) -> DelegationDecision:
    preflight: PreflightResult = preflight_check(manifest, payload, document_registry)
    if not preflight.permitted:
        return DelegationDecision(
            action=DelegationAction.DENY,
            reason=preflight.reason or "no usable tools or documents for this request",
            denial_message=_DENIAL_MESSAGE_TEMPLATE.format(reason=preflight.reason),
        )

    if not manifest.can_delegate_further():
        return DelegationDecision(
            action=DelegationAction.DENY,
            reason="delegation depth limit reached",
            denial_message=_DENIAL_MESSAGE_TEMPLATE.format(reason="this request required too many delegation steps"),
        )

    query_text = _last_user_message_text(payload)
    candidates = [s for s in sub_agents if _matches_topic(s, query_text)]
    if not candidates:
        return DelegationDecision(
            action=DelegationAction.DENY,
            reason="no sub-agent's domain matches this request",
            denial_message=_DENIAL_MESSAGE_TEMPLATE.format(reason="no matching capability found"),
        )

    requested_tools = requested_tool_names(payload)
    all_requested_tools_permitted = all(manifest.can_use_tool(t) for t in requested_tools)

    if all_requested_tools_permitted:
        for spec in candidates:
            if _manifest_supports_spec(spec, manifest):
                sub_manifest = _try_narrow(spec, manifest)
                if sub_manifest is None:
                    continue
                return DelegationDecision(
                    action=DelegationAction.DELEGATE,
                    reason=f"'{spec.name}' fully covers this request",
                    target=spec.name,
                    sub_agent_manifest=sub_manifest,
                    payload_for_sub_agent=payload,
                )

    for spec in candidates:
        tags_ok = all(manifest.can_see_doc_tag(t) for t in spec.required_doc_tags)
        if not tags_ok:
            continue
        overlap = set(spec.required_tools) & {t for t in requested_tools if manifest.can_use_tool(t)}
        if overlap or not spec.required_tools:
            sub_manifest = _try_narrow(spec, manifest)
            if sub_manifest is None:
                continue
            narrowed_payload = _strip_unpermitted_tools(payload, manifest)
            narrowed_tools = requested_tool_names(narrowed_payload)
            return DelegationDecision(
                action=DelegationAction.DELEGATE_NARROWED,
                reason=(
                    f"'{spec.name}' can answer a narrowed version of this request "
                    f"(permitted tools: {', '.join(narrowed_tools) or 'none'})"
                ),
                target=spec.name,
                sub_agent_manifest=sub_manifest,
                payload_for_sub_agent=narrowed_payload,
            )

    return DelegationDecision(
        action=DelegationAction.DENY,
        reason="no available sub-agent can handle even a narrowed version of this request",
        denial_message=_DENIAL_MESSAGE_TEMPLATE.format(reason="no capability match found"),
    )