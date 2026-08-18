from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from presidio_analyzer import AnalyzerEngine, RecongnizerResult
from presidio_analyzer.nlp_engine import NlpEngineProvider

from app.config import Settings
from app.guardrails.recognizers import build_custom_recognizers

logger = logging.getLogger(__name__)

ENTITY_LABELS: Dict[str, str] = {
    "CREDIT_CARD": "CREDIT_CARD",
    "BANK_CARD_NUMBER": "CREDIT_CARD",
    "IP_ADDRESS": "IP",
    "INTERNAL_IP_ADDRESS": "INTERNAL_IP",
    "SWIFT_BIC_CODE": "SWIFT_BIC",
    "PROPRIETARY_SOURCE_MARKER": "SOURCE_MARKER",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "US_SSN": "SSN",
    "PERSON": "PERSON",
    "US_BANK_NUMBER": "BANK_ACCOUNT",
    "IBAN_CODE": "IBAN",
    "MONETARY_AMOUNT": "AMOUNT",
}

DEFAULT_ENTITIES_TO_MASK: Tuple[str, ...] = (
    "CREDIT_CARD",
    "BANK_CARD_NUMBER",
    "IP_ADDRESS",
    "INTERNAL_IP_ADDRESS",
    "SWIFT_BIC_CODE",
    "PROPRIETARY_SOURCE_MARKER",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "US_BANK_NUMBER",
    "IBAN_CODE",
)

PERSISTED_VIEW_ENTITIES: Tuple[str, ...] = DEFAULT_ENTITIES_TO_MASK + ("MONETARY_AMOUNT",)

@dataclass
class MaskFinding:
    entity_type: str
    placeholder: str
    score: float
    masked: bool = True

@dataclass(frozen=True)
class MaskResult:
    text: str
    findings: List[MaskFinding] = field(default_factory=list)

    @property
    def had_matches(self) -> bool:
        return any(f.masked for f in self.findings)

def _resolve_overlap(results: List[RecongnizerResult]) -> List[RecongnizerResult]:
    ordered = sorted(results, key=lambda r: (-r.score, r.start))
    accepted: List[RecongnizerResult] = []
    occupied: List[Tuple[int, int]] = []
    for result in ordered:
        overlaps = any(not (result.end <= s or result.start >= e) for s, e in occupied)
        if not overlaps:
            accepted.append(result)
            occupied.append((result.start, result.end))
    return sorted(accepted, key=lambda r: r.start)

class GuardrailsEngine:

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._score_threshold = settings.guardrails_score_threshold
        self._entities_to_mask = list(settings.guardrails_entities_to_mask or DEFAULT_ENTITIES_TO_MASK)

        nlp_configuration = {
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": settings.guardrails_spacy_model}],
        }
        nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()

        self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])
        for recognizer in build_custom_recognizers():
            self._analyzer.registry.add_recognizer(recognizer)

        logger.info(
            "GuardrailsEngine initialized (model=%s, threshold=%.2f, entities=%s)",
            settings.guardrails_spacy_model,
            self._score_threshold,
            self._entities_to_mask,
        )

    def mask_text(
            self,
            text: str,
            entities: Optional[List[str]] = None,
            exempt_entities: Optional[List[str]] = None,
    ) -> MaskResult:

        if not text:
            return MaskResult(text=text)

        target_entities = entities or self._entities_to_mask
        exempt_set = set(exempt_entities or [])

        raw_results = self._analyzer.analyze(text=text, language="en", entities=target_entities)
        accepted = _resolve_overlap([r for r in raw_results if r.score >= self._score_threshold])

        if not accepted:
            return MaskResult(text=text)

        counters: Dict[str, int] = {}
        spans: List[Tuple[int, int, str]] = [] #start, end, the placeholder
        findings: List[MaskFinding] = []

        for result in accepted:
            label = ENTITY_LABELS.get(result.entity_type, result.entity_type)

            if result.entity_type in exempt_set:
                findings.append(
                    MaskFinding(entity_type=result.entity_type, placeholder="<disclosed: authorized>",
                                score=result.score, masked=False)
                )
                continue

            counters[label] = counters.get(label, 0) + 1
            placeholder = f"[MASKED_{label}_{counters[label]}]"
            spans.append((result.start, result.end, placeholder))
            findings.append(MaskFinding(entity_type=result.entity_type, placeholder=placeholder, score=result.score))

        masked = text
        for start, end, placeholder in sorted(spans, key=lambda s: s[0], reverse=True):
            masked = masked[:start] + placeholder + masked[end:]

        masked_finings = [f for f in findings if f.masked]
        exempt_findings = [f for f in findings if not f.masked]
        if masked_finings:
            logger.info(
                "Masked %d entit%s in text (%s)",
                len(masked_finings),
                "y" if len(masked_finings) == 1 else "ies",
                ", ".join(f.entity_type for f in masked_finings),
            )
        if exempt_findings:
            logger.info(
                "Authorization disclosure (masking exempt) for entity type(s): %s",
                ", ".join(f.entity_type for f in exempt_findings),
            )
        return MaskResult(text=masked, findings=findings)

_engine_singleton: Optional[GuardrailsEngine] = None

def init_guardrails_engine(settings: Settings) -> GuardrailsEngine:
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = GuardrailsEngine(settings)
    return _engine_singleton

def get_engine() -> GuardrailsEngine:
    if _engine_singleton is None:
        raise RuntimeError("GuardrailsEngine not initialized. Call init_guardrails_engine() at app startup.")
    return _engine_singleton