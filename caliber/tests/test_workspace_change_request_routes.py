"""P4-D Change Request lifecycle and review-boundary tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceRevision,
    CaliberWorkspaceSource,
    CaliberWorkspaceVersionClaim,
)
from caliber.workspace_change_request_service import (
    SemanticVersion,
    accept_change_request,
    canonical_semantic_version,
    create_version_tag,
    record_check,
    record_external_attestation,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _create_project(client: TestClient, name: str = "Change Request project") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["data"]["project_id"]


def _seed_revision(
    db_session: Session, project_id: str, revision_id: str, digest: str, *, actor: str = "@test"
) -> None:
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id=revision_id,
            project_id=project_id,
            revision_number=int(revision_id.rsplit("-", 1)[-1]),
            source_id=None,
            source_commit_sha=None,
            manifest={"schema_version": 1},
            manifest_sha256=f"manifest-{digest}",
            source_bundle_sha256=f"bundle-{digest}",
            source_attestation="caller_attested",
            revision_sha256=digest,
            status="ready",
            created_by=actor,
        )
    )
    db_session.commit()


def _add_reviewer(db_session: Session, project_id: str, user_id: str = "@reviewer") -> None:
    db_session.add(
        CaliberProjectMember(
            member_id=f"PRJM-{user_id.removeprefix('@')}",
            project_id=project_id,
            user_id=user_id,
            role="editor",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()


def _change_request_path(project_id: str, request_id: str) -> str:
    return f"{PREFIX}/projects/{project_id}/change-requests/{request_id}"


def _create_request(
    client: TestClient,
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


def test_native_lifecycle_is_head_bound_and_history_is_recoverable(  # noqa: PLR0915
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _add_reviewer(db_session, project_id)
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@reviewer"}
    )
    _seed_revision(db_session, project_id, "WSR-1", "digest-1")
    _seed_revision(db_session, project_id, "WSR-2", "digest-2")

    draft = _create_request(client, project_id, "WSR-1")
    request_id = str(draft["change_request_id"])
    path = _change_request_path(project_id, request_id)
    assert draft["status"] == "draft"
    assert draft["head_generation"] == 1
    assert draft["version_claim"]["semantic_version"] == "1.0.0"  # type: ignore[index]

    submitted = client.post(f"{path}:submit", json={})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "open"
    checks = client.get(f"{path}/checks")
    assert checks.status_code == 200, checks.text
    assert checks.json()["data"] == []
    head_id = submitted.json()["data"]["current_head"]["head_id"]

    approved = client.post(
        f"{path}/reviews",
        headers={"X-CALIBER-User": "@reviewer"},
        json={"head_id": head_id, "decision": "approve", "rationale": "Looks good"},
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["data"]["decision"] == "approve"
    assert client.get(path).json()["data"]["status"] == "technically_approved"

    changes_requested = client.post(
        f"{path}/reviews",
        headers={"X-CALIBER-User": "@reviewer"},
        json={"head_id": head_id, "decision": "request_changes"},
    )
    assert changes_requested.status_code == 201, changes_requested.text
    assert changes_requested.json()["data"]["decision"] == "request_changes"

    comment = client.post(f"{path}/comments", json={"body": "Please keep this pinned."})
    assert comment.status_code == 201, comment.text
    assert client.get(f"{path}/comments").json()["data"][0]["body"] == "Please keep this pinned."

    old_head = head_id
    lock_version = client.get(path).json()["data"]["lock_version"]
    updated = client.post(
        f"{path}:update-head",
        json={
            "revision_id": "WSR-2",
            "change_summary": "Fix review feedback",
            "expected_lock_version": lock_version,
        },
    )
    assert updated.status_code == 200, updated.text
    new_head = updated.json()["data"]["current_head"]["head_id"]
    assert new_head != old_head
    assert updated.json()["data"]["head_generation"] == 2
    assert updated.json()["data"]["status"] == "open"

    stale_review = client.post(
        f"{path}/reviews",
        headers={"X-CALIBER-User": "@reviewer"},
        json={"head_id": old_head, "decision": "approve"},
    )
    assert stale_review.status_code == 409
    assert stale_review.json()["detail"] == "review_head_is_stale"
    assert len(client.get(f"{path}/reviews").json()["data"]) == 2
    assert len(client.get(f"{path}/reviewers").json()["data"]) == 1
    assert len(client.get(f"{path}/comments").json()["data"]) == 1

    current_review = client.post(
        f"{path}/reviews",
        headers={"X-CALIBER-User": "@reviewer"},
        json={"head_id": new_head, "decision": "approve"},
    )
    assert current_review.status_code == 201, current_review.text
    assert client.get(path).json()["data"]["status"] == "technically_approved"
    assert len(client.get(f"{path}/reviews").json()["data"]) == 3
    _add_reviewer(db_session, project_id, "@second")
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@reviewer,@second"}
    )
    assigned = client.put(
        f"{path}/reviewers/@second",
        json={"expected_lock_version": client.get(path).json()["data"]["lock_version"]},
    )
    assert assigned.status_code == 201, assigned.text
    assert client.get(path).json()["data"]["status"] == "open"
    removed_second = client.request(
        "DELETE",
        f"{path}/reviewers/@second",
        json={"expected_lock_version": client.get(path).json()["data"]["lock_version"]},
    )
    assert removed_second.status_code == 200, removed_second.text
    assert removed_second.json()["data"]["status"] == "technically_approved"
    removed_original = client.request(
        "DELETE",
        f"{path}/reviewers/@reviewer",
        json={"expected_lock_version": client.get(path).json()["data"]["lock_version"]},
    )
    assert removed_original.status_code == 200, removed_original.text
    assert removed_original.json()["data"]["status"] == "open"
    filtered = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests",
        params={"reviewer_user_id": "@reviewer", "semantic_version": "1.0.0"},
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["change_request_id"] for item in filtered.json()["data"]["items"]] == [request_id]


def test_self_review_unassigned_review_and_lock_conflicts_are_denied(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Review denials")
    _add_reviewer(db_session, project_id)
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@reviewer"}
    )
    _seed_revision(db_session, project_id, "WSR-3", "digest-3")
    _seed_revision(db_session, project_id, "WSR-4", "digest-4")

    own = _create_request(client, project_id, "WSR-3", version="2.0.0", reviewers=["@test"])
    own_path = _change_request_path(project_id, str(own["change_request_id"]))
    assert client.post(f"{own_path}:submit", json={}).status_code == 200
    own_head = client.get(own_path).json()["data"]["current_head"]["head_id"]
    self_review = client.post(
        f"{own_path}/reviews", json={"head_id": own_head, "decision": "approve"}
    )
    assert self_review.status_code == 403
    assert self_review.json()["detail"] == "self_review_forbidden"

    unassigned = _create_request(
        client, project_id, "WSR-4", version="3.0.0", reviewers=["@reviewer"]
    )
    unassigned_path = _change_request_path(project_id, str(unassigned["change_request_id"]))
    assert client.post(f"{unassigned_path}:submit", json={}).status_code == 200
    assert (
        client.post(
            f"{unassigned_path}/reviews",
            headers={"X-CALIBER-User": "@test"},
            json={"head_id": unassigned["current_head"]["head_id"], "decision": "approve"},  # type: ignore[index]
        ).status_code
        == 403
    )

    stale_lock = client.post(
        f"{own_path}:update-head",
        json={"revision_id": "WSR-4", "expected_lock_version": 1},
    )
    assert stale_lock.status_code == 409
    assert stale_lock.json()["detail"] == "change_request_lock_version_conflict"


def test_claims_are_canonical_unique_and_close_burns_version(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Version claims")
    _seed_revision(db_session, project_id, "WSR-5", "digest-5")
    first = _create_request(client, project_id, "WSR-5", version="4.0.0", reviewers=[])
    duplicate = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests",
        json={
            "title": "Duplicate",
            "head_revision_id": "WSR-5",
            "semantic_version": "4.0.0",
            "reviewer_user_ids": [],
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "semantic_version_unavailable"

    path = _change_request_path(project_id, str(first["change_request_id"]))
    closed = client.post(
        f"{path}:close",
        json={"reason": "superseded", "expected_lock_version": first["lock_version"]},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["data"]["status"] == "closed"
    assert closed.json()["data"]["version_claim"]["status"] == "abandoned"
    burned = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == first["change_request_id"]
        )
    )
    assert burned is not None and burned.status == "abandoned"
    reused = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests",
        json={
            "title": "Reuse",
            "head_revision_id": "WSR-5",
            "semantic_version": "4.0.0",
            "reviewer_user_ids": [],
        },
    )
    assert reused.status_code == 409

    with pytest.raises(HTTPException, match="invalid_semantic_version"):
        canonical_semantic_version("v4.0.0")
    canonical, _parsed = canonical_semantic_version("4.0.0+build.1")
    assert canonical == "4.0.0+build.1"


def test_source_provider_review_fails_closed_and_attestation_is_trusted_only(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Provider review")
    _seed_revision(db_session, project_id, "WSR-6", "digest-6")
    db_session.add(
        CaliberWorkspaceSource(
            source_id="WSS-provider",
            project_id=project_id,
            provider="github",
            provider_host="github.com",
            canonical_repository_id="github:owner/repo",
            display_path="owner/repo",
            status="active",
            external_review_policy={"required_checks": ["ci"]},
            external_review_policy_version="policy-1",
            external_review_policy_sha256="policy-digest",
        )
    )
    db_session.commit()
    request = _create_request(
        client,
        project_id,
        "WSR-6",
        version="5.0.0",
        reviewers=[],
        backend="source_provider",
    )
    path = _change_request_path(project_id, str(request["change_request_id"]))
    unavailable = client.post(f"{path}:submit", json={})
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"] == "external_review_attestation_required"
    head_id = request["current_head"]["head_id"]  # type: ignore[index]
    with pytest.raises(HTTPException, match="verified_attestation_requires_trusted_adapter"):
        record_external_attestation(
            db_session,
            change_request_id=str(request["change_request_id"]),
            head_id=str(head_id),
            source_id="WSS-provider",
            provider_change_request_id="42",
            provider_url="https://github.com/owner/repo/pull/42",
            provider_head_commit="head",
            provider_resulting_commit="result",
            source_tree_sha256="tree",
            workspace_revision_sha256="digest-6",
            policy_version="policy-1",
            policy_sha256="policy-digest",
            required_checks=["ci"],
            trusted_check_sources=["github-actions"],
            check_conclusions={"ci": "success"},
            review_actors=[{"user_id": "@reviewer", "decision": "approve"}],
            adapter_version="fake-v1",
            verification_input_digest="input-1",
            status="verified",
        )
    verified = record_external_attestation(
        db_session,
        change_request_id=str(request["change_request_id"]),
        head_id=str(head_id),
        source_id="WSS-provider",
        provider_change_request_id="42",
        provider_url="https://github.com/owner/repo/pull/42",
        provider_head_commit="head",
        provider_resulting_commit="result",
        source_tree_sha256="tree",
        workspace_revision_sha256="digest-6",
        policy_version="policy-1",
        policy_sha256="policy-digest",
        required_checks=["ci"],
        trusted_check_sources=["github-actions"],
        check_conclusions={"ci": "success"},
        review_actors=[{"user_id": "@reviewer", "decision": "approve"}],
        adapter_version="fake-v1",
        verification_input_digest="input-1",
        status="verified",
        trusted_adapter=True,
    )
    assert verified.status == "verified"
    submitted = client.post(f"{path}:submit", json={})
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["status"] == "open"
    checks = client.get(f"{path}/checks")
    assert checks.status_code == 200, checks.text
    assert checks.json()["data"][0]["check_name"] == "ci"
    attestations = client.get(f"{path}/external-review-attestations")
    assert attestations.status_code == 200
    assert attestations.json()["data"][0]["provider_change_request_id"] == "42"


def test_acceptance_primitive_cas_moves_only_one_request(
    session_factory, db_session: Session
) -> None:
    """The public API does not expose acceptance, but Phase 5 gets a tested CAS."""
    project = CaliberProject(
        project_id="PRJ-cas",
        name="CAS project",
        owner="@test",
        next_revision_number=10,
    )
    db_session.add(project)
    db_session.commit()
    _seed_revision(db_session, "PRJ-cas", "WSR-7", "digest-7")
    _seed_revision(db_session, "PRJ-cas", "WSR-8", "digest-8")
    # Build two prepared requests directly to isolate the CAS primitive from
    # HTTP authorization and the not-yet-built QA release service.
    for index, revision_id, version in ((1, "WSR-7", "6.0.0"), (2, "WSR-8", "7.0.0")):
        request_id = f"WSCR-cas-{index}"
        cr = CaliberWorkspaceChangeRequest(
            change_request_id=request_id,
            project_id="PRJ-cas",
            base_revision_id=None,
            current_head_revision_id=revision_id,
            created_by="@test",
            title=f"CAS {index}",
            status="technically_approved",
            review_backend="caliber",
            head_generation=1,
            lock_version=1,
        )
        db_session.add(cr)
        db_session.flush()
        db_session.add(
            CaliberWorkspaceChangeRequestHead(
                head_id=f"WSCRH-cas-{index}",
                change_request_id=request_id,
                generation=1,
                revision_id=revision_id,
                revision_sha256=f"digest-{6 + index}",
                changed_by="@test",
            )
        )
        db_session.add(
            CaliberWorkspaceVersionClaim(
                claim_id=f"WSVC-cas-{index}",
                project_id="PRJ-cas",
                change_request_id=request_id,
                semantic_version=version,
                status="reserved",
                claimed_by="@test",
            )
        )
    db_session.commit()

    first_session = session_factory()
    try:
        assert (
            accept_change_request(
                first_session,
                project_id="PRJ-cas",
                change_request_id="WSCR-cas-1",
                actor="@qa",
                qa_evidence={"passed": True, "revision_sha256": "digest-7"},
            )
            is True
        )
    finally:
        first_session.close()

    second_session = session_factory()
    try:
        assert (
            accept_change_request(
                second_session,
                project_id="PRJ-cas",
                change_request_id="WSCR-cas-2",
                actor="@qa",
                qa_evidence={"passed": True, "revision_sha256": "digest-8"},
            )
            is False
        )
    finally:
        second_session.close()

    final_session = session_factory()
    try:
        final_project = final_session.get(CaliberProject, "PRJ-cas")
        assert final_project is not None and final_project.accepted_revision_id == "WSR-7"
        stale = final_session.get(CaliberWorkspaceChangeRequest, "WSCR-cas-2")
        assert stale is not None and stale.status == "out_of_date"
    finally:
        final_session.close()


@pytest.mark.parametrize(
    "value",
    ["", "v1.2.3", "1.2", "1.02.3", "1.2.3-alpha.01", "1.2.3-"],
)
def test_semver_rejects_noncanonical_forms(value: str) -> None:
    with pytest.raises(HTTPException, match="invalid_semantic_version"):
        canonical_semantic_version(value)


def test_semver_precedence_covers_numeric_and_identifier_rules() -> None:
    assert SemanticVersion(1, 0, 0, ("alpha",)) < SemanticVersion(1, 0, 0, ("beta",))
    assert SemanticVersion(1, 0, 0, ("1",)) < SemanticVersion(1, 0, 0, ("alpha",))
    assert SemanticVersion(1, 0, 0, ("alpha", "1")) < SemanticVersion(1, 0, 0, ("alpha", "2"))
    assert SemanticVersion(1, 0, 0, ("alpha",)) < SemanticVersion(1, 0, 0, ("alpha", "1"))
    assert SemanticVersion(1, 0, 0) > SemanticVersion(1, 0, 0, ("rc",))
    assert SemanticVersion(1, 1, 0) > SemanticVersion(1, 0, 9)


def test_route_context_cursor_state_and_tag_errors_are_deterministic(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Route edge cases")
    _seed_revision(db_session, project_id, "WSR-10", "digest-edge")
    request = _create_request(client, project_id, "WSR-10", version="8.0.0", reviewers=[])
    request_id = str(request["change_request_id"])
    path = _change_request_path(project_id, request_id)

    mismatch = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests",
        headers={"X-CALIBER-Project": "another-project"},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "workspace_context_mismatch"
    invalid_cursor = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests", params={"cursor": "YmFk"}
    )
    assert invalid_cursor.status_code == 400
    assert invalid_cursor.json()["detail"] == "invalid_cursor"
    invalid_status = client.get(
        f"{PREFIX}/projects/{project_id}/change-requests", params={"status": "bogus"}
    )
    assert invalid_status.status_code == 400
    assert invalid_status.json()["detail"] == "invalid_change_request_status"
    missing = client.get(_change_request_path(project_id, "WSCR-missing"))
    assert missing.status_code == 404
    assert missing.json()["detail"] == "change_request_not_found"
    missing_related = client.get(f"{path}/comments")
    assert missing_related.status_code == 200
    assert missing_related.json()["data"] == []
    assert client.get(f"{PREFIX}/projects/{project_id}/version-tags").json()["data"] == []
    missing_tag = client.get(f"{PREFIX}/projects/{project_id}/version-tags/does-not-exist")
    assert missing_tag.status_code == 404
    assert missing_tag.json()["detail"] == "version_tag_not_found"

    closed = client.post(
        f"{path}:close",
        json={"reason": "edge test", "expected_lock_version": request["lock_version"]},
    )
    assert closed.status_code == 200
    assert client.post(f"{path}:submit", json={}).json()["detail"] == "change_request_not_draft"
    frozen = client.post(
        f"{path}:update-head",
        json={
            "revision_id": "WSR-10",
            "change_summary": "not allowed",
            "expected_lock_version": closed.json()["data"]["lock_version"],
        },
    )
    assert frozen.status_code == 409
    assert frozen.json()["detail"] == "change_request_head_not_mutable"


def test_internal_checks_and_version_tags_cover_attempt_history(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Internal check hooks")
    _seed_revision(db_session, project_id, "WSR-11", "digest-hook")
    request = _create_request(client, project_id, "WSR-11", version="9.0.0", reviewers=[])
    request_id = str(request["change_request_id"])
    head_id = str(request["current_head"]["head_id"])  # type: ignore[index]

    with pytest.raises(HTTPException, match="change_request_head_not_found"):
        record_check(
            db_session,
            change_request_id=request_id,
            head_id="WSCRH-missing",
            check_name="ci",
            status="queued",
            implementation_version="check-v1",
            input_digest="digest-hook",
        )
    queued = record_check(
        db_session,
        change_request_id=request_id,
        head_id=head_id,
        check_name="ci",
        status="queued",
        implementation_version="check-v1",
        input_digest="digest-hook",
    )
    assert queued.attempt_number == 1
    passed = record_check(
        db_session,
        change_request_id=request_id,
        head_id=head_id,
        check_name="ci",
        status="passed",
        implementation_version="check-v2",
        input_digest="digest-hook-2",
        evidence_ref="artifact://ci",
        evidence_digest="evidence-1",
        claimed_by="worker-1",
    )
    assert passed.check_id == queued.check_id
    retry = record_check(
        db_session,
        change_request_id=request_id,
        head_id=head_id,
        check_name="ci",
        status="failed",
        implementation_version="check-v3",
        input_digest="digest-hook-3",
    )
    assert retry.attempt_number == 2

    with pytest.raises(HTTPException, match="version_tag_does_not_match_claim"):
        create_version_tag(
            db_session,
            project_id=project_id,
            change_request_id=request_id,
            revision_id="WSR-11",
            tag="9.0.0",
            kind="qa_candidate",
            actor="@qa",
        )
    candidate = create_version_tag(
        db_session,
        project_id=project_id,
        change_request_id=request_id,
        revision_id="WSR-11",
        tag="9.0.0-rc.1",
        kind="qa_candidate",
        actor="@qa",
    )
    db_session.commit()
    assert candidate.tag == "9.0.0-rc.1"
    listed = client.get(f"{PREFIX}/projects/{project_id}/version-tags")
    assert listed.status_code == 200
    assert listed.json()["data"][0]["tag"] == "9.0.0-rc.1"


def test_create_request_rejects_invalid_project_baselines_and_reviewers(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Create denials")
    _seed_revision(db_session, project_id, "WSR-12", "digest-denial")

    invalid_base = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests",
        json={
            "title": "Invalid base",
            "head_revision_id": "WSR-12",
            "base_revision_id": "WSR-12",
            "semantic_version": "10.0.0",
            "reviewer_user_ids": [],
        },
    )
    assert invalid_base.status_code == 409
    assert invalid_base.json()["detail"] == "base_revision_must_be_null_for_first_acceptance"
    missing_revision = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests",
        json={
            "title": "Missing head",
            "head_revision_id": "WSR-missing",
            "semantic_version": "10.0.1",
            "reviewer_user_ids": [],
        },
    )
    assert missing_revision.status_code == 404
    assert missing_revision.json()["detail"] == "revision_not_found"
    ineligible = client.post(
        f"{PREFIX}/projects/{project_id}/change-requests",
        json={
            "title": "Unknown reviewer",
            "head_revision_id": "WSR-12",
            "semantic_version": "10.0.2",
            "reviewer_user_ids": ["@not-a-member"],
        },
    )
    assert ineligible.status_code == 403
    assert ineligible.json()["detail"] == "reviewer_not_eligible"


def test_out_of_date_rebase_replaces_an_older_version_claim(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "Rebase flow")
    _seed_revision(db_session, project_id, "WSR-13", "digest-13")
    _seed_revision(db_session, project_id, "WSR-14", "digest-14")
    _seed_revision(db_session, project_id, "WSR-15", "digest-15")

    accepted = _create_request(client, project_id, "WSR-13", version="10.0.0", reviewers=[])
    accepted_claim = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == accepted["change_request_id"]
        )
    )
    assert accepted_claim is not None
    accepted_claim.status = "accepted"
    project = db_session.get(CaliberProject, project_id)
    assert project is not None
    project.accepted_revision_id = "WSR-13"
    db_session.commit()

    candidate = _create_request(
        client,
        project_id,
        "WSR-14",
        base_revision_id="WSR-13",
        version="11.0.0",
        reviewers=[],
    )
    candidate_id = str(candidate["change_request_id"])
    bump = _create_request(
        client,
        project_id,
        "WSR-14",
        base_revision_id="WSR-13",
        version="12.0.0",
        reviewers=[],
    )
    bump_claim = db_session.scalar(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == bump["change_request_id"]
        )
    )
    assert bump_claim is not None
    bump_claim.status = "accepted"
    candidate_row = db_session.get(CaliberWorkspaceChangeRequest, candidate_id)
    project.accepted_revision_id = "WSR-14"
    assert candidate_row is not None
    candidate_row.status = "out_of_date"
    db_session.commit()

    rebased = client.post(
        f"{_change_request_path(project_id, candidate_id)}:rebase",
        json={
            "revision_id": "WSR-15",
            "change_summary": "Rebase onto the new accepted baseline",
            "expected_lock_version": candidate["lock_version"],
            "semantic_version": "13.0.0",
        },
    )
    assert rebased.status_code == 200, rebased.text
    data = rebased.json()["data"]
    assert data["base_revision_id"] == "WSR-14"
    assert data["current_head_revision_id"] == "WSR-15"
    assert data["version_claim"]["semantic_version"] == "13.0.0"
    claims = db_session.scalars(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == candidate_id
        )
    ).all()
    assert {claim.status for claim in claims} == {"abandoned", "reserved"}
