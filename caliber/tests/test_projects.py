"""Project (workspace) routes: CRUD + project-scoped file upload/list/download."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import WorkflowStorageConfig
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
    CaliberWorkspaceEnvironment,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_OWNER
from caliber.storage import LocalStorageBackend, WorkingDirectoryService
from caliber.workflows.manifest import ManagedFileReference

PREFIX = "/ajax-api/2.0/mlflow/caliber"


@pytest.fixture
def proj_client(client: TestClient, tmp_path: Path) -> TestClient:
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    client.app.state.config = client.app.state.config.model_copy(update={"workflow_storage": cfg})
    client.app.state.working_dir_service = WorkingDirectoryService(
        LocalStorageBackend(cfg.base_uri), cfg
    )
    return client


def _create(client: TestClient, name: str = "Acme Support") -> str:
    resp = client.post(f"{PREFIX}/projects", json={"name": name, "description": "demo"})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["project_id"]


def test_create_list_get_project(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    assert pid.startswith("PRJ-")

    listing = proj_client.get(f"{PREFIX}/projects").json()["data"]
    assert any(
        p["project_id"] == pid and p["file_count"] == 0 and p["storage_backend"] == "local"
        for p in listing
    )

    detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    assert detail["name"] == "Acme Support" and detail["file_count"] == 0


def test_admin_lists_only_their_own_projects(proj_client: TestClient, db_session: Session) -> None:
    """`P1-B`: `caliber.admin` is no longer an implicit workspace role, so
    `list_projects` must apply the owner/member filter to admins too --
    the regression-proving test for the bypass removal *and* its coupled
    `list_projects` fix (an admin viewing a mix of owned and non-owned
    projects used to crash with an uncaught 404 the moment the
    `project_role()` bypass alone was removed without this fix)."""
    mine = _create(proj_client, "Mine")
    db_session.add(CaliberProject(project_id="PRJ-someone-elses", name="Not mine", owner="@other"))
    db_session.commit()

    resp = proj_client.get(f"{PREFIX}/projects")
    assert resp.status_code == 200, resp.text
    ids = {p["project_id"] for p in resp.json()["data"]}
    assert mine in ids
    assert "PRJ-someone-elses" not in ids


def test_project_storage_endpoint_reports_active_backend(proj_client: TestClient) -> None:
    resp = proj_client.get(f"{PREFIX}/projects/storage")
    assert resp.status_code == 200
    payload = resp.json()["data"]
    assert payload["backend"] == "local"
    assert payload["backend_label"] == "Local file system"
    assert payload["available_backends"][0]["id"] == "local"
    assert payload["available_backends"][1]["id"] == "s3"
    assert payload["available_backends"][1]["configured"] is False
    assert payload["base_uri"].startswith("file://")


def test_project_storage_endpoint_reports_configured_minio(
    proj_client: TestClient, tmp_path: Path
) -> None:
    cfg = WorkflowStorageConfig(
        backend="local",
        base_uri=f"file://{tmp_path}/ws",
        bucket="caliber-test",
        region="us-east-1",
    )
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"workflow_storage": cfg}
    )
    proj_client.app.state.working_dir_service = WorkingDirectoryService(
        LocalStorageBackend(cfg.base_uri), cfg
    )

    payload = proj_client.get(f"{PREFIX}/projects/storage").json()["data"]
    s3 = next(item for item in payload["available_backends"] if item["id"] == "s3")
    assert s3["label"] == "MinIO / S3-compatible object storage"
    assert s3["configured"] is True
    assert payload["bucket"] == "caliber-test"


def test_duplicate_name_conflicts(proj_client: TestClient) -> None:
    _create(proj_client, "Billing")
    resp = proj_client.post(f"{PREFIX}/projects", json={"name": "Billing"})
    assert resp.status_code == 409


def test_create_requires_name_and_operator(proj_client: TestClient) -> None:
    assert proj_client.post(f"{PREFIX}/projects", json={}).status_code == 400
    assert (
        proj_client.post(
            f"{PREFIX}/projects",
            json={"name": "X", "storage_backend": "minio"},
        ).status_code
        == 400
    )
    resp = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "X"},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_create_project_seeds_slug_source_mode_and_environments(
    proj_client: TestClient, db_session: Session
) -> None:
    """`P1-A`: creation transactionally derives a slug, defaults the source
    mode, and seeds all four fixed environments (dev active, the rest
    disabled) -- none of this is in the response schema yet (a separate,
    later slice), so this asserts against the DB directly."""
    pid = _create(proj_client, "Mortgage Underwriting")

    project = db_session.execute(
        select(CaliberProject).where(CaliberProject.project_id == pid)
    ).scalar_one()
    assert project.slug == "mortgage-underwriting"
    assert project.source_mode == "caliber_managed"

    environments = (
        db_session.execute(
            select(CaliberWorkspaceEnvironment)
            .where(CaliberWorkspaceEnvironment.project_id == pid)
            .order_by(CaliberWorkspaceEnvironment.promotion_order)
        )
        .scalars()
        .all()
    )
    assert [(e.name, e.environment_class, e.promotion_order, e.status) for e in environments] == [
        ("dev", "development", 10, "active"),
        ("qa", "qa", 20, "disabled"),
        ("staging", "staging", 30, "disabled"),
        ("prod", "production", 40, "disabled"),
    ]


def test_create_project_slug_collision_gets_a_deterministic_suffix(
    proj_client: TestClient, db_session: Session
) -> None:
    """Two projects whose names slugify identically (``"Demo"`` and
    ``"demo!"``) must not collide -- the second gets a numeric suffix, the
    same strategy migration 0093's backfill uses for pre-existing rows."""
    first = _create(proj_client, "Demo")
    second = _create(proj_client, "demo!")

    slugs = {
        pid: db_session.execute(
            select(CaliberProject.slug).where(CaliberProject.project_id == pid)
        ).scalar_one()
        for pid in (first, second)
    }
    assert slugs[first] == "demo"
    assert slugs[second] == "demo-2"


