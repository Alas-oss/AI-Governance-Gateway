from __future__ import annotations

import pytest

from app.auth.jwt_utils import ClearanceLevel, UserContext
from app.config import Settings
from app.policy.delegation import DelegationAction, SubAgentSpec, route_delegation
from app.policy.manifest import build_capability_manifest


@pytest.fixture()
def settings() -> Settings:
    return Settings(permissions_file_path="app/policy/permissions.yaml")


def _user(clearance: ClearanceLevel, department: str = "engineering") -> UserContext:
    return UserContext(user_id="test-user", department=department, clearance_level=clearance)


def _payload(text: str, tools=None) -> dict:
    payload = {"messages": [{"role": "user", "content": text}]}
    if tools:
        payload["tools"] = [{"type": "function", "function": {"name": t}} for t in tools]
    return payload


def test_denies_immediately_when_preflight_fails(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = _payload("run the admin tool", tools=["admin_only_tool"])
    sub_agents = [SubAgentSpec(name="finance", description="", required_tools=["admin_only_tool"])]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DENY
    assert decision.target is None
    assert decision.denial_message is not None


def test_delegates_to_fully_matching_sub_agent(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = _payload("search the web for something", tools=["web_search"])
    sub_agents = [
        SubAgentSpec(name="research", description="", required_tools=["web_search"], topic_keywords=["search"]),
    ]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DELEGATE
    assert decision.target == "research"
    assert decision.sub_agent_manifest is not None
    assert decision.sub_agent_manifest.depth == manifest.depth + 1
    assert decision.payload_for_sub_agent == payload


def test_sub_agent_manifest_cannot_exceed_callers_manifest(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = _payload("search please", tools=["web_search"])
    sub_agents = [
        SubAgentSpec(
            name="overreaching",
            description="",
            required_tools=["web_search", "admin_only_tool"],  # junior doesn't have admin_only_tool
            topic_keywords=["search"],
        ),
    ]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DELEGATE_NARROWED
    assert decision.sub_agent_manifest is not None
    assert "admin_only_tool" not in decision.sub_agent_manifest.allowed_tools


def test_delegates_narrowed_when_only_partial_match(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = _payload("search and also run the admin tool", tools=["web_search", "admin_only_tool"])
    sub_agents = [
        SubAgentSpec(
            name="research",
            description="",
            required_tools=["web_search"],
            topic_keywords=["search"],
        ),
    ]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DELEGATE_NARROWED
    assert decision.target == "research"
    assert decision.payload_for_sub_agent is not None
    narrowed_tool_names = [
        t["function"]["name"] for t in decision.payload_for_sub_agent["tools"]
    ]
    assert "web_search" in narrowed_tool_names
    assert "admin_only_tool" not in narrowed_tool_names


def test_denies_when_no_topic_match(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = _payload("what's the weather like")
    sub_agents = [
        SubAgentSpec(name="finance", description="", topic_keywords=["invoice", "payment"]),
        SubAgentSpec(name="hr", description="", topic_keywords=["pto", "benefits"]),
    ]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DENY
    assert "domain" in decision.reason


def test_denies_at_delegation_depth_ceiling(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.SENIOR), settings, max_delegation_depth=0)
    payload = _payload("search please", tools=["web_search"])
    sub_agents = [SubAgentSpec(name="research", description="", required_tools=["web_search"])]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DENY
    assert "depth" in decision.reason


def test_no_keywords_means_general_purpose_candidate(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = _payload("anything at all", tools=["web_search"])
    sub_agents = [SubAgentSpec(name="general", description="", required_tools=["web_search"])]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DELEGATE
    assert decision.target == "general"


def test_admin_manifest_delegates_to_any_required_tool_spec(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.ADMIN), settings)
    payload = _payload("do the sensitive admin thing", tools=["admin_only_tool"])
    sub_agents = [
        SubAgentSpec(name="admin-ops", description="", required_tools=["admin_only_tool"], topic_keywords=["admin"]),
    ]

    decision = route_delegation(manifest, payload, sub_agents)

    assert decision.action == DelegationAction.DELEGATE
    assert decision.sub_agent_manifest.allow_all_tools
