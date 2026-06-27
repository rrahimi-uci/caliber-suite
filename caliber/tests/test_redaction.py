"""Tests for the PII redactor + its audit-log integration.

Three layers:

1. ``Redactor.redact_string`` — pin every default pattern's behavior
   on a clear positive + a clear negative.
2. ``Redactor.redact_value`` — recursive walk over JSON-shaped values.
3. End-to-end through ``audit.record()`` — confirm a state-change call
   site doesn't have to think about redaction; the audit row lands
   sanitized.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from caliber import audit
from caliber.db.models import CaliberAuditLog
from caliber.redaction import (
    DEFAULT_PATTERNS,
    DEFAULT_REPLACEMENT,
    IDENTITY_REDACTOR,
    Redactor,
    build_redactor,
)

# ---------------------------------------------------------------------------
# Pattern coverage
# ---------------------------------------------------------------------------


@pytest.fixture
def default_redactor() -> Redactor:
    return Redactor.from_patterns(DEFAULT_PATTERNS)


def test_email_is_redacted(default_redactor: Redactor) -> None:
    out = default_redactor.redact_string("contact alice@example.com for details")
    assert "alice@example.com" not in out
    assert DEFAULT_REPLACEMENT in out


def test_email_with_plus_addressing_is_redacted(default_redactor: Redactor) -> None:
    out = default_redactor.redact_string("alice+tag@sub.example.co.uk")
    assert DEFAULT_REPLACEMENT in out
    assert "alice" not in out


def test_us_phone_with_separators_is_redacted(default_redactor: Redactor) -> None:
    inputs = [
        "415-555-0123",
        "(415) 555-0123",
        "415.555.0123",
        "+1 415 555 0123",
    ]
    for raw in inputs:
        out = default_redactor.redact_string(f"call {raw} please")
        assert raw not in out, f"phone {raw!r} not redacted"
        assert DEFAULT_REPLACEMENT in out


def test_ssn_strict_form_is_redacted(default_redactor: Redactor) -> None:
    out = default_redactor.redact_string("ssn 123-45-6789 on file")
    assert "123-45-6789" not in out
    assert DEFAULT_REPLACEMENT in out


def test_bare_9_digit_string_is_not_redacted(default_redactor: Redactor) -> None:
    """The default SSN pattern requires the strict dashed form so a
    9-digit run ID or trace ID doesn't get false-positive redacted."""
    out = default_redactor.redact_string("trace 123456789 was retried")
    assert "123456789" in out


def test_bare_text_passes_through_unchanged(default_redactor: Redactor) -> None:
    text = "The candidate prompt was improved with reasoning rubric."
    assert default_redactor.redact_string(text) == text


def test_custom_replacement_marker() -> None:
    redactor = Redactor.from_patterns(DEFAULT_PATTERNS, replacement="<HIDDEN>")
    out = redactor.redact_string("ping alice@example.com")
    assert "<HIDDEN>" in out
    assert "[REDACTED]" not in out


# ---------------------------------------------------------------------------
# Recursive walks
# ---------------------------------------------------------------------------


def test_redact_value_walks_nested_dicts(default_redactor: Redactor) -> None:
    payload = {
        "reason": "user reached out via alice@example.com",
        "context": {
            "phone": "415-555-0123",
            "notes": ["see slack thread", "fwd: alice@example.com"],
        },
    }
    out = default_redactor.redact_value(payload)
    assert "alice@example.com" not in str(out)
    assert "415-555-0123" not in str(out)
    # Shape preserved.
    assert isinstance(out, dict)
    assert isinstance(out["context"], dict)
    assert isinstance(out["context"]["notes"], list)


def test_redact_value_passes_through_non_string_leaves(default_redactor: Redactor) -> None:
    payload = {"count": 42, "score": 0.95, "approved": True, "tags": None}
    out = default_redactor.redact_value(payload)
    assert out == payload


def test_redact_value_stringifies_unknown_types(default_redactor: Redactor) -> None:
    """A leaf the redactor doesn't recognize gets coerced to ``str()``
    and run through the patterns. Catches PII inside ``__repr__`` of
    custom objects."""

    class _Custom:
        def __repr__(self) -> str:
            return "user=alice@example.com"

    out = default_redactor.redact_value(_Custom())
    assert "alice@example.com" not in str(out)
    assert DEFAULT_REPLACEMENT in str(out)


