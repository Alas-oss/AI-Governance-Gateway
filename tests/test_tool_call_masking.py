from __future__ import annotations

import json

from app.config import Settings
from app.guardrails.engine import GuardrailsEngine
from app.guardrails.json_masking import mask_json_encoded_string, mask_json_value
from app.guardrails.pipeline import build_persisted_view, mask_inbound_payload, mask_outbound_response_json

def _engine() -> GuardrailsEngine:
    settings = Settings(permissions_file_path="app/policy/permissions.yaml")
    return GuardrailsEngine(settings)

def test_mask_json_value_mask_nested_string_leaves():
    engine = _engine()
    value = {"employee": {"card": "4111 1111 1111 1111", "name": "Mysterio"}, "notes": ["backup card 4111 1111 1111 1111"]}

    masked = mask_json_value(value, engine)

    assert "4111 1111 1111 1111" not in json.dumps(masked)

def test_mask_json_value_leaves_keys_and_non_string_alone():
    engine = _engine()
    value = {"employee_id": 53, "active": True, "notes": None}

    masked = mask_json_value(value, engine)

    assert masked == {"employee_id": 53, "active": True, "notes": None}

def test_mask_json_encoded_string_round_trips_valid_json():
    engine = _engine()
    text = json.dumps({"calls": "4111 1111 1111 1111"})

    masked_text = mask_json_encoded_string(text, engine)
    parsed_back = json.loads(masked_text)

    assert "4111 1111 1111 1111" not in masked_text
    assert isinstance(parsed_back, dict)

def test_mask_json_encoded_string_falls_back_on_valid_json():
    engine = _engine()
    text = "this is not json but has a card 4111 1111 1111 1111 in it"

    masked_text = mask_json_encoded_string(text, engine)

    assert "4111 1111 1111 1111" not in masked_text

def test_mask_inbound_payload_mask_tool_call_arguments():
    engine = _engine()
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup_employee",
                            "arguments": json.dumps({"employee_id": "123", "card": "4111 1111 1111 1111"}),
                        },
                    }
                ],
            }
        ]
    }

    masked = mask_inbound_payload(payload, engine)

    arguments = masked["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert "4111 1111 1111 1111" not in arguments
    parsed = json.loads(arguments)
    assert parsed["employee_id"] == "123"

def test_mask_inbound_payload_mask_tool_result_content():
    engine = _engine()
    payload = {
        "messages": [
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": json.dumps({"card": "4111 1111 1111 1111", "balance": "$5,000.00"}),
            }
        ]
    }

    masked = mask_inbound_payload(payload, engine)

    content = masked["messages"][0]["content"]
    assert "4111 1111 1111 1111" not in content

def test_mask_outbound_response_masks_tool_call_arguments():
    engine = _engine()
    payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1", 
                            "type": "function",
                            "function":{
                                "name": "lookup_employee",
                                "arguments": json.dumps({"card": "4111 1111 1111 1111"}),
                            },
                        }
                    ],
                }
            }
        ]
    }

    masked = mask_outbound_response_json(payload, engine)

    arguments = masked["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert "4111 1111 1111 1111" not in arguments

def test_mask_outbound_response_respects_exemptions_in_tool_calls():
    engine = _engine()
    payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "lookup_card",
                                    "arguments": json.dumps({"card": "4111 1111 1111 1111"}),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    masked = mask_outbound_response_json(payload, engine, exempt_entities=["CREDIT_CARD", "BANK_CARD_NUMBER"])

    arguments = masked["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert "4111 1111 1111 1111" in arguments

def test_mask_inbound_payload_doe_not_mutate_original_tool_calls():
    engine = _engine()
    original_arguments = json.dumps({"card": "4111 1111 1111 1111"})
    payload = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "x", "arguments": original_arguments}}],
            }
        ]
    }

    mask_inbound_payload(payload, engine)
    assert payload["messages"][0]["tool_calls"][0]["function"]["arguments"] == original_arguments