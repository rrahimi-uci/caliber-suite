"""B06.4 (#735): approval decisions and dispatcher consumption are one safe boundary.

The HTTP routes only *record* a decision. The dispatcher rechecks the exact approval and consumes it
(``consumed_at``) in the PostgreSQL transaction that also claims the proposal (``proposed -> approved -> dispatched``) and
enqueues the outbox row, under the execution-owner lease the owner captured. These scenarios drive the real decision routes
and the real dispatcher against the migrated reference app:

* **Races** (PostgreSQL only). Contenders are *made* to meet: an independent connection holds the run row lock, the contenders
  are started one by one and each is observed waiting on that lock (``pg_stat_activity``) before the lock is released. The
  arrival order is therefore the lock queue order and both orders are run, so no scenario depends on a lucky interleaving.
* **Refusals** (both dialects). Expiry (to the microsecond), revocation, supersession, a stale lease, self-approval, replay and
  a decision retried after consumption: each is a stable, payload-free code, the approval stays unconsumed, and the downstream
  transport is never reached.
* **Crash after the claim commit** leaves the durable ``dispatched`` row and its outbox entry, and a retry never sends again.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.effects.dispatcher as dispatcher_module
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEventOutbox,
    CaliberProject,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import (
    EffectOutcome,
    EffectRefused,
    TransportResult,
    dispatch,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import proposal_state
from caliber.effects.step_proposal import record_step_proposal, untrusted_tool
from caliber.ownership import acquire
from caliber.workflows.approval_policy import ApprovalPolicy, approval_valid_at_commit
from tests.effect_helpers import admit_fake_backend, leased
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    holding_sqlite_write_lock,
)
from tests.egi_scenarios.test_b02_admission import PREFIX
from tests.egi_scenarios.test_v04_stale_approval import (
    EVIDENCE,
    MICROSECOND,
    NODE,
    T0,
    TTL,
    _request,
    _waiting_run,
)
from tests.test_governed_agent_step import PROTECTED
from tests.test_governed_agent_step import _context as _step_context
from tests.test_governed_agent_step import _request as _step_request
from tests.test_governed_agent_step import _result as _step_result

BACKEND_KIND = "b064"
DOWNSTREAM_ID = "b064-downstream-1"


class CrashAfterClaim(BaseException):
    """A process-level crash, which the dispatcher must not translate into a settlement."""


class CountingBackend:
    """A deterministic private transport that returns a verifiable receipt and counts how often it was reached."""

    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.calls = 0
        self._lock = threading.Lock()

    def perform(self, request: Any, _config: Any) -> TransportResult:
        with self._lock:
            self.calls += 1
        if self.crash:
            raise CrashAfterClaim()
        view = request.view
        facts = ReceiptFacts(
            attempt=view.attempt or 1,
            idempotency_key=view.logical_key,
            connector=view.connector,
            connector_version=view.connector_version,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id=DOWNSTREAM_ID,
            response_digest="sha256:" + "a" * 64,
        )
        return TransportResult(
            Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1),
            lambda: facts,
        )


class Worker(threading.Thread):
    """A thread that keeps what its target returned, or raised, so a failure surfaces in the test and not in the thread."""

    def __init__(self, target: Callable[[], Any]) -> None:
        super().__init__(daemon=True)
        self._target_fn = target
        self.result: Any = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._target_fn()
        except BaseException as exc:
            self.error = exc

    def finished(self, timeout: float = 30) -> Any:
        if self.ident is None:
            self.start()
        self.join(timeout)
        assert not self.is_alive(), "the contender never finished (a lock was not released)"
        assert self.error is None, repr(self.error)
        return self.result


@pytest.fixture
def approver(reference_app: ReferenceApp, two_workspaces: Workspaces) -> ReferenceApp:
    """The reference app where ``approver_a`` holds the project role that may approve (the seeded member is an editor)."""
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.commit()
    return reference_app


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> CountingBackend:
    """An admitted fake transport under :data:`BACKEND_KIND`, reached only through the real dispatcher claim."""
    fake = CountingBackend()
    admit_fake_backend(monkeypatch, BACKEND_KIND, fake)
    return fake


def _prepare(
    app: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    *,
    occurrence: str,
    decide: bool = True,
    requested_by: str = EDITOR_A,
    policy: ApprovalPolicy | None = None,
) -> tuple[str, str, str, Any]:
    """A live bound request for a waiting run, decided through HTTP unless ``decide`` is false, and a captured lease.

    Returns ``(run_id, approval_id, proposal_id, lease)``.
    """
    run_id = _waiting_run(app)
    monkeypatch.setattr("caliber.routes.workflow_runs._utc_now", lambda: T0 + timedelta(minutes=1))
    approval_id, proposal_id = _request(
        app,
        run_id,
        occurrence,
        requested_by=requested_by,
        policy=policy,
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    if decide:
        decided = _decide(app, run_id, approval_id)
        assert decided.status_code == 200, decided.text
    with app.session_factory() as session:
        lease = acquire(session, f"b064-{occurrence}", ttl_seconds=600)
        assert lease is not None
        session.commit()
    return run_id, approval_id, proposal_id, lease


def _decide(
    app: ReferenceApp,
    run_id: str,
    approval_id: str,
    *,
    action: str = "approve",
    user: str = APPROVER_A,
) -> Any:
    # A client of its own: the shared one runs every request on one event loop and these handlers block it while they wait for
    # a row lock, which would serialize two decisions in the *test client* instead of in the database.
    client = TestClient(app.client.app)
    headers = app.headers(user, PROJECT_A)
    token = client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return client.request(
        "POST",
        f"{PREFIX}/workflow-runs/{run_id}/approval/{action}",
        json={"runtime_approval_id": approval_id, "reason": "ship it"},
        headers={**headers, "X-CALIBER-CSRF": token},
    )


def _dispatch(
    app: ReferenceApp, proposal_id: str, approval_id: str, lease: Any, **overrides: Any
) -> EffectOutcome | EffectRefused:
    """Run the real dispatcher; a refusal is returned (it is a result here), anything else propagates."""
    kwargs: dict[str, Any] = {
        "approval_id": approval_id,
        "approver_scopes": frozenset({"caliber.approver", "caliber.viewer"}),
        "lease": lease,
        "backend_kind": BACKEND_KIND,
        "approval_validator": approval_valid_at_commit,
        "current_resource_version": "1",
        "evidence_hash": EVIDENCE,
        "now": T0 + timedelta(minutes=2),
    }
    kwargs.update(overrides)
    try:
        return dispatch(app.session_factory, proposal_id, **kwargs)
    except EffectRefused as refused:
        return refused


def _refused(result: object, code: str) -> None:
    assert isinstance(result, EffectRefused), repr(result)
    assert result.code == code, result.code


def _unconsumed(app: ReferenceApp, approval_id: str, proposal_id: str) -> None:
    """The refusal left the approval unconsumed, the proposal ``proposed`` and no outbox row (nothing was claimed)."""
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and row.consumed_at is None
        assert proposal_state(session, proposal_id).state == "proposed"
        assert (
            session.execute(
                select(func.count())
                .select_from(CaliberEventOutbox)
                .where(CaliberEventOutbox.aggregate_id == proposal_id)
            ).scalar_one()
            == 0
        )


def _consumed_once(app: ReferenceApp, approval_id: str, proposal_id: str, state: str) -> None:
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and row.consumed_at is not None
        assert proposal_state(session, proposal_id).state == state
        outbox = session.execute(
            select(CaliberEventOutbox.event_id).where(
                CaliberEventOutbox.aggregate_id == proposal_id
            )
        ).all()
        assert len(outbox) == 1, "one claim, one outbox row"


# ------------------------------------------------------------------------------ refusals (both dialects)


def test_the_decision_is_recorded_and_only_dispatch_consumes_it(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    _run, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="record")
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        assert (row.status, row.decided_by, row.consumed_at) == ("approved", APPROVER_A, None)
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)

    outcome = _dispatch(approver, proposal_id, approval_id, lease)

    assert isinstance(outcome, EffectOutcome) and outcome.state == "committed"
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")


@pytest.mark.parametrize(
    ("offset", "code"),
    [
        (timedelta(seconds=TTL) - MICROSECOND, None),
        (timedelta(seconds=TTL), "expired"),
        (timedelta(seconds=TTL) + MICROSECOND, "expired"),
        (timedelta(days=30), "expired"),
    ],
)
def test_an_approval_that_expires_before_dispatch_is_not_consumed(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    offset: timedelta,
    code: str | None,
) -> None:
    """Decided in time, dispatched at the last microsecond (consumed) or at/after expiry (refused, unconsumed, zero sends)."""
    _run, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="expiry")

    result = _dispatch(approver, proposal_id, approval_id, lease, now=T0 + offset)

    if code is None:
        assert isinstance(result, EffectOutcome) and result.state == "committed"
        assert backend.calls == 1
        _consumed_once(approver, approval_id, proposal_id, "committed")
    else:
        _refused(result, code)
        assert backend.calls == 0
        _unconsumed(approver, approval_id, proposal_id)


@pytest.mark.parametrize("revocation", ["role_downgraded", "member_deactivated", "scope_dropped"])
def test_an_approver_revoked_after_deciding_is_not_consumed(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    revocation: str,
) -> None:
    """The approver's *current* authority is rechecked at the claim, not the authority they had when they clicked."""
    _run, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="revoked")
    scopes = frozenset({"caliber.approver", "caliber.viewer"})
    if revocation == "scope_dropped":
        scopes = frozenset({"caliber.viewer"})
    else:
        with approver.session_factory() as session:
            member = session.execute(
                select(CaliberProjectMember).where(
                    CaliberProjectMember.project_id == PROJECT_A,
                    CaliberProjectMember.user_id == APPROVER_A,
                )
            ).scalar_one()
            if revocation == "role_downgraded":
                member.role = "viewer"
            else:
                member.status = "deactivated"
                member.deactivated_at = T0
            session.commit()

    result = _dispatch(approver, proposal_id, approval_id, lease, approver_scopes=scopes)

    _refused(result, "role_revoked")
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)


