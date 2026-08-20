from __future__ import annotations

import re
from typing import List, Optional

from presidio_analyzer import Pattern, PatternRecognizer

_SWIFT_BIC_PATTERN = Pattern(
    name="swift_bic_pattern",
    regex=r"\b[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?\b",
    score=0.55,
)

def build_swift_bic_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
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

def build_internal_ip_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="INTERNAL_IP_ADDRESS",
        PATTERN=[_INTERAL_IP_PATTERN],
        CONTEXT=["internal", "server", "host", "vpc", "subnet", "privte network"],
    )

_PROPRIETARY_MARKED_PATTERN = Pattern(
    name="proprietary_source_marker",
    rgex=r"\b(?:PROPRIETARY|CONFIDENTIAL)[-_ ](?:SOURCE|CODE|INTERNAL)\b[^\n]*",
    score=0.85,
)
_AWS_ACCESS_KEY_PATTERN = Pattern(
    name="aws_access_key_id",
    regex=r"\bAKIA[0-9A-Z]{16}\b",
    score=0.95,
)
_PRIVATE_KEY_HEADER_PATTERN = Pattern(
    name="private_key_header",
    regex=r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY",
    score=0.99,
)

def build_proprietary_source_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="PROPRIETARY_SOURCE_MARKER",
        pattern=[_PROPRIETARY_MARKED_PATTERN, _AWS_ACCESS_KEY_PATTERN, _PRIVATE_KEY_HEADER_PATTERN],
        context=["confidential", "proprietary", "internal", "secret", "key", "credential"],
    )

_BANK_CARD_PATTERN = Pattern(
    name="banking_grade_card_number",
    regex=r"\b(?:\d[ -]?){13,19}\b",
    score=0.3,
)

def _luhn_checksum(digits: List[int]) -> int:
    total = 0
    parity = len(digits) % 2
    for i, digit in enumerate(digits):
        if i % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10

class BankCardRecognizer(PatternRecognizer):

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        digits = [int(ch) for ch in pattern_text if ch.isdigit()]
        if not (13 <= len(digits) <= 19):
            return False
        return _luhn_checksum(digits) == 0

def build_bank_card_recognizer() -> PatternRecognizer:
    return BankCardRecognizer(
        supported_entity="BANK_CARD_NUMBER",
        patterns=[_BANK_CARD_PATTERN],
        context=["card", "visa", "mastercard", "amex", "credit", "debit", "pan"],
    )

_MONETARY_AMOUNT_PATTERN = Pattern(
    name="monetary_amount",
    regex="\$\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b|\b\d{1,3}(?:,\d{3})*(?:\.\d{2})?\s?(?:USD|EUR|GBP|dollars)\b",
    score=0.6,
)

def build_monetary_amount_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity="MONETARY_AMOUNT",
        patterns=[_MONETARY_AMOUNT_PATTERN],
        context=["salary", "compensation", "pay", "wage", "income", "bonus", "per month", "per year", "annual", "cost", "price"]
    )

def build_custom_recognizers() -> List[PatternRecognizer]:
    return [
        build_swift_bic_recognizer(),
        build_internal_ip_recognizer(),
        build_proprietary_source_recognizer(),
        build_bank_card_recognizer(),
        build_monetary_amount_recognizer(),
    ]