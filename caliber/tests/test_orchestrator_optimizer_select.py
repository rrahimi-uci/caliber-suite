"""Tests for the optimizer-selection function."""

from __future__ import annotations

from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.orchestrator.optimizer_select import select_optimizer


def _seed(
    session: Session, *, optimizer_config: dict[str, object] | None = None
) -> tuple[CaliberAgentConfig, CaliberRefinementJob]:
    agent = CaliberAgentConfig(
        agent_id="support-agent",
        experiment_id="exp",
        name="Support",
        owner="@sarah",
        artifact_types=["prompt"],
        eval_thresholds={},
        optimizer_config=optimizer_config or {},
        approval_policy={},
    )
    session.add(agent)
    session.flush()
    session.add(
        CaliberVerificationItem(
            item_id="FB-O",
            agent_id="support-agent",
            category="hallucination",
            free_text="...",
            severity="critical",
        )
    )
    session.flush()
    job = CaliberRefinementJob(
        job_id="RFN-O",
        agent_id="support-agent",
        primary_item_id="FB-O",
        artifact_type="prompt",
        bundle_targets=[],
    )
    session.add(job)
    session.commit()
    return agent, job


def test_default_returns_metaprompt(db_session: Session) -> None:
    agent, job = _seed(db_session)
    assert select_optimizer(agent, job) == "MetaPrompt"


def test_auto_override_falls_through_to_default(db_session: Session) -> None:
    """An explicit ``Auto`` override is treated as "no override"."""
    agent, job = _seed(db_session, optimizer_config={"type": "Auto"})
    assert select_optimizer(agent, job) == "MetaPrompt"


def test_explicit_override_wins(db_session: Session) -> None:
    agent, job = _seed(db_session, optimizer_config={"type": "GEPA"})
    assert select_optimizer(agent, job) == "GEPA"


def test_job_level_override_wins_over_agent_config(db_session: Session) -> None:
    agent, job = _seed(db_session, optimizer_config={"type": "MetaPrompt"})
    job.optimizer_type = "GEPA"
    db_session.commit()
    assert select_optimizer(agent, job) == "GEPA"


def test_override_case_insensitive_for_auto(db_session: Session) -> None:
    """Operator override values aren't lowercase-normalized for arbitrary names,
    but ``"auto"`` in any case is treated as "fall through"."""
    agent, job = _seed(db_session, optimizer_config={"type": "AUTO"})
    assert select_optimizer(agent, job) == "MetaPrompt"


def test_empty_optimizer_config(db_session: Session) -> None:
    agent, job = _seed(db_session, optimizer_config={})
    assert select_optimizer(agent, job) == "MetaPrompt"