def test_an_approval_superseded_after_the_decision_is_not_consumed(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="old")
    newer_id, _newer_proposal = _request(approver, run_id, "new", now=T0 + timedelta(minutes=1))

    result = _dispatch(approver, proposal_id, approval_id, lease)

    _refused(result, "superseded")
    assert backend.calls == 0
    with approver.session_factory() as session:
        old = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert old is not None and (old.status, old.superseded_by) == ("superseded", newer_id)
        assert old.consumed_at is None
    # The newer request is the live question; the HTTP route refuses the superseded one with a stable code.
    refused = _decide(approver, run_id, approval_id)
    assert refused.status_code == 409 and refused.json()["reason_code"] == "superseded"


def test_a_stale_lease_cannot_claim_and_leaves_the_approval_unconsumed(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """A takeover makes the captured lease stale: the old owner's claim is refused and nothing it did is half-recorded."""
    _run, approval_id, proposal_id, stale = _prepare(approver, monkeypatch, occurrence="takeover")
    with approver.session_factory() as session:
        taken = leased(session, "b064-new-owner")
        session.commit()
    assert taken.fencing_token != stale.fencing_token

    result = _dispatch(approver, proposal_id, approval_id, stale)

    assert isinstance(result, EffectRefused) and result.code in {"lease_lost", "stale_owner"}
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)
    # The current owner claims it normally.
    current = _dispatch(approver, proposal_id, approval_id, taken)
    assert isinstance(current, EffectOutcome) and current.state == "committed"
    assert backend.calls == 1


