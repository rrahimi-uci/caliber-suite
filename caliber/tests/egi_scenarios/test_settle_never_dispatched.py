"""#1074: a run that ends settles what it never dispatched, and an approved decision can be withdrawn before dispatch consumes it.

Drives the real routes (reject, cancel, the new ``POST .../approvals/{id}/withdraw``) and the real dispatcher claim against the
migrated reference app (PostgreSQL; the same logic on SQLite in the development profile):

* **Settlement at the end of a run.** Rejecting a bound approval, or cancelling the run, moves the run's ``proposed`` proposals to
  ``failed`` (reason ``approval_rejected`` / ``run_cancelled``) and withdraws its live bound requests in the transaction that ends
  the run; the dispatcher's claim is then refused and nothing is sent. A proposal that was already dispatched, or a request
  already consumed, is never touched.
* **Withdrawal.** An approved, unconsumed decision is taken back by someone with the approval authority (never the requester
  alone), audited, idempotent; a consumed one is ``409 already_consumed`` and never withdrawn; the claim refuses a withdrawn
  one (``withdrawn``) and consumes nothing.
* **Races** (PostgreSQL only; the contenders are made to meet on the run lock as in ``test_approval_consume_race``): withdraw
  against dispatch, cancel against dispatch, and two withdrawals. At most one side wins, whichever arrives first, and a consumed
  request is never withdrawn and a settled one never dispatched.
* **Backstop and restart.** A run that ended before this existed (its proposal still ``proposed``) cannot be claimed
  (``run_ended``) and is settled by the owner-only sweep, which is idempotent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
)
from caliber.effects.dispatcher import EffectOutcome
from caliber.effects.records import proposal_state, record_proposal
from caliber.effects.run_settlement import settle_ended_runs
from caliber.workflows.approval_policy import ApprovalPolicy
from caliber.workflows.approval_requests import create_approval_request
from tests.effect_helpers import admit_fake_backend, leased
from tests.egi_scenarios import test_approval_consume_race as _race
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    ReferenceApp,
)
from tests.egi_scenarios.test_approval_consume_race import (
    BACKEND_KIND,
    PREFIX,
    CountingBackend,
    CrashAfterClaim,
    _consumed_once,
    _decide,
    _dispatch,
    _holding_run_lock,
    _need_postgresql,
    _prepare,
    _queue,
    _refused,
    _unconsumed,
)
from tests.egi_scenarios.test_v04_stale_approval import (
    EVIDENCE,
    T0,
    TTL,
    _snapshot,
)
from tests.test_effect_proposals import _intent

#: The fixtures of the B06.4 scenarios, shared by name (importing them would be flagged as redefined in every test).
approver = _race.approver
backend = _race.backend


def _post(
    app: ReferenceApp,
    path: str,
    *,
    user: str = APPROVER_A,
    project: str = PROJECT_A,
    body: dict[str, Any] | None = None,
) -> Any:
    """A POST as ``user`` on a client of its own (the shared one runs every request on one event loop)."""
    client = TestClient(app.client.app)
    headers = app.headers(user, project)
    token = client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return client.request(
        "POST", f"{PREFIX}{path}", json=body or {}, headers={**headers, "X-CALIBER-CSRF": token}
    )


def _withdraw(
    app: ReferenceApp,
    run_id: str,
    approval_id: str,
    *,
    user: str = APPROVER_A,
    project: str = PROJECT_A,
    body: dict[str, Any] | None = None,
) -> Any:
    return _post(
        app,
        f"/workflow-runs/{run_id}/approvals/{approval_id}/withdraw",
        user=user,
        project=project,
        body={"reason": "no longer wanted"} if body is None else body,
    )


def _cancel(app: ReferenceApp, run_id: str) -> Any:
    return _post(app, f"/workflow-runs/{run_id}/cancel", user=EDITOR_A, body={"reason": "stop it"})


def _request_row(app: ReferenceApp, approval_id: str) -> dict[str, Any]:
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _proposal(app: ReferenceApp, proposal_id: str) -> Any:
    with app.session_factory() as session:
        return proposal_state(session, proposal_id)


def _audits(app: ReferenceApp, action: str) -> int:
    with app.session_factory() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(CaliberAuditLog)
                .where(CaliberAuditLog.action == action)
            ).scalar_one()
        )


def _events(app: ReferenceApp, run_id: str, event_type: str) -> list[CaliberWorkflowRunEvent]:
    with app.session_factory() as session:
        return list(
            session.execute(
                select(CaliberWorkflowRunEvent).where(
                    CaliberWorkflowRunEvent.workflow_run_id == run_id,
                    CaliberWorkflowRunEvent.event_type == event_type,
                )
            ).scalars()
        )


def _run_status(app: ReferenceApp, run_id: str) -> tuple[str, str | None]:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        return run.status, run.error_code


def _refusal_body(response: Any, status: int, code: str, *secrets: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["reason_code"] == code
    for secret in secrets:
        assert secret and secret not in response.text, "the refusal must carry no id or principal"


# ------------------------------------------------------------------ settlement when the run ends


def test_rejecting_a_bound_request_fails_the_run_and_settles_its_proposals_and_other_requests(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """The rejected request is ``rejected``; every other live bound request is withdrawn and every ``proposed`` proposal fails."""
    run_id, rejected_id, rejected_p, lease = _prepare(
        approver, monkeypatch, occurrence="rej-a", decide=False
    )
    # A second proposal on another node, with an approved decision: it is not the one being rejected.
    with approver.session_factory() as session:
        recorded = record_proposal(
            session,
            workflow_run_id=run_id,
            node_id="other_gate",
            occurrence_id="rej-b",
            intent=_intent(),
        )
        proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
        assert proposal is not None
        second = create_approval_request(
            session,
            proposal=proposal,
            policy=ApprovalPolicy(),
            evidence_hash=EVIDENCE,
            requested_by=EDITOR_A,
            ttl_seconds=TTL,
            now=T0,
        )
        second.status = "approved"
        second.decided_by = APPROVER_A
        other_id, other_p = second.runtime_approval_id, proposal.proposal_id
        session.commit()
    legacy_before = _snapshot(approver, run_id)["requests"]

    rejected = _decide(approver, run_id, rejected_id, action="reject")

    assert rejected.status_code == 200, rejected.text
    assert _run_status(approver, run_id) == ("failed", "approval_rejected")
    for proposal_id in (rejected_p, other_p):
        state = _proposal(approver, proposal_id)
        assert (state.state, state.reason) == ("failed", "approval_rejected")
        assert state.dispatched_at is None and state.receipt_id is None
    assert _request_row(approver, rejected_id)["status"] == "rejected"
    assert _request_row(approver, other_id)["status"] == "withdrawn"
    assert (
        _request_row(approver, other_id)["decided_by"] == APPROVER_A
    )  # the decision is not rewritten
    # a legacy node-level request guards no effect and keeps its status
    for request_id, before in legacy_before.items():
        if request_id not in {rejected_id, other_id}:
            assert _request_row(approver, request_id)["status"] == before["status"]
    assert _audits(approver, "settle_run_effects") == 1
    # the claim is refused and nothing is sent or consumed
    _refused(_dispatch(approver, rejected_p, rejected_id, lease), "proposal_not_approved")
    assert backend.calls == 0


@pytest.mark.parametrize("decided", [False, True])
def test_cancelling_the_run_settles_its_proposal_and_request_and_the_claim_is_refused(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    decided: bool,
) -> None:
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence=f"cancel-{decided}", decide=decided
    )

    cancelled = _cancel(approver, run_id)

    assert cancelled.status_code == 200, cancelled.text
    assert _run_status(approver, run_id)[0] == "cancelled"
    state = _proposal(approver, proposal_id)
    assert (state.state, state.reason) == ("failed", "run_cancelled")
    assert state.version == 2 and state.settled_at is not None
    row = _request_row(approver, approval_id)
    assert (row["status"], row["consumed_at"], row["superseded_by"]) == ("withdrawn", None, None)
    assert (_audits(approver, "settle_run_effects")) == 1
    _refused(_dispatch(approver, proposal_id, approval_id, lease), "proposal_not_approved")
    assert backend.calls == 0
    with approver.session_factory() as session:
        assert (
            session.get(CaliberRuntimeApprovalRequest, approval_id).consumed_at is None  # type: ignore[union-attr]
        )


def test_a_dispatched_proposal_and_a_consumed_request_are_never_touched_by_the_end_of_the_run(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="sent")
    outcome = _dispatch(approver, proposal_id, approval_id, lease)
    assert isinstance(outcome, EffectOutcome) and outcome.state == "committed"
    before = (_proposal(approver, proposal_id), _request_row(approver, approval_id))

    assert _cancel(approver, run_id).status_code == 200

    assert (_proposal(approver, proposal_id), _request_row(approver, approval_id)) == before
    assert _audits(approver, "settle_run_effects") == 0, (
        "nothing was settled, so nothing is audited"
    )
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")


def test_a_proposal_of_a_run_that_ended_before_this_existed_cannot_be_claimed_and_the_sweep_settles_it(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """Data written by an older build: the run is ``failed``, its proposal still ``proposed``, its request still approved."""
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="legacy")
    with approver.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status, run.error_code = "failed", "boom"
        session.commit()

    # the gap before the sweep: the claim still refuses, with its own stable code, sending and consuming nothing
    _refused(_dispatch(approver, proposal_id, approval_id, lease), "run_ended")
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)

    assert settle_ended_runs(approver.session_factory, lease) == 1

    state = _proposal(approver, proposal_id)
    assert (state.state, state.reason) == ("failed", "run_failed")
    assert _request_row(approver, approval_id)["status"] == "withdrawn"
    assert settle_ended_runs(approver.session_factory, lease) == 0, "a second sweep changes nothing"
    assert _audits(approver, "settle_run_effects") == 1
    _refused(_dispatch(approver, proposal_id, approval_id, lease), "proposal_not_approved")
    assert backend.calls == 0


def test_the_sweep_never_touches_a_dispatched_or_unknown_proposal_of_an_ended_run(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proposal was claimed (``dispatched``) and the process died: the run later failed. The reconciler owns it, not the sweep."""
    crashing = CountingBackend(crash=True)
    admit_fake_backend(monkeypatch, BACKEND_KIND, crashing)
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="died")
    with pytest.raises(CrashAfterClaim):
        _dispatch(approver, proposal_id, approval_id, lease)
    with approver.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "failed"
        session.commit()
    before = (_proposal(approver, proposal_id), _request_row(approver, approval_id))
    assert before[0].state == "dispatched" and before[1]["consumed_at"] is not None

    assert settle_ended_runs(approver.session_factory, lease) == 0

    assert (_proposal(approver, proposal_id), _request_row(approver, approval_id)) == before


