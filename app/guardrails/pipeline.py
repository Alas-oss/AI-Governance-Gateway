from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from app.guardrails.engine import PERSISTED_VIEW_ENTITIES, GuardrailsEngine

logger = logging.getLogger(__name__)

def mask_inbound_payload(
        payload: Dict[str, Any],
        engine: GuardrailsEngine,
        exempt_entities: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    mutated = dict(payload)
    messages = mutated.get("messages")
    if isinstance(messages, list):
        new_messages = []
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                result = engine.mask_text(message["content"], entities=entities, exempt_entities=exempt_entities)
                if result.had_matches:
                    message = {**message, "content": result.text}
            new_messages.append(message)
        mutated["messages"] = new_messages
    return mutated

def _mask_openai_style_choices(
        payload: Dict[str, Any],
        engine: GuardrailsEngine,
        exempt_entities: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return payload

    for choice in choices:
        if not isinstance(choices, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message["content"] = engine.mask_text(message["content"], entities=entities, exempt_entities=exempt_entities).text
        if isinstance(choice.get("text"), str):
            choice["text"] = engine.mask_text(choice["text"], entities=entities, exempt_entities=exempt_entities).text
    return payload

def mask_outbound_response_json(
        payload: Dict[str, Any],
        engine: GuardrailsEngine,
        exempt_entities: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return _mask_openai_style_choices(dict(payload), engine, exempt_entities=exempt_entities, entities=entities)

def mask_outbound_text(
        text: str, 
        engine: GuardrailsEngine,
        exempt_entities: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
) -> str:
    return engine.mask_text(text, entities=entities, exempt_entities=exempt_entities).text

def build_persisted_view(payload: Dict[str, Any], engine: GuardrailsEngine, *, is_response: bool = False) -> Dict[str, Any]:
    if is_response:
        return mask_outbound_response_json(payload, engine, exempt_entities=None, entities=list(PERSISTED_VIEW_ENTITIES))
    return mask_inbound_payload(payload, engine, exempt_entities=None, entities=list(PERSISTED_VIEW_ENTITIES))