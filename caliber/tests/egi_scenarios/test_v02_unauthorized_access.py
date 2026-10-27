"""V02 public path: unauthorized access is denied before anything is dispatched, and the denial leaves an audit row (B03.5, #688).

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile with admission on). A
caller from another project, a caller whose identity header is forged (no trusted-proxy secret), and a caller asking
for more than their role allows are each refused with nothing stored and nothing built. A run whose grant was revoked
is refused at dispatch and the decision is written to the audit log with identifiers and reasons only: the run's
input, and anything secret-shaped in it, never reaches the audit row.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from caliber.db.models import (
    CaliberAuditLog,
    CaliberPolicyDecision,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflowRun,
)
from caliber.orchestrator import workflow_run_worker as worker_module
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.policy import grants
from caliber.policy.decisions import decide
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call, _published_workflow
from tests.workflow_helpers import make_tool_payload

PREFIX = "/ajax-api/2.0/mlflow/caliber"
SECRET_INPUT = "ssn 078-05-1120 password hunter2-INPUT-SENTINEL"


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The plans the worker builds: anything dispatched shows up here."""
    seen: list[Any] = []
    real = worker_module.build_plan

    def capture(*args: Any, **kwargs: Any) -> Any:
        seen.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(worker_module, "build_plan", capture)
    return seen


def _stored(app: ReferenceApp) -> tuple[int, ...]:
    with app.session_factory() as session:
        return tuple(
            int(session.execute(select(func.count()).select_from(model)).scalar_one())
            for model in (
                CaliberWorkflowRun,
                CaliberRunGrant,
                CaliberRunManifest,
                CaliberPolicyDecision,
            )
        )


def _work(app: ReferenceApp) -> None:
    WorkflowRunWorker(
        session_factory=app.session_factory,
        config=app.client.app.state.config,  # type: ignore[attr-defined]
        event_bus=getattr(app.client.app.state, "event_bus", None),
    )._tick()


def test_foreign_workspace_denied_before_dispatch(
    reference_app: ReferenceApp, two_workspaces: Workspaces, built: list[Any]
) -> None:
    workflow_id = _published_workflow(reference_app)
    before = _stored(reference_app)
    body = {
        "workflow_id": workflow_id,
        "alias": "manual",
        "input": "hi",
        "idempotency_key": "v02-foreign",
        **seed_release(reference_app, PROJECT_A),
    }
    refused = _call(reference_app, "POST", EDITOR_B, PROJECT_B, "/workflow-runs", body)
    assert refused.status_code in (403, 404), refused.text
    assert _stored(reference_app) == before, "nothing was stored for the foreign caller"
    _work(reference_app)
    assert built == [], "and nothing was dispatched"


def test_forged_user_header_without_proxy_secret_denied(
    reference_app: ReferenceApp, two_workspaces: Workspaces, built: list[Any]
) -> None:
    workflow_id = _published_workflow(reference_app)
    before = _stored(reference_app)
    body = {
        "workflow_id": workflow_id,
        "alias": "manual",
        "input": "hi",
        "idempotency_key": "v02-forged",
        **seed_release(reference_app, PROJECT_A),
    }
    forged = {"X-CALIBER-User": EDITOR_A, "X-CALIBER-Project": PROJECT_A}
    for headers in (forged, {**forged, "X-CALIBER-Proxy-Secret": "not-the-secret"}):
        # A CSRF token is public (any client may fetch one); the identity behind the request is what is refused.
        token = reference_app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
        response = reference_app.client.post(
            f"{PREFIX}/workflow-runs", json=body, headers={**headers, "X-CALIBER-CSRF": token}
        )
        assert response.status_code == 401, response.text
        assert "identity" in response.text or "authentication" in response.text
    assert _stored(reference_app) == before
    # control: the same request through the trusted proxy is accepted
    allowed = _call(reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", body)
    assert allowed.status_code == 202, allowed.text
    _work(reference_app)
    assert len(built) == 1, "only the legitimate request was dispatched"


def test_request_for_more_authority_denied(
    reference_app: ReferenceApp, two_workspaces: Workspaces, built: list[Any]
) -> None:
    """An editor cannot do an administrator's action through the API, and a run's grant cannot be used for an action
    it was not issued for: the policy denies it by name and the denial is on record."""
    refused = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, "/tools", make_tool_payload("escalate")
    )
    assert refused.status_code == 403, refused.text

    workflow_id = _published_workflow(reference_app)
    started = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "hi",
            "idempotency_key": "v02-more",
            **seed_release(reference_app, PROJECT_A),
        },
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["data"]["workflow_run_id"]
    with reference_app.session_factory() as session:
        row = session.execute(select(CaliberRunGrant)).scalar_one()
        grant = grants.load_grant(session, run_id=run_id, grant_id=row.grant_id)
        decision = decide(
            session,
            run_id=run_id,
            grant_id=grant.grant_id,
            subject=grant.subject,
            workload=grant.workload,  # the run's own subject and workload; only the action is more than was granted
            action="release.approve",
            resource=f"workflow:{workflow_id}",
            purpose="workflow_run:api",
        )
        session.commit()
        assert decision.allowed is False and decision.reason_codes == ("action_not_allowed",)
        audited = session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.entity_id == decision.decision_id)
        ).scalar_one()
        assert (audited.action, audited.severity) == ("policy_deny", "high")
        assert (audited.details or {})["reason_codes"] == ["action_not_allowed"]
    _work(reference_app)
    assert len(built) == 1, "the run itself, which asked for what it was granted, still executes"


def test_deny_writes_redacted_audit_row(
    reference_app: ReferenceApp, two_workspaces: Workspaces, built: list[Any]
) -> None:
    workflow_id = _published_workflow(reference_app)
    started = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": SECRET_INPUT,
            "idempotency_key": "v02-audit",
            **seed_release(reference_app, PROJECT_A),
        },
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["data"]["workflow_run_id"]
    with reference_app.session_factory() as session:
        grant = session.execute(select(CaliberRunGrant)).scalar_one()
        grants.revoke_grant(
            session, run_id=run_id, grant_id=grant.grant_id, by="@egi", reason="offboarded"
        )
        session.commit()
    _work(reference_app)
    assert built == [], "refused before dispatch"

    with reference_app.session_factory() as session:
        rows = list(
            session.execute(
                select(CaliberAuditLog)
                .where(CaliberAuditLog.entity_type == "policy_decision")
                .order_by(CaliberAuditLog.log_id)
            ).scalars()
        )
        decisions = {
            d.decision_id: d for d in session.execute(select(CaliberPolicyDecision)).scalars()
        }
    assert [(r.action, r.severity) for r in rows] == [
        ("policy_allow", "standard"),
        ("policy_deny", "standard"),
    ]
    deny = rows[-1]
    assert deny.actor == EDITOR_A and deny.entity_id in decisions
    details = deny.details or {}
    assert details["run_id"] == run_id and details["reason_codes"] == ["grant_revoked"]
    assert details["result"] == "deny" and details["grant_id"] == grant.grant_id
    assert decisions[deny.entity_id].reason_codes == ["grant_revoked"]
    for row in rows:
        dumped = json.dumps(row.details)
        assert (
            "hunter2" not in dumped and "078-05-1120" not in dumped and SECRET_INPUT not in dumped
        )
