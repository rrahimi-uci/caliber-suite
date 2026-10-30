"""V04 public path: an approval is decided by HTTP and stale requests remain unusable (B06.4, #735).

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile). A run waits at a node for
approval; a request bound to an effect proposal is made for that node through the real writer. An approver then answers through
the public decision routes (``POST /workflow-runs/{id}/approval/approve|reject``):

* a request at or past its ``expires_at`` is refused ``409 expired`` (the clock is fixed; one microsecond earlier it is
  not expired);
* a request that a newer one replaced (the same node asked again, for the same or a changed intent) is refused
  ``409 superseded``, even when it was already approved and not yet consumed, and superseded is reported before expired;
* the refusal carries no id, hash or requester, and the run, the request, the proposal, the run events and the audit log are
  exactly as they were;
* a live bound request records the HTTP decision but remains unconsumed until the dispatcher claims the proposal.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
)
from caliber.effects.records import record_proposal
from caliber.workflows.approval_policy import ApprovalPolicy
from caliber.workflows.approval_requests import create_approval_request
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call, _published_workflow
from tests.test_effect_proposals import _intent
from tests.test_routes_workflow_runs_async import _seed_waiting_approval

T0 = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
TTL = 3600
NODE = "human_gate"
EVIDENCE = "sha256:" + "e" * 64
MICROSECOND = timedelta(microseconds=1)
EXPIRED = "expired"
SUPERSEDED = "superseded"


def _waiting_run(app: ReferenceApp) -> str:
    """A run of project A that waits for approval at ``NODE``, with a legacy approved request already on the node."""
    workflow_id = _published_workflow(app)
    app.client.app.state.config = app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )
    started = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "refund?",
            "idempotency_key": "egi-v04",
            **seed_release(app, PROJECT_A),
        },
    )
    assert started.status_code == 202, started.text
    run_id: str = started.json()["data"]["workflow_run_id"]
    _seed_waiting_approval(
        app.client,
        run_id,
        approval_status="approved",
        node_id=NODE,
        checkpoint_input_by_port={"input": "refund?"},
    )
    return run_id


def _request(
    app: ReferenceApp, run_id: str, occurrence: str, *, now: datetime = T0, **intent: Any
) -> tuple[str, str]:
    """A bound request for the proposal of ``occurrence`` at ``NODE``: ``(request_id, proposal_id)``."""
    with app.session_factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id=run_id,
            node_id=NODE,
            occurrence_id=occurrence,
            intent=_intent(**intent),
        )
        proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
        assert proposal is not None
        request = create_approval_request(
            session,
            proposal=proposal,
            policy=ApprovalPolicy(),
            evidence_hash=EVIDENCE,
            requested_by=EDITOR_A,
            ttl_seconds=TTL,
            now=now,
        )
        session.commit()
        return request.runtime_approval_id, proposal.proposal_id


def _decide(app: ReferenceApp, run_id: str, action: str, request_id: str) -> Any:
    return _call(
        app,
        "POST",
        APPROVER_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/approval/{action}",
        {"runtime_approval_id": request_id, "reason": "ship it"},
    )


def _snapshot(app: ReferenceApp, run_id: str, *request_ids: str) -> dict[str, Any]:
    """Everything a decision could have touched, as plain values."""
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        requests = {
            r.runtime_approval_id: {
                column.name: getattr(r, column.name) for column in r.__table__.columns
            }
            for r in session.execute(
                select(CaliberRuntimeApprovalRequest).where(
                    CaliberRuntimeApprovalRequest.workflow_run_id == run_id
                )
            ).scalars()
        }
        proposals = {
            p.proposal_id: (p.state, p.version)
            for p in session.execute(select(CaliberEffectProposal)).scalars()
        }
        return {
            "run": {column.name: getattr(run, column.name) for column in run.__table__.columns},
            "requests": requests,
            "proposals": proposals,
            "events": session.execute(
                select(func.count()).select_from(CaliberWorkflowRunEvent)
            ).scalar_one(),
            "audit": session.execute(
                select(func.count()).select_from(CaliberAuditLog)
            ).scalar_one(),
        }


def _clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    monkeypatch.setattr("caliber.routes.workflow_runs._utc_now", lambda: now)


def _assert_refused(response: Any, code: str, *secrets: str) -> None:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["reason_code"] == code and body["status_code"] == 409
    for secret in secrets:
        assert secret and secret not in response.text, (
            "the refusal must carry no id, hash or requester"
        )


@pytest.fixture
def approver(reference_app: ReferenceApp, two_workspaces: Workspaces) -> ReferenceApp:
    """The reference app where ``approver_a`` may approve in project A (the seeded member is an editor)."""
    from caliber.db.models import CaliberProjectMember

    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(CaliberProjectMember.user_id == APPROVER_A)
        ).scalar_one()
        member.role = "reviewer"
        session.commit()
    return reference_app


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_expired_approval_unusable(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    app = approver
    run_id = _waiting_run(app)
    request_id, proposal_id = _request(app, run_id, "occ-1")
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert row is not None
        secrets = (request_id, proposal_id, row.intent_hash or "", row.evidence_hash or "")
        assert row.expires_at == (T0 + timedelta(seconds=TTL)).replace(tzinfo=None)
    expiry = T0 + timedelta(seconds=TTL)
    before = _snapshot(app, run_id)

    # At the instant it ends, and afterwards, it is expired; the clock is the only thing that changed.
    for moment in (expiry, expiry + MICROSECOND, expiry + timedelta(days=30)):
        _clock(monkeypatch, moment)
        _assert_refused(_decide(app, run_id, action, request_id), EXPIRED, *secrets)
    assert _snapshot(app, run_id) == before, "a refused decision changes nothing"

    # An expired request is still listed, as it was requested: expiry is computed, never stored.
    listed = _call(app, "GET", APPROVER_A, PROJECT_A, f"/workflow-runs/{run_id}/approvals")
    assert {r["runtime_approval_id"]: r["status"] for r in listed.json()["data"]}[
        request_id
    ] == "pending"


def test_an_expired_approval_that_was_already_approved_is_still_expired(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = approver
    run_id = _waiting_run(app)
    request_id, _proposal_id = _request(app, run_id, "occ-1")
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert row is not None
        row.status = "approved"
        row.decided_by = "approver_b"
        row.decided_at = T0.replace(tzinfo=None)
        session.commit()
    before = _snapshot(app, run_id)
    _clock(monkeypatch, T0 + timedelta(seconds=TTL))
    _assert_refused(_decide(app, run_id, "approve", request_id), EXPIRED, request_id)
    assert _snapshot(app, run_id) == before


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_superseded_approval_unusable(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    app = approver
    run_id = _waiting_run(app)
    _clock(monkeypatch, T0 + timedelta(minutes=1))
    old_id, old_proposal = _request(app, run_id, "occ-1")
    # The same node is asked again for a changed intent (the version moved on): the first request is replaced.
    new_id, new_proposal = _request(
        app, run_id, "occ-2", now=T0 + timedelta(minutes=2), expected_resource_version="8"
    )
    with app.session_factory() as session:
        old = session.get(CaliberRuntimeApprovalRequest, old_id)
        assert old is not None and old.superseded_by == new_id and old.status == "superseded"
        secrets = (old_id, new_id, old_proposal, new_proposal, old.intent_hash or "", "editor_a")
    before = _snapshot(app, run_id)

    _assert_refused(_decide(app, run_id, action, old_id), SUPERSEDED, *secrets)
    assert _snapshot(app, run_id) == before, "a refused decision changes nothing"

    # The current request is live and can record a decision. It is not consumed by
    # the HTTP route; the dispatcher owns that later transition.
    decided = _decide(app, run_id, action, new_id)
    assert decided.status_code == 200, decided.text
    with app.session_factory() as session:
        current = session.get(CaliberRuntimeApprovalRequest, new_id)
        assert current is not None
        assert current.status == ("approved" if action == "approve" else "rejected")
        assert current.decided_by == APPROVER_A
        assert current.consumed_at is None


def test_an_approved_but_unconsumed_approval_is_superseded_by_a_newer_request(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = approver
    run_id = _waiting_run(app)
    _clock(monkeypatch, T0 + timedelta(minutes=1))
    old_id, _ = _request(app, run_id, "occ-1")
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, old_id)
        assert row is not None
        row.status = "approved"
        row.decided_by = "approver_b"
        row.decided_at = T0.replace(tzinfo=None)
        session.commit()
    new_id, _ = _request(app, run_id, "occ-1b", now=T0 + timedelta(minutes=2), args={"changed": 1})
    before = _snapshot(app, run_id)
    assert before["requests"][old_id]["status"] == "superseded"
    assert before["requests"][old_id]["superseded_by"] == new_id
    assert (
        before["requests"][old_id]["decided_by"] == "approver_b"
    )  # the recorded decision is kept, unusable

    _assert_refused(_decide(app, run_id, "approve", old_id), SUPERSEDED, old_id, new_id)
    assert _snapshot(app, run_id) == before


def test_superseded_is_reported_before_expired(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = approver
    run_id = _waiting_run(app)
    old_id, _ = _request(app, run_id, "occ-1")
    _request(app, run_id, "occ-2", now=T0 + timedelta(minutes=1), expected_resource_version="9")
    _clock(monkeypatch, T0 + timedelta(days=3))  # well past both expiries
    _assert_refused(_decide(app, run_id, "approve", old_id), SUPERSEDED)


def test_a_request_for_another_node_does_not_supersede_and_a_legacy_request_is_untouched(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = approver
    run_id = _waiting_run(app)
    _clock(monkeypatch, T0 + timedelta(minutes=1))
    mine, _ = _request(app, run_id, "occ-1")
    with app.session_factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id=run_id,
            node_id="another_gate",
            occurrence_id="occ-x",
            intent=_intent(),
        )
        proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
        assert proposal is not None
        other = create_approval_request(
            session,
            proposal=proposal,
            policy=ApprovalPolicy(),
            evidence_hash=EVIDENCE,
            requested_by=EDITOR_A,
            ttl_seconds=TTL,
            now=T0,
        ).runtime_approval_id
        session.commit()
    snapshot = _snapshot(app, run_id)
    assert snapshot["requests"][mine]["status"] == "pending"
    assert snapshot["requests"][other]["status"] == "pending"
    legacy = [r for r in snapshot["requests"].values() if r["proposal_id"] is None]
    assert [(r["status"], r["superseded_by"]) for r in legacy] == [("approved", None)]
    mine_response = _decide(app, run_id, "approve", mine)
    assert mine_response.status_code == 200, mine_response.text
    other_response = _decide(app, run_id, "approve", other)
    assert other_response.status_code == 409, other_response.text
    assert "current waiting node" in other_response.json()["detail"]
