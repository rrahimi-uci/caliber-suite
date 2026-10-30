"""B04.1 public-profile integration for the governed effect dispatcher."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from caliber.db.models import CaliberProjectMember, CaliberRuntimeApprovalRequest
from caliber.effects.dispatcher import dispatch
from caliber.effects.records import proposal_state
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import bind_egress_policy
from caliber.ownership import acquire
from caliber.workflows.approval_policy import approval_valid_at_commit
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    PROJECT_A,
    Downstream,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_v04_stale_approval import (
    EVIDENCE,
    T0,
    _request,
)
from tests.egi_scenarios.test_v05_revocation_while_waiting import _waiting_run


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
