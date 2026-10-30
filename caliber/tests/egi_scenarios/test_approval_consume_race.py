"""B06.4: approval decisions and dispatcher consumption are one safe boundary.

The HTTP route writes only the decision. The dispatcher rechecks the exact
approval and consumes it in the same PostgreSQL transaction that claims the
proposal and outbox event. These scenarios exercise contention and the crash
window after that commit, where the durable ``dispatched`` state is the recovery
record and a retry must not invoke the downstream a second time.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEventOutbox,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.dispatcher import (
    BACKENDS,
    BackendRegistration,
    EffectOutcome,
    EffectRefused,
    TransportResult,
    dispatch,
)
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import proposal_state
from caliber.ownership import acquire
from caliber.workflows.approval_policy import approval_valid_at_commit
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_v04_stale_approval import EVIDENCE, T0, _request
from tests.egi_scenarios.test_v05_revocation_while_waiting import _waiting_run


class CrashAfterClaim(BaseException):
    """A process-level crash, which the dispatcher must not translate to settlement."""


class CountingBackend:
    """A deterministic private transport that returns a verifiable receipt."""

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
            downstream_request_id="b064-downstream-1",
            response_digest="sha256:" + "a" * 64,
        )
        return TransportResult(
            Classified(TransportOutcome.COMMITTED_RECEIPT, "http_201", 201, 1),
            lambda: facts,
        )


@pytest.fixture
def approver(reference_app: ReferenceApp, two_workspaces: Workspaces) -> ReferenceApp:
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


def _prepare(
    app: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
    *,
    occurrence: str,
    decide: bool = True,
) -> tuple[str, str, Any]:
    """Create a live bound request, optionally decide it through HTTP, and capture a lease."""
    run_id = _waiting_run(app)
    monkeypatch.setattr("caliber.routes.workflow_runs._utc_now", lambda: T0 + timedelta(minutes=1))
    approval_id, proposal_id = _request(
        app,
        run_id,
        occurrence,
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    if decide:
        decided = _call(
            app,
            "POST",
            APPROVER_A,
            PROJECT_A,
            f"/workflow-runs/{run_id}/approval/approve",
            {"runtime_approval_id": approval_id, "reason": "ship it"},
        )
        assert decided.status_code == 200, decided.text
    with app.session_factory() as session:
        lease = acquire(session, f"b064-{occurrence}", ttl_seconds=600)
        assert lease is not None
        session.commit()
    return approval_id, proposal_id, lease


def _dispatch_kwargs(approval_id: str, lease: Any) -> dict[str, Any]:
    return {
        "approval_id": approval_id,
        "approver_scopes": frozenset({"caliber.approver", "caliber.viewer"}),
        "lease": lease,
        "backend_kind": "b064",
        "approval_validator": approval_valid_at_commit,
        "current_resource_version": "1",
        "evidence_hash": EVIDENCE,
        "now": T0 + timedelta(minutes=2),
    }


def test_concurrent_approve_and_dispatch_are_serialized_and_recoverable(  # noqa: PLR0915 - complete cross-transaction scenario
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL serializes the decision/claim race; exactly one retry can finish it."""
    if approver.dialect != "postgresql":
        pytest.skip("concurrent approval/dispatch requires PostgreSQL row locks")
    approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence="approve-dispatch-race", decide=False
    )
    backend = CountingBackend()
    monkeypatch.setitem(
        BACKENDS,
        "b064",
        BackendRegistration(backend, "document-request", "1"),
    )
    barrier = threading.Barrier(2)
    observed: list[tuple[str, object]] = []
    observed_lock = threading.Lock()

    def approve() -> None:
        barrier.wait()
        response = _call(
            approver,
            "POST",
            APPROVER_A,
            PROJECT_A,
            f"/workflow-runs/{_run_for(approver, proposal_id)}/approval/approve",
            {"runtime_approval_id": approval_id, "reason": "raced"},
        )
        with observed_lock:
            observed.append(("approve", response))

    def dispatch_once() -> None:
        barrier.wait()
        try:
            result = dispatch(
                approver.session_factory,
                proposal_id,
                **_dispatch_kwargs(approval_id, lease),
            )
        except EffectRefused as exc:
            result = exc
        except BaseException as exc:
            result = exc
        with observed_lock:
            observed.append(("dispatch", result))

    threads = [threading.Thread(target=approve), threading.Thread(target=dispatch_once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "the PostgreSQL lock race did not complete"

    approval_response = next(value for name, value in observed if name == "approve")
    dispatch_result = next(value for name, value in observed if name == "dispatch")
    assert not isinstance(dispatch_result, BaseException) or isinstance(
        dispatch_result, EffectRefused
    ), repr(dispatch_result)
    if isinstance(dispatch_result, EffectRefused):
        assert dispatch_result.code == "not_approved"
    assert approval_response.status_code in {200, 409}
    if approval_response.status_code == 409:
        assert approval_response.json()["reason_code"] == "already_consumed"
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        consumed = row.consumed_at is not None
    if not consumed:
        # The dispatcher may have validated the still-pending request before the
        # route won the run lock. A clean retry after the HTTP commit is the
        # recoverable outcome this race is required to prove.
        assert approval_response.status_code == 200
        retried = dispatch(
            approver.session_factory,
            proposal_id,
            **_dispatch_kwargs(approval_id, lease),
        )
        assert retried.state == "committed"
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and row.consumed_at is not None
        state = proposal_state(session, proposal_id)
        run_id = session.execute(
            select(CaliberEffectProposal.workflow_run_id).where(
                CaliberEffectProposal.proposal_id == proposal_id
            )
        ).scalar_one()
        assert state.state in {"dispatched", "committed"}
        assert session.execute(
            select(CaliberEventOutbox.event_id).where(
                CaliberEventOutbox.aggregate_id == proposal_id
            )
        ).scalar_one()
    assert run_id
    assert backend.calls == 1


def test_two_concurrent_dispatchers_consume_one_approval(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two owners may race, but the locked approval and proposal admit one transport call."""
    if approver.dialect != "postgresql":
        pytest.skip("concurrent dispatch requires PostgreSQL row locks")
    approval_id, proposal_id, lease = _prepare(approver, monkeypatch, occurrence="two-dispatchers")
    backend = CountingBackend()
    monkeypatch.setitem(
        BACKENDS,
        "b064",
        BackendRegistration(backend, "document-request", "1"),
    )
    barrier = threading.Barrier(2)
    results: list[object] = []
    results_lock = threading.Lock()

    def dispatch_once() -> None:
        barrier.wait()
        try:
            result: object = dispatch(
                approver.session_factory,
                proposal_id,
                **_dispatch_kwargs(approval_id, lease),
            )
        except EffectRefused as exc:
            result = exc
        except BaseException as exc:
            result = exc
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=dispatch_once), threading.Thread(target=dispatch_once)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive(), "the PostgreSQL dispatch race did not complete"

    assert len(results) == 2
    assert backend.calls == 1
    successful = [result for result in results if isinstance(result, EffectOutcome)]
    assert len(successful) == 1
    assert successful[0].state == "committed"
    refusals = [result for result in results if isinstance(result, EffectRefused)]
    assert len(refusals) == 1
    assert refusals[0].code in {"already_consumed", "proposal_not_approved"}
    unexpected = [
        result
        for result in results
        if isinstance(result, BaseException) and not isinstance(result, EffectRefused)
    ]
    assert unexpected == [], repr(unexpected)
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and row.consumed_at is not None
        assert proposal_state(session, proposal_id).state == "committed"


def test_crash_after_claim_leaves_a_recoverable_dispatched_state(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after the claim commit leaves durable recovery evidence and no retry call."""
    approval_id, proposal_id, lease = _prepare(
        approver, monkeypatch, occurrence="crash-after-claim"
    )
    backend = CountingBackend(crash=True)
    monkeypatch.setitem(
        BACKENDS,
        "b064",
        BackendRegistration(backend, "document-request", "1"),
    )
    with pytest.raises(CrashAfterClaim):
        dispatch(
            approver.session_factory,
            proposal_id,
            **_dispatch_kwargs(approval_id, lease),
        )

    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None and row.consumed_at is not None
        assert proposal_state(session, proposal_id).state == "dispatched"
        outbox = session.execute(
            select(CaliberEventOutbox).where(CaliberEventOutbox.aggregate_id == proposal_id)
        ).scalar_one()
        assert outbox.event_type == "effect.proposal.dispatched"

    backend.crash = False
    recovered = dispatch(
        approver.session_factory,
        proposal_id,
        **_dispatch_kwargs(approval_id, lease),
    )
    assert recovered.state == "dispatched"
    assert recovered.applied is False
    assert backend.calls == 1


def _run_for(app: ReferenceApp, proposal_id: str) -> str:
    with app.session_factory() as session:
        return str(
            session.execute(
                select(CaliberEffectProposal.workflow_run_id).where(
                    CaliberEffectProposal.proposal_id == proposal_id
                )
            ).scalar_one()
        )