def test_the_requester_cannot_be_the_approver_at_decision_or_at_claim(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """The principal comes from the authenticated request, never the body: an approver who also asked is refused twice."""
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence="requester", decide=False, requested_by=APPROVER_A
    )

    at_decision = _decide(approver, run_id, approval_id)

    assert at_decision.status_code == 403, at_decision.text
    assert "second person" in at_decision.json()["detail"]
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and (row.status, row.decided_by) == ("pending", None)
        # A decision recorded around the route (a bug elsewhere, a manual edit) is still refused by the claim.
        row.status = "approved"
        row.decided_by = APPROVER_A
        row.decided_at = T0 + timedelta(minutes=1)
        session.commit()

    result = _dispatch(approver, proposal_id, approval_id, lease)

    _refused(result, "self_approval")
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)


def test_a_replay_after_the_claim_is_stable_and_never_sends_again(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """Dispatch twice and decide again: the decision is terminal (``already_consumed``) and the downstream is reached once."""
    run_id, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="replay")
    first = _dispatch(approver, proposal_id, approval_id, lease)
    assert isinstance(first, EffectOutcome) and first.state == "committed"

    again = _dispatch(approver, proposal_id, approval_id, lease)

    _refused(again, "proposal_not_approved")
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")
    before = _run_state(approver, run_id)
    for action in ("approve", "reject"):
        late = _decide(approver, run_id, approval_id, action=action)
        assert late.status_code == 409, late.text
        body = late.json()
        assert body["reason_code"] == "already_consumed" and body["status_code"] == 409
        for secret in (approval_id, proposal_id, APPROVER_A, EDITOR_A):
            assert secret not in late.text, "the refusal must carry no id or principal"
    assert _run_state(approver, run_id) == before, (
        "a late reject must not fail a run whose effect was sent"
    )


def _run_state(app: ReferenceApp, run_id: str) -> tuple[Any, ...]:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        return (run.status, run.error_code, run.completed_at, run.current_node_id)


