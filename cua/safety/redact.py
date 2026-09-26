"""Redaction: the single choke point every log line and evidence file passes through.

Two layers:
1. Pattern scrubbers for data that is recognizable by shape (SSNs, card and
   account numbers, phones, emails, dollar amounts).
2. A value registry for data that is *not* recognizable by shape (a member's
   name, a balance read as an output). Anything the runtime knows is sensitive -
   a sensitive input it was given, a sensitive output it extracted, a secret it
   resolved - is registered, and every later occurrence is scrubbed verbatim.

Known limit (documented in REPORT): free text that is neither pattern-shaped nor
registered (e.g. a name merely *visible* on a page) is not caught here. That is
why raw page snapshots are not persisted by default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..core.actions import SecretRef


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    Rule("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    Rule("phone", re.compile(r"\(?\b\d{3}\)?[ .-]?\d{3}-\d{4}\b"), "[PHONE]"),
    Rule("amount", re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"), "[AMOUNT]"),
    # remaining long digit runs (cards via Luhn, else account numbers): _scrub_long_digits
)

# Regexes used to black out regions of screenshots (same data classes as above).
SCREENSHOT_MASKS: list[str] = [
    r"\$\s?\d[\d,]*(?:\.\d{2})?",
    r"\(?\d{3}\)?[ .-]?\d{3}-\d{4}",
    r"\d{3}-\d{2}-\d{4}",
]

_LONG_DIGITS = re.compile(r"\b\d(?:[ -]?\d){8,18}\b")


@dataclass
class Redactor:
    rules: tuple[Rule, ...] = DEFAULT_RULES
    _values: dict[str, str] = field(default_factory=dict)

    def register(self, value: str, label: str) -> None:
        """Scrub this exact value everywhere from now on (min length avoids nuking '1')."""
        v = str(value).strip()
        if len(v) >= 3:
            self._values[v] = f"[{label.upper()}]"

    def text(self, s: str) -> str:
        for v in sorted(self._values, key=len, reverse=True):   # longest first
            s = s.replace(v, self._values[v])
        for r in self.rules:          # shaped identifiers first (an SSN is also 9 digits)
            s = r.pattern.sub(r.replacement, s)
        return _LONG_DIGITS.sub(self._scrub_long_digits, s)

    @staticmethod
    def _scrub_long_digits(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return "[CARD]"
        return "[ACCT]"   # 9+ digit runs: account / routing numbers

    def mask_patterns(self) -> list[str]:
        """Screenshot masks: shape-based patterns plus every registered sensitive value."""
        return SCREENSHOT_MASKS + [re.escape(v) for v in self._values]

    def obj(self, value: Any) -> Any:
        """Recursively redact dicts/lists/strings. SecretRefs render as their name only."""
        if isinstance(value, SecretRef):
            return str(value)
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {k: self.obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.obj(v) for v in value]
        return value