def test_create_requires_both_operator_and_approver(proj_client: TestClient) -> None:
    """`P1-A`: `project.create` is now an AND of two scopes, not an OR --
    holding only one is not enough. The default test identity (`@test`) is
    admin-scoped, which implies both, so this reconfigures the fixture app
    with dedicated single-scope users to prove the conjunction directly."""
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "@op-only", "approver_users": "@appr-only"}
    )

    operator_only = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Operator Only"},
        headers={"X-CALIBER-User": "@op-only"},
    )
    assert operator_only.status_code == 403

    approver_only = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Approver Only"},
        headers={"X-CALIBER-User": "@appr-only"},
    )
    assert approver_only.status_code == 403

    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "@both", "approver_users": "@both"}
    )
    both = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Both Scopes"},
        headers={"X-CALIBER-User": "@both"},
    )
    assert both.status_code == 201, both.text


def test_update_project_rename(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Old Name")
    resp = proj_client.patch(f"{PREFIX}/projects/{pid}", json={"name": "New Name"})
    assert resp.status_code == 200
    assert resp.json()["data"]["name"] == "New Name"


def test_update_project_rejects_status_in_favor_of_dedicated_routes(
    proj_client: TestClient,
) -> None:
    """`P1-C`, section 12.2: `PATCH` narrows to name/description only --
    lifecycle status now moves exclusively through `:archive`/`:restore`,
    which also record provenance (`archived_at`/`archived_by`) that the old
    bare status flip never set."""
    pid = _create(proj_client, "Still Active")
    resp = proj_client.patch(f"{PREFIX}/projects/{pid}", json={"status": "archived"})
    assert resp.status_code == 400
    assert proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]["status"] == "active"


def test_archive_and_restore_project(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Archivable")

    archived = proj_client.post(f"{PREFIX}/projects/{pid}/archive")
    assert archived.status_code == 200, archived.text
    data = archived.json()["data"]
    assert data["status"] == "archived"
    assert data["archived_by"] == "@test"
    assert data["archived_at"] is not None

    # archived projects are excluded from the default list
    listing = proj_client.get(f"{PREFIX}/projects").json()["data"]
    assert all(p["project_id"] != pid for p in listing)
    # ...but visible with ?status=all
    all_listing = proj_client.get(f"{PREFIX}/projects?status=all").json()["data"]
    assert any(p["project_id"] == pid for p in all_listing)

    # archiving an already-archived project conflicts
    assert proj_client.post(f"{PREFIX}/projects/{pid}/archive").status_code == 409
    # restoring a non-archived project conflicts
    other = _create(proj_client, "Never Archived")
    assert proj_client.post(f"{PREFIX}/projects/{other}/restore").status_code == 409

    restored = proj_client.post(f"{PREFIX}/projects/{pid}/restore")
    assert restored.status_code == 200, restored.text
    restored_data = restored.json()["data"]
    assert restored_data["status"] == "active"
    assert restored_data["archived_at"] is None
    assert restored_data["archived_by"] is None


def test_archive_requires_operator_scope(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Scope Guarded")
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/archive", headers={"X-CALIBER-User": "@viewer-only"}
    )
    assert resp.status_code == 403


def test_project_membership_roles_and_permissions(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Access controlled project")

    detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    assert detail["access_role"] == "owner"
    assert "project.manage_members" in detail["permissions"]

    added = proj_client.post(
        f"{PREFIX}/projects/{pid}/members",
        json={"user_id": "@reader", "role": "viewer"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["data"]["role"] == "viewer"

    reader_headers = {"X-CALIBER-User": "@reader"}
    reader_detail = proj_client.get(f"{PREFIX}/projects/{pid}", headers=reader_headers)
    assert reader_detail.status_code == 200
    assert reader_detail.json()["data"]["access_role"] == "viewer"
    assert (
        proj_client.patch(
            f"{PREFIX}/projects/{pid}",
            json={"name": "should not change"},
            headers=reader_headers,
        ).status_code
        == 403
    )

    members = proj_client.get(f"{PREFIX}/projects/{pid}/members", headers=reader_headers)
    assert members.status_code == 200
    assert {row["user_id"] for row in members.json()["data"]["members"]} == {"@test", "@reader"}

    removed = proj_client.delete(f"{PREFIX}/projects/{pid}/members/@reader")
    assert removed.status_code == 200
    assert proj_client.get(f"{PREFIX}/projects/{pid}", headers=reader_headers).status_code == 404


def test_project_role_alone_is_not_enough_without_the_operator_scope(
    proj_client: TestClient, db_session: Session
) -> None:
    """GitHub Copilot review (round 2, PR #294): `add_project_member` /
    `update_project_member` / `remove_project_member` / `update_project` all
    gained a `require_scopes(request, [SCOPE_OPERATOR])` call, closing a gap
    where a real project-role holder with only the universal
    `caliber.viewer` scope could still perform an action section 2.4 says
    needs `caliber.operator`. That fix had route-level test coverage only
    through the (admin-scoped, and therefore operator-implying) default
    `@test` identity, which cannot regress-test the scope check at all. This
    proves the negative case directly with real, non-admin project members.

    `add_project_member` now allows granting `role="owner"` (`P1-C`), but
    only to a target whose live platform scopes already include both
    `caliber.operator` and `caliber.approver` -- `@viewer-owner` here has
    neither, so it would be rejected (409) at the API layer too. The
    owner-role member used below is inserted directly via `db_session`,
    bypassing that eligibility check, specifically to exercise
    `update_project_member`/`remove_project_member`'s *scope-ceiling* gap
    (this test's actual subject) against a real non-primary-owner Admin,
    without also having to construct an eligible one.
    """
    pid = _create(proj_client, "Scope ceiling check")

    db_session.add_all(
        [
            CaliberProjectMember(
                member_id="M-viewer-owner",
                project_id=pid,
                user_id="@viewer-owner",
                role=ROLE_OWNER,
                created_by="@test",
            ),
            CaliberProjectMember(
                member_id="M-viewer-editor",
                project_id=pid,
                user_id="@viewer-editor",
                role=ROLE_EDITOR,
                created_by="@test",
            ),
        ]
    )
    db_session.commit()

    # Neither "@viewer-owner" nor "@viewer-editor" is in any
    # operator/approver/admin allow-list, so `resolve_identity` grants them
    # only `caliber.viewer` (the universal default) -- real project-role
    # permission, no global-scope ceiling.
    viewer_owner_headers = {"X-CALIBER-User": "@viewer-owner"}
    viewer_editor_headers = {"X-CALIBER-User": "@viewer-editor"}

    add_resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/members",
        json={"user_id": "@newcomer", "role": "viewer"},
        headers=viewer_owner_headers,
    )
    assert add_resp.status_code == 403, add_resp.text

    update_resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@test",
        json={"role": "viewer"},
        headers=viewer_owner_headers,
    )
    assert update_resp.status_code == 403, update_resp.text

    remove_resp = proj_client.delete(
        f"{PREFIX}/projects/{pid}/members/@test",
        headers=viewer_owner_headers,
    )
    assert remove_resp.status_code == 403, remove_resp.text

    rename_resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}",
        json={"name": "should not change"},
        headers=viewer_editor_headers,
    )
    assert rename_resp.status_code == 403, rename_resp.text

    # The project itself, and the real membership rows, are untouched.
    detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    assert detail["name"] == "Scope ceiling check"