def test_crash_after_claim_leaves_a_recoverable_dispatched_state(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the claim commit leaves durable recovery evidence; a retry reads it and does not send again."""
    crashing = CountingBackend(crash=True)
    admit_fake_backend(monkeypatch, BACKEND_KIND, crashing)
    _run, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="crash")

    with pytest.raises(CrashAfterClaim):
        _dispatch(approver, proposal_id, approval_id, lease)

    with approver.session_factory() as session:
        outbox = session.execute(
            select(CaliberEventOutbox).where(CaliberEventOutbox.aggregate_id == proposal_id)
        ).scalar_one()
        assert outbox.event_type == "effect.proposal.dispatched"
    _consumed_once(approver, approval_id, proposal_id, "dispatched")

    crashing.crash = False
    recovered = _dispatch(approver, proposal_id, approval_id, lease)

    assert isinstance(recovered, EffectOutcome)
    assert (recovered.state, recovered.applied, recovered.reason) == (
        "dispatched",
        False,
        "already_dispatched",
    )
    assert crashing.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "dispatched")


def test_a_governed_agent_steps_own_request_is_decided_over_http_and_consumed_once(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request the B08.2 writer made for a parked agent step is decided by id over HTTP and consumed by the dispatcher.

    The route knows nothing about the step or its backend: it records the decision for the request the step parked behind,
    the run stays ``waiting_approval`` (a decision does not wake it), and the claim consumes the decision once.
    """
    run_id = _waiting_run(approver)
    context = _step_context(
        approver.session_factory, workflow_run_id=run_id, requested_by="agent-step-runner"
    )
    request = _step_request(workflow_run_id=run_id, node_id=NODE)
    recorded = record_step_proposal(
        context,
        request,
        _step_result(request),
        {PROTECTED: untrusted_tool(PROTECTED, (PROTECTED, "1.0"))},
    )
    with approver.session_factory() as session:
        proposal = session.get(CaliberEffectProposal, recorded.proposal_id)
        row = session.get(CaliberRuntimeApprovalRequest, recorded.approval_request_id)
        assert proposal is not None and row is not None
        connector, version = proposal.connector, proposal.connector_version
        expected_version, evidence = proposal.expected_resource_version, row.evidence_hash
        assert row.requested_by == "agent-step-runner"
    fake = CountingBackend()
    admit_fake_backend(monkeypatch, BACKEND_KIND, fake, connector=connector, version=version)
    with approver.session_factory() as session:
        lease = acquire(session, "b064-agent-step", ttl_seconds=600)
        assert lease is not None
        session.commit()

    decided = _decide(approver, run_id, recorded.approval_request_id)

    assert decided.status_code == 200, decided.text
    assert _run_state(approver, run_id)[0] == "waiting_approval"
    _unconsumed(approver, recorded.approval_request_id, recorded.proposal_id)
    outcome = _dispatch(
        approver,
        recorded.proposal_id,
        recorded.approval_request_id,
        lease,
        current_resource_version=expected_version,
        evidence_hash=evidence,
        now=None,
    )
    assert isinstance(outcome, EffectOutcome) and outcome.state == "committed", repr(outcome)
    assert fake.calls == 1
    _consumed_once(approver, recorded.approval_request_id, recorded.proposal_id, "committed")


APPROVER_B = "approver_b"
QUORUM_OF_TWO = ApprovalPolicy(approval_count=2)


@pytest.fixture
def two_approvers(approver: ReferenceApp) -> ReferenceApp:
    """The reference app where ``approver_a`` and ``approver_b`` may both approve in project A."""
    with approver.session_factory() as session:
        session.add(
            CaliberProjectMember(
                member_id="egi-m-approver-b",
                project_id=PROJECT_A,
                user_id=APPROVER_B,
                role="reviewer",
                created_by="@egi",
            )
        )
        session.commit()
    # ``approver_b`` holds the approver scope like ``approver_a`` does (the listed approvers).
    approver.client.app.state.config = approver.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"approver_users": f"{APPROVER_A},{APPROVER_B}"}
    )
    return approver


def _set_member_role(app: ReferenceApp, user: str, role: str) -> None:
    with app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == user,
            )
        ).scalar_one()
        member.role = role
        session.commit()


def test_a_bound_quorum_of_two_is_completed_over_http_and_consumed_once(
    two_approvers: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """One approver is not a quorum of two: the claim waits for a second, distinct, still-authorized approver."""
    app = two_approvers
    run_id, approval_id, proposal_id, lease = _prepare(
        app, monkeypatch, occurrence="quorum", decide=False, policy=QUORUM_OF_TWO
    )

    first = _decide(app, run_id, approval_id)
    assert first.status_code == 200, first.text
    ack = first.json()["data"]
    assert (ack["status"], ack["approvals"], ack["remaining_approvals"]) == ("pending", 1, 1)
    assert _decide(app, run_id, approval_id).status_code == 403, (
        "the same person is not a second approver"
    )
    _refused(_dispatch(app, proposal_id, approval_id, lease), "not_approved")
    _unconsumed(app, approval_id, proposal_id)

    second = _decide(app, run_id, approval_id, user=APPROVER_B)
    assert second.status_code == 200, second.text
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and (row.status, row.decided_by) == ("approved", APPROVER_B)
        assert (row.policy_snapshot or {})["approvals"] == [APPROVER_A, APPROVER_B]
    assert backend.calls == 0

    outcome = _dispatch(app, proposal_id, approval_id, lease)

    assert isinstance(outcome, EffectOutcome) and outcome.state == "committed", repr(outcome)
    assert backend.calls == 1
    _consumed_once(app, approval_id, proposal_id, "committed")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("first_downgraded", "role_revoked"),
        ("last_downgraded", "role_revoked"),
        ("first_is_now_the_initiator", "self_approval"),
    ],
)
def test_every_approver_of_the_quorum_is_rechecked_at_the_claim(
    two_approvers: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    change: str,
    code: str,
) -> None:
    """The regression behind the review: A approves, A loses the role, B approves; the claim must not consume it."""
    app = two_approvers
    run_id, approval_id, proposal_id, lease = _prepare(
        app, monkeypatch, occurrence=f"recheck-{change}", decide=False, policy=QUORUM_OF_TWO
    )
    assert _decide(app, run_id, approval_id).status_code == 200
    if change == "first_downgraded":
        _set_member_role(app, APPROVER_A, "viewer")  # revoked before the last approver decides
    assert _decide(app, run_id, approval_id, user=APPROVER_B).status_code == 200
    if change == "last_downgraded":
        _set_member_role(app, APPROVER_B, "viewer")
    if change == "first_is_now_the_initiator":
        with app.session_factory() as session:
            run = session.get(CaliberWorkflowRun, run_id)
            assert run is not None
            run.created_by = APPROVER_A
            session.commit()

    result = _dispatch(app, proposal_id, approval_id, lease)

    _refused(result, code)
    assert backend.calls == 0
    _unconsumed(app, approval_id, proposal_id)
    if change != "first_is_now_the_initiator":
        # Restoring the role restores the quorum: the refusal was the revocation, nothing else.
        _set_member_role(
            app, APPROVER_A if change == "first_downgraded" else APPROVER_B, "reviewer"
        )
        restored = _dispatch(app, proposal_id, approval_id, lease)
        assert isinstance(restored, EffectOutcome) and restored.state == "committed"
        assert backend.calls == 1


