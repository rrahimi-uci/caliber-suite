"""`P2` (isolation closure, item 1, slice 5): visibility gaps on
``/caliber/workflows/{workflow_id}/calibration/*``.

``get_options`` and ``create_run`` (via ``enqueue_workflow_calibration_run``)
both looked their workflow up with a bare ``session.get`` -- no visibility
check at all -- and ``enqueue_workflow_calibration_run`` did the same for the
target agent. A non-member could read calibration options for (or queue a
calibration run against) another project's workflow or agent by id.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAgentConfig, CaliberWorkflow

PREFIX = "/ajax-api/2.0/mlflow/caliber"

# Deliberately not in the test suite's permissive admin list.
STRANGER = "@stranger-wf-cal"


def _grant_operator(client: TestClient, *users: str) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": ",".join(users)}
    )


def _seed_hidden_workflow(session: Session, **overrides: object) -> CaliberWorkflow:
    defaults: dict[str, object] = {
        "workflow_id": "WF-hidden",
        "name": "hidden-workflow",
        "description": "",
        "owner": "@owner",
        "status": "active",
        "visibility": "project",
        "project_id": "P-hidden",
    }
    defaults.update(overrides)
    wf = CaliberWorkflow(**defaults)
    session.add(wf)
    session.commit()
    return wf


def test_get_options_hides_a_hidden_workflow(client: TestClient, db_session: Session) -> None:
    _seed_hidden_workflow(db_session)

    resp = client.get(
        f"{PREFIX}/workflows/WF-hidden/calibration/options",
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text


def test_create_run_hides_a_hidden_workflow(client: TestClient, db_session: Session) -> None:
    _seed_hidden_workflow(db_session)
    _grant_operator(client, STRANGER)

    resp = client.post(
        f"{PREFIX}/workflows/WF-hidden/calibration/runs",
        json={"agent_id": "whatever-agent"},
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text


def test_create_run_hides_a_hidden_agent_behind_a_visible_workflow(
    client: TestClient, db_session: Session
) -> None:
    """A workflow the caller CAN see (their own, personal) must not let
    them queue a calibration run against another project's agent."""
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-visible",
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
            agent_id="hidden-agent-wf-cal",
            experiment_id="exp-hidden-wf-cal",
            name="Hidden Agent",
            owner="@owner",
            enabled=True,
            visibility="project",
            project_id="P-hidden",
        )
    )
    db_session.commit()
    _grant_operator(client, STRANGER)

    resp = client.post(
        f"{PREFIX}/workflows/WF-visible/calibration/runs",
        json={"agent_id": "hidden-agent-wf-cal"},
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 400, resp.text
    assert "not registered" in resp.json()["detail"]
