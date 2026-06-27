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

from caliber.db.models import CaliberAgentConfig, CaliberVerificationItem


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