# ------------------------------------------------------------------ withdrawing an approved decision


def test_an_approved_decision_is_withdrawn_audited_and_the_claim_refuses_it(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="wd")
    before = _request_row(approver, approval_id)

    response = _withdraw(approver, run_id, approval_id)

    assert response.status_code == 200, response.text
    ack = response.json()["data"]
    assert (ack["workflow_run_id"], ack["runtime_approval_id"], ack["status"]) == (
        run_id,
        approval_id,
        "withdrawn",
    )
    row = _request_row(approver, approval_id)
    assert row["status"] == "withdrawn"
    # only the status moved: the decision, the approvers and every frozen binding stay as recorded
    assert {k: v for k, v in row.items() if k != "status"} == {
        k: v for k, v in before.items() if k != "status"
    }
    assert _audits(approver, "withdraw_workflow_run_runtime_approval") == 1
    [event] = _events(approver, run_id, "workflow.run.approval.withdrawn")
    assert event.payload["withdrawn_by"] == APPROVER_A
    assert event.payload["reason"] == "no longer wanted"
    with approver.session_factory() as session:
        audit = session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.action == "withdraw_workflow_run_runtime_approval"
            )
        ).scalar_one()
        assert (audit.actor, audit.entity_id) == (APPROVER_A, approval_id)
    # nothing is consumed or sent, and the claim refuses with the stable code
    _refused(_dispatch(approver, proposal_id, approval_id, lease), "withdrawn")
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)
    # the other decision routes refuse it with the same stable code, no id in the message
    for action in ("approve", "reject"):
        late = _decide(approver, run_id, approval_id, action=action)
        _refusal_body(late, 409, "withdrawn", approval_id, proposal_id, APPROVER_A)
    # the run itself is untouched: a withdrawal does not fail it
    assert _run_status(approver, run_id)[0] == "waiting_approval"


