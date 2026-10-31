"""B04.1 public-profile integration for the governed effect dispatcher."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select

from caliber.db.models import (
    CaliberEffectProposal,
    CaliberProjectMember,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
)
from caliber.effects.dispatcher import dispatch
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import logical_key, proposal_state
from caliber.effects.registered_tools import load_registered_tool_identity
from caliber.egress import EgressPolicy
from caliber.ids import new_tool_id
from caliber.integrations.openapi.executor import bind_egress_policy
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
from tests.test_registered_tool_dispatch import tool_manifest
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


def _registered_tool_identity(reference_app: ReferenceApp, name: str, version: str) -> Any:
    with reference_app.session_factory() as session:
        return load_registered_tool_identity(session, name=name, version=version)


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
    authorized: list[str] = []
    now = datetime.now(timezone.utc)

    def authorize(proposal: Any) -> EffectAuthorization:
        authorized.append(proposal.proposal_id)
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
            lease = acquire(session, "egi-b04-3-runtime", ttl_seconds=600)
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
