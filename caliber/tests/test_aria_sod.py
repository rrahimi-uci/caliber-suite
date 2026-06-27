"""Phase 3 — separation of duties on gated interactions.

Approving an interaction that carries a ``required_scope`` (gated steps do) is
allowed only when the answerer (a) holds that authority and (b) is a distinct
identity from the plan's owner/proposer. Denial is always allowed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.assistant.executor import PlanExecutor, PlanForbiddenError
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAriaInteraction,
    CaliberAriaPlan,
    CaliberAriaPlanStep,
    CaliberJudge,
)
from caliber.ids import (
    new_aria_interaction_id,
    new_aria_plan_id,
    new_aria_plan_step_id,
)

# @sarah is an approver; @reza/@bob are not. @reza (the plan owner) holds
# operator so the judge.create step can run once a distinct approver clears the
# gate (the executor enforces a capability's scopes against the plan owner).
_CFG = CaliberConfig(approver_users="@sarah", operator_users="@reza")


def _seed_gated(
    factory, *, owner: str, judge_name: str
) -> tuple[str, str]:
    """Seed a paused plan + waiting judge.create step + gated interaction."""
    plan_id = new_aria_plan_id()
    step_id = new_aria_plan_step_id()
    iid = new_aria_interaction_id()
    with factory() as db:
        db.add(
            CaliberAriaPlan(
                plan_id=plan_id, goal="g", status="paused", autonomy="ask_each", owner=owner
            )
        )
        db.add(
            CaliberAriaPlanStep(
                step_id=step_id,
                plan_id=plan_id,
                seq=0,
                capability_key="judge.create",
                title="Create judge",
                inputs={"name": judge_name, "instructions": "Rate {{ outputs }}."},
                depends_on=[],
                status="waiting_input",
            )
        )
        db.add(
            CaliberAriaInteraction(
                interaction_id=iid,
                plan_id=plan_id,
                step_id=step_id,
                kind="permission",
                prompt="Approve?",
                required_scope="approver",
                status="pending",
            )
        )
        db.commit()
    return plan_id, iid


def test_owner_cannot_approve_own_gated_step(session_factory) -> None:
    _, iid = _seed_gated(session_factory, owner="@reza", judge_name="sod-owner")
    with pytest.raises(PlanForbiddenError, match="separation of duties"):
        PlanExecutor().answer(
            session_factory=session_factory, config=_CFG, actor="@reza", interaction_id=iid,
            approved=True,
        )


def test_non_approver_cannot_approve_gated_step(session_factory) -> None:
    _, iid = _seed_gated(session_factory, owner="@reza", judge_name="sod-scope")
    # @bob is a distinct identity but lacks approver authority.
    with pytest.raises(PlanForbiddenError, match="approver"):
        PlanExecutor().answer(
            session_factory=session_factory, config=_CFG, actor="@bob", interaction_id=iid,
            approved=True,
        )


def test_distinct_approver_can_approve_and_step_runs(session_factory) -> None:
    plan_id, iid = _seed_gated(session_factory, owner="@reza", judge_name="sod-ok")
    detail = PlanExecutor().answer(
        session_factory=session_factory, config=_CFG, actor="@sarah", interaction_id=iid,
        approved=True,
    )
    assert detail["plan"]["status"] == "completed"
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "sod-ok")).scalars().first()
            is not None
        )


def test_owner_may_deny_own_gated_step(session_factory) -> None:
    # Denial is the safe direction — no authority/SoD gate on it.
    plan_id, iid = _seed_gated(session_factory, owner="@reza", judge_name="sod-deny")
    detail = PlanExecutor().answer(
        session_factory=session_factory, config=_CFG, actor="@reza", interaction_id=iid,
        approved=False,
    )
    assert detail["steps"][0]["status"] == "skipped"
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "sod-deny"))
            .scalars()
            .first()
            is None
        )


# --- route ------------------------------------------------------------------

_ANSWER_API = "/ajax-api/2.0/mlflow/caliber/aria/interactions/{interaction_id}/answer"


def _seed_gated_db(db_session: Session, *, owner: str, judge_name: str) -> str:
    plan_id = new_aria_plan_id()
    step_id = new_aria_plan_step_id()
    iid = new_aria_interaction_id()
    db_session.add(
        CaliberAriaPlan(
            plan_id=plan_id, goal="g", status="paused", autonomy="ask_each", owner=owner
        )
    )
    db_session.add(
        CaliberAriaPlanStep(
            step_id=step_id, plan_id=plan_id, seq=0, capability_key="judge.create",
            title="Create judge", inputs={"name": judge_name, "instructions": "Rate {{ outputs }}."},
            depends_on=[], status="waiting_input",
        )
    )
    db_session.add(
        CaliberAriaInteraction(
            interaction_id=iid, plan_id=plan_id, step_id=step_id, kind="permission",
            prompt="Approve?", required_scope="approver", status="pending",
        )
    )
    db_session.commit()
    return iid


def test_route_owner_approval_forbidden(client: TestClient, db_session: Session) -> None:
    # The default client user is @test (an admin → approver) and also the owner →
    # separation of duties blocks the self-approval with 403.
    iid = _seed_gated_db(db_session, owner="@test", judge_name="route-sod-owner")
    resp = client.post(_ANSWER_API.format(interaction_id=iid), json={"approved": True})
    assert resp.status_code == 403, resp.text


def test_route_distinct_approver_succeeds(client: TestClient, db_session: Session) -> None:
    iid = _seed_gated_db(db_session, owner="@test", judge_name="route-sod-ok")
    # Answer as a distinct admin/approver identity.
    resp = client.post(
        _ANSWER_API.format(interaction_id=iid),
        json={"approved": True},
        headers={"X-CALIBER-User": "@sarah"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["plan"]["status"] == "completed"