def test_a_repeated_withdrawal_is_the_same_answer_and_writes_nothing_more(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="twice")
    first = _withdraw(approver, run_id, approval_id)
    after_first = _snapshot(approver, run_id, approval_id)

    again = _withdraw(approver, run_id, approval_id)

    assert first.status_code == again.status_code == 200
    assert first.json()["data"] == again.json()["data"]
    assert _snapshot(approver, run_id, approval_id) == after_first
    assert _audits(approver, "withdraw_workflow_run_runtime_approval") == 1
    assert len(_events(approver, run_id, "workflow.run.approval.withdrawn")) == 1


def test_a_consumed_decision_is_never_withdrawn_and_nothing_changes(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="used")
    assert isinstance(_dispatch(approver, proposal_id, approval_id, lease), EffectOutcome)
    before = _snapshot(approver, run_id, approval_id)

    response = _withdraw(approver, run_id, approval_id)

    _refusal_body(response, 409, "already_consumed", approval_id, proposal_id, APPROVER_A)
    assert _snapshot(approver, run_id, approval_id) == before
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")


def test_a_consumed_decision_stays_not_withdrawable_after_its_expiry(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="old")
    assert isinstance(_dispatch(approver, proposal_id, approval_id, lease), EffectOutcome)
    monkeypatch.setattr(
        "caliber.routes.workflow_runs._utc_now", lambda: T0 + timedelta(seconds=TTL + 1)
    )

    _refusal_body(_withdraw(approver, run_id, approval_id), 409, "already_consumed")