def test_an_approver_who_owns_the_project_counts_only_with_the_owner_scopes(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """The owner's project role needs the operator and approver scopes; without them it is an editor's, which may not approve."""
    _run, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="owner")
    with approver.session_factory() as session:
        project = session.get(CaliberProject, PROJECT_A)
        assert project is not None
        project.owner = APPROVER_A
        session.commit()

    _refused(
        _dispatch(
            approver,
            proposal_id,
            approval_id,
            lease,
            approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
        ),
        "role_revoked",
    )
    _unconsumed(approver, approval_id, proposal_id)

    outcome = _dispatch(
        approver,
        proposal_id,
        approval_id,
        lease,
        approver_scopes=frozenset({"caliber.approver", "caliber.operator"}),
    )
    assert isinstance(outcome, EffectOutcome) and outcome.state == "committed"
    assert backend.calls == 1


def test_a_decision_recorded_around_the_route_with_too_few_approvers_is_not_a_quorum(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """``approved`` with one recorded approver under a quorum of two (a writer that skipped the route) is not consumed."""
    _run, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence="short-quorum", decide=False, policy=QUORUM_OF_TWO
    )
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        row.status = "approved"
        row.decided_by = APPROVER_A
        row.decided_at = T0 + timedelta(minutes=1)
        row.policy_snapshot = {**(row.policy_snapshot or {}), "approvals": [APPROVER_A]}
        session.commit()

    result = _dispatch(approver, proposal_id, approval_id, lease)

    _refused(result, "not_approved")
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)


# ------------------------------------------------------------------------------ races (PostgreSQL)


def _need_postgresql(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip("the race needs PostgreSQL row locks")


@contextmanager
def _holding_run_lock(app: ReferenceApp, run_id: str) -> Iterator[Session]:
    """An independent connection that holds ``FOR UPDATE`` on the run row until the block ends (yielded for probes)."""
    engine = create_engine(app.database_url)
    holder = sessionmaker(bind=engine)()
    try:
        holder.execute(
            select(CaliberWorkflowRun.workflow_run_id)
            .where(CaliberWorkflowRun.workflow_run_id == run_id)
            .with_for_update()
        ).one()
        yield holder
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()


def _lock_waiters(app: ReferenceApp) -> int:
    """Sessions of this database waiting for a lock (by wait event: PostgreSQL truncates long statement text)."""
    with app.session_factory() as observer:
        return int(
            observer.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock'"
                )
            ).scalar_one()
        )


def _queue(app: ReferenceApp, contenders: list[Callable[[], Any]]) -> list[Worker]:
    """Start each contender in turn, and wait until it is observed waiting before the next one starts.

    Called while the run lock is held, so each contender queues behind it in the order given. The caller releases the lock,
    and the contenders then take it one at a time in exactly that order.
    """
    workers: list[Worker] = []
    for position, contender in enumerate(contenders, start=1):
        worker = Worker(contender)
        worker.start()
        workers.append(worker)
        deadline = time.monotonic() + 20
        while _lock_waiters(app) < position and time.monotonic() < deadline:
            time.sleep(
                0.02
            )  # polling for the lock wait to be observed, not waiting for a race to happen
        assert _lock_waiters(app) >= position, f"contender {position} never queued on the lock"
        assert worker.is_alive()
    return workers


