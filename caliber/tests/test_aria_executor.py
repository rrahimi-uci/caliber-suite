"""Phase 2 — plan executor: autonomy gate, run/ask, interaction answer + resume."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.assistant.executor import (
    PlanExecutionError,
    PlanExecutor,
    PlanForbiddenError,
    gate_decision,
)
from caliber.assistant.plans import PlannedStep, PlanService
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAriaInteraction,
    CaliberAriaPlan,
    CaliberAriaPlanStep,
    CaliberJudge,
)
from caliber.ids import new_aria_plan_id, new_aria_plan_step_id

# The plan owner (@reza) must hold the scope a mutate capability declares
# (judge.create → operator); the executor now enforces capability scopes against
# the plan owner.
_CFG = CaliberConfig(operator_users="@reza")


# --- the gate (pure) --------------------------------------------------------


@pytest.mark.parametrize(
    ("autonomy", "tier", "expected"),
    [
        ("ask_each", "read", "run"),
        ("approve_plan", "read", "run"),
        ("ask_each", "safe", "ask"),
        ("ask_each", "mutate", "ask"),
        ("approve_plan", "mutate", "run"),
        ("auto_guarded", "mutate", "run"),
        # gated always asks — the non-negotiable floor.
        ("ask_each", "gated", "ask"),
        ("approve_plan", "gated", "ask"),
        ("auto_guarded", "gated", "ask"),
    ],
)
def test_gate_decision(autonomy: str, tier: str, expected: str) -> None:
    assert gate_decision(autonomy, tier) == expected


# --- executor (service level) -----------------------------------------------


class _JudgePlanner:
    """Planner that emits a single, fully-specified judge.create step."""

    def __init__(self, name: str) -> None:
        self._name = name

    def plan(self, goal, *, capabilities):
        return [
            PlannedStep(
                capability_key="judge.create",
                title="Create judge",
                inputs={"name": self._name, "instructions": "Rate {{ outputs }}."},
            )
        ]


def _approved_plan(session_factory, *, autonomy: str, judge_name: str) -> str:
    svc = PlanService(planner=_JudgePlanner(judge_name))
    detail = svc.create_plan(
        session_factory=session_factory, goal="make a judge", owner="@reza", autonomy=autonomy
    )
    plan_id = detail["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    return plan_id


def test_execute_runs_mutate_step_under_approve_plan(session_factory) -> None:
    plan_id = _approved_plan(session_factory, autonomy="approve_plan", judge_name="exec-aj")
    detail = PlanExecutor().execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    assert detail["plan"]["status"] == "completed"
    assert detail["steps"][0]["status"] == "done"
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "exec-aj")).scalars().first()
            is not None
        )


def test_execute_pauses_for_interaction_under_ask_each(session_factory) -> None:
    plan_id = _approved_plan(session_factory, autonomy="ask_each", judge_name="ask-aj")
    ex = PlanExecutor()
    detail = ex.execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    assert detail["plan"]["status"] == "paused"
    assert detail["steps"][0]["status"] == "waiting_input"
    pending = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)
    assert len(pending) == 1 and pending[0].kind == "permission"
    # The judge was NOT created — Aria is waiting for permission.
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "ask-aj")).scalars().first()
            is None
        )


def test_answer_approve_resumes_and_runs_step(session_factory) -> None:
    plan_id = _approved_plan(session_factory, autonomy="ask_each", judge_name="resume-aj")
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    iid = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)[0].interaction_id

    # A non-gated ask_each permission is the plan owner's to answer (a distinct
    # approver is only for gated steps — see test_aria_sod).
    detail = ex.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=iid,
        approved=True,
    )
    assert detail["plan"]["status"] == "completed"
    assert detail["steps"][0]["status"] == "done"
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "resume-aj"))
            .scalars()
            .first()
            is not None
        )
        ia = db.execute(select(CaliberAriaInteraction)).scalars().first()
        assert ia.status == "answered" and ia.responded_by == "@reza"


def test_answer_deny_skips_step(session_factory) -> None:
    plan_id = _approved_plan(session_factory, autonomy="ask_each", judge_name="deny-aj")
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    iid = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)[0].interaction_id

    detail = ex.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=iid,
        approved=False,
    )
    assert detail["plan"]["status"] == "completed"  # nothing left pending
    assert detail["steps"][0]["status"] == "skipped"
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "deny-aj")).scalars().first()
            is None
        )


def test_answer_already_answered_raises(session_factory) -> None:
    from caliber.assistant.executor import PlanExecutionError

    plan_id = _approved_plan(session_factory, autonomy="ask_each", judge_name="twice-aj")
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    iid = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)[0].interaction_id
    ex.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=iid,
        approved=True,
    )
    with pytest.raises(PlanExecutionError):
        ex.answer(
            session_factory=session_factory,
            config=_CFG,
            actor="@reza",
            interaction_id=iid,
            approved=True,
        )


def test_third_party_cannot_answer_non_scoped_ask(session_factory) -> None:
    """A non-gated permission ask is the plan owner's to answer — a third party
    (not owner, not admin) is rejected, closing the IDOR where any user could
    approve/deny another user's interaction."""
    plan_id = _approved_plan(session_factory, autonomy="ask_each", judge_name="authz-aj")
    ex = PlanExecutor()
    ex.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    iid = ex.list_interactions(session_factory=session_factory, plan_id=plan_id)[0].interaction_id
    with pytest.raises(PlanForbiddenError, match="owner"):
        ex.answer(
            session_factory=session_factory,
            config=_CFG,
            actor="@stranger",
            interaction_id=iid,
            approved=True,
        )