def _make_eligible(client: TestClient, *user_ids: str) -> None:
    """Grant every ``user_id`` both `caliber.operator` and `caliber.approver`
    -- the scopes `is_eligible_for_owner_role` requires -- by adding them to
    both allow-lists, matching `test_create_requires_both_operator_and_approver`'s
    own config-mutation pattern."""
    csv = ",".join(user_ids)
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": csv, "approver_users": csv}
    )


def test_add_member_as_owner_requires_scope_eligibility(proj_client: TestClient) -> None:
    """`P1-C`: multiple `owner`-role (Admin) memberships are now allowed,
    but only for a target whose live scopes already include both
    `caliber.operator` and `caliber.approver` (section 2.4/19.1 item 4)."""
    pid = _create(proj_client, "Multi Admin")

    ineligible = proj_client.post(
        f"{PREFIX}/projects/{pid}/members",
        json={"user_id": "@ineligible", "role": "owner"},
    )
    assert ineligible.status_code == 409, ineligible.text

    _make_eligible(proj_client, "@second-admin")
    eligible = proj_client.post(
        f"{PREFIX}/projects/{pid}/members",
        json={"user_id": "@second-admin", "role": "owner"},
    )
    assert eligible.status_code == 201, eligible.text
    assert eligible.json()["data"]["role"] == "owner"

    # The primary owner (`CaliberProject.owner`) is unchanged by adding a
    # second Admin -- adding another Admin is ordinary member administration.
    detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    assert detail["owner"] == "@test"


def test_promote_existing_member_to_owner_requires_eligibility(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Promotion")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@future-admin", "role": "editor"}
    )

    still_ineligible = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@future-admin", json={"role": "owner"}
    )
    assert still_ineligible.status_code == 409, still_ineligible.text

    _make_eligible(proj_client, "@future-admin")
    promoted = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@future-admin", json={"role": "owner"}
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["data"]["role"] == "owner"


def test_reactivating_a_lapsed_owner_role_member_rechecks_eligibility(
    proj_client: TestClient,
) -> None:
    """Review finding on the first version of this PR: `update_project_member`'s
    eligibility check only fired when the request body explicitly set
    `role: "owner"` -- a status-only reactivation (`{"status": "active"}`,
    no `role` field) of a member whose *stored* role was already `owner`
    skipped the check entirely, since `payload.role` was `None`. That let a
    secondary Admin, deactivated while eligible and since lapsed, be
    silently reactivated with full Admin permissions and zero
    re-verification -- contradicting `is_eligible_for_owner_role`'s own
    "checked against live grants, not a snapshot" contract. This proves the
    fix: reactivation now re-checks eligibility even with no `role` field
    in the request.
    """
    pid = _create(proj_client, "Reactivation Check")
    _make_eligible(proj_client, "@second-admin")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@second-admin", "role": "owner"}
    )
    deactivate = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@second-admin", json={"status": "inactive"}
    )
    assert deactivate.status_code == 200, deactivate.text

    # The member's platform scopes lapse while deactivated.
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "", "approver_users": ""}
    )

    # A status-only reactivation (no `role` field) must re-check eligibility
    # against the member's *stored* role ("owner") and reject it.
    reactivate = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@second-admin", json={"status": "active"}
    )
    assert reactivate.status_code == 409, reactivate.text

    # The member is still inactive -- the rejected request changed nothing.
    members = proj_client.get(f"{PREFIX}/projects/{pid}/members").json()["data"]["members"]
    assert not any(m["user_id"] == "@second-admin" for m in members)

    # Once eligible again, the same status-only reactivation succeeds.
    _make_eligible(proj_client, "@second-admin")
    reactivate_ok = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@second-admin", json={"status": "active"}
    )
    assert reactivate_ok.status_code == 200, reactivate_ok.text
    assert reactivate_ok.json()["data"]["role"] == "owner"


