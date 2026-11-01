"""B04.1 public-profile integration for the governed effect dispatcher."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

import caliber.effects.dispatcher as dispatcher_module
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
)
from caliber.effects.dispatcher import EffectRefused, dispatch, submit_proposal
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.reconcile import NOT_BOUND, RECONCILED_COMMITTED, reconcile_one
from caliber.effects.records import logical_key, proposal_state, record_proposal
from caliber.effects.registered_tools import load_registered_tool_identity
from caliber.egress import EgressPolicy
from caliber.ids import new_tool_id
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
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.effect_helpers import leased
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    PROJECT_A,
    Downstream,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_b03_run_identity import _run, _start, _work
from tests.egi_scenarios.test_v04_stale_approval import (
    EVIDENCE,
    T0,
    TTL,
    _request,
)
from tests.egi_scenarios.test_v05_revocation_while_waiting import _approved, _waiting_run
from tests.test_effect_proposals import _intent
from tests.test_mlflow_tracing import FakeMlflow
from tests.test_registered_tool_dispatch import tool_manifest
from tests.test_workflow_webhook_node import _manifest as webhook_manifest
from tests.workflow_helpers import make_tool_payload


def test_dispatcher_uses_canonical_approval_and_registered_connector(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    downstream: Downstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real reference-profile approval and lease reach the private connector once.

    The approval is recorded through the public decision route, while consumption
    remains owned by the dispatcher claim transaction. The test exercises the
    migrated application, canonical commit validator, real lease store,
    outbox/receipt persistence, and the real downstream HTTP fixture.
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
    monkeypatch.setattr("caliber.routes.workflow_runs._utc_now", lambda: T0 + timedelta(minutes=1))
    approval_id, proposal_id = _request(
        reference_app,
        run_id,
        "occ-b04-dispatch",
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    approved = _call(
        reference_app,
        "POST",
        APPROVER_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/approval/approve",
        {"runtime_approval_id": approval_id, "reason": "ship it"},
    )
    assert approved.status_code == 200, approved.text

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


def _registered_tool_identity(reference_app: ReferenceApp, name: str, version: str) -> Any:
    with reference_app.session_factory() as session:
        return load_registered_tool_identity(session, name=name, version=version)


def _scenario_context(
    reference_app: ReferenceApp, downstream: Downstream, run_id: str
) -> tuple[Any, Any, list[str]]:
    """The run's real authorization provider and receipt verifier, over the reference database and the downstream.

    ``authorize`` creates a real exact-intent approval request, approves it through the producer-owned fixture and
    acquires the execution-owner lease; ``verify`` asks the downstream by key and believes only what it stores.
    """
    authorized: list[str] = []
    now = datetime.now(timezone.utc)

    def authorize(proposal: Any) -> EffectAuthorization:
        authorized.append(proposal.proposal_id)
        with reference_app.session_factory() as session:
            row = session.get(CaliberEffectProposal, proposal.proposal_id)
            assert row is not None
            expected_version = row.expected_resource_version
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
            lease = acquire(session, "egi-b04-3-runtime", ttl_seconds=600)
            assert lease is not None
            session.commit()
        return EffectAuthorization(
            approval_id=approval_id,
            approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
            lease=lease,
            # The version the approver's check read: it matched when the approval was committed. A downstream that moves
            # afterwards is the downstream's own precondition to refuse.
            current_resource_version=expected_version,
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

    return authorize, verify, authorized


def _registered_tool_world(
    reference_app: ReferenceApp, downstream: Downstream, *, level: str
) -> tuple[RuntimePlan, str, list[str], str]:
    """A tool node over a registered tool that posts to the downstream, on the real reference database.

    Returns the plan, the run id, the proposals the runtime asked to authorize, and the stable key the tool sends.
    """
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name="request_document",
                version="1.0",
                module_path="ops.document_tools",
                callable_name="request_document",
                side_effect_level=level,
                owner="@ops",
            )
        )
        session.commit()
    run_id = _waiting_run(reference_app)
    key = logical_key(workflow_run_id=run_id, node_id="tool_node", occurrence_id="tool_node")
    url = f"{downstream.base_url}/cases/MORT-2026-000184/document-requests"

    def request_document(document_type: str, idempotency_key: str) -> dict[str, Any]:
        response = httpx.post(
            url, json={"document_type": document_type}, headers={"Idempotency-Key": idempotency_key}
        )
        return {"status": response.status_code}

    entry = ToolRegistryEntry(
        name="request_document",
        version="1.0",
        module_path="ops.document_tools",
        callable_name="request_document",
        side_effect_level=level,
    )
    resolver = InMemoryToolResolver([entry])
    resolver.register(entry, request_document)
    authorize, verify, authorized = _scenario_context(reference_app, downstream, run_id)
    manifest = parse_manifest(tool_manifest("request_document"))
    plan = RuntimePlan(
        ir=build_ir(manifest, resolver, version="1"),
        resolver=resolver,
        workflow_version_id="egi-b04-3-version",
        registered_tool_identity=lambda name, version: _registered_tool_identity(
            reference_app, name, version
        ),
        governed_effects=GovernedEffectContext(
            session_factory=reference_app.session_factory,
            workflow_run_id=run_id,
            authorize=authorize,
            receipt_verifier=verify,
        ),
    )
    return plan, run_id, authorized, key


def _tool_arguments(key: str) -> str:
    return f'{{"document_type": "pay_statement", "idempotency_key": "{key}"}}'


def _tool_proposals(reference_app: ReferenceApp, run_id: str) -> list[CaliberEffectProposal]:
    with reference_app.session_factory() as session:
        return list(
            session.execute(
                select(CaliberEffectProposal).where(CaliberEffectProposal.workflow_run_id == run_id)
            ).scalars()
        )


def test_a_registered_write_tool_reaches_the_downstream_once_through_the_dispatcher(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """The tool's downstream write happens after the real approval and lease, and is measured at the downstream."""
    plan, run_id, authorized, key = _registered_tool_world(reference_app, downstream, level="write")

    result = execute(plan, _tool_arguments(key), executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    assert len(authorized) == 1
    assert downstream.mutations() == 1
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert proposal.connector == "registered_tool"
    assert proposal.state == "committed"
    assert proposal.receipt_id is not None


def test_a_registered_write_tool_without_approval_never_reaches_the_downstream(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    plan, run_id, _authorized, key = _registered_tool_world(
        reference_app, downstream, level="write"
    )
    assert plan.governed_effects is not None
    plan.governed_effects = replace(plan.governed_effects, authorize=lambda _proposal: None)

    result = execute(plan, _tool_arguments(key), executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "authorization was not granted" in (result.error or "")
    assert downstream.mutations() == 0
    assert downstream.query(key) is None
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert proposal.state == "proposed"


def test_a_registered_read_tool_runs_without_a_proposal(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """A read-class row keeps the inline path: the tool runs (the downstream shows it) and nothing is proposed."""
    plan, run_id, authorized, key = _registered_tool_world(reference_app, downstream, level="read")

    result = execute(plan, _tool_arguments(key), executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    assert authorized == []
    assert _tool_proposals(reference_app, run_id) == []
    assert downstream.query(key) is not None


def test_untrusted_connectors_are_refused_on_the_public_and_dispatcher_paths(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """B12.3a (#738): a user tool called ``document_request`` and unpinned connectors never reach the downstream.

    Registration goes through the public ``POST /tools`` route, the proposals and dispatch through the real
    dispatcher on the reference database, and the independent downstream fixture counts every mutation.
    """
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"admin_users": "@egi-admin"}
    )
    prefix = "/ajax-api/2.0/mlflow/caliber"
    admin = reference_app.headers("@egi-admin", "")
    token = reference_app.client.get(f"{prefix}/csrf", headers=admin).json()["data"]["token"]
    spoof = reference_app.client.post(
        f"{prefix}/tools",
        json=make_tool_payload("document_request"),
        headers={**admin, "X-CALIBER-CSRF": token},
    )
    assert spoof.status_code == 409, spoof.text
    assert "reserved_tool_name" in spoof.text and "trusted connector" in spoof.text
    listed = reference_app.client.get(f"{prefix}/tools", headers=admin).json()["data"]
    assert [row for row in listed if row["name"] == "document_request"] == []

    run_id = _waiting_run(reference_app)
    with reference_app.session_factory() as session:
        before = len(session.scalars(select(CaliberEffectProposal.proposal_id)).all())
        with pytest.raises(EffectRefused) as bare:
            submit_proposal(
                session,
                workflow_run_id=run_id,
                node_id="agent",
                occurrence_id="occ-spoof",
                connector="document-request",
                connector_version="1",
                effect_class_name="irreversible_write",
                resource="cases/MORT-2026-000184/document-requests",
                args={"document_type": "pay_statement"},
                tool_name="document_request",
                caliber_defined=False,
            )
        assert bare.value.code == "connector_not_admitted"
        session.rollback()
        assert len(session.scalars(select(CaliberEffectProposal.proposal_id)).all()) == before

    approval_id, proposal_id = _request(
        reference_app,
        run_id,
        "occ-b12-3a",
        connector_version="1",
        resource="cases/MORT-2026-000184/document-requests",
        args={"document_type": "pay_statement"},
        expected_resource_version="1",
    )
    _approved(reference_app, run_id, approval_id)
    with reference_app.session_factory() as session:
        lease = acquire(session, "egi-b12-3a", ttl_seconds=600)
        assert lease is not None
        session.commit()
    for backend_kind, config, code in (
        ("mcp_stdio", {}, "unsupported_backend"),
        ("host_file_export", {}, "unsupported_backend"),
        ("document_request", {"base_url": "file:///etc/passwd"}, "connector_config_not_admitted"),
        (
            "document_request",
            {"base_url": downstream.base_url, "headers": {}},
            "connector_config_not_admitted",
        ),
    ):
        with pytest.raises(EffectRefused) as refused:
            dispatch(
                reference_app.session_factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
                lease=lease,
                backend_kind=backend_kind,
                backend_config=config,
                current_resource_version="1",
                evidence_hash=EVIDENCE,
                approval_validator=approval_valid_at_commit,
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == code
    assert downstream.mutations() == 0
    with reference_app.session_factory() as session:
        approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert approval is not None and approval.consumed_at is None
        assert proposal_state(session, proposal_id).state == "proposed"


def test_an_admission_refusal_binds_no_row_to_the_span(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """B12.3a with B11.3a: admission refuses before the claim, so no id is read, bound or linked."""
    _run_id, approval_id, proposal_id = _approved_request(reference_app)
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    with pytest.raises(EffectRefused) as refused:
        _dispatch_traced(
            reference_app,
            downstream,
            proposal_id,
            approval_id,
            backend_config={"base_url": "file:///etc/passwd"},
        )

    assert refused.value.code == "connector_config_not_admitted"
    dispatched = next(span for span in fake.spans if span.name == "effect.dispatch")
    assert _causal(dispatched) == {}, "nothing was read from a row, so nothing is linked"
    assert dispatched.attributes["caliber.refusal_code"] == "connector_config_not_admitted"
    for span in fake.spans:
        assert approval_id not in repr(span.attributes)
    assert not [span for span in fake.spans if span.name == "effect.approval.commit_check"]
    assert downstream.mutations() == 0
    with reference_app.session_factory() as session:
        approval = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert approval is not None and approval.consumed_at is None
        assert proposal_state(session, proposal_id).state == "proposed"


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
    real = transport._send_raw

    def verification_fails(**kwargs: Any) -> Any:
        if (
            kwargs["execution_config"]["method"] == "GET"
        ):  # the receipt verification, after the send
            raise ConnectionError("the verifier lost its connection")
        return real(**kwargs)

    bind_egress_policy(EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})))
    try:
        # only the dispatcher's own verification fails: the reconciler's later lookup (the public read entry, which
        # delegates to the same function) must reach the downstream
        with monkeypatch.context() as failing, pytest.raises(ConnectionError):
            failing.setattr(transport, "_send_raw", verification_fails)
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


# ---------------------------------------------------------------- B04.4 (#750): registered OpenAPI tools


def _openapi_tool_world(
    reference_app: ReferenceApp,
    downstream: Downstream,
    *,
    level: str,
    config: dict[str, Any],
) -> tuple[RuntimePlan, str, list[str], str]:
    """A tool node over a registered OpenAPI tool whose row points at the downstream, on the real reference database.

    Returns the plan, the run id, the proposals the runtime asked to authorize, and the stable key the platform sends.
    """
    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        member.role = "reviewer"
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name="request_document_api",
                version="1.0",
                module_path="<openapi_http>",
                callable_name="invoke",
                execution_backend="openapi_http",
                backend_config=config,
                side_effect_level=level,
                owner="@ops",
            )
        )
        session.commit()
    run_id = _waiting_run(reference_app)
    key = logical_key(workflow_run_id=run_id, node_id="tool_node", occurrence_id="tool_node")
    entry = ToolRegistryEntry(
        name="request_document_api",
        version="1.0",
        module_path="<openapi_http>",
        callable_name="invoke",
        execution_backend="openapi_http",
        backend_config=config,
        side_effect_level=level,
    )
    resolver = InMemoryToolResolver([entry])
    authorize, verify, authorized = _scenario_context(reference_app, downstream, run_id)
    plan = RuntimePlan(
        ir=build_ir(parse_manifest(tool_manifest("request_document_api")), resolver, version="1"),
        resolver=resolver,
        workflow_version_id="egi-b04-4-version",
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
        registered_tool_identity=lambda name, version: _registered_tool_identity(
            reference_app, name, version
        ),
        governed_effects=GovernedEffectContext(
            session_factory=reference_app.session_factory,
            workflow_run_id=run_id,
            authorize=authorize,
            receipt_verifier=verify,
        ),
    )
    return plan, run_id, authorized, key


def _submit_config(downstream: Downstream, **overrides: Any) -> dict[str, Any]:
    from caliber.effects.connectors import document_request

    config = document_request.submit_config(downstream.base_url, **overrides)
    config["expected_resource_version_header"] = "If-Match"
    return config


def _submit_arguments(version: str | None = "1") -> str:
    arguments: dict[str, Any] = {
        "path_params": {"case_id": "MORT-2026-000184"},
        "body": {"document_type": "pay_statement"},
    }
    if version is not None:
        arguments["expected_resource_version"] = version
    return json.dumps(arguments)


def test_an_openapi_write_reaches_the_downstream_once_through_the_dispatcher(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """The write happens after the real approval and lease, under the platform's key, and is measured at the downstream."""
    plan, run_id, authorized, key = _openapi_tool_world(
        reference_app, downstream, level="write", config=_submit_config(downstream)
    )

    result = execute(plan, _submit_arguments(), executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    assert len(authorized) == 1
    assert downstream.mutations() == 1
    stored = downstream.query(key)
    assert stored is not None, "the downstream holds the request under the proposal's stable key"
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert proposal.connector == "openapi-http"
    assert (
        proposal.resource
        == f"openapi/POST {downstream.base_url}/cases/{{case_id}}/document-requests"
    )
    assert proposal.expected_resource_version == "1"
    assert proposal.state == "committed"
    assert proposal.receipt_id is not None


def test_an_openapi_write_without_approval_never_reaches_the_downstream(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    plan, run_id, _authorized, key = _openapi_tool_world(
        reference_app, downstream, level="write", config=_submit_config(downstream)
    )
    assert plan.governed_effects is not None
    plan.governed_effects = replace(plan.governed_effects, authorize=lambda _proposal: None)

    result = execute(plan, _submit_arguments(), executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "authorization was not granted" in (result.error or "")
    assert downstream.mutations() == 0
    assert downstream.query(key) is None
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert proposal.state == "proposed"


def test_an_openapi_read_runs_inline_without_a_proposal(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    from caliber.effects.connectors import document_request

    plan, run_id, authorized, _key = _openapi_tool_world(
        reference_app,
        downstream,
        level="read",
        config=document_request.lookup_config(downstream.base_url),
    )

    result = execute(
        plan,
        json.dumps({"query_params": {"idempotency_key": "nothing-stored-under-this"}}),
        executor=FakeWorkflowExecutor(),
    )

    assert result.status == "completed", result.error
    assert authorized == []
    assert _tool_proposals(reference_app, run_id) == []


def test_a_dropped_answer_is_retried_with_the_same_key_and_the_downstream_commits_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """Timeout after commit with a bound key: the retry replays the stored answer, so one mutation, verified, committed."""
    downstream.set_fault("drop_response_after_commit")
    plan, run_id, _authorized, key = _openapi_tool_world(
        reference_app,
        downstream,
        level="write",
        config=_submit_config(downstream, max_attempts=2, timeout_seconds=5),
    )

    result = execute(plan, _submit_arguments(), executor=FakeWorkflowExecutor())

    assert result.status == "completed", result.error
    assert downstream.mutations() == 1
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert proposal.state == "committed"
    assert downstream.query(key) is not None


def test_a_dropped_answer_with_no_retry_is_unknown_not_resent_and_not_reconcilable_here(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """The effect committed but nobody saw the answer. It is ``unknown``: no second send, and the generic OpenAPI connector
    has no lookup binding, so the reconciler leaves it for an operator instead of guessing."""
    downstream.set_fault("drop_response_after_commit")
    plan, run_id, _authorized, key = _openapi_tool_world(
        reference_app,
        downstream,
        level="write",
        config=_submit_config(downstream, max_attempts=1, timeout_seconds=5),
    )

    result = execute(plan, _submit_arguments(), executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "did not commit: ambiguous" in (result.error or "")
    assert downstream.mutations() == 1
    assert downstream.query(key) is not None, "a later lookup by key does show the commit"
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert proposal.state == "unknown"

    again = execute(plan, _submit_arguments(), executor=FakeWorkflowExecutor())
    assert "was already recorded as unknown; not run again" in (again.error or "")
    assert downstream.mutations() == 1

    with reference_app.session_factory() as session:
        lease = leased(session, "egi-b04-4-reconciler")
        session.commit()
    settled = reconcile_one(
        reference_app.session_factory,
        proposal.proposal_id,
        lease=lease,
        targets={("openapi-http", PROJECT_A): downstream.base_url},
    )
    assert (settled.outcome, settled.state) == (NOT_BOUND, "unknown")
    assert downstream.mutations() == 1


def test_a_stale_precondition_is_sent_as_bound_and_the_downstream_commits_nothing(
    reference_app: ReferenceApp, two_workspaces: Workspaces, downstream: Downstream
) -> None:
    """If-Match is preserved: the downstream sees the proposal's stale version and refuses (412). The connector documents no
    rejection status, so the platform records ``unknown`` rather than claiming the effect did not happen."""
    plan, run_id, _authorized, key = _openapi_tool_world(
        reference_app, downstream, level="write", config=_submit_config(downstream)
    )

    result = execute(plan, _submit_arguments("99"), executor=FakeWorkflowExecutor())

    assert result.status == "error"
    assert "did not commit: ambiguous:unrecognised_4xx" in (result.error or "")
    assert downstream.mutations() == 0
    assert downstream.query(key) is None
    (proposal,) = _tool_proposals(reference_app, run_id)
    assert (proposal.state, proposal.expected_resource_version) == ("unknown", "99")


# ---------------------------------------------------------------- the two profiles differ exactly as documented (#1068)


def _protected_tool_workflow(reference_app: ReferenceApp) -> str:
    """A published workflow whose only node is the demo ``escalate`` tool, registered as protected (``external_action``)."""
    with reference_app.session_factory() as session:
        session.add(
            CaliberToolRegistry(
                tool_id=new_tool_id(),
                name="escalate",
                version="1.0",
                module_path="caliber.workflows.demo_tools",
                callable_name="escalate",
                side_effect_level="external_action",
                requires_approval=True,
                project_id=PROJECT_A,
                owner="@ops",
            )
        )
        session.commit()
    created = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": "EGI protected tool"}
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    manifest = tool_manifest("escalate", requires_approval=False)
    manifest["workflow_id"] = workflow_id
    draft = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": manifest},
    )
    assert draft.status_code == 201, draft.text
    published = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-versions/{draft.json()['data']['version_id']}/publish",
    )
    assert published.status_code == 200, published.text
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"workflow_run_queue_enabled": True, "admission_enabled": True}
    )
    return str(workflow_id)


def test_the_profiles_differ_on_a_protected_tool_node_when_no_governed_context_is_bound(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The same published workflow, run by the real worker, on each profile (docs/operate/reference-profile.md).

    Reference profile (the PostgreSQL job): refused with the governed-context error; the tool never runs and no proposal
    exists. Development profile (SQLite): the pre-dispatcher legacy path, so the tool runs; there is still no proposal,
    because the legacy path is not the exact-intent governed path.
    """
    workflow_id = _protected_tool_workflow(reference_app)
    run_id = _start(reference_app, workflow_id, "profile-semantics")

    _work(reference_app)

    status, _code, summary = _run(reference_app, run_id)
    output = _call(reference_app, "GET", EDITOR_A, PROJECT_A, f"/workflow-runs/{run_id}").json()[
        "data"
    ]
    assert _tool_proposals(reference_app, run_id) == []
    if reference_app.profile == "reference":
        assert status == "failed"
        assert "requires a governed effect context to run protected registered tool" in str(summary)
        assert "escalated" not in json.dumps(output)
    else:
        assert status == "completed", summary
        assert "escalated" in json.dumps(output)