@pytest.mark.parametrize(
    ("state", "code"),
    [("pending", "not_approved"), ("rejected", "not_approved"), ("superseded", "superseded")],
)
def test_only_an_approved_request_can_be_withdrawn(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, state: str, code: str
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"state-{state}", decide=False
    )
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        row.status = state
        if state == "superseded":
            row.superseded_by = "RAP-newer"
        session.commit()
    before = _snapshot(approver, run_id, approval_id)

    response = _withdraw(approver, run_id, approval_id)

    _refusal_body(response, 409, code, approval_id)
    assert _snapshot(approver, run_id, approval_id) == before


def test_an_expired_request_is_refused_as_expired_not_withdrawn(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="late")
    monkeypatch.setattr(
        "caliber.routes.workflow_runs._utc_now", lambda: T0 + timedelta(seconds=TTL)
    )

    _refusal_body(_withdraw(approver, run_id, approval_id), 409, "expired", approval_id)
    assert _request_row(approver, approval_id)["status"] == "approved"


def test_a_legacy_request_guards_no_effect_so_there_is_nothing_to_withdraw(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, _approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="x")
    with approver.session_factory() as session:
        legacy = session.execute(
            select(CaliberRuntimeApprovalRequest).where(
                CaliberRuntimeApprovalRequest.workflow_run_id == run_id,
                CaliberRuntimeApprovalRequest.proposal_id.is_(None),
            )
        ).scalar_one()
        legacy_id = legacy.runtime_approval_id

    _refusal_body(_withdraw(approver, run_id, legacy_id), 409, "not_bound", legacy_id)
    assert _request_row(approver, legacy_id)["status"] == "approved"