def test_member_deactivation_provenance_is_set_and_cleared(proj_client: TestClient) -> None:
    """`P1-C`: `deactivated_at`/`deactivated_by` (migration `0093`, unwired
    until `P1-C`) are set when a member is removed/deactivated and cleared
    on reactivation -- exercised through both reactivation paths
    (`add_project_member`'s re-add branch and `update_project_member`'s
    status flip)."""
    pid = _create(proj_client, "Deactivation Provenance")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@member-1", "role": "editor"}
    )
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@member-2", "role": "editor"}
    )

    removed = proj_client.delete(f"{PREFIX}/projects/{pid}/members/@member-1")
    assert removed.status_code == 200, removed.text

    deactivated = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@member-2", json={"status": "inactive"}
    )
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["data"]["deactivated_at"] is not None
    assert deactivated.json()["data"]["deactivated_by"] == "@test"

    # Reactivating via `add_project_member` clears the provenance.
    readded = proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@member-1", "role": "editor"}
    )
    assert readded.status_code == 201, readded.text
    assert readded.json()["data"]["deactivated_at"] is None
    assert readded.json()["data"]["deactivated_by"] is None

    # Reactivating via `update_project_member` clears the provenance too.
    reactivated = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@member-2", json={"status": "active"}
    )
    assert reactivated.status_code == 200, reactivated.text
    assert reactivated.json()["data"]["deactivated_at"] is None
    assert reactivated.json()["data"]["deactivated_by"] is None


def test_primary_owner_row_protected_but_secondary_admin_is_not(proj_client: TestClient) -> None:
    """`P1-C`: the primary-owner invariant now lives on `CaliberProject.owner`,
    not on holding the `owner` role -- a secondary Admin's membership row can
    be demoted/removed through the ordinary member routes; the primary
    owner's own row cannot (that's `transfer-ownership`'s job)."""
    pid = _create(proj_client, "Ownership Guarded")
    _make_eligible(proj_client, "@second-admin")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@second-admin", "role": "owner"}
    )

    # The primary owner ("@test") cannot be demoted or removed here.
    demote_owner = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@test", json={"role": "viewer"}
    )
    assert demote_owner.status_code == 409, demote_owner.text
    remove_owner = proj_client.delete(f"{PREFIX}/projects/{pid}/members/@test")
    assert remove_owner.status_code == 409, remove_owner.text

    # The secondary Admin can be demoted...
    demote_secondary = proj_client.patch(
        f"{PREFIX}/projects/{pid}/members/@second-admin", json={"role": "editor"}
    )
    assert demote_secondary.status_code == 200, demote_secondary.text
    assert demote_secondary.json()["data"]["role"] == "editor"

    # ...and removed.
    _make_eligible(proj_client, "@second-admin", "@third-admin")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@third-admin", "role": "owner"}
    )
    remove_secondary = proj_client.delete(f"{PREFIX}/projects/{pid}/members/@third-admin")
    assert remove_secondary.status_code == 200, remove_secondary.text


def test_transfer_ownership_moves_the_primary_owner_pointer(
    proj_client: TestClient, db_session: Session
) -> None:
    pid = _create(proj_client, "Transferable")
    _make_eligible(proj_client, "@new-owner")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@new-owner", "role": "owner"}
    )

    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": "@new-owner"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["owner"] == "@new-owner"

    project = db_session.execute(
        select(CaliberProject).where(CaliberProject.project_id == pid)
    ).scalar_one()
    assert project.owner == "@new-owner"
    # Both Admin memberships (old and new primary owner) still exist --
    # transfer moves the pointer, it does not touch membership rows.
    roles = {
        (m.user_id, m.role)
        for m in db_session.execute(
            select(CaliberProjectMember).where(CaliberProjectMember.project_id == pid)
        )
        .scalars()
        .all()
    }
    assert roles == {("@test", ROLE_OWNER), ("@new-owner", ROLE_OWNER)}


def test_transfer_ownership_only_current_primary_owner_may_initiate(
    proj_client: TestClient,
) -> None:
    """Exit criterion: "secondary Admin does not change primary owner" --
    an `owner`-role member who is not `CaliberProject.owner` may not
    transfer ownership, even to themselves."""
    pid = _create(proj_client, "Guarded Transfer")
    _make_eligible(proj_client, "@second-admin")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@second-admin", "role": "owner"}
    )

    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": "@second-admin"},
        headers={"X-CALIBER-User": "@second-admin"},
    )
    assert resp.status_code == 403, resp.text

    project_owner = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]["owner"]
    assert project_owner == "@test"


def test_transfer_ownership_target_must_be_an_eligible_active_admin(
    proj_client: TestClient,
) -> None:
    pid = _create(proj_client, "Target Checks")

    not_a_member = proj_client.post(
        f"{PREFIX}/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": "@nobody"},
    )
    assert not_a_member.status_code == 409, not_a_member.text

    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@just-editor", "role": "editor"}
    )
    wrong_role = proj_client.post(
        f"{PREFIX}/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": "@just-editor"},
    )
    assert wrong_role.status_code == 409, wrong_role.text

    # An Admin whose scopes are later revoked stays scope-ineligible for
    # transfer, even though the stored `owner` role is untouched -- the
    # eligibility check reads *live* scopes, not a grant-time snapshot.
    _make_eligible(proj_client, "@lapsed-admin")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@lapsed-admin", "role": "owner"}
    )
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "", "approver_users": ""}
    )
    lapsed = proj_client.post(
        f"{PREFIX}/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": "@lapsed-admin"},
    )
    assert lapsed.status_code == 409, lapsed.text


