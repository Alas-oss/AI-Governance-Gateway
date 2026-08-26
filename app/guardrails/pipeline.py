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

    new_choices = []
    for choice in choices:
        if not isinstance(choice, dict):
            new_choices.append(choice)
            continue
        choice = dict(choice) 
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            masked_content = engine.mask_text(message["content"], entities=entities, exempt_entities=exempt_entities).text
            choice["message"] = {**message, "content": masked_content}
        if isinstance(choice.get("text"), str):
            choice["text"] = engine.mask_text(choice["text"], entities=entities, exempt_entities=exempt_entities).text
        new_choices.append(choice)
    payload["choices"] = new_choices
    return payload

def mask_outbound_response_json(
    payload: Dict[str, Any],
    engine: GuardrailsEngine,
    exempt_entities: Optional[List[str]] = None,
    entities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return _mask_openai_style_choices(dict(payload), engine, exempt_entities=exempt_entities, entities=entities)

def mask_outbound_text(
        text: str, engine: GuardrailsEngine, exempt_entities: Optional[List[str]] = None, entities: Optional[List[str]] = None
) -> str:
    return engine.mask_text(text, entities=entities, exempt_entities=exempt_entities).text

def _strip_documents_from_paylaod(payload: Dict[str, Any], document_token: str, *, is_response: bool) -> Dict[str, Any]:

    from app.documents.pipeline import strip_document_content

    mutated = dict(payload)

    if is_response:
        choices = mutated.get("choices")
        if isinstance(choices, list):
            new_choices = []
            for choice in choices:
                if not isinstance(choice, dict):
                    new_choices.append(choice)
                    continue
                choice = dict(choice)
                message = choice.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    choice["message"] = {**message, "content": strip_document_content(message["content"], document_token)}
                if isinstance(choice.get("text"), str):
                    choice["text"] = strip_document_content(choice["text"], document_token)
                new_choices.append(choice)
            mutated["choices"] = new_choices
        return mutated

    messages = mutated.get("messages")
    if isinstance(messages, list):
        new_message = []
        for message in messages: 
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                message = {**message, "content": strip_document_content(message["content"], document_token)}
            new_message.append(message)
        mutated["messages"] = new_message
    return mutated

def build_persisted_view(
        payload: Dict[str, Any],
        engine: GuardrailsEngine,
        *,
        is_response: bool = False,
        document_token: Optional[str] = None,
) -> Dict[str, Any]:
    working = payload
    if document_token:
        working = _strip_documents_from_paylaod(working, document_token, is_response=is_response)

    if is_response:
        return mask_outbound_response_json(working, engine, exempt_entities=None, entities=list(PERSISTED_VIEW_ENTITIES))
    return mask_inbound_payload(working, engine, exempt_entities=None, entities=list(PERSISTED_VIEW_ENTITIES))