def test_execute_fails_on_unknown_capability(session_factory) -> None:
    """A plan step naming an unregistered capability fails the plan (not a crash,
    not a silent skip) — guards the `cap is None` branch in _plan_next_action."""

    class _BogusPlanner:
        def plan(self, goal, *, capabilities):
            return [PlannedStep(capability_key="nonexistent.capability", title="Bogus")]

    svc = PlanService(planner=_BogusPlanner())
    plan_id = svc.create_plan(
        session_factory=session_factory, goal="x", owner="@reza", autonomy="auto_guarded"
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    detail = PlanExecutor().execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    assert detail["plan"]["status"] == "failed"
    assert "unknown capability" in (detail["steps"][0]["error"] or "").lower()


def test_execute_blocks_step_when_owner_lacks_capability_scope(session_factory) -> None:
    """The executor enforces a capability's declared scopes against the plan owner:
    an owner without ``operator`` can't run an operator-tier mutate even under
    auto_guarded — the step (and plan) fail instead of silently running."""
    svc = PlanService()
    plan_id = svc.create_plan(
        session_factory=session_factory,
        goal="make a judge",
        owner="@nobody",
        autonomy="auto_guarded",
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@nobody"
    )
    detail = PlanExecutor().execute(
        session_factory=session_factory, config=_CFG, actor="@nobody", plan_id=plan_id
    )
    assert detail["plan"]["status"] == "failed"
    assert "scope" in (detail["steps"][0]["error"] or "").lower()
    with session_factory() as db:
        # The handler never ran, so no judge was created.
        assert db.execute(select(CaliberJudge)).scalars().first() is None


def test_missing_capability_inputs_are_collected_and_typed(session_factory) -> None:
    svc = PlanService()
    plan_id = svc.create_plan(
        session_factory=session_factory,
        goal="create a judge",
        owner="@reza",
        autonomy="auto_guarded",
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    executor = PlanExecutor()

    paused = executor.execute(
        session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id
    )
    assert paused["plan"]["status"] == "paused"
    interaction = executor.list_interactions(session_factory=session_factory, plan_id=plan_id)[0]
    assert interaction.kind == "input"
    assert interaction.evidence["missing"] == ["name", "instructions"]
    assert interaction.evidence["input_schema"]["properties"]["tags"]["type"] == "array"

    completed = executor.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=interaction.interaction_id,
        approved=True,
        response={
            "inputs": {
                "name": "typed-input-judge",
                "instructions": "Rate {{ outputs }}.",
                "tags": ["production"],
            }
        },
    )
    assert completed["plan"]["status"] == "completed"
    assert completed["steps"][0]["inputs"]["tags"] == ["production"]
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "typed-input-judge"))
            .scalars()
            .first()
            is not None
        )


def test_missing_input_interaction_can_be_denied(session_factory) -> None:
    svc = PlanService()
    plan_id = svc.create_plan(
        session_factory=session_factory,
        goal="create a judge",
        owner="@reza",
        autonomy="auto_guarded",
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    executor = PlanExecutor()
    executor.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    interaction = executor.list_interactions(session_factory=session_factory, plan_id=plan_id)[0]
    assert interaction.kind == "input"

    completed = executor.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=interaction.interaction_id,
        approved=False,
    )

    assert completed["plan"]["status"] == "completed"
    assert completed["steps"][0]["status"] == "skipped"
    with session_factory() as db:
        assert db.execute(select(CaliberJudge)).scalars().first() is None