def test_transfer_ownership_write_is_conditional_not_just_the_earlier_read(
    proj_client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub Copilot review of this PR's first version: the route's earlier
    `project.owner != identity.user_id` check reads a value that can go
    stale by the time the actual write happens -- two concurrent transfer
    requests from the same primary owner both pass that check before either
    commits, and whichever commits second would silently overwrite the
    first (a lost update, plus a misleading audit record for a transfer
    that never took effect). The fix makes the *write* itself conditional
    on `owner` still matching at write time (a real SQL compare-and-set),
    not just the earlier read.

    This proves it deterministically, without real threads: monkeypatches
    `_require_owner_role_eligible` (called *between* the route's ownership
    check and its own write) to commit a competing owner change through a
    second, independent session first -- simulating another request's
    transfer landing in that exact window -- then asserts this request's
    write is rejected with `409` rather than clobbering it.
    """
    import caliber.routes.projects as projects_module

    pid = _create(proj_client, "Race Guarded")
    _make_eligible(proj_client, "@second-admin", "@interloper")
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@second-admin", "role": "owner"}
    )
    proj_client.post(
        f"{PREFIX}/projects/{pid}/members", json={"user_id": "@interloper", "role": "owner"}
    )

    original_check = projects_module._require_owner_role_eligible

    def racing_eligibility_check(request: object, user_id: str) -> None:
        # A competing transfer commits here, between this request's earlier
        # `project.owner == identity.user_id` read and its own write below.
        db_session.execute(
            sa_update(CaliberProject)
            .where(CaliberProject.project_id == pid)
            .values(owner="@interloper")
        )
        db_session.commit()
        return original_check(request, user_id)  # type: ignore[arg-type]

    monkeypatch.setattr(projects_module, "_require_owner_role_eligible", racing_eligibility_check)

    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/transfer-ownership",
        json={"new_owner_user_id": "@second-admin"},
    )
    assert resp.status_code == 409, resp.text

    # The competing (simulated) transfer's write stands, unclobbered.
    project = db_session.execute(
        select(CaliberProject).where(CaliberProject.project_id == pid)
    ).scalar_one()
    assert project.owner == "@interloper"


def test_upload_list_download_project_file(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": ("policy.md", b"# Refund policy\n", "text/markdown")},
        data={"kind": "input", "path": "policies/refund/policy.md"},
    )
    assert resp.status_code == 201, resp.text
    rec = resp.json()["data"]
    assert rec["file_ref"] == f"caliber://projects/{pid}/input/policies/refund/policy.md"
    assert rec["relative_path"] == "policies/refund/policy.md"
    assert rec["storage_backend"] == "local"
    assert rec["project_id"] == pid
    file_id = rec["file_id"]

    items = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["items"]
    assert [i["file_id"] for i in items] == [file_id]
    directories = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["directories"]
    assert [d["path"] for d in directories] == ["policies", "policies/refund"]

    # project file_count reflects the upload
    assert proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]["file_count"] == 1

    content = proj_client.get(f"{PREFIX}/projects/{pid}/files/{file_id}/content")
    assert content.status_code == 200
    assert content.content == b"# Refund policy\n"
    assert content.headers["x-content-type-options"] == "nosniff"


def test_create_project_folder_is_idempotent_and_hidden_from_files(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    resp = proj_client.post(f"{PREFIX}/projects/{pid}/folders", json={"path": "datasets/raw"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["path"] == "datasets/raw"
    assert resp.json()["data"]["file_ref"] == (
        f"caliber://projects/{pid}/metadata/datasets/raw/.caliber-folder"
    )
    again = proj_client.post(f"{PREFIX}/projects/{pid}/folders", json={"path": "datasets/raw"})
    assert again.status_code == 201

    payload = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]
    assert payload["items"] == []
    assert [d["path"] for d in payload["directories"]] == ["datasets", "datasets/raw"]
    assert proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]["file_count"] == 0


def test_minio_backed_project_folder_and_file(proj_client: TestClient, tmp_path: Path) -> None:
    moto = pytest.importorskip("moto")
    boto3 = pytest.importorskip("boto3")
    bucket = "caliber-test"
    cfg = WorkflowStorageConfig(
        backend="local",
        base_uri=f"file://{tmp_path}/ws",
        bucket=bucket,
        region="us-east-1",
    )

    with moto.mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        proj_client.app.state.config = proj_client.app.state.config.model_copy(
            update={"workflow_storage": cfg}
        )
        proj_client.app.state.working_dir_service = WorkingDirectoryService(
            LocalStorageBackend(cfg.base_uri), cfg
        )

        project_resp = proj_client.post(
            f"{PREFIX}/projects",
            json={"name": "Blob Directory", "storage_backend": "minio"},
        )
        assert project_resp.status_code == 201, project_resp.text
        pid = project_resp.json()["data"]["project_id"]
        assert project_resp.json()["data"]["storage_backend"] == "s3"

        folder_resp = proj_client.post(
            f"{PREFIX}/projects/{pid}/folders", json={"path": "datasets/raw"}
        )
        assert folder_resp.status_code == 201, folder_resp.text
        assert folder_resp.json()["data"]["storage_backend"] == "s3"

        file_resp = proj_client.post(
            f"{PREFIX}/projects/{pid}/files",
            files={"file": ("cases.csv", b"id,text\n1,hello\n", "text/csv")},
            data={"kind": "input", "path": "datasets/raw/cases.csv"},
        )
        assert file_resp.status_code == 201, file_resp.text
        file_id = file_resp.json()["data"]["file_id"]
        assert file_resp.json()["data"]["storage_backend"] == "s3"
        assert proj_client.get(f"{PREFIX}/projects/{pid}/files/{file_id}/content").content == (
            b"id,text\n1,hello\n"
        )


def test_project_file_isolation(proj_client: TestClient) -> None:
    pid_a = _create(proj_client, "A")
    pid_b = _create(proj_client, "B")
    file_id = proj_client.post(
        f"{PREFIX}/projects/{pid_a}/files",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"kind": "input"},
    ).json()["data"]["file_id"]
    # B cannot see/download A's file
    assert proj_client.get(f"{PREFIX}/projects/{pid_b}/files/{file_id}/content").status_code == 404
    assert proj_client.get(f"{PREFIX}/projects/{pid_b}/files").json()["data"]["items"] == []


def test_delete_project_file_soft(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    file_id = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": ("d.txt", b"x", "text/plain")},
        data={"kind": "input"},
    ).json()["data"]["file_id"]
    assert proj_client.delete(f"{PREFIX}/projects/{pid}/files/{file_id}").status_code == 200
    # gone from the list
    assert proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["items"] == []


def _file_input_manifest(workflow_id: str, pinned: ManagedFileReference) -> dict:
    """A minimal manifest whose one node pins ``pinned`` (route-level test only;
    not run through the compiler/validator)."""
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "name": "File input workflow",
        "nodes": {
            "file_in": {
                "id": "file_in",
                "type": "file_input",
                "file_ref": pinned.model_dump(mode="json"),
            },
        },
        "edges": [],
        "tools": {},
    }


def _deploy_with_pinned_file(
    db_session: Session,
    *,
    workflow_id: str,
    project_id: str,
    pinned: ManagedFileReference,
    status: str = "active",
) -> None:
    db_session.add(
        CaliberWorkflow(
            workflow_id=workflow_id,
            name=workflow_id,
            owner="@test",
            project_id=project_id,
        )
    )
    db_session.add(
        CaliberWorkflowVersion(
            version_id=f"{workflow_id}-v1",
            workflow_id=workflow_id,
            version_number=1,
            status="published",
            manifest=_file_input_manifest(workflow_id, pinned),
            manifest_hash=f"hash-{workflow_id}",
        )
    )
    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id=f"{workflow_id}-dep",
            workflow_id=workflow_id,
            alias="prod",
            version_id=f"{workflow_id}-v1",
            status=status,
            deployed_by="@test",
            deployed_at=datetime.now(timezone.utc),
            rollback_checkpoint=[],
        )
    )
    db_session.commit()


def _upload_project_file(proj_client: TestClient, pid: str, name: str = "pinned.txt") -> dict:
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": (name, b"pinned-bytes", "text/plain")},
        data={"kind": "input"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def test_delete_project_file_blocked_by_active_deployment(
    proj_client: TestClient, db_session: Session
) -> None:
    """`P2-C`'s remaining "reconstructability" gap: deleting a project file that
    an active deployment's ``file_input`` node pins must be refused, not silently
    orphan the deployment's next preflight/run."""
    pid = _create(proj_client)
    uploaded = _upload_project_file(proj_client, pid)
    pinned = ManagedFileReference(
        file_id=uploaded["file_id"],
        file_ref=uploaded["file_ref"],
        sha256=uploaded["sha256"],
        name=uploaded["name"],
        size_bytes=uploaded["size_bytes"],
    )
    _deploy_with_pinned_file(db_session, workflow_id="WF-PIN-BLOCKS", project_id=pid, pinned=pinned)

    resp = proj_client.delete(f"{PREFIX}/projects/{pid}/files/{uploaded['file_id']}")
    assert resp.status_code == 409, resp.text
    assert "WF-PIN-BLOCKS" in resp.json()["detail"]

    # Never soft-deleted: it is still visible and downloadable.
    listing = proj_client.get(f"{PREFIX}/projects/{pid}/files").json()["data"]["items"]
    assert any(item["file_id"] == uploaded["file_id"] for item in listing)


def test_delete_project_file_allowed_when_deployment_inactive(
    proj_client: TestClient, db_session: Session
) -> None:
    """A rolled-back/superseded deployment's old pin no longer blocks deletion --
    only *active* deployments protect a file, matching the tool/MCP-server
    precedent's own ``status == "active"`` scope."""
    pid = _create(proj_client)
    uploaded = _upload_project_file(proj_client, pid)
    pinned = ManagedFileReference(
        file_id=uploaded["file_id"],
        file_ref=uploaded["file_ref"],
        sha256=uploaded["sha256"],
        name=uploaded["name"],
        size_bytes=uploaded["size_bytes"],
    )
    _deploy_with_pinned_file(
        db_session,
        workflow_id="WF-PIN-INACTIVE",
        project_id=pid,
        pinned=pinned,
        status="rolled_back",
    )

    resp = proj_client.delete(f"{PREFIX}/projects/{pid}/files/{uploaded['file_id']}")
    assert resp.status_code == 200, resp.text


def test_delete_project_file_allowed_for_unpinned_file(
    proj_client: TestClient, db_session: Session
) -> None:
    """An active deployment pinning a *different* file_id never blocks deleting
    this one -- the check must match on file_id, not merely "some deployment
    exists"."""
    pid = _create(proj_client)
    uploaded = _upload_project_file(proj_client, pid, name="unrelated.txt")
    other = _upload_project_file(proj_client, pid, name="pinned-elsewhere.txt")
    pinned = ManagedFileReference(
        file_id=other["file_id"],
        file_ref=other["file_ref"],
        sha256=other["sha256"],
        name=other["name"],
        size_bytes=other["size_bytes"],
    )
    _deploy_with_pinned_file(db_session, workflow_id="WF-PIN-OTHER", project_id=pid, pinned=pinned)

    resp = proj_client.delete(f"{PREFIX}/projects/{pid}/files/{uploaded['file_id']}")
    assert resp.status_code == 200, resp.text


def test_upload_requires_operator(proj_client: TestClient) -> None:
    pid = _create(proj_client)
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/files",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"kind": "input"},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_missing_project_404(proj_client: TestClient) -> None:
    assert proj_client.get(f"{PREFIX}/projects/PRJ-nope").status_code == 404
    assert proj_client.get(f"{PREFIX}/projects/PRJ-nope/files").status_code == 404


# ---------------------------------------------------------------------------
# Workspace environments (`P1-F`, Phase 1 items 10/11): explicit enable/
# disable lifecycle, plus effective-capability projection on the response.
# ---------------------------------------------------------------------------


def test_list_project_environments_returns_all_four_in_promotion_order(
    proj_client: TestClient,
) -> None:
    pid = _create(proj_client, "Environments project")
    resp = proj_client.get(f"{PREFIX}/projects/{pid}/environments")
    assert resp.status_code == 200, resp.text
    environments = resp.json()["data"]["environments"]
    assert [(e["name"], e["environment_class"], e["status"]) for e in environments] == [
        ("dev", "development", "active"),
        ("qa", "qa", "disabled"),
        ("staging", "staging", "disabled"),
        ("prod", "production", "disabled"),
    ]
    # Capability projection (item 11): the same `access_role`/`permissions`
    # `GET /projects/{id}` itself returns for this identity, since an
    # environment carries no separate per-environment role in this MVP.
    project_detail = proj_client.get(f"{PREFIX}/projects/{pid}").json()["data"]
    for env in environments:
        assert env["access_role"] == project_detail["access_role"] == "owner"
        assert env["permissions"] == project_detail["permissions"]
        assert "environment.manage" in env["permissions"]


def test_get_project_environment_detail(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Env detail project")
    resp = proj_client.get(f"{PREFIX}/projects/{pid}/environments/qa")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "qa"
    assert data["environment_class"] == "qa"
    assert data["promotion_order"] == 20
    assert data["status"] == "disabled"
    assert data["project_id"] == pid


def test_get_project_environment_404_for_unknown_name(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Env 404 project")
    assert proj_client.get(f"{PREFIX}/projects/{pid}/environments/nope").status_code == 404


def test_enable_and_disable_project_environment(
    proj_client: TestClient, db_session: Session
) -> None:
    pid = _create(proj_client, "Env lifecycle project")

    enabled = proj_client.post(f"{PREFIX}/projects/{pid}/environments/qa/enable")
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["data"]["status"] == "active"

    disabled = proj_client.post(f"{PREFIX}/projects/{pid}/environments/qa/disable")
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["data"]["status"] == "disabled"

    from caliber.db.models import CaliberAuditLog

    actions = {
        row.action
        for row in db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.entity_type == "workspace_environment")
        .all()
    }
    assert actions == {"enable_workspace_environment", "disable_workspace_environment"}


def test_enabling_an_already_active_environment_conflicts(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Already active project")
    # dev starts active by default (P1-A's seeding).
    resp = proj_client.post(f"{PREFIX}/projects/{pid}/environments/dev/enable")
    assert resp.status_code == 409


def test_disabling_an_already_disabled_environment_conflicts(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Already disabled project")
    # qa starts disabled by default (P1-A's seeding).
    resp = proj_client.post(f"{PREFIX}/projects/{pid}/environments/qa/disable")
    assert resp.status_code == 409


def test_environment_enable_requires_operator_scope(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Scope guarded env project")
    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/environments/qa/enable",
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_environment_enable_requires_owner_role_not_just_operator_scope(
    proj_client: TestClient,
) -> None:
    """`environment.manage` is Admin-only (section 2.4) -- an Editor with
    real operator scope is still denied, proving this is a genuine
    project-role gate and not just the global-scope check above."""
    pid = _create(proj_client, "Editor guarded env project")
    added = proj_client.post(
        f"{PREFIX}/projects/{pid}/members",
        json={"user_id": "@editor-user", "role": "editor"},
    )
    assert added.status_code == 201, added.text
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "@editor-user"}
    )

    resp = proj_client.post(
        f"{PREFIX}/projects/{pid}/environments/qa/enable",
        headers={"X-CALIBER-User": "@editor-user"},
    )
    assert resp.status_code == 403


def test_environment_put_delete_still_have_no_route(proj_client: TestClient) -> None:
    """PUT/DELETE remain absent for an environment -- there is still no way
    to replace or remove one. PATCH now exists (policy updates, below), but
    that does not reopen "identity fields cannot be edited": see
    ``test_patch_environment_rejects_identity_fields``."""
    pid = _create(proj_client, "No put delete route project")
    for method in ("put", "delete"):
        resp = getattr(proj_client, method)(f"{PREFIX}/projects/{pid}/environments/qa")
        assert resp.status_code == 405, (method, resp.text)


def test_patch_environment_rejects_identity_fields(proj_client: TestClient) -> None:
    """Section 12.2's acceptance criterion -- "environment identity fields
    cannot be edited or deleted" -- still holds even though PATCH now
    exists: ``WorkspaceEnvironmentUpdateRequest`` forbids extra fields, so
    ``name``/``environment_class``/``promotion_order`` (never declared on
    it) are rejected outright rather than silently ignored or accepted."""
    pid = _create(proj_client, "Identity immutable project")
    resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}/environments/qa",
        json={
            "name": "renamed",
            "policy": {},
            "policy_sha256": "a" * 64,
            "expected_lock_version": 1,
        },
    )
    assert resp.status_code == 400, resp.text


def test_update_project_environment_policy(proj_client: TestClient, db_session: Session) -> None:
    pid = _create(proj_client, "Env policy project")
    resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}/environments/qa",
        json={
            "policy": {"max_concurrent_releases": 1},
            "policy_sha256": "a" * 64,
            "expected_lock_version": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["policy_sha256"] == "a" * 64
    assert data["lock_version"] == 2

    # A second call, correctly quoting the *new* lock_version, succeeds again --
    # proving the CAS field is genuinely read from the freshly written row, not
    # a stale in-memory copy.
    second = proj_client.patch(
        f"{PREFIX}/projects/{pid}/environments/qa",
        json={"policy": {}, "policy_sha256": "b" * 64, "expected_lock_version": 2},
    )
    assert second.status_code == 200, second.text
    assert second.json()["data"]["lock_version"] == 3

    from caliber.db.models import CaliberAuditLog

    action = (
        db_session.query(CaliberAuditLog)
        .filter(CaliberAuditLog.entity_type == "workspace_environment")
        .filter(CaliberAuditLog.action == "update_workspace_environment_policy")
        .first()
    )
    assert action is not None


def test_update_project_environment_stale_lock_version_conflicts(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Env stale lock project")
    resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}/environments/qa",
        json={"policy": {}, "policy_sha256": "c" * 64, "expected_lock_version": 99},
    )
    assert resp.status_code == 409, resp.text


