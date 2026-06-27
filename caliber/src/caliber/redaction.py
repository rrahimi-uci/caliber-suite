"""PII redaction for audit-log payloads.

The audit log records every state change CALIBER makes, including the
``details`` JSON column that often carries free-text fields (reviewer
notes, error messages, dismiss reasons). Those fields can pick up PII
on the way through — an email address copied into a comment, a phone
number in a customer report. We redact at write time so the DB never
holds the raw value; the audit row keeps its shape (the offending
substring just gets a sentinel replacement).

Design:

* :class:`Redactor` is a small dataclass-like object holding a
  precompiled list of regex patterns. ``redact_value`` walks any
  JSON-shaped Python value and substitutes matches in every string it
  finds — recursive across dicts and lists.
* :data:`DEFAULT_PATTERNS` covers the most common operator-visible PII:
  emails, US phone numbers, SSNs. Operators add more via
  ``CaliberConfig.pii_redaction_extra_patterns`` — newline-separated
  regex strings so an operator can include API-key prefixes
  (``sk-...``), AWS access keys (``AKIA...``), JWTs, etc.
* :func:`build_redactor` constructs the runtime instance from config;
  :mod:`caliber.audit` parks the result behind a module-level setter so
  ``record()`` doesn't need to know about app state.

Defaults are intentionally conservative — covering only patterns where
a false-positive (redacting a legitimate ID that happens to match the
regex) has a low cost compared to the false-negative (PII leaking into
an audit row). The configurable extras let operators tune the trade.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

# The default redaction marker. Configurable per-deployment if an
# operator wants a different sentinel (e.g. ``[REDACTED-PII]`` to
# distinguish from manually-redacted text).
DEFAULT_REPLACEMENT: Final[str] = "[REDACTED]"

# Default regex catalog. Each pattern is documented inline so a future
# maintainer can audit the trade-off (what it catches, what it might
# false-positive on).
DEFAULT_PATTERNS: Final[tuple[str, ...]] = (
    # RFC 5322-ish email. Deliberately loose — we want to catch
    # ``alice@example.com`` and ``alice+tag@sub.domain.co.uk`` without
    # writing the full RFC parser.
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    # US-style phone number: optional country code, separators are
    # optional dashes / dots / spaces. Skips bare 10-digit strings to
    # avoid clobbering trace IDs and run IDs.
    r"\b\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b",
    # US Social Security Number. The strict ``\d{3}-\d{2}-\d{4}`` form
    # — no bare-digit variant because the false-positive rate on
    # 9-digit IDs is too high.
    r"\b\d{3}-\d{2}-\d{4}\b",
)


@dataclass(frozen=True)
class Redactor:
    """Recursively redact PII from JSON-shaped values.

    Constructed with a list of compiled regex patterns and a replacement
    string. The replacement is a literal — regex backrefs are not
    supported (and not wanted; the marker should be unambiguous).
    """

    patterns: tuple[re.Pattern[str], ...]
    replacement: str = DEFAULT_REPLACEMENT

    @classmethod
    def from_patterns(
        cls,
        patterns: Iterable[str],
        *,
        replacement: str = DEFAULT_REPLACEMENT,
    ) -> Redactor:
        """Compile a list of pattern strings into a :class:`Redactor`.

        Each pattern is precompiled once so the per-call cost is just
        the regex match against each substring. Errors in any single
        pattern raise at construction time so a typo can't lurk until
        runtime.
        """
        compiled = tuple(re.compile(p) for p in patterns if p)
        return cls(patterns=compiled, replacement=replacement)

    def redact_string(self, value: str) -> str:
        """Apply every pattern to a single string in order.

        Patterns are applied independently, so two patterns matching
        overlapping spans both fire. The order in :data:`patterns` is
        the order they were configured in.
        """
        out = value
        for pattern in self.patterns:
            out = pattern.sub(self.replacement, out)
        return out

    def redact_value(self, value: Any) -> Any:
        """Recursively redact any JSON-shaped value.

        Walks ``dict`` and ``list`` containers; substitutes inside
        string leaves; passes booleans, ints, floats, and ``None``
        through unchanged. Tuples are converted to lists on the way out
        because the destination is JSON and JSON only knows arrays —
        but the input shape (e.g. ``audit.details``) is always a dict
        so this edge case doesn't fire in practice.
        """
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            if not self.patterns:
                return value
            return self.redact_string(value)
        if isinstance(value, dict):
            return {key: self.redact_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact_value(item) for item in value]
        # Unknown type — coerce to string so the redactor still catches
        # PII in a stringified repr. The caller is going to JSON-encode
        # this downstream anyway.
        return self.redact_string(str(value))


# Identity redactor used when redaction is disabled. Returns the input
# value unchanged — no patterns, no compilation, no per-call overhead
# beyond a function call.
IDENTITY_REDACTOR: Final[Redactor] = Redactor(patterns=(), replacement="")


def build_redactor(
    *,
    enabled: bool,
    extra_patterns: str,
    replacement: str = DEFAULT_REPLACEMENT,
) -> Redactor:
    """Build a redactor from raw config values.

    When ``enabled`` is false the function returns :data:`IDENTITY_REDACTOR`,
    so the audit-log path always has *something* to call without
    branching on whether redaction is configured.

    ``extra_patterns`` is a newline-separated string — empty lines are
    skipped, whitespace is stripped. Newline (not comma) so an operator
    can include a comma inside a single regex without escaping.
    """
    if not enabled:
        return IDENTITY_REDACTOR
    extras = tuple(line.strip() for line in extra_patterns.splitlines() if line.strip())
    return Redactor.from_patterns(DEFAULT_PATTERNS + extras, replacement=replacement)
