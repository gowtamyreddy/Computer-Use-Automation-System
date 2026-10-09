"""Redaction: one choke point that every log line, transcript, DOM snapshot and artifact passes through.

Three layers, applied in order:
1. exact values registered at runtime (resolved secrets, input parameter values) -> placeholder
2. pattern rules (SSN, PAN, phone, email, dates, money, session tokens in URLs)
3. label-driven rules (the *perception* layer knows a cell is labelled "SSN" or "Name", even when the
   value itself matches no pattern -- e.g. a person's name). Applied where structure is available
   (observations, screenshots), see `SENSITIVE_LABELS`.

Redaction is lossy by design. Where a value must stay comparable across runs (e.g. an extracted
balance in discovery vs replay) we log a short salted hash instead of the value.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

# Labels whose *values* are personal data regardless of format.
SENSITIVE_LABELS = ["Name", "SSN", "Social Security", "Tax ID", "Date of Birth", "DOB", "Phone", "Email", "Address"]

_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    ("url_token", re.compile(r"(?i)([?&](?:sid|session|sessionid|jsessionid|token|auth|key)=)[^&#\s\"']+"), r"\1[REDACTED]"),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    ("email", re.compile(r"\b[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    ("phone", re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}|\b\d{3}-\d{3}-\d{4}\b"), "[PHONE]"),
    ("date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "[DATE]"),
    ("money", re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"), "$[AMOUNT]"),
]

# JS-compatible versions for in-page screenshot masking.
SCREENSHOT_MASK_PATTERNS = [r"\d{3}-\d{2}-\d{4}", r"\(\d{3}\)\s?\d{3}-\d{4}", r"\$\s?\d[\d,]*\.\d{2}", r"\d{1,2}/\d{1,2}/\d{4}"]

# Contiguous 13-19 digits, or 4-digit groups with one consistent separator (4111 1111 1111 1111).
_PAN = re.compile(r"\b\d{13,19}\b|\b\d{4}([ -])\d{4}\1\d{4}\1\d{1,7}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _redact_pan(s: str) -> str:
    """Card numbers: 13-19 digits that pass the Luhn check (avoids eating timestamps / ids)."""
    return _PAN.sub(lambda m: "[PAN]" if _luhn_ok(re.sub(r"\D", "", m.group())) else m.group(), s)


_SALT = os.environ.get("CUA_HASH_SALT", "cua-demo-salt")


def fingerprint(value: str) -> str:
    """Stable, non-reversible token for comparing sensitive values across runs."""
    return "sha256:" + hashlib.sha256((_SALT + value).encode()).hexdigest()[:12]


class Redactor:
    def __init__(self) -> None:
        self._exact: dict[str, str] = {}

    def register(self, value: str | None, placeholder: str) -> None:
        """Register a concrete value (secret / input) to be replaced wherever it appears."""
        if value and len(value) >= 3:
            self._exact[value] = placeholder

    def text(self, s: str) -> str:
        if not s:
            return s
        for value in sorted(self._exact, key=len, reverse=True):
            s = s.replace(value, self._exact[value])
        for _, rx, repl in _PATTERNS:
            s = rx.sub(repl, s)
        return _redact_pan(s)

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def literal_patterns(self) -> list[str]:
        """Registered values as JS-compatible regexes, so screenshots can mask them too."""
        return [re.escape(v) for v in self._exact]

    def is_clean(self, s: str) -> bool:
        return self.text(s) == s


def label_is_sensitive(label: str | None) -> bool:
    if not label:
        return False
    k = label.strip().rstrip(":").lower()
    return any(k == s.lower() for s in SENSITIVE_LABELS)