def test_a_caller_without_the_required_role_cannot_tell_a_legacy_request_from_a_bound_one(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authority is checked first for both kinds: the same ``403``, never ``not_bound`` for one and ``403`` for the other."""
    run_id, bound_id, _proposal_id, _lease = _prepare(
        approver,
        monkeypatch,
        occurrence="kinds",
        decide=False,
        policy=ApprovalPolicy(required_role="caliber.admin"),
    )
    with approver.session_factory() as session:
        legacy = session.execute(
            select(CaliberRuntimeApprovalRequest).where(
                CaliberRuntimeApprovalRequest.workflow_run_id == run_id,
                CaliberRuntimeApprovalRequest.proposal_id.is_(None),
            )
        ).scalar_one()
        legacy.policy_snapshot = {
            **(legacy.policy_snapshot or {}),
            "required_role": "caliber.admin",
        }
        legacy_id = legacy.runtime_approval_id
        bound = session.get(CaliberRuntimeApprovalRequest, bound_id)
        assert bound is not None
        bound.status, bound.decided_by = "approved", APPROVER_A
        session.commit()
    before = _snapshot(approver, run_id, bound_id, legacy_id)

    legacy_answer = _withdraw(approver, run_id, legacy_id)
    bound_answer = _withdraw(approver, run_id, bound_id)

    assert legacy_answer.status_code == bound_answer.status_code == 403
    assert legacy_answer.json().get("reason_code") == bound_answer.json().get("reason_code")
    assert _snapshot(approver, run_id, bound_id, legacy_id) == before


def test_an_unknown_request_and_another_runs_request_are_not_found(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="nf")

    assert _withdraw(approver, run_id, "RAP-nope").status_code == 404
    assert _withdraw(approver, "WR-nope", approval_id).status_code == 404
    assert _request_row(approver, approval_id)["status"] == "approved"


def test_the_body_may_carry_a_reason_and_nothing_else(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="body")
    before = _snapshot(approver, run_id, approval_id)

    for bad in (
        {"status": "approved"},
        {"runtime_approval_id": approval_id},
        {"withdrawn_by": "someone"},
        {"reason": "x" * 1001},
        {"reason": 7},
    ):
        response = _withdraw(approver, run_id, approval_id, body=bad)
        assert response.status_code in (400, 422), (bad, response.text)
    assert _snapshot(approver, run_id, approval_id) == before

    # no body at all is a withdrawal without a reason
    empty = _post(approver, f"/workflow-runs/{run_id}/approvals/{approval_id}/withdraw")
    assert empty.status_code == 200, empty.text
    [event] = _events(approver, run_id, "workflow.run.approval.withdrawn")
    assert event.payload["reason"] is None


@pytest.mark.parametrize(
    ("user", "project", "status"),
    [
        (
            EDITOR_A,
            PROJECT_A,
            403,
        ),  # the requester alone: an editor holds neither the approve action nor the role
        (EDITOR_B, PROJECT_B, 404),  # another project cannot even see the run
    ],
)
def test_only_someone_who_may_approve_can_withdraw(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    user: str,
    project: str,
    status: int,
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="who")
    before = _snapshot(approver, run_id, approval_id)

    response = _withdraw(approver, run_id, approval_id, user=user, project=project)

    assert response.status_code == status, response.text
    assert _snapshot(approver, run_id, approval_id) == before


def test_a_caller_without_the_nodes_required_role_learns_nothing_of_the_requests_state(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node asks for ``caliber.admin``; the project reviewer holds only ``caliber.approver``: ``403`` before any state code."""
    run_id, approval_id, _proposal_id, _lease = _prepare(
        approver,
        monkeypatch,
        occurrence="role",
        decide=False,
        policy=ApprovalPolicy(required_role="caliber.admin"),
    )
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        row.status, row.decided_by = "approved", APPROVER_A
        session.commit()
    before = _snapshot(approver, run_id, approval_id)

    refused = _withdraw(approver, run_id, approval_id)

    assert refused.status_code == 403, refused.text
    assert "reason_code" not in refused.json() or refused.json()["reason_code"] is None
    assert _snapshot(approver, run_id, approval_id) == before
    # and a consumed request does not reveal itself to that caller either
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        row.consumed_at = T0
        row.commit_decision_id = "pd-x"
        session.commit()
    assert _withdraw(approver, run_id, approval_id).status_code == 403


def test_the_requester_who_also_holds_the_approval_authority_may_withdraw_their_own_request(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """Withdrawing only removes authority to act (like a rejection), so the separation-of-duties rule is not applied to it."""
    run_id, approval_id, proposal_id, lease = _prepare(
        approver,
        monkeypatch,
        occurrence="mine",
        decide=False,
        requested_by=APPROVER_A,
    )
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        row.status, row.decided_by = "approved", "approver_b"  # decided by someone else
        session.commit()

    response = _withdraw(approver, run_id, approval_id)

    assert response.status_code == 200, response.text
    _refused(_dispatch(approver, proposal_id, approval_id, lease), "withdrawn")
    assert backend.calls == 0


def test_a_withdrawal_works_whatever_the_runs_status_is_while_it_is_not_over(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a resume the run is ``running`` (about to dispatch): the decision can still be taken back."""
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="run")
    with approver.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        run.status = "running"
        session.commit()

    assert _withdraw(approver, run_id, approval_id).status_code == 200
    assert _request_row(approver, approval_id)["status"] == "withdrawn"


def test_a_request_the_lifecycle_already_withdrew_answers_a_late_withdrawal_idempotently(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="late")
    assert _cancel(approver, run_id).status_code == 200
    before = _snapshot(approver, run_id, approval_id)

    response = _withdraw(approver, run_id, approval_id)

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "withdrawn"
    assert _snapshot(approver, run_id, approval_id) == before
    assert _audits(approver, "withdraw_workflow_run_runtime_approval") == 0


def test_after_a_withdrawal_a_new_request_for_the_same_proposal_can_be_approved_and_consumed(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """The proposal is not stuck: it is still ``proposed``, and a fresh request is decided and consumed once."""
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="again")
    assert _withdraw(approver, run_id, approval_id).status_code == 200
    with approver.session_factory() as session:
        proposal = session.get(CaliberEffectProposal, proposal_id)
        assert proposal is not None
        fresh = create_approval_request(
            session,
            proposal=proposal,
            policy=ApprovalPolicy(),
            evidence_hash=EVIDENCE,
            requested_by=EDITOR_A,
            ttl_seconds=TTL,
            now=T0,
        )
        fresh_id = fresh.runtime_approval_id
        session.commit()
    assert _decide(approver, run_id, fresh_id).status_code == 200

    outcome = _dispatch(approver, proposal_id, fresh_id, lease)

    assert isinstance(outcome, EffectOutcome) and outcome.state == "committed"
    assert backend.calls == 1
    assert _request_row(approver, approval_id)["status"] == "withdrawn"
    assert _request_row(approver, approval_id)["consumed_at"] is None
    _consumed_once(approver, fresh_id, proposal_id, "committed")


def test_the_view_reports_a_withdrawn_request_and_closes_both_decisions(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="view")
    assert _withdraw(approver, run_id, approval_id).status_code == 200
    client = TestClient(approver.client.app)

    view = client.get(
        f"{PREFIX}/workflow-runs/{run_id}/approvals/{approval_id}",
        headers=approver.headers(APPROVER_A, PROJECT_A),
    )

    assert view.status_code == 200, view.text
    data = view.json()["data"]
    assert data["state"]["code"] == "withdrawn"
    assert data["caller"]["approve"] == {"allowed": False, "reason_code": "withdrawn"}
    assert data["caller"]["reject"] == {"allowed": False, "reason_code": "withdrawn"}
    assert data["approval"]["status"] == "withdrawn"


# ------------------------------------------------------------------ races (PostgreSQL)


@pytest.mark.parametrize("first", ["withdraw", "dispatch"])
def test_a_withdrawal_and_a_dispatch_racing_have_exactly_one_winner(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    first: str,
) -> None:
    """Both meet on the run lock; whichever the database lets in first wins and the other is refused, in both orders.

    * withdrawal first: the decision is withdrawn, the claim is refused ``withdrawn``, nothing is sent, the request is
      unconsumed and the proposal is still ``proposed``;
    * dispatch first: the claim consumes and sends once, and the withdrawal is ``409 already_consumed`` (a consumed request is
      never withdrawn).
    """
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence=f"race-{first}"
    )

    def withdraw() -> Any:
        return _withdraw(approver, run_id, approval_id)

    def dispatch_now() -> Any:
        return _dispatch(approver, proposal_id, approval_id, lease)

    order = [withdraw, dispatch_now] if first == "withdraw" else [dispatch_now, withdraw]
    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, order)
    results = [worker.finished() for worker in workers]
    withdrawn = results[order.index(withdraw)]
    dispatched = results[order.index(dispatch_now)]

    if first == "withdraw":
        assert withdrawn.status_code == 200, withdrawn.text
        _refused(dispatched, "withdrawn")
        assert backend.calls == 0
        _unconsumed(approver, approval_id, proposal_id)
        assert _request_row(approver, approval_id)["status"] == "withdrawn"
    else:
        assert isinstance(dispatched, EffectOutcome) and dispatched.state == "committed"
        _refusal_body(withdrawn, 409, "already_consumed")
        assert backend.calls == 1
        _consumed_once(approver, approval_id, proposal_id, "committed")
        assert _request_row(approver, approval_id)["status"] == "approved"


@pytest.mark.parametrize("first", ["cancel", "dispatch"])
def test_the_end_of_the_run_and_a_dispatch_racing_never_double_send_or_settle_a_sent_effect(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    first: str,
) -> None:
    """Cancel and claim meet on the run lock.

    * cancel first: the proposal is settled ``failed`` and the request withdrawn in the cancel's transaction, so the claim,
      which re-reads the run under the lock, is refused ``run_ended``; nothing is sent;
    * dispatch first: the claim consumes and the effect is sent once; the cancel then finds the proposal ``dispatched`` (or
      already ``committed``) and the request consumed and touches neither.
    """
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence=f"end-{first}"
    )

    def cancel() -> Any:
        return _cancel(approver, run_id)

    def dispatch_now() -> Any:
        return _dispatch(approver, proposal_id, approval_id, lease)

    order = [cancel, dispatch_now] if first == "cancel" else [dispatch_now, cancel]
    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, order)
    results = [worker.finished() for worker in workers]
    cancelled = results[order.index(cancel)]
    dispatched = results[order.index(dispatch_now)]

    assert cancelled.status_code == 200, cancelled.text
    assert _run_status(approver, run_id)[0] == "cancelled"
    if first == "cancel":
        _refused(dispatched, "run_ended")
        assert backend.calls == 0
        state = _proposal(approver, proposal_id)
        assert (state.state, state.reason) == ("failed", "run_cancelled")
        assert _request_row(approver, approval_id)["status"] == "withdrawn"
        assert _request_row(approver, approval_id)["consumed_at"] is None
    else:
        assert isinstance(dispatched, EffectOutcome) and dispatched.state == "committed"
        assert backend.calls == 1
        _consumed_once(approver, approval_id, proposal_id, "committed")
        assert _request_row(approver, approval_id)["status"] == "approved"
        assert _audits(approver, "settle_run_effects") == 0


def test_two_withdrawals_racing_record_one_and_both_answer_ok(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql(approver)
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="2wd")

    def withdraw() -> Any:
        return _withdraw(approver, run_id, approval_id)

    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, [withdraw, withdraw])
    first, second = (worker.finished() for worker in workers)

    assert first.status_code == second.status_code == 200
    assert _audits(approver, "withdraw_workflow_run_runtime_approval") == 1
    assert len(_events(approver, run_id, "workflow.run.approval.withdrawn")) == 1


def test_a_withdrawal_that_cannot_get_the_run_lock_is_a_retryable_503_and_changes_nothing(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_postgresql(approver)
    run_id, approval_id, _proposal_id, _lease = _prepare(approver, monkeypatch, occurrence="busy")
    monkeypatch.setattr("caliber.routes.workflow_runs.LOCK_TIMEOUT_MS", 300)
    before = _snapshot(approver, run_id, approval_id)

    with _holding_run_lock(approver, run_id):
        response = _withdraw(approver, run_id, approval_id)

    _refusal_body(response, 503, "approval_busy", approval_id)
    assert _snapshot(approver, run_id, approval_id) == before
    assert _withdraw(approver, run_id, approval_id).status_code == 200, "nothing was left locked"


def test_a_withdrawal_survives_a_restart_and_a_new_owner_cannot_claim_it(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """Withdrawn is a durable row, not process state: another owner (a new lease) still refuses the claim."""
    run_id, approval_id, proposal_id, _old = _prepare(approver, monkeypatch, occurrence="restart")
    assert _withdraw(approver, run_id, approval_id).status_code == 200
    with approver.session_factory() as session:
        new_lease = leased(session, "b1074-new-owner")  # the first owner's lease is taken over
        session.commit()

    _refused(_dispatch(approver, proposal_id, approval_id, new_lease), "withdrawn")
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)
