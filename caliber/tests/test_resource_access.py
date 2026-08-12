"""Central project/resource authorization policy tests."""

from __future__ import annotations

from caliber.auth import SCOPE_ADMIN, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import CaliberProject, CaliberProjectMember
from caliber.resource_access import (
    ROLE_EDITOR,
    ROLE_REVIEWER,
    ROLE_VIEWER,
    decide_project_access,
    permissions_for_role,
)


def _identity(user_id: str, *, admin: bool = False) -> CaliberIdentity:
    scopes = {SCOPE_VIEWER}
    if admin:
        scopes.add(SCOPE_ADMIN)
    return CaliberIdentity(user_id=user_id, scopes=frozenset(scopes), active_project_id="P1")


def test_project_roles_have_expected_action_boundaries(db_session) -> None:
    project = CaliberProject(project_id="P1", name="one", owner="@owner")
    db_session.add(project)
    db_session.add_all(
        [
            CaliberProjectMember(
                member_id="M-editor",
                project_id="P1",
                user_id="@editor",
                role=ROLE_EDITOR,
                created_by="@owner",
            ),
            CaliberProjectMember(
                member_id="M-reviewer",
                project_id="P1",
                user_id="@reviewer",
                role=ROLE_REVIEWER,
                created_by="@owner",
            ),
            CaliberProjectMember(
                member_id="M-viewer",
                project_id="P1",
                user_id="@viewer",
                role=ROLE_VIEWER,
                created_by="@owner",
            ),
        ]
    )
    db_session.commit()

    assert decide_project_access(
        db_session, _identity("@editor"), project, "resource.write"
    ).allowed
    assert decide_project_access(
        db_session, _identity("@editor"), project, "resource.publish"
    ).allowed
    assert not decide_project_access(
        db_session, _identity("@editor"), project, "resource.approve"
    ).allowed
    assert decide_project_access(
        db_session, _identity("@reviewer"), project, "resource.approve"
    ).allowed
    assert not decide_project_access(
        db_session, _identity("@viewer"), project, "resource.write"
    ).allowed


def test_owner_and_admin_have_management_permissions(db_session) -> None:
    project = CaliberProject(project_id="P2", name="two", owner="@owner")
    db_session.add(project)
    db_session.commit()

    assert decide_project_access(
        db_session, _identity("@owner"), project, "project.manage_members"
    ).allowed
    assert decide_project_access(
        db_session, _identity("@admin", admin=True), project, "project.manage_members"
    ).allowed
    assert "project.manage_members" not in permissions_for_role(ROLE_VIEWER)