def test_update_project_environment_requires_operator_scope(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Env update scope project")
    resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}/environments/qa",
        json={"policy": {}, "policy_sha256": "d" * 64, "expected_lock_version": 1},
        headers={"X-CALIBER-User": "@viewer-only"},
    )
    assert resp.status_code == 403


def test_update_project_environment_404_for_unknown_name(proj_client: TestClient) -> None:
    pid = _create(proj_client, "Env update 404 project")
    resp = proj_client.patch(
        f"{PREFIX}/projects/{pid}/environments/nope",
        json={"policy": {}, "policy_sha256": "e" * 64, "expected_lock_version": 1},
    )
    assert resp.status_code == 404


def test_environments_are_hidden_for_a_project_the_caller_cannot_see(
    proj_client: TestClient,
) -> None:
    pid = _create(proj_client, "Hidden env project")
    resp = proj_client.get(
        f"{PREFIX}/projects/{pid}/environments", headers={"X-CALIBER-User": "@stranger"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Owner-eligibility re-checked at use time, not just at grant time (`P1-F`,
# Phase 1 item 6's residual "conjunction-safe global-scope checks" scope).
# ---------------------------------------------------------------------------


def test_a_primary_owner_who_loses_a_required_scope_is_narrowed_not_locked_out(
    proj_client: TestClient,
) -> None:
    """`is_eligible_for_owner_role` gates *granting* the `owner` role
    (`P1-C`); this proves the same `{operator, approver}` conjunction is
    also re-checked on every request against the caller's own *live*
    scopes. `@lapsing-owner` creates the project with both scopes (the
    project's real primary owner, via `CaliberProject.owner`, not merely
    an `owner`-role membership row) -- then loses `approver` at the
    platform-identity level, same as any other config change an operator
    might make for an unrelated reason."""
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"operator_users": "@lapsing-owner", "approver_users": "@lapsing-owner"}
    )
    created = proj_client.post(
        f"{PREFIX}/projects",
        json={"name": "Lapsing owner project"},
        headers={"X-CALIBER-User": "@lapsing-owner"},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["data"]["project_id"]

    # Still fully eligible right after creation.
    detail = proj_client.get(
        f"{PREFIX}/projects/{pid}", headers={"X-CALIBER-User": "@lapsing-owner"}
    ).json()["data"]
    assert detail["access_role"] == "owner"
    assert "project.manage_members" in detail["permissions"]

    # Loses `approver` -- one scope short of eligibility now.
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"approver_users": ""}
    )

    narrowed = proj_client.get(
        f"{PREFIX}/projects/{pid}", headers={"X-CALIBER-User": "@lapsing-owner"}
    ).json()["data"]
    assert narrowed["access_role"] == "editor"
    assert "project.manage_members" not in narrowed["permissions"]

    # Admin-only actions are refused...
    archive = proj_client.post(
        f"{PREFIX}/projects/{pid}/archive", headers={"X-CALIBER-User": "@lapsing-owner"}
    )
    assert archive.status_code == 403

    # ...but ordinary content access is not: this is a narrowing, not the
    # bare admin-bypass-removal lockout `P1-B` already established.
    renamed = proj_client.patch(
        f"{PREFIX}/projects/{pid}",
        json={"name": "still editable"},
        headers={"X-CALIBER-User": "@lapsing-owner"},
    )
    assert renamed.status_code == 200, renamed.text

    # Restoring the scope reinstates the real owner role.
    proj_client.app.state.config = proj_client.app.state.config.model_copy(
        update={"approver_users": "@lapsing-owner"}
    )
    reinstated = proj_client.get(
        f"{PREFIX}/projects/{pid}", headers={"X-CALIBER-User": "@lapsing-owner"}
    ).json()["data"]
    assert reinstated["access_role"] == "owner"
