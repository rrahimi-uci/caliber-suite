"""Audit-log helper.

Single entry point — :func:`record` — for writing rows to
``caliber_audit_log``. Centralizing the write makes it easy to reason about
what's auditable and to evolve the row shape without touching every caller.

Callers pass an open SQLAlchemy session and do *not* commit; they should
commit themselves alongside the state change they're auditing so the audit
entry and the state change land in the same transaction. If the state change
rolls back, the audit row rolls back too — auditability is meaningless if it
can disagree with the actual state.

Every call goes through the active :class:`caliber.redaction.Redactor`
before the row is persisted. :func:`configure_redactor` installs the
runtime instance at server startup; tests can swap it in/out to verify
the redaction path. The default is :data:`caliber.redaction.IDENTITY_REDACTOR`
so an audit call from a unit test that never configures the system
behaves exactly as it did before redaction landed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from caliber.db.models import CaliberAuditLog
from caliber.redaction import IDENTITY_REDACTOR, Redactor

# Module-level redactor used by every :func:`record` call. Defaults to
# identity so unconfigured callers (e.g. unit tests that don't go
# through ``create_app``) behave the same as before redaction was added.
# Production deployments call :func:`configure_redactor` from
# :func:`caliber.server.create_app`.
_redactor: Redactor = IDENTITY_REDACTOR


def configure_redactor(redactor: Redactor) -> None:
    """Install the runtime redactor for subsequent :func:`record` calls.

    Idempotent. Called once at server startup from
    :func:`caliber.server.create_app`; tests can swap a different
    instance in and reset to :data:`IDENTITY_REDACTOR` on teardown.
    """
    global _redactor  # noqa: PLW0603 — single module-level singleton; see docstring
    _redactor = redactor


def get_redactor() -> Redactor:
    """Return the active redactor — useful for tests + introspection."""
    return _redactor


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, Any] | None = None,
) -> CaliberAuditLog:
    """Append one row to the audit log.

    Parameters
    ----------
    session:
        An open SQLAlchemy session. **Not committed by this function.** The
        caller commits when its own state change is durable so the audit row
        is part of the same transaction.
    actor:
        Identifier of the user or system component performing the action.
        :data:`caliber.auth.ANONYMOUS` when no identity is available.
    action:
        Short verb describing the action: ``verify``, ``dismiss``, ``approve``,
        ``reject``, ``create_job``, ``advance_stage``, ``rollback``, etc. The
        vocabulary is intentionally small — add new verbs sparingly.
    entity_type:
        One of ``verification_item``, ``refinement_job``, ``approval``,
        ``agent``, ``workflow``.
    entity_id:
        ID of the affected entity.
    details:
        Optional structured context (the from/to state, the verifier's notes,
        the rejection reason, etc.). Keep it small and predictable. The
        active :class:`Redactor` rewrites any PII matches in this payload
        before the row is persisted, so callers don't have to think about
        sanitization at call sites.
    """
    redacted_details: dict[str, Any] | None
    if details is None:
        redacted_details = None
    else:
        redacted = _redactor.redact_value(details)
        # ``redact_value`` always returns a dict when fed a dict; mypy can't
        # infer that from the ``Any`` signature, so we cast at the boundary.
        redacted_details = redacted if isinstance(redacted, dict) else dict(redacted)
    row = CaliberAuditLog(
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=redacted_details,
    )
    session.add(row)
    session.flush()  # so callers can read row.log_id within the same transaction
    return row
