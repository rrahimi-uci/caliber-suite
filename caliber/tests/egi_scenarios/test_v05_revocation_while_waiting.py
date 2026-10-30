"""B06.3 commit-time approval rechecks.

The decision endpoint deliberately does not consume a bound effect approval.  This
scenario exercises the producer-owned recheck that the future dispatcher will call
inside its fenced claim transaction: exact intent/evidence/policy/version pins,
the original run grant, and the approving principal's current authority are all
checked independently.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from caliber.db.models import (
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberRuntimeApprovalRequest,
)
from caliber.effects.records import ProposalError, record_proposal
from caliber.policy import grants
from caliber.resource_access import OWNER_ROLE_REQUIRED_SCOPES
from caliber.workflows import approval_policy, run_identity
from caliber.workflows.approval_policy import (
    ApprovalCommitRefused,
    approval_valid_at_commit,
)
from caliber.workflows.run_identity import AUTHORITY_UNAVAILABLE, IdentityDenied
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_v04_stale_approval import (
    EVIDENCE,
    T0,
    TTL,
    _request,
    _waiting_run,
)
from tests.test_effect_proposals import _intent


def _approved(app: ReferenceApp, run_id: str, request_id: str) -> None:
    with app.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, request_id)
        assert row is not None
        row.status = "approved"
        row.decided_by = APPROVER_A
        row.decided_at = T0 + timedelta(minutes=1)
        session.commit()


@pytest.fixture
def approver(reference_app: ReferenceApp, two_workspaces: Workspaces) -> ReferenceApp:
    """Give the seeded approver the project role required by ``resource.approve``."""
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


def _valid_request(app: ReferenceApp) -> tuple[str, str, str]:
    run_id = _waiting_run(app)
    approval_id, proposal_id = _request(app, run_id, "occ-v05")
    _approved(app, run_id, approval_id)
    return run_id, approval_id, proposal_id


def test_valid_commit_recheck_uses_the_original_run_identity(
    approver: ReferenceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, approval_id, proposal_id = _valid_request(approver)
    observed: dict[str, Any] = {}
    real_decide = run_identity.decide

    def capture(session: Any, **kwargs: Any) -> Any:
        observed.update(kwargs)
        return real_decide(session, **kwargs)

    monkeypatch.setattr(run_identity, "decide", capture)
    with approver.session_factory() as session:
        checked = approval_valid_at_commit(
            session,
            approval_id=approval_id,
            proposal_id=proposal_id,
            approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
            current_resource_version="7",
            evidence_hash=EVIDENCE,
            now=T0 + timedelta(minutes=2),
        )
        session.commit()

    assert checked.run_id == run_id
    assert checked.approver_id == APPROVER_A
    assert checked.proposal_id == proposal_id
    assert observed["run_id"] == run_id
    assert observed["grant_id"] == checked.grant_id
    assert observed["subject"].user_id == EDITOR_A
    assert observed["purpose"] == "workflow_run:api"
    assert observed["subject"].user_id != checked.approver_id


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("expired", "expired"),
        ("superseded", "superseded"),
        ("stale_version", "stale_version"),
        ("scope_revoked", "role_revoked"),
        ("consumed", "already_consumed"),
    ],
)
def test_commit_recheck_refuses_expiry_supersession_version_and_authority_changes(
    approver: ReferenceApp,
    mutation: str,
    expected: str,
) -> None:
    run_id, approval_id, proposal_id = _valid_request(approver)
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        if mutation == "superseded":
            row.status = "superseded"
            row.superseded_by = "ar-newer"
        elif mutation == "consumed":
            row.consumed_at = T0 + timedelta(minutes=2)
        elif mutation == "scope_revoked":
            member = session.execute(
                select(CaliberProjectMember).where(
                    CaliberProjectMember.project_id == PROJECT_A,
                    CaliberProjectMember.user_id == APPROVER_A,
                )
            ).scalar_one()
            member.status = "inactive"
        session.commit()

    kwargs: dict[str, Any] = {
        "approval_id": approval_id,
        "proposal_id": proposal_id,
        "approver_scopes": frozenset({"caliber.approver", "caliber.viewer"}),
        "current_resource_version": "7",
        "evidence_hash": EVIDENCE,
        "now": T0 + timedelta(seconds=TTL) if mutation == "expired" else T0 + timedelta(minutes=2),
    }
    if mutation == "stale_version":
        kwargs["current_resource_version"] = "8"
    if mutation == "scope_revoked":
        # The scope is still present; the current project membership is what was revoked.
        kwargs["approver_scopes"] = frozenset({"caliber.approver", "caliber.viewer"})

    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(session, **kwargs)
        assert refused.value.code == expected


def test_changed_intent_cannot_reuse_an_approval(approver: ReferenceApp) -> None:
    run_id, approval_id, _ = _valid_request(approver)
    # A replacement for the same node would correctly supersede the first
    # request before the intent check runs. Use a separate node so this test
    # isolates the exact-proposal binding rather than supersession.
    with approver.session_factory() as session:
        changed = record_proposal(
            session,
            workflow_run_id=run_id,
            node_id="other_gate",
            occurrence_id="occ-v05-changed",
            intent=_intent(connector_version="2.0.0"),
        )
        changed_proposal_id = changed.proposal_id
        session.commit()
    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=changed_proposal_id,
                approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
                current_resource_version="7",
                evidence_hash=EVIDENCE,
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "intent_mismatch"


def test_revoked_run_grant_is_not_replaced_by_the_approver(
    approver: ReferenceApp,
) -> None:
    run_id, approval_id, proposal_id = _valid_request(approver)
    with approver.session_factory() as session:
        grant = session.execute(
            select(CaliberRunGrant).where(CaliberRunGrant.workflow_run_id == run_id)
        ).scalar_one()
        grants.revoke_grant(
            session,
            run_id=run_id,
            grant_id=grant.grant_id,
            by="owner",
            reason="revoked while waiting",
            now=T0 + timedelta(minutes=2),
        )
        session.commit()

    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=frozenset({"caliber.approver", "caliber.viewer"}),
                current_resource_version="7",
                evidence_hash=EVIDENCE,
                now=T0 + timedelta(minutes=3),
            )
        assert refused.value.code == "role_revoked"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("not_approved", "not_approved"),
        ("wrong_evidence", "intent_mismatch"),
        ("wrong_policy", "intent_mismatch"),
        ("self_approval", "self_approval"),
        ("missing_version", "stale_version"),
    ],
)
def test_commit_recheck_refuses_unproven_binding_and_separation_of_duties(
    approver: ReferenceApp,
    mutation: str,
    expected: str,
) -> None:
    run_id = _waiting_run(approver)
    approval_id, proposal_id = _request(approver, run_id, f"occ-v05-{mutation}")
    if mutation != "not_approved":
        _approved(approver, run_id, approval_id)
    with approver.session_factory() as session:
        row = session.get(CaliberRuntimeApprovalRequest, approval_id)
        assert row is not None
        if mutation == "wrong_policy":
            row.policy_snapshot = {
                "required_role": "caliber.admin",
                "approval_count": 1,
                "timeout_behavior": "block",
                "allow_self_approval": False,
            }
        elif mutation == "self_approval":
            row.decided_by = EDITOR_A
        session.commit()

    kwargs: dict[str, Any] = {
        "approval_id": approval_id,
        "proposal_id": proposal_id,
        "approver_scopes": frozenset({"caliber.approver", "caliber.viewer"}),
        "current_resource_version": "7",
        "evidence_hash": "sha256:" + "x" * 64 if mutation == "wrong_evidence" else EVIDENCE,
        "now": T0 + timedelta(minutes=2),
    }
    if mutation == "missing_version":
        kwargs.pop("current_resource_version")

    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(session, **kwargs)
        assert refused.value.code == expected


def test_commit_recheck_refuses_unknown_rows_and_untrusted_clock(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, proposal_id = _valid_request(approver)
    valid_scopes = frozenset({"caliber.approver", "caliber.viewer"})

    with approver.session_factory() as session:
        checked = approval_valid_at_commit(
            session,
            approval_id=approval_id,
            proposal_id=proposal_id,
            approver_scopes=valid_scopes,
            current_resource_version="7",
            now=T0 + timedelta(minutes=2),
        )
        # The evidence is optional at this boundary; the stored, shape-checked
        # evidence remains the pin carried into the claim.
        assert checked.approval_id == approval_id

    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id="missing-proposal",
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "not_found"

    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id="missing-approval",
                proposal_id=proposal_id,
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "not_found"

    with approver.session_factory() as session:
        monkeypatch.setattr(
            approval_policy,
            "_proposal_run_id",
            lambda _session, _proposal_id: run_id,
        )
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id="missing-proposal",
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "not_found"

    monkeypatch.undo()
    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=datetime(2026, 1, 1),
            )
        assert refused.value.code == "authority_unavailable"


def test_commit_recheck_covers_corrupt_read_and_current_approver_policy(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, proposal_id = _valid_request(approver)
    valid_scopes = frozenset({"caliber.approver", "caliber.viewer"})
    original_reader = approval_policy.read_proposal
    monkeypatch.setattr(
        approval_policy,
        "read_proposal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ProposalError("corrupt", "bad")),
    )
    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "intent_mismatch"

    monkeypatch.setattr(
        approval_policy,
        "read_proposal",
        lambda *_args, **_kwargs: {"intent_hash": "sha256:" + "f" * 64},
    )
    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "intent_mismatch"
    monkeypatch.setattr(approval_policy, "read_proposal", original_reader)

    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=frozenset({"caliber.viewer"}),
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "role_revoked"

    with approver.session_factory() as session:
        project = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.project_id == PROJECT_A,
                CaliberProjectMember.user_id == APPROVER_A,
            )
        ).scalar_one()
        project_user = project.user_id
        project.user_id = "another-approver"
        session.commit()
    try:
        with approver.session_factory() as session:
            with pytest.raises(ApprovalCommitRefused) as refused:
                approval_valid_at_commit(
                    session,
                    approval_id=approval_id,
                    proposal_id=proposal_id,
                    approver_scopes=valid_scopes,
                    current_resource_version="7",
                    now=T0 + timedelta(minutes=2),
                )
            assert refused.value.code == "role_revoked"
    finally:
        with approver.session_factory() as session:
            member = session.execute(
                select(CaliberProjectMember).where(
                    CaliberProjectMember.project_id == PROJECT_A,
                    CaliberProjectMember.user_id == "another-approver",
                )
            ).scalar_one()
            member.user_id = project_user
            session.commit()


def test_commit_recheck_preserves_owner_role_and_authority_refusal_paths(
    approver: ReferenceApp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, proposal_id = _valid_request(approver)
    valid_scopes = frozenset(OWNER_ROLE_REQUIRED_SCOPES | {"caliber.approver", "caliber.viewer"})
    with approver.session_factory() as session:
        from caliber.db.models import CaliberProject

        project = session.get(CaliberProject, PROJECT_A)
        assert project is not None
        project.owner = APPROVER_A
        session.commit()
    with approver.session_factory() as session:
        checked = approval_valid_at_commit(
            session,
            approval_id=approval_id,
            proposal_id=proposal_id,
            approver_scopes=valid_scopes,
            current_resource_version="7",
            now=T0 + timedelta(minutes=2),
        )
        assert checked.approver_id == APPROVER_A

    real_identity = approval_policy.identity_for_run
    monkeypatch.setattr(
        approval_policy,
        "identity_for_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            IdentityDenied(AUTHORITY_UNAVAILABLE, "down")
        ),
    )
    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "authority_unavailable"

    monkeypatch.setattr(approval_policy, "identity_for_run", lambda *_args, **_kwargs: None)
    with approver.session_factory() as session:
        with pytest.raises(ApprovalCommitRefused) as refused:
            approval_valid_at_commit(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=valid_scopes,
                current_resource_version="7",
                now=T0 + timedelta(minutes=2),
            )
        assert refused.value.code == "role_revoked"
    monkeypatch.setattr(approval_policy, "identity_for_run", real_identity)
