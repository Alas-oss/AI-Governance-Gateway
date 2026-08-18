from __future__ import annotations

import re
from typing import List, Optional

from presidio_analyzer import Pattern, PatternRecognizers

_SWIFT_BIC_PATTERN = Pattern(
    name="swift_bic_pattern",
    regex=r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b",
    score=0.55,
)

def build_swift_boc_recognizer() -> PatternRecognizers:
    return PatternRecognizers(
        supported_entity="SWIFT_BIC_CODE",
        pattern=[_SWIFT_BIC_PATTERN],
        context=["swift", "bic", "iban", "wire", "bank code", "correspondent bank"],
        global_regex_flags=re.DOTALL | re.MULTILINE,
    )

_INTERAL_IP_PATTERN = Pattern(
    name="internal_ip_10_block",
    regex=r"\b10(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}\b",
    score=0.9,
)

def build_internal_ip_recognizer() -> PatternRecognizers:
    return PatternRecognizers(
        supported_entity="internal_ip_10"
    )

## Check this out later after 4 pm