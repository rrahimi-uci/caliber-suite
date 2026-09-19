"""Smoke tests for the ORM models.

These exercise enough of the schema to catch real-world breakage: that the
defaults fire, that the FK to the agent row works, and that the self-FK
for duplicates works. They don't replace the migration test, which validates
that ``alembic upgrade head`` produces the same shape.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberIncident,
    CaliberReworkTask,
    CaliberVerificationItem,
)


def _make_agent(session: Session, **overrides: object) -> CaliberAgentConfig:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "experiment_id": "exp-support-prod",
        "name": "Support Agent",
        "owner": "@sarah",
        "artifact_types": ["prompt"],
        "eval_thresholds": {"min_aggregate_score": 0.85},
        "optimizer_config": {"type": "MetaPrompt"},
        "approval_policy": {},
    }
    defaults.update(overrides)
    agent = CaliberAgentConfig(**defaults)
    session.add(agent)
    session.commit()
    return agent


def test_agent_config_round_trip(db_session: Session) -> None:
    agent = _make_agent(db_session)
    fetched = db_session.get(CaliberAgentConfig, agent.agent_id)
    assert fetched is not None
    assert fetched.agent_id == "support-agent"
    assert fetched.optimize_for == "quality"  # server default
    assert fetched.enabled is True
    assert fetched.required_approvals == 1
    assert isinstance(fetched.created_at, datetime)
    assert isinstance(fetched.updated_at, datetime)


def test_agent_config_experiment_id_is_unique(db_session: Session) -> None:
    _make_agent(db_session, agent_id="a-1", experiment_id="shared-exp")
    with pytest.raises(IntegrityError):
        _make_agent(db_session, agent_id="a-2", experiment_id="shared-exp")
    db_session.rollback()


def test_verification_item_round_trip(db_session: Session) -> None:
    _make_agent(db_session)
    item = CaliberVerificationItem(
        item_id="FB-047",
        agent_id="support-agent",
        assessment_id="assess-1",
        trace_id="tr-8f2a",
        experiment_id="exp-support-prod",
        category="hallucination",
        free_text="Refund policy fabricated",
        severity="critical",
    )
    db_session.add(item)
    db_session.commit()

    fetched = db_session.get(CaliberVerificationItem, "FB-047")
    assert fetched is not None
    assert fetched.status == "pending"  # server default
    assert fetched.priority == 0  # server default
    assert fetched.duplicate_of_id is None


def test_verification_item_duplicate_self_fk(db_session: Session) -> None:
    """``duplicate_of_id`` is a self-FK; both rows must coexist."""
    _make_agent(db_session)
    original = CaliberVerificationItem(
        item_id="FB-040",
        agent_id="support-agent",
        category="hallucination",
        free_text="Original report",
        severity="critical",
    )
    db_session.add(original)
    db_session.commit()

    dupe = CaliberVerificationItem(
        item_id="FB-041",
        agent_id="support-agent",
        category="hallucination",
        free_text="Same issue reported again",
        severity="critical",
        status="duplicate",
        duplicate_of_id="FB-040",
    )
    db_session.add(dupe)
    db_session.commit()

    fetched = db_session.get(CaliberVerificationItem, "FB-041")
    assert fetched is not None
    assert fetched.duplicate_of_id == "FB-040"


def test_verification_item_assessment_id_is_unique(db_session: Session) -> None:
    """Duplicate ``assessment_id`` is rejected — this is what makes the
    feedback poller idempotent across retries and replica restarts."""
    _make_agent(db_session)
    db_session.add(
        CaliberVerificationItem(
            item_id="FB-100",
            agent_id="support-agent",
            assessment_id="dup-assess",
            category="hallucination",
            free_text="...",
            severity="critical",
        )
    )
    db_session.commit()
    db_session.add(
        CaliberVerificationItem(
            item_id="FB-101",
            agent_id="support-agent",
            assessment_id="dup-assess",
            category="hallucination",
            free_text="...",
            severity="critical",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_select_returns_inserted_rows(db_session: Session) -> None:
    _make_agent(db_session, agent_id="a-1", experiment_id="e-1")
    _make_agent(db_session, agent_id="a-2", experiment_id="e-2")
    rows = db_session.execute(select(CaliberAgentConfig)).scalars().all()
    assert {row.agent_id for row in rows} == {"a-1", "a-2"}


def test_incident_index_allows_history_but_only_one_open_row(db_session: Session) -> None:
    """Resolved history may repeat an objective; its current open state may not."""
    now = datetime.now()
    db_session.add_all(
        [
            CaliberIncident(
                incident_id="INC-history",
                objective="latency<=1",
                signal="latency",
                severity="warning",
                status="resolved",
                detail="old breach",
                opened_at=now,
                resolved_at=now,
            ),
            CaliberIncident(
                incident_id="INC-current",
                objective="latency<=1",
                signal="latency",
                severity="critical",
                status="open",
                detail="current breach",
                opened_at=now,
            ),
        ]
    )
    db_session.commit()

    db_session.add(
        CaliberIncident(
            incident_id="INC-duplicate-open",
            objective="latency<=1",
            signal="latency",
            severity="critical",
            status="open",
            detail="racing replica",
            opened_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()

    rows = db_session.execute(
        select(CaliberIncident).where(CaliberIncident.objective == "latency<=1")
    ).scalars()
    assert {row.incident_id for row in rows} == {"INC-history", "INC-current"}


def test_incident_notification_markers_track_open_and_resolution_independently(
    db_session: Session,
) -> None:
    now = datetime.now()
    row = CaliberIncident(
        incident_id="INC-notification-markers",
        objective="latency<=1",
        signal="latency",
        severity="warning",
        status="resolved",
        detail="independent notification lifecycle",
        opened_at=now,
        resolved_at=now,
        notified_at=now,
    )
    db_session.add(row)
    db_session.commit()

    assert row.notified_at == now
    assert row.resolved_notified_at is None

    row.resolved_notified_at = now
    db_session.commit()

    stored = db_session.get(CaliberIncident, row.incident_id)
    assert stored is not None
    assert stored.notified_at == now
    assert stored.resolved_notified_at == now


# ---------------------------------------------------------------------------
# CaliberReworkTask -- exactly-one-source (job_id XOR workspace_release_id)
# ---------------------------------------------------------------------------
#
# These use raw FK values without creating the referenced job/agent/release/
# project rows: the test engine never turns on SQLite's ``PRAGMA
# foreign_keys``, so only the CHECK and UNIQUE constraints under test are
# actually enforced here (consistent with the rest of this module -- see
# ``test_verification_item_duplicate_self_fk``, which relies on the same
# thing). A real end-to-end row -- with a real job/release behind it -- is
# covered by ``test_routes_rework_tasks.py`` and
# ``test_workspace_release_governance.py``.


def _make_rework_task(session: Session, **overrides: object) -> CaliberReworkTask:
    defaults: dict[str, object] = {
        "task_id": "RWT-model-1",
        "job_id": "RFN-model-1",
        "workspace_release_id": None,
        "agent_id": "support-agent",
        "project_id": None,
        "failure_kind": "machine_gate",
        "reason": "regression gate failed",
        "status": "open",
        "created_by": "@system",
    }
    defaults.update(overrides)
    task = CaliberReworkTask(**defaults)
    session.add(task)
    session.commit()
    return task


def test_rework_task_job_sourced_round_trip(db_session: Session) -> None:
    task = _make_rework_task(db_session)
    fetched = db_session.get(CaliberReworkTask, task.task_id)
    assert fetched is not None
    assert fetched.job_id == "RFN-model-1"
    assert fetched.agent_id == "support-agent"
    assert fetched.workspace_release_id is None
    assert fetched.project_id is None


def test_rework_task_release_sourced_round_trip(db_session: Session) -> None:
    task = _make_rework_task(
        db_session,
        task_id="RWT-release-1",
        job_id=None,
        workspace_release_id="WSREL-model-1",
        agent_id=None,
        project_id="PRJ-model-1",
        failure_kind="release_no_go",
        reason="release rejected in QA",
    )
    fetched = db_session.get(CaliberReworkTask, task.task_id)
    assert fetched is not None
    assert fetched.job_id is None
    assert fetched.agent_id is None
    assert fetched.workspace_release_id == "WSREL-model-1"
    assert fetched.project_id == "PRJ-model-1"
    assert fetched.failure_kind == "release_no_go"


def test_rework_task_exactly_one_source_check_rejects_both_sources_set(
    db_session: Session,
) -> None:
    with pytest.raises(IntegrityError):
        _make_rework_task(
            db_session,
            task_id="RWT-both-sources",
            job_id="RFN-both-sources",
            workspace_release_id="WSREL-both-sources",
        )
    db_session.rollback()


def test_rework_task_exactly_one_source_check_rejects_neither_source_set(
    db_session: Session,
) -> None:
    with pytest.raises(IntegrityError):
        _make_rework_task(
            db_session,
            task_id="RWT-no-source",
            job_id=None,
            workspace_release_id=None,
            agent_id=None,
        )
    db_session.rollback()


def test_rework_task_workspace_release_id_is_unique(db_session: Session) -> None:
    _make_rework_task(
        db_session,
        task_id="RWT-rel-a",
        job_id=None,
        workspace_release_id="WSREL-shared",
        agent_id=None,
        project_id="PRJ-model-1",
        failure_kind="release_no_go",
    )
    with pytest.raises(IntegrityError):
        _make_rework_task(
            db_session,
            task_id="RWT-rel-b",
            job_id=None,
            workspace_release_id="WSREL-shared",
            agent_id=None,
            project_id="PRJ-model-1",
            failure_kind="release_no_go",
        )
    db_session.rollback()