def test_invalid_typed_input_does_not_answer_interaction(session_factory) -> None:
    svc = PlanService()
    plan_id = svc.create_plan(
        session_factory=session_factory,
        goal="create a judge",
        owner="@reza",
        autonomy="auto_guarded",
    )["plan"]["plan_id"]
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    executor = PlanExecutor()
    executor.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    interaction = executor.list_interactions(session_factory=session_factory, plan_id=plan_id)[0]

    with pytest.raises(PlanExecutionError, match="name must be string"):
        executor.answer(
            session_factory=session_factory,
            config=_CFG,
            actor="@reza",
            interaction_id=interaction.interaction_id,
            approved=True,
            response={"inputs": {"name": 7, "instructions": "Rate {{ outputs }}."}},
        )
    pending = executor.list_interactions(session_factory=session_factory, plan_id=plan_id)
    assert [item.interaction_id for item in pending] == [interaction.interaction_id]


def test_planner_output_dependency_removes_redundant_queue_id_prompt(session_factory) -> None:
    svc = PlanService()
    detail = svc.create_plan(
        session_factory=session_factory,
        goal="create a review queue and add review items",
        owner="@reza",
        autonomy="auto_guarded",
    )
    plan_id = detail["plan"]["plan_id"]
    create_step, add_step = detail["steps"]
    assert add_step["depends_on"] == [create_step["step_id"]]
    assert add_step["inputs"]["queue_id"] == {
        "$from_step": create_step["step_id"],
        "path": "queue_id",
    }
    svc.set_status(
        session_factory=session_factory, plan_id=plan_id, status="approved", actor="@reza"
    )
    executor = PlanExecutor()
    executor.execute(session_factory=session_factory, config=_CFG, actor="@reza", plan_id=plan_id)
    first = executor.list_interactions(session_factory=session_factory, plan_id=plan_id)[0]
    resumed = executor.answer(
        session_factory=session_factory,
        config=_CFG,
        actor="@reza",
        interaction_id=first.interaction_id,
        approved=True,
        response={
            "inputs": {
                "name": "dependency queue",
                "questions": [
                    {
                        "key": "quality",
                        "title": "Is this correct?",
                        "type": "pass_fail",
                        "required": True,
                        "target": "feedback",
                    }
                ],
            }
        },
    )
    assert resumed["plan"]["status"] == "paused"
    second = executor.list_interactions(session_factory=session_factory, plan_id=plan_id)[0]
    assert second.step_id == add_step["step_id"]
    assert second.evidence["missing"] == ["trace_ids"]
    assert second.evidence["current_inputs"]["queue_id"]["$from_step"] == create_step["step_id"]


# --- routes -----------------------------------------------------------------

_PLAN_API = "/ajax-api/2.0/mlflow/caliber/aria/plans"
_ANSWER_API = "/ajax-api/2.0/mlflow/caliber/aria/interactions/{interaction_id}/answer"


def _seed_plan_with_judge_step(
    db_session: Session, *, autonomy: str, name: str, status: str = "approved"
) -> str:
    plan_id = new_aria_plan_id()
    db_session.add(
        CaliberAriaPlan(plan_id=plan_id, goal="g", status=status, autonomy=autonomy, owner="@test")
    )
    db_session.add(
        CaliberAriaPlanStep(
            step_id=new_aria_plan_step_id(),
            plan_id=plan_id,
            seq=0,
            capability_key="judge.create",
            title="Create judge",
            inputs={"name": name, "instructions": "Rate {{ outputs }}."},
            depends_on=[],
            status="pending",
        )
    )
    db_session.commit()
    return plan_id


def test_route_execute_runs_under_approve_plan(client: TestClient, db_session: Session) -> None:
    plan_id = _seed_plan_with_judge_step(db_session, autonomy="approve_plan", name="route-aj")
    resp = client.post(f"{_PLAN_API}/{plan_id}/execute")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["plan"]["status"] == "completed"
    assert (
        db_session.execute(select(CaliberJudge).where(CaliberJudge.name == "route-aj"))
        .scalars()
        .first()
        is not None
    )


def test_route_execute_then_answer_flow(client: TestClient, db_session: Session) -> None:
    plan_id = _seed_plan_with_judge_step(db_session, autonomy="ask_each", name="route-ask-aj")
    paused = client.post(f"{_PLAN_API}/{plan_id}/execute").json()["data"]
    assert paused["plan"]["status"] == "paused"

    interactions = client.get(f"{_PLAN_API}/{plan_id}/interactions").json()["data"]
    assert len(interactions) == 1
    iid = interactions[0]["interaction_id"]

    answered = client.post(_ANSWER_API.format(interaction_id=iid), json={"approved": True})
    assert answered.status_code == 200, answered.text
    assert answered.json()["data"]["plan"]["status"] == "completed"


def test_route_execute_draft_conflicts(client: TestClient, db_session: Session) -> None:
    plan_id = _seed_plan_with_judge_step(
        db_session, autonomy="approve_plan", name="draft-aj", status="draft"
    )
    assert client.post(f"{_PLAN_API}/{plan_id}/execute").status_code == 409
