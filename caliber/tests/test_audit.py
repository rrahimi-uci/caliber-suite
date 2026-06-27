"""Tests for the audit-log helper."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.audit import record
from caliber.db.models import CaliberAuditLog


def test_record_inserts_row(db_session: Session) -> None:
    log = record(
        db_session,
        actor="@sarah",
        action="verify",
        entity_type="verification_item",
        entity_id="FB-001",
        details={"severity": "critical"},
    )
    db_session.commit()

    assert log.log_id is not None
    rows = db_session.execute(select(CaliberAuditLog)).scalars().all()
    assert len(rows) == 1
    only = rows[0]
    assert only.actor == "@sarah"
    assert only.action == "verify"
    assert only.entity_type == "verification_item"
    assert only.entity_id == "FB-001"
    assert only.details == {"severity": "critical"}
    assert only.timestamp is not None


def test_record_details_optional(db_session: Session) -> None:
    record(
        db_session,
        actor="system",
        action="poll_tick",
        entity_type="agent",
        entity_id="support-agent",
    )
    db_session.commit()
    row = db_session.execute(select(CaliberAuditLog)).scalar_one()
    assert row.details is None


def test_record_does_not_commit_on_its_own(db_session: Session) -> None:
    """Critical contract: the helper must not commit. The caller controls the
    transaction so audit rows are atomic with the state change they describe."""
    record(
        db_session,
        actor="system",
        action="test",
        entity_type="agent",
        entity_id="a",
    )
    # No commit yet — the row should still be visible in this session (flush did its job)
    # but should disappear on rollback.
    db_session.rollback()
    rows = db_session.execute(select(CaliberAuditLog)).scalars().all()
    assert rows == []