def test_two_consumers_one_dispatch(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """Two owners present the same approval together: exactly one claim, one outbox row, one send; the other is refused."""
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence="two-consumers"
    )

    def consume() -> EffectOutcome | EffectRefused:
        return _dispatch(approver, proposal_id, approval_id, lease)

    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, [consume, consume])
    results = [worker.finished() for worker in workers]

    winner, loser = results  # the lock queue order: the first contender claims
    assert isinstance(winner, EffectOutcome) and winner.state == "committed"
    _refused(loser, "already_consumed")
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")


@pytest.mark.parametrize("first", ["approve", "dispatch"])
def test_a_decision_and_a_dispatch_racing_serialize_and_leave_one_recoverable_claim(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    first: str,
) -> None:
    """The decision and the dispatch meet on the run lock. Whichever arrives first, nothing is consumed twice.

    * decision first: it commits, the dispatch then sees ``approved`` and claims it (``committed``, one send);
    * dispatch first: it sees a still-pending request and refuses ``not_approved`` *without* consuming anything, the decision
      then commits, and a retry of the dispatch claims it (one send, recoverable).
    """
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence=f"race-{first}", decide=False
    )

    def approve() -> Any:
        return _decide(approver, run_id, approval_id)

    def dispatch_now() -> EffectOutcome | EffectRefused:
        return _dispatch(approver, proposal_id, approval_id, lease)

    order = [approve, dispatch_now] if first == "approve" else [dispatch_now, approve]
    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, order)
    results = [worker.finished() for worker in workers]
    decision = results[order.index(approve)]
    dispatched = results[order.index(dispatch_now)]

    assert decision.status_code == 200, decision.text
    if first == "approve":
        assert isinstance(dispatched, EffectOutcome) and dispatched.state == "committed"
    else:
        _refused(dispatched, "not_approved")
        _unconsumed(approver, approval_id, proposal_id)
        assert backend.calls == 0
        retried = _dispatch(approver, proposal_id, approval_id, lease)
        assert isinstance(retried, EffectOutcome) and retried.state == "committed"
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")


@pytest.mark.parametrize("first", ["approve", "reject"])
def test_approve_and_reject_racing_leave_exactly_one_decision(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """Two decisions for one request: the second sees the first under the lock and is refused with a stable answer."""
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"decide-{first}", decide=False
    )
    contenders = {
        "approve": lambda: _decide(approver, run_id, approval_id, action="approve"),
        "reject": lambda: _decide(approver, run_id, approval_id, action="reject"),
    }
    names = ["approve", "reject"] if first == "approve" else ["reject", "approve"]
    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, [contenders[name] for name in names])
    answers = dict(zip(names, (worker.finished() for worker in workers), strict=True))

    assert answers[first].status_code == 200, answers[first].text
    second = "reject" if first == "approve" else "approve"
    assert answers[second].status_code == 409, answers[second].text
    if first == "approve":
        # The approval holds; the run still waits for dispatch to consume it.
        assert answers[second].json()["reason_code"] == "already_decided"
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and row.consumed_at is None
        assert row.status == ("approved" if first == "approve" else "rejected")
    _unconsumed(approver, approval_id, proposal_id)


@contextmanager
def _holding_request_lock(app: ReferenceApp, approval_id: str) -> Iterator[Session]:
    """An independent connection that holds ``FOR UPDATE`` on one approval request row until the block ends."""
    engine = create_engine(app.database_url)
    holder = sessionmaker(bind=engine)()
    try:
        holder.execute(
            select(CaliberRuntimeApprovalRequest.runtime_approval_id)
            .where(CaliberRuntimeApprovalRequest.runtime_approval_id == approval_id)
            .with_for_update()
        ).one()
        yield holder
    finally:
        holder.rollback()
        holder.close()
        engine.dispose()


@pytest.mark.parametrize("held", ["run", "request"])
def test_a_decision_that_cannot_get_its_lock_is_a_retryable_503_and_decides_nothing(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    """A stuck lock must not pin the handler: after the lock timeout the answer is ``503 approval_busy``, nothing is decided."""
    _need_postgresql(approver)
    monkeypatch.setattr("caliber.routes.workflow_runs.LOCK_TIMEOUT_MS", 300)
    run_id, approval_id, proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"busy-{held}", decide=False
    )
    holding = (
        _holding_run_lock(approver, run_id)
        if held == "run"
        else _holding_request_lock(approver, approval_id)
    )
    before = _run_state(approver, run_id)
    with holding as holder:
        busy = Worker(lambda: _decide(approver, run_id, approval_id)).finished(timeout=20)
        if held == "request":
            # The aborted decision gave back the run lock it had taken: nothing is left queued behind it.
            holder.execute(
                select(CaliberWorkflowRun.workflow_run_id)
                .where(CaliberWorkflowRun.workflow_run_id == run_id)
                .with_for_update(nowait=True)
            ).one()

    assert busy.status_code == 503, busy.text
    body = busy.json()
    assert body["reason_code"] == "approval_busy" and body["status_code"] == 503
    for secret in (approval_id, proposal_id, run_id, APPROVER_A):
        assert secret not in busy.text
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and (row.status, row.decided_by, row.consumed_at) == (
            "pending",
            None,
            None,
        )
    assert _run_state(approver, run_id) == before
    retried = _decide(approver, run_id, approval_id)  # the lock is free again
    assert retried.status_code == 200, retried.text


