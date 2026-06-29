"""Phase 4 — self-correction: a below-gate step escalates instead of silently passing."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from caliber.assistant import capabilities as caps
from caliber.assistant.capabilities import Capability
from caliber.assistant.executor import PlanExecutor
from caliber.assistant.plans import PlannedStep, PlanService
from caliber.config import CaliberConfig
from caliber.db.models import CaliberAriaInteraction, CaliberAriaPlan

_CFG = CaliberConfig()
_GATE = {"metric": "score", "min": 0.9}


@pytest.fixture
def score_cap():
    """A transient capability that echoes a score from its inputs as evidence."""
    cap = Capability(
        key="test.score",
        title="Scoring step",
        description="returns a score",
        tier="mutate",
        handler=lambda _ctx, args: {"score": args.get("score")},
    )
    caps.register(cap)
    try:
        yield cap
    finally:
        caps.unregister("test.score")


class _ScorePlanner:
    def __init__(self, score: float) -> None:
        self._score = score

    def plan(self, goal, *, capabilities):
        return [
            PlannedStep(
                capability_key="test.score",
                title="Score it",
                inputs={"score": self._score},
                gate=dict(_GATE),
            )
        ]


def _run(session_factory, score: float) -> tuple[str, dict]:
    svc = PlanService(planner=_ScorePlanner(score))
    plan_id = svc.create_plan(
        session_factory=session_factory, goal="score", owner="@reza", autonomy="approve_plan"
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    detail = PlanExecutor().execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    return plan_id, detail


def test_step_at_or_above_gate_completes(score_cap, session_factory) -> None:
    _plan_id, detail = _run(session_factory, 0.95)
    assert detail["plan"]["status"] == "completed"
    assert detail["steps"][0]["status"] == "done"


def test_below_gate_escalates_not_silently_passes(score_cap, session_factory) -> None:
    plan_id, detail = _run(session_factory, 0.5)
    assert detail["plan"]["status"] == "paused"
    assert detail["steps"][0]["status"] == "waiting_input"
    pending = PlanExecutor().list_interactions(session_factory=session_factory, plan_id=plan_id)
    assert len(pending) == 1
    assert pending[0].kind == "confirm"
    assert pending[0].evidence.get("value") == 0.5 and pending[0].evidence.get("min") == 0.9


def test_gate_accept_completes_with_below_result(score_cap, session_factory) -> None:
    plan_id, _detail = _run(session_factory, 0.5)
    ex = PlanExecutor()
    iid = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)[0].interaction_id
    # The plan owner can accept their own below-gate result (no SoD on a confirm).
    detail = ex.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=iid,
        approved=True,
    )
    assert detail["plan"]["status"] == "completed"
    assert detail["steps"][0]["status"] == "done"
    assert detail["steps"][0]["result"].get("score") == 0.5  # the result is preserved


def test_gate_reject_skips_step(score_cap, session_factory) -> None:
    plan_id, _detail = _run(session_factory, 0.5)
    ex = PlanExecutor()
    iid = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)[0].interaction_id
    detail = ex.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=iid,
        approved=False,
    )
    assert detail["steps"][0]["status"] == "skipped"
    with session_factory() as db:
        ia = db.execute(select(CaliberAriaInteraction)).scalars().first()
        assert ia.status == "answered"
        plan = db.get(CaliberAriaPlan, plan_id)
        assert plan.status == "completed"  # nothing left pending