def test_identity_redactor_passes_everything_through() -> None:
    """The identity redactor is what audit.record() falls back to when
    redaction is disabled; it must not mutate."""
    payload = {"email": "alice@example.com", "phone": "415-555-0123"}
    assert IDENTITY_REDACTOR.redact_value(payload) == payload


# ---------------------------------------------------------------------------
# build_redactor (config glue)
# ---------------------------------------------------------------------------


def test_build_redactor_disabled_returns_identity() -> None:
    redactor = build_redactor(enabled=False, extra_patterns="")
    assert redactor is IDENTITY_REDACTOR


def test_build_redactor_enables_default_patterns_only() -> None:
    redactor = build_redactor(enabled=True, extra_patterns="")
    out = redactor.redact_value({"e": "alice@example.com"})
    assert "[REDACTED]" in out["e"]


def test_build_redactor_includes_extra_patterns() -> None:
    """A deployment-specific token (here: a fake OpenAI-style key) gets
    redacted alongside the defaults."""
    redactor = build_redactor(
        enabled=True,
        extra_patterns="sk-[A-Za-z0-9]{20,}\n",
    )
    out = redactor.redact_string("token: sk-AbCdEfGhIjKlMnOpQrStUv")
    assert "sk-AbCdEfGhIjKlMnOpQrStUv" not in out
    assert "[REDACTED]" in out


def test_build_redactor_skips_blank_extra_lines() -> None:
    """A trailing newline or whitespace-only line shouldn't compile as
    an empty regex (which would match every position)."""
    redactor = build_redactor(
        enabled=True,
        extra_patterns="\n\n   \nsk-[A-Za-z0-9]{20,}\n\n",
    )
    assert redactor.redact_string("hello world") == "hello world"


# ---------------------------------------------------------------------------
# audit.record() integration
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_with_default_redactor() -> Iterator[None]:
    """Install the default-pattern redactor into the audit module for
    the duration of the test, then restore the previous value."""
    previous = audit.get_redactor()
    audit.configure_redactor(build_redactor(enabled=True, extra_patterns=""))
    try:
        yield
    finally:
        audit.configure_redactor(previous)


def test_audit_record_redacts_pii_in_details(
    db_session: Session, audit_with_default_redactor: None
) -> None:
    _ = audit_with_default_redactor
    audit.record(
        db_session,
        actor="@reviewer",
        action="dismiss",
        entity_type="verification_item",
        entity_id="FB-1",
        details={
            "reason": "Customer alice@example.com phoned in 415-555-0123",
            "ticket": "T-99",
        },
    )
    db_session.commit()

    row = db_session.query(CaliberAuditLog).filter_by(entity_id="FB-1").one()
    assert row.details is not None
    reason = row.details["reason"]
    assert "alice@example.com" not in reason
    assert "415-555-0123" not in reason
    assert "[REDACTED]" in reason
    # Non-PII fields pass through untouched.
    assert row.details["ticket"] == "T-99"


def test_audit_record_with_none_details_does_not_crash(db_session: Session) -> None:
    """``details=None`` is the call-shape the audit-record path uses
    for "no extra context" actions. Must not blow up regardless of
    whether a redactor is installed."""
    audit.record(
        db_session,
        actor="@reviewer",
        action="dismiss",
        entity_type="verification_item",
        entity_id="FB-NONE",
        details=None,
    )
    db_session.commit()
    row = db_session.query(CaliberAuditLog).filter_by(entity_id="FB-NONE").one()
    assert row.details is None


def test_audit_record_without_redactor_keeps_pii(db_session: Session) -> None:
    """The default fallback is the identity redactor. A test that
    never calls ``configure_redactor`` still gets the same behavior as
    pre-redaction code — important so unit tests for unrelated paths
    don't have to set anything up."""
    previous = audit.get_redactor()
    audit.configure_redactor(IDENTITY_REDACTOR)
    try:
        audit.record(
            db_session,
            actor="@reviewer",
            action="dismiss",
            entity_type="verification_item",
            entity_id="FB-IDENT",
            details={"reason": "see alice@example.com"},
        )
        db_session.commit()
        row = db_session.query(CaliberAuditLog).filter_by(entity_id="FB-IDENT").one()
        assert row.details is not None
        assert "alice@example.com" in row.details["reason"]
    finally:
        audit.configure_redactor(previous)
