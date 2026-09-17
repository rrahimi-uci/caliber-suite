"""Focused fail-closed and helper coverage for the P4-D service boundary."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

import caliber.routes.workspace_change_requests as change_request_routes
from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceChangeRequestReviewer,
    CaliberWorkspaceExternalReviewAttestation,
    CaliberWorkspaceRevision,
    CaliberWorkspaceSource,
    CaliberWorkspaceVersionClaim,
)
from caliber.workspace_change_request_service import (
    CR_DRAFT,
    CR_OPEN,
    CR_OUT_OF_DATE,
    SemanticVersion,
    _active_source,
    _ensure_version_is_new,
    _head,
    _highest_accepted_version,
    _native_approval_complete,
    _native_checks_pass,
    _queue_required_checks,
    _request,
    _require_reviewer_eligible,
    _reserve_claim,
    _revision,
    _source_policy,
    _verified_external_attestation,
    accept_change_request,
    add_comment,
    assign_reviewer,
    canonical_semantic_version,
    close_change_request,
    create_change_request,
    create_version_tag,
    rebase_change_request,
    record_check,
    record_external_attestation,
    remove_reviewer,
    submit_change_request,
    submit_review,
    update_change_request_head,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client, name: str) -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["project_id"])


def _seed_revision(db_session: Session, project_id: str, revision_id: str, digest: str) -> None:
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id=revision_id,
            project_id=project_id,
            revision_number=int(revision_id.rsplit("-", 1)[-1].replace("WSE", "") or 1),
            manifest={"schema_version": 1},
            manifest_sha256=f"manifest-{digest}",
            source_bundle_sha256=f"bundle-{digest}",
            source_attestation="caller_attested",
            revision_sha256=digest,
            status="ready",
            created_by="@test",
        )
    )
    db_session.commit()


def _create_request(
    client,
    project_id: str,
    head_revision_id: str,
    *,
    base_revision_id: str | None = None,
    version: str = "1.0.0",
    reviewers: list[str] | None = None,
    backend: str = "caliber",
) -> dict[str, object]:
    response = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests",
        json={
            "title": "Ship package",
            "description": "A deterministic package change",
            "head_revision_id": head_revision_id,
            "base_revision_id": base_revision_id,
            "semantic_version": version,
            "review_backend": backend,
            "reviewer_user_ids": ["@reviewer"] if reviewers is None else reviewers,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _identity(project_id: str, user_id: str = "@test") -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user_id,
        scopes=frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR}),
        active_project_id=project_id,
    )


def _add_member(db_session: Session, project_id: str, user_id: str, role: str = "editor") -> None:
    db_session.add(
        CaliberProjectMember(
            member_id=f"PRJM-{user_id.removeprefix('@')}-{role}",
            project_id=project_id,
            user_id=user_id,
            role=role,
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()


def _source(db_session: Session, project_id: str, *, required: list[str] | None = None) -> None:
    db_session.add(
        CaliberWorkspaceSource(
            source_id=f"WSS-{project_id}",
            project_id=project_id,
            provider="github",
            provider_host="github.com",
            canonical_repository_id="github:owner/repo",
            display_path="owner/repo",
            status="active",
            external_review_policy={"required_checks": required or []},
            external_review_policy_version="policy-1",
            external_review_policy_sha256="policy-digest",
        )
    )
    db_session.commit()


def test_semver_and_lookup_helpers_fail_closed(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client, "Service helper edges")
    _seed_revision(db_session, project_id, "WSE-1", "edge-1")
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id="WSE-validating",
            project_id=project_id,
            revision_number=2,
            manifest={"schema_version": 1},
            manifest_sha256="manifest-validating",
            source_bundle_sha256="bundle-validating",
            source_attestation="caller_attested",
            revision_sha256="validating",
            status="validating",
            created_by="@test",
        )
    )
    db_session.commit()

    assert SemanticVersion(1, 0, 0).__lt__(object()) is NotImplemented
    assert SemanticVersion(1, 0, 0, ("alpha",)) < SemanticVersion(1, 0, 0)
    assert SemanticVersion(1, 0, 0, ("alpha",)) < SemanticVersion(1, 0, 0, ("alpha", "1"))
    assert SemanticVersion(1, 0, 0, ("alpha", "1")) > SemanticVersion(1, 0, 0, ("alpha",))
    assert SemanticVersion(1, 0, 0, ("alpha",)) == SemanticVersion(1, 0, 0, ("alpha",))
    assert canonical_semantic_version(" 1.2.3 ")[0] == "1.2.3"

    with pytest.raises(HTTPException, match="revision_not_found"):
        _revision(db_session, project_id, "WSE-missing")
    with pytest.raises(HTTPException, match="revision_not_ready"):
        _revision(db_session, project_id, "WSE-validating")
    with pytest.raises(HTTPException, match="change_request_not_found"):
        _request(db_session, project_id, "WSCR-missing")
    with pytest.raises(HTTPException, match="source_provider_unavailable"):
        _active_source(db_session, project_id)
    with pytest.raises(HTTPException, match="source_provider_unavailable"):
        _source_policy(db_session, project_id)

    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    _add_member(db_session, project_id, "@viewer", role="viewer")
    with pytest.raises(HTTPException, match="reviewer_not_eligible"):
        _require_reviewer_eligible(db_session, project, "@viewer", client.app.state.config)
    _add_member(db_session, project_id, "@editor")
    with pytest.raises(HTTPException, match="reviewer_not_eligible"):
        _require_reviewer_eligible(
            db_session,
            project,
            "@editor",
            client.app.state.config.model_copy(update={"admin_users": "", "operator_users": ""}),
        )

    assert _highest_accepted_version(db_session, project_id) is None
    assert _iso_for_test(None) is None
    assert _iso_for_test(datetime(2026, 1, 1, tzinfo=timezone.utc))

    db_session.add(
        CaliberWorkspaceVersionClaim(
            claim_id="WSVC-helper",
            project_id=project_id,
            change_request_id="WSCR-helper",
            semantic_version="1.0.0",
            status="accepted",
            claimed_by="@test",
        )
    )
    db_session.add(
        CaliberWorkspaceVersionClaim(
            claim_id="WSVC-helper-reserved",
            project_id=project_id,
            change_request_id="WSCR-helper-reserved",
            semantic_version="2.0.0",
            status="reserved",
            claimed_by="@test",
        )
    )
    db_session.commit()
    assert _highest_accepted_version(db_session, project_id) == SemanticVersion(1, 0, 0)
    with pytest.raises(HTTPException, match="semantic_version_not_greater_than_accepted"):
        _ensure_version_is_new(db_session, project_id, SemanticVersion(1, 0, 0))

    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    with pytest.raises(HTTPException, match="semantic_version_unavailable"):
        _reserve_claim(
            db_session,
            project=project,
            change_request_id="WSCR-helper-duplicate",
            semantic_version="2.0.0",
            actor="@test",
        )

    def fail_flush() -> None:
        raise IntegrityError("duplicate", {}, Exception("constraint"))

    monkeypatch.setattr(db_session, "flush", fail_flush)
    with pytest.raises(HTTPException, match="semantic_version_unavailable"):
        _reserve_claim(
            db_session,
            project=project,
            change_request_id="WSCR-helper-race",
            semantic_version="2.0.1",
            actor="@test",
        )


def _iso_for_test(value: datetime | None) -> str | None:
    """Keep the public serializer assertion independent from route internals."""
    from caliber.workspace_change_request_service import schema_timestamp

    return schema_timestamp(value)


def test_create_and_submit_denials_cover_current_base_and_claim_edges(
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Service submit edges")
    _seed_revision(db_session, project_id, "WSE-10", "edge-10")
    _seed_revision(db_session, project_id, "WSE-11", "edge-11")
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.accepted_revision_id = "WSE-10"
    db_session.commit()

    wrong_base = client.post(
        f"/ajax-api/2.0/mlflow/caliber/projects/{project_id}/change-requests",
        json={
            "title": "Wrong base",
            "description": "",
            "head_revision_id": "WSE-11",
            "base_revision_id": None,
            "semantic_version": "2.0.0",
            "reviewer_user_ids": [],
        },
    )
    assert wrong_base.status_code == 409
    assert wrong_base.json()["detail"] == "base_revision_not_current"

    same_head = client.post(
        f"/ajax-api/2.0/mlflow/caliber/projects/{project_id}/change-requests",
        json={
            "title": "Same head",
            "description": "",
            "head_revision_id": "WSE-10",
            "base_revision_id": "WSE-10",
            "semantic_version": "2.0.1",
            "reviewer_user_ids": [],
        },
    )
    assert same_head.status_code == 400
    assert same_head.json()["detail"] == "base_and_head_must_differ"

    _source(db_session, project_id)
    provider_with_reviewers = client.post(
        f"/ajax-api/2.0/mlflow/caliber/projects/{project_id}/change-requests",
        json={
            "title": "Mixed backend",
            "description": "",
            "head_revision_id": "WSE-11",
            "base_revision_id": "WSE-10",
            "semantic_version": "2.0.2",
            "review_backend": "source_provider",
            "reviewer_user_ids": ["@test"],
        },
    )
    assert provider_with_reviewers.status_code == 400
    assert provider_with_reviewers.json()["detail"] == "review_backends_cannot_be_combined"

    _add_member(db_session, project_id, "@editor")
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@editor"}
    )
    duplicate_reviewers = client.post(
        f"/ajax-api/2.0/mlflow/caliber/projects/{project_id}/change-requests",
        json={
            "title": "Duplicate reviewers",
            "description": "",
            "head_revision_id": "WSE-11",
            "base_revision_id": "WSE-10",
            "semantic_version": "2.0.3",
            "reviewer_user_ids": ["@editor", "@editor"],
        },
    )
    assert duplicate_reviewers.status_code == 201
    duplicate_id = duplicate_reviewers.json()["data"]["change_request_id"]
    assert (
        db_session.query(CaliberWorkspaceChangeRequestReviewer)
        .filter_by(change_request_id=duplicate_id, active=True)
        .count()
        == 1
    )

    no_reviewer = _create_request(
        client, project_id, "WSE-11", base_revision_id="WSE-10", version="2.0.4", reviewers=[]
    )
    no_reviewer_id = str(no_reviewer["change_request_id"])
    no_reviewer_row = db_session.get(CaliberWorkspaceChangeRequest, no_reviewer_id)
    assert no_reviewer_row is not None
    with pytest.raises(HTTPException, match="reviewer_assignment_required"):
        submit_change_request(
            db_session,
            project_id=project_id,
            change_request_id=no_reviewer_id,
            identity=_identity(project_id),
            config=client.app.state.config,
        )

    missing_claim = _create_request(
        client, project_id, "WSE-11", base_revision_id="WSE-10", version="2.0.5", reviewers=[]
    )
    claim = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == missing_claim["change_request_id"]
        )
    )
    assert claim is not None
    claim.status = "abandoned"
    db_session.commit()
    with pytest.raises(HTTPException, match="version_claim_required"):
        submit_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(missing_claim["change_request_id"]),
            identity=_identity(project_id),
            config=client.app.state.config,
        )

    out_of_date = _create_request(
        client, project_id, "WSE-11", base_revision_id="WSE-10", version="2.0.6", reviewers=[]
    )
    project.accepted_revision_id = "WSE-11"
    db_session.commit()
    with pytest.raises(HTTPException, match="change_request_out_of_date"):
        submit_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(out_of_date["change_request_id"]),
            identity=_identity(project_id),
            config=client.app.state.config,
        )
    assert (
        db_session.get(CaliberWorkspaceChangeRequest, out_of_date["change_request_id"]).status
        == CR_OUT_OF_DATE
    )  # type: ignore[union-attr]


def test_head_pointer_and_policy_helpers_cover_history_and_duplicate_queue(
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Policy helper edges")
    _seed_revision(db_session, project_id, "WSE-20", "edge-20")
    _source(db_session, project_id, required=["ci", "security"])
    request = _create_request(
        client, project_id, "WSE-20", version="3.0.0", reviewers=[], backend="source_provider"
    )
    request_id = str(request["change_request_id"])
    row = db_session.get(CaliberWorkspaceChangeRequest, request_id)
    assert row is not None
    head = _head(db_session, row)
    assert _source_policy(db_session, project_id) == (
        "policy-1",
        "policy-digest",
        ["ci", "security"],
    )
    _queue_required_checks(db_session, row, head)
    db_session.flush()
    _queue_required_checks(db_session, row, head)
    db_session.commit()
    assert (
        db_session.query(CaliberWorkspaceChangeRequestReviewer)
        .filter_by(change_request_id=request_id)
        .count()
        == 0
    )
    assert row.status == CR_DRAFT

    row.head_generation = 99
    with pytest.raises(RuntimeError, match="head pointer"):
        _head(db_session, row)


def test_close_requires_creator_or_manage_access_and_rejects_closed_state(
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Close helper edges")
    _seed_revision(db_session, project_id, "WSE-30", "edge-30")
    request = _create_request(client, project_id, "WSE-30", version="4.0.0", reviewers=[])
    request_id = str(request["change_request_id"])
    path = f"/ajax-api/2.0/mlflow/caliber/projects/{project_id}/change-requests/{request_id}"
    closed = client.post(
        f"{path}:close",
        json={"reason": "done", "expected_lock_version": request["lock_version"]},
    )
    assert closed.status_code == 200
    with pytest.raises(HTTPException, match="change_request_not_closeable"):
        close_change_request(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            identity=_identity(project_id),
            reason="again",
            expected_lock_version=int(closed.json()["data"]["lock_version"]),
        )

    # A non-creator with both project scopes may use the manage action.
    admin_identity = _identity(project_id, "@admin")
    _add_member(db_session, project_id, "@admin", role="owner")
    db_session.expire_all()
    closed_row = db_session.get(CaliberWorkspaceChangeRequest, request_id)
    assert closed_row is not None
    closed_row.status = CR_OPEN
    closed_row.lock_version += 1
    db_session.commit()
    reopened = close_change_request(
        db_session,
        project_id=project_id,
        change_request_id=request_id,
        identity=admin_identity,
        reason="administrative close",
        expected_lock_version=closed_row.lock_version,
    )
    assert reopened.status == "closed"


def test_direct_change_request_creation_uses_identity_and_preserves_head_history(
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Direct service create")
    _seed_revision(db_session, project_id, "WSE-40", "edge-40")
    request = create_change_request(
        db_session,
        project_id=project_id,
        identity=_identity(project_id),
        config=client.app.state.config,
        title="Direct create",
        description="description",
        head_revision_id="WSE-40",
        base_revision_id=None,
        semantic_version="5.0.0",
        review_backend="caliber",
        reviewer_user_ids=[],
    )
    assert request.status == CR_DRAFT
    assert (
        db_session.scalar(
            select(CaliberWorkspaceChangeRequestHead).where(
                CaliberWorkspaceChangeRequestHead.change_request_id == request.change_request_id
            )
        )
        is not None
    )
    request.status = "closed"
    db_session.commit()
    with pytest.raises(HTTPException, match="change_request_not_draft"):
        submit_change_request(
            db_session,
            project_id=project_id,
            change_request_id=request.change_request_id,
            identity=_identity(project_id),
            config=client.app.state.config,
        )


def test_comment_head_validation_is_scoped_to_the_request(client, db_session: Session) -> None:
    project_id = _create_project(client, "Comment helper edges")
    _seed_revision(db_session, project_id, "WSE-50", "edge-50")
    request = _create_request(client, project_id, "WSE-50", version="6.0.0", reviewers=[])
    with pytest.raises(HTTPException, match="change_request_head_not_found"):
        add_comment(
            db_session,
            project_id=project_id,
            change_request_id=str(request["change_request_id"]),
            identity=_identity(project_id),
            body="bad head",
            head_id="WSCRH-missing",
            resource_type=None,
            resource_name=None,
            source_path=None,
        )
    valid = add_comment(
        db_session,
        project_id=project_id,
        change_request_id=str(request["change_request_id"]),
        identity=_identity(project_id),
        body="valid head",
        head_id=str(request["current_head"]["head_id"]),
        resource_type="workflow",
        resource_name="example",
        source_path="workflows/example.yaml",
    )
    assert valid.head_id == request["current_head"]["head_id"]


def test_rebase_denials_cover_status_baseline_claim_and_version_rules(
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Rebase denial edges")
    for revision_id in ("WSE-60", "WSE-61", "WSE-62"):
        _seed_revision(db_session, project_id, revision_id, f"digest-{revision_id}")
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.accepted_revision_id = "WSE-60"
    db_session.commit()

    candidate = _create_request(
        client, project_id, "WSE-61", base_revision_id="WSE-60", version="10.0.0", reviewers=[]
    )
    accepted_version = _create_request(
        client, project_id, "WSE-61", base_revision_id="WSE-60", version="99.0.0", reviewers=[]
    )
    accepted_claim = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == accepted_version["change_request_id"]
        )
    )
    assert accepted_claim is not None
    accepted_claim.status = "accepted"
    existing_version = _create_request(
        client, project_id, "WSE-61", base_revision_id="WSE-60", version="100.0.0", reviewers=[]
    )
    project.accepted_revision_id = "WSE-62"
    candidate_row = db_session.get(CaliberWorkspaceChangeRequest, candidate["change_request_id"])
    assert candidate_row is not None
    candidate_row.status = CR_OUT_OF_DATE
    db_session.commit()

    with pytest.raises(HTTPException, match="change_request_not_out_of_date"):
        rebase_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(existing_version["change_request_id"]),
            identity=_identity(project_id),
            revision_id="WSE-61",
            change_summary="wrong status",
            expected_lock_version=1,
            semantic_version=None,
        )

    project.accepted_revision_id = "WSE-61"
    db_session.commit()
    with pytest.raises(HTTPException, match="base_and_head_must_differ"):
        rebase_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(candidate["change_request_id"]),
            identity=_identity(project_id),
            revision_id="WSE-61",
            change_summary="same base and head",
            expected_lock_version=1,
            semantic_version=None,
        )

    project.accepted_revision_id = "WSE-62"
    candidate_claim = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == candidate["change_request_id"],
            CaliberWorkspaceVersionClaim.status == "reserved",
        )
    )
    assert candidate_claim is not None
    candidate_claim.status = "abandoned"
    db_session.commit()
    with pytest.raises(HTTPException, match="version_claim_required"):
        rebase_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(candidate["change_request_id"]),
            identity=_identity(project_id),
            revision_id="WSE-61",
            change_summary="missing claim",
            expected_lock_version=1,
            semantic_version=None,
        )

    candidate_claim.status = "reserved"
    db_session.commit()
    with pytest.raises(HTTPException, match="new_semantic_version_required"):
        rebase_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(candidate["change_request_id"]),
            identity=_identity(project_id),
            revision_id="WSE-61",
            change_summary="version required",
            expected_lock_version=1,
            semantic_version=None,
        )
    with pytest.raises(HTTPException, match="semantic_version_not_greater_than_accepted"):
        rebase_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(candidate["change_request_id"]),
            identity=_identity(project_id),
            revision_id="WSE-61",
            change_summary="version too low",
            expected_lock_version=1,
            semantic_version="98.0.0",
        )
    with pytest.raises(HTTPException, match="semantic_version_unavailable"):
        rebase_change_request(
            db_session,
            project_id=project_id,
            change_request_id=str(candidate["change_request_id"]),
            identity=_identity(project_id),
            revision_id="WSE-61",
            change_summary="claimed version",
            expected_lock_version=1,
            semantic_version="100.0.0",
        )
    existing_row = db_session.get(
        CaliberWorkspaceChangeRequest, existing_version["change_request_id"]
    )
    assert existing_row is not None
    existing_row.status = CR_OUT_OF_DATE
    db_session.commit()
    rebased_without_version_change = rebase_change_request(
        db_session,
        project_id=project_id,
        change_request_id=str(existing_version["change_request_id"]),
        identity=_identity(project_id),
        revision_id="WSE-61",
        change_summary="claim already advances beyond accepted version",
        expected_lock_version=1,
        semantic_version=None,
    )
    assert rebased_without_version_change.status == CR_OPEN


def test_reviewer_and_native_check_edges_are_enforced(  # noqa: PLR0915
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Reviewer and check edges")
    _seed_revision(db_session, project_id, "WSE-70", "digest-70")
    _seed_revision(db_session, project_id, "WSE-71", "digest-71")
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.accepted_revision_id = "WSE-70"
    db_session.commit()
    _add_member(db_session, project_id, "@reviewer")
    _add_member(db_session, project_id, "@second")
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@reviewer,@second"}
    )

    request = _create_request(
        client,
        project_id,
        "WSE-71",
        base_revision_id="WSE-70",
        version="20.0.0",
        reviewers=["@reviewer"],
    )
    request_id = str(request["change_request_id"])
    request_row = db_session.get(CaliberWorkspaceChangeRequest, request_id)
    assert request_row is not None
    request_row.status = CR_OPEN
    db_session.commit()
    with pytest.raises(HTTPException, match="base_and_head_must_differ"):
        update_change_request_head(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            identity=_identity(project_id),
            revision_id="WSE-70",
            change_summary="same revision",
            expected_lock_version=request_row.lock_version,
        )
    with pytest.raises(HTTPException, match="reviewer_already_assigned"):
        assign_reviewer(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            identity=_identity(project_id),
            config=client.app.state.config,
            user_id="@reviewer",
            expected_lock_version=request_row.lock_version,
        )
    assigned_on_open = assign_reviewer(
        db_session,
        project_id=project_id,
        change_request_id=request_id,
        identity=_identity(project_id),
        config=client.app.state.config,
        user_id="@second",
        expected_lock_version=request_row.lock_version,
    )
    assert assigned_on_open.user_id == "@second"

    _source(db_session, project_id, required=["ci"])
    source_request = _create_request(
        client,
        project_id,
        "WSE-71",
        base_revision_id="WSE-70",
        version="21.0.0",
        reviewers=[],
        backend="source_provider",
    )
    source_row = db_session.get(CaliberWorkspaceChangeRequest, source_request["change_request_id"])
    assert source_row is not None
    with pytest.raises(HTTPException, match="review_backends_cannot_be_combined"):
        assign_reviewer(
            db_session,
            project_id=project_id,
            change_request_id=str(source_request["change_request_id"]),
            identity=_identity(project_id),
            config=client.app.state.config,
            user_id="@reviewer",
            expected_lock_version=source_row.lock_version,
        )

    draft_with_reviewer = _create_request(
        client,
        project_id,
        "WSE-71",
        base_revision_id="WSE-70",
        version="22.0.0",
        reviewers=["@reviewer"],
    )
    draft_row = db_session.get(
        CaliberWorkspaceChangeRequest, draft_with_reviewer["change_request_id"]
    )
    assert draft_row is not None
    removed = remove_reviewer(
        db_session,
        project_id=project_id,
        change_request_id=str(draft_with_reviewer["change_request_id"]),
        identity=_identity(project_id),
        user_id="@reviewer",
        expected_lock_version=draft_row.lock_version,
    )
    assert removed.status == CR_DRAFT
    with pytest.raises(HTTPException, match="reviewer_not_assigned"):
        remove_reviewer(
            db_session,
            project_id=project_id,
            change_request_id=str(draft_with_reviewer["change_request_id"]),
            identity=_identity(project_id),
            user_id="@reviewer",
            expected_lock_version=removed.lock_version,
        )
    draft_row.status = "accepted"
    db_session.commit()
    with pytest.raises(HTTPException, match="reviewer_assignment_frozen"):
        remove_reviewer(
            db_session,
            project_id=project_id,
            change_request_id=str(draft_with_reviewer["change_request_id"]),
            identity=_identity(project_id),
            user_id="@second",
            expected_lock_version=draft_row.lock_version,
        )
    with pytest.raises(HTTPException, match="reviewer_assignment_frozen"):
        assign_reviewer(
            db_session,
            project_id=project_id,
            change_request_id=str(draft_with_reviewer["change_request_id"]),
            identity=_identity(project_id),
            config=client.app.state.config,
            user_id="@second",
            expected_lock_version=draft_row.lock_version,
        )

    provider_for_checks = _create_request(
        client,
        project_id,
        "WSE-71",
        base_revision_id="WSE-70",
        version="23.0.0",
        reviewers=[],
        backend="source_provider",
    )
    provider_row = db_session.get(
        CaliberWorkspaceChangeRequest, provider_for_checks["change_request_id"]
    )
    assert provider_row is not None
    provider_head = _head(db_session, provider_row)
    assert not _native_checks_pass(db_session, provider_row, provider_head)
    assert not _native_approval_complete(db_session, provider_row, provider_head)
    check = record_check(
        db_session,
        change_request_id=provider_row.change_request_id,
        head_id=provider_head.head_id,
        check_name="ci",
        status="queued",
        implementation_version="edge-check",
        input_digest="digest-71",
    )
    assert check.status == "queued"
    record_check(
        db_session,
        change_request_id=provider_row.change_request_id,
        head_id=provider_head.head_id,
        check_name="ci",
        status="passed",
        implementation_version="edge-check-2",
        input_digest="digest-71-2",
    )
    record_check(
        db_session,
        change_request_id=provider_row.change_request_id,
        head_id=provider_head.head_id,
        check_name="ci",
        status="failed",
        implementation_version="edge-check-3",
        input_digest="digest-71-3",
    )
    record_check(
        db_session,
        change_request_id=provider_row.change_request_id,
        head_id=provider_head.head_id,
        check_name="ci",
        status="passed",
        implementation_version="edge-check-4",
        input_digest="digest-71-4",
    )
    assert _native_checks_pass(db_session, provider_row, provider_head)

    native_request = _create_request(
        client,
        project_id,
        "WSE-71",
        base_revision_id="WSE-70",
        version="24.0.0",
        reviewers=["@reviewer", "@second"],
    )
    native_row = db_session.get(CaliberWorkspaceChangeRequest, native_request["change_request_id"])
    assert native_row is not None
    native_head = _head(db_session, native_row)
    with pytest.raises(HTTPException, match="change_request_not_reviewable"):
        submit_review(
            db_session,
            project_id=project_id,
            change_request_id=native_row.change_request_id,
            identity=CaliberIdentity(
                "@reviewer",
                frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR}),
                active_project_id=project_id,
            ),
            config=client.app.state.config,
            head_id=native_head.head_id,
            decision="approve",
            rationale="too early",
        )
    submit_change_request(
        db_session,
        project_id=project_id,
        change_request_id=native_row.change_request_id,
        identity=_identity(project_id),
        config=client.app.state.config,
    )
    first_approval = submit_review(
        db_session,
        project_id=project_id,
        change_request_id=native_row.change_request_id,
        identity=CaliberIdentity(
            "@reviewer",
            frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR}),
            active_project_id=project_id,
        ),
        config=client.app.state.config,
        head_id=native_head.head_id,
        decision="approve",
        rationale="partial approval",
    )
    assert first_approval.decision == "approve"
    assert native_row.status == CR_OPEN
    provider_head = _head(db_session, provider_row)
    with pytest.raises(HTTPException, match="review_backends_cannot_be_combined"):
        submit_review(
            db_session,
            project_id=project_id,
            change_request_id=provider_row.change_request_id,
            identity=_identity(project_id),
            config=client.app.state.config,
            head_id=provider_head.head_id,
            decision="approve",
            rationale="wrong backend",
        )


def test_external_attestation_validation_and_duplicate_guards(client, db_session: Session) -> None:
    project_id = _create_project(client, "Attestation edge cases")
    _seed_revision(db_session, project_id, "WSE-80", "digest-80")
    _source(db_session, project_id, required=["ci"])
    request = _create_request(
        client, project_id, "WSE-80", version="30.0.0", reviewers=[], backend="source_provider"
    )
    request_id = str(request["change_request_id"])
    row = db_session.get(CaliberWorkspaceChangeRequest, request_id)
    assert row is not None
    head = _head(db_session, row)
    source_id = f"WSS-{project_id}"

    def attestation_kwargs(**overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "change_request_id": request_id,
            "head_id": head.head_id,
            "source_id": source_id,
            "provider_change_request_id": "provider-1",
            "provider_url": "https://github.com/owner/repo/pull/1",
            "provider_head_commit": "head",
            "provider_resulting_commit": "result",
            "source_tree_sha256": "tree",
            "workspace_revision_sha256": "digest-80",
            "policy_version": "policy-1",
            "policy_sha256": "policy-digest",
            "required_checks": ["ci"],
            "trusted_check_sources": ["github-actions"],
            "check_conclusions": {"ci": "success"},
            "review_actors": [{"user_id": "@reviewer", "decision": "approve"}],
            "adapter_version": "edge-adapter",
            "verification_input_digest": "input-1",
            "status": "verified",
            "trusted_adapter": True,
        }
        values.update(overrides)
        return values

    for index, overrides in enumerate(
        (
            {"policy_version": "old-policy"},
            {"trusted_check_sources": []},
            {"check_conclusions": {"ci": "failure"}},
            {"review_actors": [{"user_id": "@test", "decision": "approve"}]},
            {"review_actors": [{"user_id": "@reviewer", "decision": "request_changes"}]},
        ),
        start=2,
    ):
        values = attestation_kwargs(
            provider_change_request_id=f"provider-{index}",
            verification_input_digest=f"input-{index}",
            **overrides,
        )
        db_session.add(
            CaliberWorkspaceExternalReviewAttestation(
                attestation_id=f"WSXA-{index}",
                **{k: v for k, v in values.items() if k != "trusted_adapter"},
            )
        )
    db_session.commit()
    assert not _verified_external_attestation(
        db_session, row, head, _revision(db_session, project_id, "WSE-80")
    )

    valid = record_external_attestation(db_session, **attestation_kwargs())
    assert valid.status == "verified"
    with pytest.raises(HTTPException, match="duplicate_external_attestation"):
        record_external_attestation(db_session, **attestation_kwargs())

    with pytest.raises(HTTPException, match="change_request_head_not_found"):
        record_external_attestation(
            db_session, **attestation_kwargs(change_request_id="WSCR-missing")
        )
    caliber_request = _create_request(client, project_id, "WSE-80", version="31.0.0", reviewers=[])
    with pytest.raises(HTTPException, match="review_backends_cannot_be_combined"):
        record_external_attestation(
            db_session,
            **attestation_kwargs(
                change_request_id=str(caliber_request["change_request_id"]),
                head_id=str(caliber_request["current_head"]["head_id"]),
                provider_change_request_id="provider-caliber",
                verification_input_digest="input-caliber",
            ),
        )
    with pytest.raises(HTTPException, match="source_not_found"):
        record_external_attestation(
            db_session,
            **attestation_kwargs(
                source_id="WSS-missing",
                provider_change_request_id="provider-source",
                verification_input_digest="input-source",
            ),
        )
    with pytest.raises(HTTPException, match="attestation_revision_digest_mismatch"):
        record_external_attestation(
            db_session,
            **attestation_kwargs(
                provider_change_request_id="provider-digest",
                verification_input_digest="input-digest",
                workspace_revision_sha256="wrong",
            ),
        )


def test_version_tag_and_acceptance_primitives_reject_invalid_inputs(
    client, db_session: Session
) -> None:
    project_id = _create_project(client, "Tag and accept edge cases")
    _seed_revision(db_session, project_id, "WSE-90", "digest-90")
    request = _create_request(client, project_id, "WSE-90", version="40.0.0", reviewers=[])
    request_id = str(request["change_request_id"])
    with pytest.raises(HTTPException, match="project_not_found"):
        create_version_tag(
            db_session,
            project_id="PRJ-missing",
            change_request_id=request_id,
            revision_id="WSE-90",
            tag="40.0.0",
            kind="accepted",
            actor="@qa",
        )
    claim = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == request_id
        )
    )
    assert claim is not None
    claim.status = "abandoned"
    db_session.commit()
    with pytest.raises(HTTPException, match="version_claim_required"):
        create_version_tag(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            revision_id="WSE-90",
            tag="40.0.0",
            kind="accepted",
            actor="@qa",
        )

    claim.status = "reserved"
    db_session.commit()
    with pytest.raises(HTTPException, match="change_request_not_qa_eligible"):
        accept_change_request(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            actor="@qa",
            qa_evidence={"passed": True, "revision_sha256": "digest-90"},
        )
    tag = create_version_tag(
        db_session,
        project_id=project_id,
        change_request_id=request_id,
        revision_id="WSE-90",
        tag="40.0.0",
        kind="accepted",
        actor="@qa",
    )
    db_session.commit()
    assert tag.tag == "40.0.0"
    with pytest.raises(HTTPException, match="version_tag_unavailable"):
        create_version_tag(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            revision_id="WSE-90",
            tag="40.0.0",
            kind="accepted",
            actor="@qa",
        )

    with pytest.raises(HTTPException, match="change_request_not_found"):
        accept_change_request(
            db_session,
            project_id=project_id,
            change_request_id="WSCR-missing",
            actor="@qa",
            qa_evidence={"passed": True, "revision_sha256": "digest-90"},
        )
    with pytest.raises(HTTPException, match="change_request_not_found"):
        accept_change_request(
            db_session,
            project_id="PRJ-missing",
            change_request_id=request_id,
            actor="@qa",
            qa_evidence={"passed": True, "revision_sha256": "digest-90"},
        )
    request_row = db_session.get(CaliberWorkspaceChangeRequest, request_id)
    assert request_row is not None
    request_row.status = "technically_approved"
    claim.status = "reserved"
    db_session.commit()
    with pytest.raises(HTTPException, match="qa_evidence_insufficient"):
        accept_change_request(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            actor="@qa",
            qa_evidence={"passed": False, "revision_sha256": "wrong"},
        )
    claim.status = "abandoned"
    db_session.commit()
    with pytest.raises(HTTPException, match="version_claim_required"):
        accept_change_request(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            actor="@qa",
            qa_evidence={"passed": True, "revision_sha256": "digest-90"},
        )


def test_route_filters_cursors_related_history_and_tags(
    client, db_session: Session, session_factory
) -> None:
    project_id = _create_project(client, "Route coverage edges")
    _seed_revision(db_session, project_id, "WSE-100", "digest-100")
    _seed_revision(db_session, project_id, "WSE-101", "digest-101")
    _add_member(db_session, project_id, "@reviewer")
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@reviewer"}
    )
    first = _create_request(client, project_id, "WSE-100", version="50.0.0")
    second = _create_request(client, project_id, "WSE-101", version="51.0.0", reviewers=[])
    first_id = str(first["change_request_id"])
    first_path = f"{PREFIX}/projects/{project_id}/change-requests/{first_id}"

    listed = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests",
        params={
            "limit": 1,
            "status": "draft",
            "created_by": "@test",
            "reviewer_user_id": "@reviewer",
            "semantic_version": "50.0.0",
        },
    )
    assert listed.status_code == 200
    assert [item["change_request_id"] for item in listed.json()["data"]["items"]] == [first_id]
    assert listed.json()["next_cursor"] is None

    unfiltered = client.get(f"{PREFIX}/projects/{project_id}/change-requests", params={"limit": 1})
    assert unfiltered.status_code == 200
    cursor = unfiltered.json()["next_cursor"]
    assert cursor
    page_two = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests",
        params={"limit": 1, "cursor": cursor},
    )
    assert page_two.status_code == 200
    assert page_two.json()["data"]["items"]

    missing_related = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests/WSCR-missing/comments"
    )
    assert missing_related.status_code == 404
    assert missing_related.json()["detail"] == "change_request_not_found"
    head_items, head_cursor = change_request_routes._list_related_sync(
        session_factory,
        project_id=project_id,
        change_request_id=first_id,
        identity=_identity(project_id),
        kind="heads",
        limit=1,
        cursor=None,
        project_action="read",
    )
    assert len(head_items) == 1 and head_cursor is None

    for body in ("first comment", "second comment"):
        response = client.post(f"{first_path}/comments", json={"body": body})
        assert response.status_code == 201
    comments_page = client.get(f"{first_path}/comments", params={"limit": 1})
    assert comments_page.status_code == 200
    comments_cursor = comments_page.json()["next_cursor"]
    assert comments_cursor
    comments_page_two = client.get(
        f"{first_path}/comments", params={"limit": 1, "cursor": comments_cursor}
    )
    assert comments_page_two.status_code == 200
    assert comments_page_two.json()["data"]
    encoded = change_request_routes._encode_cursor(datetime.now(timezone.utc), "cursor-id")
    assert change_request_routes._decode_cursor(encoded)[1] == "cursor-id"

    first_tag = create_version_tag(
        db_session,
        project_id=project_id,
        change_request_id=first_id,
        revision_id="WSE-100",
        tag="50.0.0",
        kind="accepted",
        actor="@qa",
    )
    second_tag = create_version_tag(
        db_session,
        project_id=project_id,
        change_request_id=str(second["change_request_id"]),
        revision_id="WSE-101",
        tag="51.0.0",
        kind="accepted",
        actor="@qa",
    )
    db_session.commit()
    tags_page = client.get(f"{PREFIX}/projects/{project_id}/version-tags", params={"limit": 1})
    assert tags_page.status_code == 200
    tags_cursor = tags_page.json()["next_cursor"]
    assert tags_cursor
    tags_page_two = client.get(
        f"{PREFIX}/projects/{project_id}/version-tags",
        params={"limit": 1, "cursor": tags_cursor},
    )
    assert tags_page_two.status_code == 200
    assert tags_page_two.json()["data"]
    tag_response = client.get(f"{PREFIX}/projects/{project_id}/version-tags/{first_tag.tag}")
    assert tag_response.status_code == 200
    assert tag_response.json()["data"]["tag"] == first_tag.tag
    assert second_tag.tag == "51.0.0"


def test_refresh_external_route_dispatch_and_fail_closed_backends(
    client, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _create_project(client, "Refresh route edges")
    _seed_revision(db_session, project_id, "WSE-110", "digest-110")
    request = _create_request(client, project_id, "WSE-110", version="60.0.0", reviewers=[])
    request_path = f"{PREFIX}/projects/{project_id}/change-requests/{request['change_request_id']}"
    caliber_error = client.post(f"{request_path}:refresh-external-review")
    assert caliber_error.status_code == 409
    assert caliber_error.json()["detail"] == "review_backends_cannot_be_combined"

    _source(db_session, project_id)
    provider = _create_request(
        client,
        project_id,
        "WSE-110",
        version="61.0.0",
        reviewers=[],
        backend="source_provider",
    )
    provider_path = (
        f"{PREFIX}/projects/{project_id}/change-requests/{provider['change_request_id']}"
    )
    missing = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests/WSCR-missing:refresh-external-review"
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == "change_request_not_found"
    unavailable = client.post(f"{provider_path}:refresh-external-review")
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"] == "source_provider_unavailable"

    monkeypatch.setattr(
        change_request_routes, "_refresh_external_sync", lambda *args, **kwargs: None
    )
    queued = client.post(f"{provider_path}:refresh-external-review")
    assert queued.status_code == 202
    assert queued.json()["data"]["status"] == "queued"
