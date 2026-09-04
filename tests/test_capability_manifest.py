from __future__ import annotations

import pytest

from app.auth.jwt_utils import ClearanceLevel, UserContext
from app.config import Settings
from app.documents.registry import DocumentRecord, DocumentRegistry
from app.policy.manifest import (
    DelegationDepthExceeded,
    build_capability_manifest,
    preflight_check,
    referenced_document_ids,
    requested_tool_names,
)

@pytest.fixture()
def settings() -> Settings:
    return Settings(permissions_file_path="app/policy/permissions.yaml")

def _user(clearance: ClearanceLevel, department: str = "engineering") -> UserContext:
    return UserContext(user_id="text-user", department=department, clearance_level=clearance)

def test_manifest_reflects_the_users_own_policy(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    assert manifest.clearance_level == ClearanceLevel.JUNIOR
    assert manifest.origin_user_id == "test-user"
    assert manifest.can_use_tool("web_search")
    assert not manifest.allow_all_tools

def test_admin_manifest_allows_all_tools(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.ADMIN), settings)
    assert manifest.allow_all_tools
    assert manifest.can_use_tool("literally_anything")

def test_narrow_can_only_shrink_never_grow(settings):
    junior_manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)

    narrowed = junior_manifest.narrow(allowed_tools=["web_search", "some_tool_junior_never_had"])
    assert "web_search" in narrowed.allowed_tools
    assert "some_tool_junior_never_had" not in narrowed.allowed_tools

def test_narrow_increments_depth(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.SENIOR), settings, max_delegation_depth=2)
    assert manifest.depth == 0
    hop1 = manifest.narrow()
    assert hop1.depth == 1
    with pytest.raises(DelegationDepthExceeded):
        hop1.narrow()

def test_requested_tool_names_extracts_function_names():
    payload = {
        "messages": [],
        "tools": [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "function", "function": {"name": "admin_only_tool"}},
        ],
    }
    assert requested_tool_names(payload) == ["web_search", "admin_only_tool"]

def test_referenced_document_ids_extracts_ids():
    payload = {
        "messages": [
            {"role": "user", "content": "Please summarize [[DOCUMENT:doc-123]] for me."},
        ]
    }
    assert referenced_document_ids(payload) == ["doc-123"]

def test_preflight_permits_when_fully_covered(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "web_search"}}],
    }
    result = preflight_check(manifest, payload)
    assert result.permitted

def tesT_preflight_permits_when_partially_covered(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {"name": "web_search"}}, 
            {"type": "function", "function": {"name": "admin_only_tool"}},
        ]
    }
    result = preflight_check(manifest, payload)
    assert result.permitted

def test_preflight_denies_when_nothing_is_covered(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "admin_only_tool"}}],
    }
    result = preflight_check(manifest, payload)
    assert not result.permitted
    assert "admin_only_tool" in result.missing_tools

def tesT_preflight_permits_when_no_tools_or_documents_requested(settings):
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = {"messages": [{"role": "user", "content": "just a plain question"}]}
    result = preflight_check(manifest, payload)
    assert result.permitted

def test_preflight_checks_document_authorization(settings):
    registry = DocumentRegistry()
    registry.register(
        DocumentRecord(
            doc_id="secret-doc",
            name="Executive Compensation",
            content="...",
            required_clearance=ClearanceLevel.ADMIN,
            required_department=None,
        )
    )
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = {"messages": [{"role": "user", "content": "Summarize [[DOCUMENT:secret-doc]]"}]}

    result = preflight_check(manifest, payload, registry)
    assert not result.permitted
    assert "secret-doc" in result.missing_documents

def test_preflight_permits_authorized_document(settings):
    registry = DocumentRegistry()
    registry.register(
        DocumentRecord(
            doc_id="public-doc",
            name="Onboard Guide",
            content="...",
            required_clearance=None,
            required_department=None,
        )
    )
    manifest = build_capability_manifest(_user(ClearanceLevel.JUNIOR), settings)
    payload = {"messages": [{"role": "user", "content": "Summarize [[DOCUMENT:public-doc]]"}]}

    result = preflight_check(manifest, payload, registry)
    assert result.permitted
