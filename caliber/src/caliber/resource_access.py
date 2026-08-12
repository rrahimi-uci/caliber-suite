"""Centralized project/resource authorization for CALIBER.

Authentication answers who the caller is. This module answers what that caller
may do inside a project. It intentionally stays in-process: routes and workers
can share the same decision function without adding a network dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import CaliberProject, CaliberProjectMember

ROLE_OWNER: Final[str] = "owner"
ROLE_EDITOR: Final[str] = "editor"
ROLE_REVIEWER: Final[str] = "reviewer"
ROLE_VIEWER: Final[str] = "viewer"
PROJECT_ROLES: Final[frozenset[str]] = frozenset(
    {ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER}
)

PROJECT_ACTIONS: Final[dict[str, frozenset[str]]] = {
    "read": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER}),
    "project.update": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "project.manage_members": frozenset({ROLE_OWNER}),
    "resource.write": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "resource.publish": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "resource.approve": frozenset({ROLE_OWNER, ROLE_REVIEWER}),
    "resource.execute": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER}),
}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    role: str | None
    reason: str
    permissions: frozenset[str]


def permissions_for_role(role: str | None) -> frozenset[str]:
    if role is None:
        return frozenset()
    return frozenset(action for action, roles in PROJECT_ACTIONS.items() if role in roles)


def project_role(
    session: Session, identity: CaliberIdentity, project: CaliberProject
) -> str | None:
    """Resolve the caller's project role, with admin and owner compatibility."""
    if identity.has_scope(SCOPE_ADMIN):
        return ROLE_OWNER
    if project.owner == identity.user_id:
        return ROLE_OWNER
    member = session.execute(
        select(CaliberProjectMember).where(
            CaliberProjectMember.project_id == project.project_id,
            CaliberProjectMember.user_id == identity.user_id,
            CaliberProjectMember.status == "active",
        )
    ).scalar_one_or_none()
    return member.role if member is not None else None


def decide_project_access(
    session: Session,
    identity: CaliberIdentity,
    project: CaliberProject | None,
    action: str = "read",
) -> AccessDecision:
    if project is None:
        return AccessDecision(False, None, "project_not_found", frozenset())
    role = project_role(session, identity, project)
    permissions = permissions_for_role(role)
    if role is not None and action in permissions:
        return AccessDecision(True, role, "granted", permissions)
    if role is None:
        return AccessDecision(False, None, "project_access_denied", permissions)
    return AccessDecision(False, role, "permission_denied", permissions)


def require_project_access(
    session: Session,
    identity: CaliberIdentity,
    project_id: str,
    action: str = "read",
    *,
    hide_forbidden: bool = True,
) -> tuple[CaliberProject, AccessDecision]:
    """Fetch a project and enforce one action.

    Hidden projects return 404 to avoid existence leaks. A visible project with
    an insufficient role returns 403, which lets the UI explain the missing role.
    """
    project = session.get(CaliberProject, project_id)
    decision = decide_project_access(session, identity, project, action)
    if project is None or (not decision.allowed and decision.role is None and hide_forbidden):
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    if not decision.allowed:
        raise HTTPException(
            status_code=403,
            detail=f"project role {decision.role!r} cannot perform {action}",
        )
    return project, decision


def member_payload(member: CaliberProjectMember) -> dict[str, object]:
    return {
        "member_id": member.member_id,
        "project_id": member.project_id,
        "user_id": member.user_id,
        "role": member.role,
        "status": member.status,
        "created_by": member.created_by,
        "created_at": member.created_at.isoformat() if member.created_at else None,
        "updated_at": member.updated_at.isoformat() if member.updated_at else None,
    }
