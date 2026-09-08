from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.guardrails.engine import INJECTION_ENTITY_TYPES, GuardrailsEngine

@dataclass(frozen=True)
class Injectionfinding:
    entity_type: str
    score: float
    snippet: str
    source: str

@dataclass(frozen=True)
class InjectionScanResult:
    findings: List[Injectionfinding] = field(default_factory=list)

    @property 
    def has_findings(self) -> bool:
        return len(self.findings) > 0

    def max_score(self) -> float:
        return max((f.score for c in self.findings), default=0.0)

    def exceeds(self, threshold: float) -> bool:
        return self.max_score() >= threshold

_SNIPPET_RADIUS = 40

def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - _SNIPPET_RADIUS)
    hi = min(len(text), end + _SNIPPET_RADIUS)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{text[lo:hi]}{suffix}"

def scan_text_for_injection(text: str, engine, GuardrailsEngine, *, source: str = "text") -> List[Injectionfinding]:
    if not text:
        return []
    matches = engine.scan_entities(text, entities=list(INJECTION_ENTITY_TYPES))
    return [
        Injectionfinding(
            entity_type=match.entity_type,
            score=match.score,
            snipped=_snippet(text, match.start, match.end),
            source=source,
        )
        for match in matches
    ]

def scan_payload_for_injection(payload: Dict[str, Any], engine: GuardrailsEngine) -> InjectionScanResult:
    findings: List[Injectionfinding] = []

    messages = payload.get("messages")
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = message.get("role", "unknown")
            content = message.get("content")
            if isinstance(content, str):
                findings.extend(
                    scan_text_for_injection(content, engine, source=f"messages[{index}]({role}).content")
                )
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call_index, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        continue
                    functoin = call.get("function")
                    if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                        findings.extend(
                            scan_text_for_injection(
                                function["arguments"],
                                engine, source=f"messages[{index}].tool_calls[{call_index}].arguments",
                            )
                        )

    choices = payload.get("choices")
    if isinstance(choices, list):
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                findings.extend(
                    scan_text_for_injection(message["content"], engine, source=f"choices[{index}].message.content")
                )
            if isinstance(choice.get("text"), str):
                findings.extend(scan_text_for_injection(choice["text"], engine, source=f"choices[{index}].text"))

    return InjectionScanResult(findings=findings)