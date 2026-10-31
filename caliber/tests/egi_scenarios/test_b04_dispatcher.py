"""B04.1 public-profile integration for the governed effect dispatcher."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

import caliber.effects.dispatcher as dispatcher_module
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects.dispatcher import EffectRefused, dispatch
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.reconcile import RECONCILED_COMMITTED
from caliber.effects.records import proposal_state, record_proposal
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy
from caliber.observability.mlflow_tracing import Tracer, set_tracer
from caliber.orchestrator.effect_reconciler import COMPLETE, EffectReconcilerTask
from caliber.ownership import acquire
from caliber.run_manifest import canonical_bytes
from caliber.workflows.approval_policy import ApprovalPolicy, approval_valid_at_commit
from caliber.workflows.approval_requests import create_approval_request
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    PROJECT_A,
    Downstream,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_v04_stale_approval import (
    EVIDENCE,
    T0,
    TTL,
    _request,
)
from tests.egi_scenarios.test_v05_revocation_while_waiting import _approved, _waiting_run
from tests.test_effect_proposals import _intent
from tests.test_mlflow_tracing import FakeMlflow
from tests.test_workflow_webhook_node import _manifest as webhook_manifest


def test_dispatcher_uses_canonical_approval_and_registered_connector(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """A real reference-profile approval and lease reach the private connector once.

    The approval is marked through the producer-owned fixture because the public
    decision route intentionally remains ``approval_bound_not_wired`` until the
    B06.4 task moves consumption into this dispatcher transaction.  The test still
    exercises the migrated application, canonical commit validator, real lease
    store, outbox/receipt persistence, and the real downstream HTTP fixture.
    """
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.commit()

    run_id = _waiting_run(reference_app)
    approval_id, proposal_id = _request(
        reference_app,
        run_id,
        "occ-b04-dispatch",
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    _approved(reference_app, run_id, approval_id)

    with reference_app.session_factory() as session:
        lease = acquire(session, "egi-b04-dispatcher", ttl_seconds=600)
        assert lease is not None
        session.commit()

    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    try:
        result = dispatch(
            reference_app.session_factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
            lease=lease,
            backend_kind="document_request",
            backend_config={"base_url": downstream.base_url},
            current_resource_version="1",
            evidence_hash=EVIDENCE,
            approval_validator=approval_valid_at_commit,
            now=T0 + timedelta(minutes=2),
        )
    finally:
        bind_egress_policy(None)

    assert result.state == "committed"
    assert downstream.mutations() == 1
    with reference_app.session_factory() as session:
        approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert approval is not None and approval.consumed_at is not None
        assert proposal_state(session, proposal_id).state == "committed"


def test_webhook_runtime_uses_registered_http_dispatcher_and_verified_receipt(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """The runtime caller, reference DB and independent downstream form one governed path."""
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.commit()

    run_id = _waiting_run(reference_app)
    now = datetime.now(timezone.utc)
    url = f"{downstream.base_url}/cases/MORT-2026-000184/document-requests"
    resolver = InMemoryToolResolver({})
    manifest = parse_manifest(webhook_manifest(url))
    authorization_calls = 0

    def authorize(proposal: Any) -> EffectAuthorization:
        nonlocal authorization_calls
        authorization_calls += 1
        with reference_app.session_factory() as session:
            row = session.get(CaliberEffectProposal, proposal.proposal_id)
            assert row is not None
            request = create_approval_request(
                session,
                proposal=row,
                policy=ApprovalPolicy(),
                evidence_hash=EVIDENCE,
                requested_by=EDITOR_A,
                ttl_seconds=TTL,
                now=now,
            )
            session.commit()
            approval_id = request.runtime_approval_id
        _approved(reference_app, run_id, approval_id)
        with reference_app.session_factory() as session:
            lease = acquire(session, "egi-b04-2-runtime", ttl_seconds=600)
            assert lease is not None
            session.commit()
        return EffectAuthorization(
            approval_id=approval_id,
            approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
            lease=lease,
            evidence_hash=EVIDENCE,
        )

    def verify(request: Any, _response: dict[str, Any]) -> ReceiptFacts | None:
        found = downstream.query(request.view.logical_key)
        if found is None:
            return None
        return ReceiptFacts(
            attempt=request.view.attempt or 1,
            idempotency_key=request.view.logical_key,
            connector=request.view.connector,
            connector_version=request.view.connector_version,
            resource=request.view.resource,
            intent_hash=request.view.intent_hash,
            downstream_request_id=str(found["request_id"]),
            response_digest="sha256:" + hashlib.sha256(canonical_bytes(found)).hexdigest(),
            resource_version=str(found["case_version"]),
        )

    plan = RuntimePlan(
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        workflow_version_id="egi-b04-2-version",
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
        governed_effects=GovernedEffectContext(
            session_factory=reference_app.session_factory,
            workflow_run_id=run_id,
            authorize=authorize,
            receipt_verifier=verify,
        ),
    )

    result = execute(plan, '{"document_type":"pay_statement"}', executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    assert authorization_calls == 1
    assert downstream.mutations() == 1
    with reference_app.session_factory() as session:
        proposal = session.execute(
            select(CaliberEffectProposal).where(
                CaliberEffectProposal.workflow_run_id == run_id,
                CaliberEffectProposal.node_id == "hook",
            )
        ).scalar_one()
        assert proposal.connector == "http"
        assert proposal.state == "committed"
        assert proposal.receipt_id is not None


# ---------------------------------------------------------------------------
# B11.3a (#737): the spans of the governed path name the rows that authorised the effect
# ---------------------------------------------------------------------------

_CAUSAL = ("caliber.proposal_id", "caliber.decision_id", "caliber.approval_id", "caliber.effect_id")


def _causal(span: Any) -> dict[str, Any]:
    return {key: span.attributes[key] for key in _CAUSAL if key in span.attributes}


def _approved_request(reference_app: ReferenceApp) -> tuple[str, str, str]:
    """An approved, bound request for a versioned document request: ``(run, approval, proposal)`` ids."""
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.commit()
    run_id = _waiting_run(reference_app)
    approval_id, proposal_id = _request(
        reference_app,
        run_id,
        "occ-b11-3a",
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    _approved(reference_app, run_id, approval_id)
    return run_id, approval_id, proposal_id


def _dispatch_traced(
    reference_app: ReferenceApp,
    downstream: Downstream,
    proposal_id: str,
    approval_id: str,
    **overrides: Any,
) -> Any:
    with reference_app.session_factory() as session:
        lease = acquire(session, "egi-b11-3a", ttl_seconds=600)
        assert lease is not None
        session.commit()
    arguments: dict[str, Any] = {
        "approval_id": approval_id,
        "approver_scopes": frozenset({"caliber.approver", "caliber.viewer"}),
        "lease": lease,
        "backend_kind": "document_request",
        "backend_config": {"base_url": downstream.base_url},
        "current_resource_version": "1",
        "evidence_hash": EVIDENCE,
        "approval_validator": approval_valid_at_commit,
        "now": T0 + timedelta(minutes=2),
    }
    arguments.update(overrides)
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    try:
        return dispatch(reference_app.session_factory, proposal_id, **arguments)
    finally:
        bind_egress_policy(None)


def test_governed_path_spans_carry_the_ids_of_the_persisted_rows(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """Real validator, real lease, real downstream: every id on the spans is a row the database holds."""
    run_id, approval_id, proposal_id = _approved_request(reference_app)
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    result = _dispatch_traced(reference_app, downstream, proposal_id, approval_id)

    assert result.state == "committed" and downstream.mutations() == 1
    with reference_app.session_factory() as session:
        proposal = session.get(CaliberEffectProposal, proposal_id)
        approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
        receipt = session.scalars(
            select(CaliberEffectReceipt).where(CaliberEffectReceipt.proposal_id == proposal_id)
        ).one()
        decisions = {
            row.decision_id
            for row in session.scalars(
                select(CaliberPolicyDecision).where(CaliberPolicyDecision.workflow_run_id == run_id)
            )
        }
        assert proposal is not None and approval is not None
        assert proposal.receipt_id == receipt.receipt_id and approval.consumed_at is not None

    check = next(span for span in fake.spans if span.name == "effect.approval.commit_check")
    dispatched = next(span for span in fake.spans if span.name == "effect.dispatch")
    written = next(span for span in fake.spans if span.name == "effect.receipt.write")

    decision_id = dispatched.attributes["caliber.decision_id"]
    assert decision_id in decisions
    assert _causal(dispatched) == {
        "caliber.proposal_id": proposal.proposal_id,
        "caliber.decision_id": decision_id,
        "caliber.approval_id": approval.runtime_approval_id,
        "caliber.effect_id": receipt.receipt_id,
    }
    # The commit-time recheck names the same proposal, approval and decision, and has no outcome yet.
    assert _causal(check) == {
        "caliber.proposal_id": proposal_id,
        "caliber.decision_id": decision_id,
        "caliber.approval_id": approval_id,
    }
    assert _causal(written) == {
        "caliber.proposal_id": proposal_id,
        "caliber.approval_id": approval_id,
        "caliber.effect_id": receipt.receipt_id,
    }
    for span in (check, dispatched, written):
        assert span.attributes["caliber.status"] == "completed"
        # Nothing but the allow-listed ids (and the tracer's own status) is on a governed-effect span.
        assert set(span.attributes) <= {*_CAUSAL, "caliber.status"}


def test_a_refused_recheck_names_what_it_bound_and_its_code_and_nothing_else(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    _run_id, approval_id, proposal_id = _approved_request(reference_app)
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    with pytest.raises(EffectRefused) as refused:
        _dispatch_traced(
            reference_app, downstream, proposal_id, approval_id, current_resource_version="2"
        )

    assert refused.value.code == "stale_version"
    assert downstream.mutations() == 0
    check = next(span for span in fake.spans if span.name == "effect.approval.commit_check")
    dispatched = next(span for span in fake.spans if span.name == "effect.dispatch")
    # The request was bound to the proposal's exact intent before the resource version was compared, so the id is
    # a real link; the decision is only known once every check has passed, so it is absent.
    assert _causal(check) == {
        "caliber.proposal_id": proposal_id,
        "caliber.approval_id": approval_id,
    }
    assert check.attributes["caliber.refusal_code"] == "stale_version"
    assert check.attributes["caliber.status"] == "failed"
    # The dispatch span only names ids the validator returned, which it never did.
    assert _causal(dispatched) == {"caliber.proposal_id": proposal_id}
    assert dispatched.attributes["caliber.refusal_code"] == "stale_version"
    assert not [span for span in fake.spans if span.name == "effect.receipt.write"]
    with reference_app.session_factory() as session:
        approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert approval is not None and approval.consumed_at is None
        assert proposal_state(session, proposal_id).state == "proposed"


def test_another_proposals_approval_is_not_linked_by_a_refused_recheck(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """An approval row that exists but is bound to a different proposal is not a link for this one."""
    run_id, approval_id, _proposal_id = _approved_request(reference_app)
    with reference_app.session_factory() as session:
        other_proposal = record_proposal(
            session,
            workflow_run_id=run_id,
            node_id="other_node",
            occurrence_id="occ-b11-3a-other",
            intent=_intent(
                connector_version="1",
                resource="cases/MORT-2026-000184/document-requests",
                args={"document_type": "bank_statement"},
                expected_resource_version="1",
            ),
        ).proposal_id
        session.commit()
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    with pytest.raises(EffectRefused) as refused:
        _dispatch_traced(reference_app, downstream, other_proposal, approval_id)

    assert refused.value.code == "intent_mismatch"
    assert downstream.mutations() == 0
    for span in fake.spans:
        assert span.attributes.get("caliber.approval_id") is None
        assert span.attributes.get("caliber.decision_id") is None
        assert span.attributes["caliber.proposal_id"] == other_proposal
    check = next(span for span in fake.spans if span.name == "effect.approval.commit_check")
    assert check.attributes["caliber.refusal_code"] == "intent_mismatch"
    with reference_app.session_factory() as session:
        approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert approval is not None and approval.consumed_at is None


def test_an_unknown_approval_id_is_never_linked_from_a_span(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    _run_id, _approval_id, proposal_id = _approved_request(reference_app)
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    with pytest.raises(EffectRefused) as refused:
        _dispatch_traced(reference_app, downstream, proposal_id, "RAP-forged00")

    assert refused.value.code == "not_found"
    for span in fake.spans:
        assert "caliber.approval_id" not in span.attributes
        assert "caliber.decision_id" not in span.attributes
        assert "RAP-forged00" not in repr(span.attributes)
    check = next(span for span in fake.spans if span.name == "effect.approval.commit_check")
    assert _causal(check) == {"caliber.proposal_id": proposal_id}
    assert check.attributes["caliber.refusal_code"] == "not_found"


def test_a_settle_failure_under_the_current_lease_is_recovered_by_the_owner_loop_with_one_mutation(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    downstream: Downstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B05.5d (#997): the dispatcher keeps its lease, sends the effect, and its receipt verification fails.

    The row must not stay ``dispatched`` under a lease that is still current (the reconciler's takeover rule would never touch
    it): the dispatcher records ``unknown`` itself, and the owner-only loop, under the same lease, asks the downstream by key,
    commits with a verified receipt, and the downstream counts exactly one mutation.
    """
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.commit()

    run_id = _waiting_run(reference_app)
    approval_id, proposal_id = _request(
        reference_app,
        run_id,
        "occ-b05-5d",
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    _approved(reference_app, run_id, approval_id)
    with reference_app.session_factory() as session:
        lease = acquire(session, "egi-b05-5d-owner", ttl_seconds=600)
        assert lease is not None
        session.commit()

    transport = dispatcher_module._raw_connector_transport
    real = transport.execute_openapi_http_tool

    def verification_fails(**kwargs: Any) -> Any:
        if (
            kwargs["execution_config"]["method"] == "GET"
        ):  # the receipt verification, after the send
            raise ConnectionError("the verifier lost its connection")
        return real(**kwargs)

    monkeypatch.setattr(transport, "execute_openapi_http_tool", verification_fails)
    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    try:
        with pytest.raises(ConnectionError):
            dispatch(
                reference_app.session_factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
                lease=lease,
                backend_kind="document_request",
                backend_config={"base_url": downstream.base_url},
                current_resource_version="1",
                evidence_hash=EVIDENCE,
                approval_validator=approval_valid_at_commit,
                now=T0 + timedelta(minutes=2),
            )
        assert downstream.mutations() == 1
        with reference_app.session_factory() as session:
            state = proposal_state(session, proposal_id)
            assert (state.state, state.reason) == ("unknown", "settle_failed:ConnectionError")
            assert state.dispatch_token == lease.fencing_token, "still the current lease's token"

        loop = EffectReconcilerTask(
            reference_app.session_factory,
            SimpleNamespace(
                effect_callback_connectors=json.dumps(
                    [
                        {
                            "connector": "document-request",
                            "project_id": PROJECT_A,
                            "secret_source": "env:UNUSED",
                            "base_url": downstream.base_url,
                        }
                    ]
                )
            ),
            lease_provider=lambda: lease,
        )
        report = loop.tick()
    finally:
        bind_egress_policy(None)

    assert report is not None and report.ended == COMPLETE
    assert [(r.proposal_id, r.outcome, r.state) for r in report.results] == [
        (proposal_id, RECONCILED_COMMITTED, "committed")
    ]
    assert downstream.mutations() == 1, "found by key, never sent again"
    with reference_app.session_factory() as session:
        assert proposal_state(session, proposal_id).state == "committed"
