from __future__ import annotations

from app.documents.pipeline import _sentinel_wrap, generate_request_token, strip_document_content
from app.interagent.pipeline import (
    STRUCTURAL_SENTINEL_PATTERN,
    contains_interagent_payload,
    strip_interagent_payload,
    unwrap_interagent_payload,
    wrap_interagent_payload,
)

def test_wrap_then_strip_never_leaks_string_content():
    token = generate_request_token()
    wrapped = wrap_interagent_payload(token, "SUBAGENT:finance-summary", "The real Q3 valuation is $480M.")

    stripped = strip_interagent_payload(wrapped, token)

    assert "$480M" not in stripped
    assert "SUBAGENT:finance-summary" in stripped
    assert stripped == "[Inter-agent payload: SUBAGENT:finance-summary]"


def test_wrap_then_strip_never_leaks_structured_content():
    token = generate_request_token()
    payload = {"ssn": "123-45-6789", "employee": "Mysterio"}
    wrapped = wrap_interagent_payload(token, "TOOL_RESULT:lookup_ssn", payload)

    stripped = strip_interagent_payload(wrapped, token)

    assert "123-45-6789" not in stripped
    assert "Mysterio" not in stripped

def test_unwrap_recovers_the_real_content():
    token = generate_request_token()
    wrapped = wrap_interagent_payload(token, "SUBAGENT:context", "sensitive reasoning notes here")

    recovered = unwrap_interagent_payload(wrapped, token)

    assert len(recovered) == 1
    label, content = recovered[0]
    assert label == "SUBAGENT:context"
    assert content == "sensitive reasoning notes here"

def test_different_tokens_do_not_cross_strip():
    token_a = generate_request_token()
    token_b = generate_request_token()
    wrapped_with_a = wrap_interagent_payload(token_a, "SUBAGENT:x", "secret content")

    stripped_with_wrong_token = strip_interagent_payload(wrapped_with_a, token_b)

    assert "secret content" in stripped_with_wrong_token

def test_contains_interagent_payload_detects_presence():
    token = generate_request_token()
    wrapped = wrap_interagent_payload(token, "SUBAGENT:x", "hello")
    assert contains_interagent_payload(wrapped, token)
    assert not contains_interagent_payload("plain text with nothing wrapped", token)

def test_does_not_collide_with_document_sentinels_in_the_same_text():
    token = generate_request_token()
    doc_wrapped = _sentinel_wrap(token, "HR Policy", "Employees get 20 days PTO per year.")
    iap_wrapped = wrap_interagent_payload(token, "SUBAGENT:hr-summary", "The employee has 12 days left.")
    combined = f"Contect: {doc_wrapped} Sub-agent said: {iap_wrapped}"

    only_doc_stripped = strip_document_content(combined, token)
    assert "PTO per year" not in only_doc_stripped
    assert "12 days left" in only_doc_stripped

    only_iap_stripped = strip_interagent_payload(combined, token)
    assert "12 days left" not in only_iap_stripped
    assert "PTO per year" in only_iap_stripped

def test_structural_sentinel_pattern_matches_regardless_of_token():
    token = generate_request_token()
    wrapped = wrap_interagent_payload(token, "SUBAGENT:x", "should never appear in a trace")

    redacted = STRUCTURAL_SENTINEL_PATTERN.sub("[INTER-AGENT PAYLOAD REDACTED]", wrapped)

    assert "should never appear in a trace" not in redacted
    assert redacted == "[INTER-AGENT PAYLOAD REDACTED]"

def test_build_persisted_view_stirps_interagent_payload_end_to_end():
    from app.config import Settings
    from app.guardrails.engine import GuardrailsEngine
    from app.guardrails.pipeline import build_persisted_view

    settings = Settings(permissions_file_path="app/policy/permissions.yaml")
    engine = GuardrailsEngine(settings)

    token = generate_request_token()
    wrapped = wrap_interagent_payload(
        token, "SUBAGENT:finance-summary", "The real Q3 valuation is $480M, card on file 4111 1111 1111 1111."
    )
    payload = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"Sub-agent context: {wrapped}"},
        ]
    }

    persisted = build_persisted_view(payload, engine, interagent_token=token)

    persisted_text = persisted["messages"][1]["content"]
    assert "$480M" not in persisted_text
    assert "4111 1111 1111 1111" not in persisted_text
    assert "SUBAGENT:finance-summary" in persisted_text