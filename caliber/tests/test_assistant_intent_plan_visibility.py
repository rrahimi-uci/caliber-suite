"""`P2-K`/`P2-L` (isolation closure, item 1, slices 5/6): the "legacy
``assistant/service.py`` intent-plan dispatch identity gap" both rows named
as a still-open, separately-deferred plumbing task.

That characterization went stale the day after it was written: `P2-M`
(commit ``ff390c39a731``, "fix(authz): thread identity through assistant
turns") gave ``AssistantService.execute_intent_plan`` a real ``identity``
parameter and threaded it into every adapter that already had a
visibility-aware route helper to delegate to -- including
``_execute_workflow_calibration`` -> ``routes.workflow_calibration.
enqueue_workflow_calibration_run``, the exact call site `P2-K`'s row quoted
as "still passes ``None``". `P2-M`'s own row and this session's follow-up
audit confirmed the code is fixed; what was missing was proof through the
*actual* dispatch entrypoint (``execute_intent_plan``, not the private
``_execute_workflow_calibration`` adapter called directly) and the live HTTP
route a caller actually uses (``POST .../plans/execute``), plus reconciling
`P2-K`'s/`P2-L`'s stale trailers and a matching stale docstring in
``routes/workflow_calibration.py``. This file supplies that missing
end-to-end proof.

Note on shape: unlike the direct REST routes (``create_run``, which surfaces
a bare 404), a refusal reached through the intent-plan dispatch path stays
inside `execute_intent_plan`'s uniform error envelope -- HTTP 201 with an
operation ``status: "failed"`` and the underlying ``HTTPException`` detail
in ``result.error``/``result.warnings``. That envelope is pre-existing,
applies to every adapter error in this dispatch path (not just visibility
refusals), and is out of scope for this fix; the assertions below match it
rather than asserting a bare 404, and instead prove the authorization
outcome that matters: no calibration item/job gets queued against the
hidden workflow/agent.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
    CaliberWorkflow,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"

# Deliberately not in the test suite's permissive admin list.
STRANGER = "@stranger-intent-plan"


def _grant_operator(client: TestClient, *users: str) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": ",".join(users)}
    )


def _create_session(client: TestClient, user: str) -> str:
    resp = client.post(
        f"{PREFIX}/sessions",
        headers={"X-CALIBER-User": user},
        json={"title": "intent-plan-visibility"},
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["data"]["session_id"])


def test_execute_plan_hides_a_hidden_workflow_via_intent_dispatch(
    client: TestClient, db_session: Session
) -> None:
    """The full route -> service.execute_intent_plan -> adapter chain must
    refuse a `run_workflow_calibration` intent against a workflow the caller
    cannot see, exactly as `create_run` (the direct REST route) already does
    -- proving the identity resolved for the HTTP request genuinely reaches
    the legacy intent-plan dispatch path end to end, not just the private
    adapter method in isolation."""
    db_session.add(
        CaliberAgentConfig(
            agent_id="hidden-agent-intent-plan",
            experiment_id="exp-hidden-intent-plan",
            name="Hidden Agent",
            owner="@owner",
            project_id="P-hidden",
            visibility="project",
            enabled=True,
        )
    )
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-hidden-intent-plan",
            name="hidden-workflow",
            description="",
            owner="@owner",
            status="active",
            visibility="project",
            project_id="P-hidden",
        )
    )
    db_session.commit()
    _grant_operator(client, STRANGER)
    sid = _create_session(client, STRANGER)

    planned = client.post(
        f"{PREFIX}/sessions/{sid}/plans",
        headers={"X-CALIBER-User": STRANGER},
        json={
            "intent_name": "run_workflow_calibration",
            "slot_overrides": {
                "workflow_id": "WF-hidden-intent-plan",
                "agent_id": "hidden-agent-intent-plan",
            },
        },
    )
    assert planned.status_code == 201, planned.text
    plan_id = planned.json()["data"]["plan_id"]

    executed = client.post(
        f"{PREFIX}/sessions/{sid}/plans/execute",
        headers={"X-CALIBER-User": STRANGER},
        json={"plan_id": plan_id, "confirm": True},
    )
    assert executed.status_code == 201, executed.text
    payload = executed.json()["data"]
    assert payload["status"] == "failed"
    assert "not found" in payload["result"]["error"]
    assert "WF-hidden-intent-plan" in payload["result"]["error"]

    # No verification item / refinement job was queued against the hidden
    # workflow -- the refusal happened before any write, same as the direct
    # REST route.
    items = (
        db_session.execute(
            select(CaliberVerificationItem).where(
                CaliberVerificationItem.workflow_id == "WF-hidden-intent-plan"
            )
        )
        .scalars()
        .all()
    )
    jobs = (
        db_session.execute(
            select(CaliberRefinementJob).where(
                CaliberRefinementJob.workflow_id == "WF-hidden-intent-plan"
            )
        )
        .scalars()
        .all()
    )
    assert items == []
    assert jobs == []


def test_execute_plan_hides_a_hidden_agent_behind_a_visible_workflow(
    client: TestClient, db_session: Session
) -> None:
    """A workflow the caller CAN see (their own, personal) must not let the
    intent-plan dispatch path queue a calibration run against another
    project's agent -- the sibling scenario `P2-K`'s acceptance criteria
    named for the direct REST route, mirrored here for the dispatch path."""
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-visible-intent-plan",
            name="visible-workflow",
            description="",
            owner=STRANGER,
            status="active",
            visibility="user",
            project_id=None,
        )
    )
    db_session.add(
        CaliberAgentConfig(
            agent_id="hidden-agent-behind-visible-wf",
            experiment_id="exp-hidden-behind-visible-wf",
            name="Hidden Agent",
            owner="@owner",
            enabled=True,
            visibility="project",
            project_id="P-hidden",
        )
    )
    db_session.commit()
    _grant_operator(client, STRANGER)
    sid = _create_session(client, STRANGER)

    planned = client.post(
        f"{PREFIX}/sessions/{sid}/plans",
        headers={"X-CALIBER-User": STRANGER},
        json={
            "intent_name": "run_workflow_calibration",
            "slot_overrides": {
                "workflow_id": "WF-visible-intent-plan",
                "agent_id": "hidden-agent-behind-visible-wf",
            },
        },
    )
    assert planned.status_code == 201, planned.text
    plan_id = planned.json()["data"]["plan_id"]

    executed = client.post(
        f"{PREFIX}/sessions/{sid}/plans/execute",
        headers={"X-CALIBER-User": STRANGER},
        json={"plan_id": plan_id, "confirm": True},
    )
    assert executed.status_code == 201, executed.text
    payload = executed.json()["data"]
    assert payload["status"] == "failed"
    assert "not registered" in payload["result"]["error"]

    jobs = (
        db_session.execute(
            select(CaliberRefinementJob).where(
                CaliberRefinementJob.workflow_id == "WF-visible-intent-plan"
            )
        )
        .scalars()
        .all()
    )
    assert jobs == []
