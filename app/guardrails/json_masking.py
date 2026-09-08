from __future__ import annotations

import json
from typing import Any, List, Optional

from app.guardrails.engine import GuardrailsEngine

def mask_json_value(
        value: Any, 
        engine: GuardrailsEngine,
        exempt_entities: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
) -> Any:

    if isinstance(value, str):
        return engine.mask_text(value, entities=entities, exempt_entities=exempt_entities).text
    if isinstance(value, dict):
        return {key: mask_json_value(val, engine, exempt_entities, entities) for key, val, in value.items()}
    if isinstance(value, list):
        return [mask_json_value(item, engine, exempt_entities, entities) for item in value]
    return value

def mask_json_encoded_string(
        text: str, 
        engine: GuardrailsEngine,
        exempt_entities: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
) -> str:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return engine.mask_text(text, entities=entities, exempt_entities=exempt_entities).text

    masked = mask_json_value(parsed, engine, exempt_entities, entities)
    return json.dumps(masked)