def _deadlocks(app: ReferenceApp) -> int:
    """How many deadlocks the server has detected in this database so far."""
    with app.session_factory() as session:
        return int(
            session.execute(
                text("SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()")
            ).scalar_one()
        )


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_decision_that_is_a_deadlock_victim_is_a_retryable_503_and_decides_nothing(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A writer that takes the rows in the opposite order (request, then run) deadlocks with the decision (run, then request).

    The writer asks for the run row only after the decision is *observed* waiting for the request row it holds, so the
    decision is the first waiter and PostgreSQL aborts it as the victim. The lock timeout is raised far above the deadlock
    detector's delay, so the 503 can only be the deadlock mapping.
    """
    _need_postgresql(approver)
    monkeypatch.setattr("caliber.routes.workflow_runs.LOCK_TIMEOUT_MS", 120_000)
    run_id, approval_id, proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"deadlock-{action}", decide=False
    )
    before = _run_state(approver, run_id)
    deadlocks = _deadlocks(approver)
    with _holding_request_lock(approver, approval_id) as writer:
        decision = Worker(lambda: _decide(approver, run_id, approval_id, action=action))
        decision.start()
        deadline = time.monotonic() + 20
        while _lock_waiters(approver) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)  # polling for the wait to be observed, not for a race to happen
        assert _lock_waiters(approver) >= 1, "the decision never waited for the request row"
        assert decision.is_alive()
        started = time.monotonic()
        # The cycle closes: the writer wants the run row the decision holds. Returns once the decision was aborted.
        writer.execute(
            select(CaliberWorkflowRun.workflow_run_id)
            .where(CaliberWorkflowRun.workflow_run_id == run_id)
            .with_for_update()
        ).one()
        resolved = time.monotonic() - started
    busy = decision.finished(timeout=30)
    assert busy.status_code == 503, busy.text
    body = busy.json()
    assert body["reason_code"] == "approval_busy" and body["status_code"] == 503
    for secret in (approval_id, proposal_id, run_id, APPROVER_A):
        assert secret not in busy.text
    assert resolved < 60  # the deadlock detector, not the 120 s lock timeout
    end = time.monotonic() + 20
    while _deadlocks(approver) <= deadlocks and time.monotonic() < end:
        time.sleep(0.1)  # the statistics are flushed a little after the abort
    assert _deadlocks(approver) > deadlocks, "PostgreSQL never reported a deadlock"
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and (row.status, row.decided_by, row.consumed_at) == (
            "pending",
            None,
            None,
        )
    assert _run_state(approver, run_id) == before
    retried = _decide(approver, run_id, approval_id, action=action)
    assert retried.status_code == 200, retried.text


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_busy_sqlite_database_is_a_retryable_503_and_a_decision_decides_nothing(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """SQLite's write lock is held past the busy timeout: the decision's first write fails and the answer is the busy 503."""
    if approver.dialect != "sqlite":
        pytest.skip("the busy timeout is SQLite's single-writer file lock")
    run_id, approval_id, proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"busy-sqlite-{action}", decide=False
    )
    before = _run_state(approver, run_id)
    with holding_sqlite_write_lock(approver):
        busy = _decide(approver, run_id, approval_id, action=action)
    assert busy.status_code == 503, busy.text
    body = busy.json()
    assert body["reason_code"] == "approval_busy" and body["status_code"] == 503
    for secret in (approval_id, proposal_id, run_id, APPROVER_A):
        assert secret not in busy.text
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and (row.status, row.decided_by, row.consumed_at) == (
            "pending",
            None,
            None,
        )
    assert _run_state(approver, run_id) == before
    retried = _decide(approver, run_id, approval_id, action=action)
    assert retried.status_code == 200, retried.text


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_caller_without_the_approve_role_is_refused_before_the_run_lock(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """A project editor who may not approve gets a ``403`` at once; they never queue behind (or hold up) a dispatcher."""
    _need_postgresql(approver)
    monkeypatch.setattr("caliber.routes.workflow_runs.LOCK_TIMEOUT_MS", 300)
    run_id, approval_id, _proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"no-role-{action}", decide=False
    )
    with _holding_run_lock(approver, run_id):
        refused = Worker(
            lambda: _decide(approver, run_id, approval_id, action=action, user=EDITOR_A)
        ).finished(timeout=20)
    assert refused.status_code == 403, refused.text


def test_a_stale_owner_is_refused_before_it_waits_for_any_row_lock(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """The lease is fenced first (lease, then run, proposal, request): a stale owner never queues behind the run lock.

    While another connection holds the run lock, a dispatch with a stale lease must finish at once with its refusal; if the
    fence came after the run lock it would wait there, holding up the owner that is meant to win.
    """
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, stale = _prepare(
        approver, monkeypatch, occurrence="stale-first"
    )
    with approver.session_factory() as session:
        leased(session, "b064-successor")
        session.commit()

    with _holding_run_lock(approver, run_id):
        worker = Worker(lambda: _dispatch(approver, proposal_id, approval_id, stale))
        worker.start()
        refused = worker.finished(timeout=10)  # still holding the run lock: it did not wait for it

    assert isinstance(refused, EffectRefused) and refused.code in {"lease_lost", "stale_owner"}
    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_decision_waits_for_the_run_lock_before_it_touches_the_request_row(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """The lock order is run, then request: a decision queued behind the run lock holds no request lock yet.

    The dispatcher and the request writer both take the run lock first and the request row second. A decision route that took
    the request row first and then wanted the run (a reject updates it) could deadlock against a dispatcher that holds the run
    and wants the request. While this connection holds the run lock, a queued decision must therefore not own the request row:
    ``FOR UPDATE NOWAIT`` on it succeeds.
    """
    _need_postgresql(approver)
    run_id, approval_id, _proposal_id, _lease = _prepare(
        approver, monkeypatch, occurrence=f"order-{action}", decide=False
    )
    with _holding_run_lock(approver, run_id) as holder:
        workers = _queue(approver, [lambda: _decide(approver, run_id, approval_id, action=action)])
        probe = holder.execute(
            select(CaliberRuntimeApprovalRequest.runtime_approval_id)
            .where(CaliberRuntimeApprovalRequest.runtime_approval_id == approval_id)
            .with_for_update(nowait=True)
        ).scalar_one()
        assert probe == approval_id
    assert workers[0].finished().status_code == 200


@pytest.mark.parametrize("first", ["supersede", "dispatch"])
def test_supersession_and_dispatch_racing_never_consume_a_replaced_approval(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    backend: CountingBackend,
    first: str,
) -> None:
    """A newer request for the step races the dispatch of the old one: consumed first stays consumed, else it is refused."""
    _need_postgresql(approver)
    run_id, approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence=f"supersede-{first}"
    )
    created: dict[str, str] = {}

    def supersede() -> str:
        new_id, _ = _request(approver, run_id, f"newer-{first}", now=T0 + timedelta(minutes=1))
        created["id"] = new_id
        return new_id

    def dispatch_now() -> EffectOutcome | EffectRefused:
        return _dispatch(approver, proposal_id, approval_id, lease)

    order = [supersede, dispatch_now] if first == "supersede" else [dispatch_now, supersede]
    with _holding_run_lock(approver, run_id):
        workers = _queue(approver, order)
    results = [worker.finished() for worker in workers]
    dispatched = results[order.index(dispatch_now)]

    with approver.session_factory() as session:
        old = session.get(CaliberRuntimeApprovalRequest, approval_id)
        newer = session.get(CaliberRuntimeApprovalRequest, created["id"])
        assert old is not None and newer is not None
        assert newer.status == "pending", "the newer request is the live question either way"
        if first == "supersede":
            assert (old.status, old.superseded_by, old.consumed_at) == (
                "superseded",
                newer.runtime_approval_id,
                None,
            )
        else:
            assert (old.status, old.superseded_by) == ("approved", None)
            assert old.consumed_at is not None
    if first == "supersede":
        _refused(dispatched, "superseded")
        assert backend.calls == 0
        _unconsumed(approver, approval_id, proposal_id)
    else:
        assert isinstance(dispatched, EffectOutcome) and dispatched.state == "committed"
        assert backend.calls == 1
        _consumed_once(approver, approval_id, proposal_id, "committed")


def test_the_claim_is_one_transaction_a_failed_enqueue_consumes_nothing(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch, backend: CountingBackend
) -> None:
    """Consumption, the state moves and the outbox row commit together: a failure inside the claim rolls all of them back."""
    _run, approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="atomic")
    real_enqueue = dispatcher_module.enqueue

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("the outbox is unavailable")

    monkeypatch.setattr(dispatcher_module, "enqueue", boom)
    with pytest.raises(RuntimeError, match="outbox is unavailable"):
        _dispatch(approver, proposal_id, approval_id, lease)

    assert backend.calls == 0
    _unconsumed(approver, approval_id, proposal_id)

    monkeypatch.setattr(dispatcher_module, "enqueue", real_enqueue)
    retried = _dispatch(approver, proposal_id, approval_id, lease)
    assert isinstance(retried, EffectOutcome) and retried.state == "committed"
    assert backend.calls == 1
    _consumed_once(approver, approval_id, proposal_id, "committed")
