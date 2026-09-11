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

from caliber.auth import CaliberIdentity
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

#: `P1-B`: a hand-bumped marker for `AccessDecision.policy_version` (section
#: 5.4). Bump this string whenever this module's decision policy changes in
#: a way an auditor reading old decisions would need to know about (e.g. the
#: admin-owner-bypass removal this same PR makes) -- not on every unrelated
#: edit to this file.
POLICY_VERSION: Final[str] = "p1b-2026-09"

#: `AccessDecision.reason` values, named rather than left as bare literals
#: sprinkled through `decide_project_access` -- matching this module's own
#: `ROLE_*`/`PROJECT_ROLES` idiom (a closed vocabulary, not a Python `Enum`;
#: no precedent for that pattern anywhere in this codebase). Literal values
#: are unchanged from before this PR -- confirmed nothing outside this
#: module and its own tests pattern-matches them, so naming them is
#: additive, not a behavior change.
REASON_GRANTED: Final[str] = "granted"
REASON_PROJECT_NOT_FOUND: Final[str] = "project_not_found"
REASON_NO_MEMBERSHIP: Final[str] = "project_access_denied"
REASON_PERMISSION_DENIED: Final[str] = "permission_denied"
ACCESS_REASONS: Final[frozenset[str]] = frozenset(
    {REASON_GRANTED, REASON_PROJECT_NOT_FOUND, REASON_NO_MEMBERSHIP, REASON_PERMISSION_DENIED}
)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    role: str | None
    reason: str
    permissions: frozenset[str]
    #: Section 5.4's fifth `AccessDecision` field. Trails with a default so
    #: every existing 4-positional-arg construction below keeps working.
    policy_version: str = POLICY_VERSION


def permissions_for_role(role: str | None) -> frozenset[str]:
    if role is None:
        return frozenset()
    return frozenset(action for action, roles in PROJECT_ACTIONS.items() if role in roles)


def project_role(
    session: Session, identity: CaliberIdentity, project: CaliberProject
) -> str | None:
    """Resolve the caller's project role.

    `caliber.admin` is deliberately **not** an implicit workspace role
    (section 5.4, `P1-B`) -- ordinary platform maintenance cannot read or
    mutate workspace content through this path. A future audited-recovery
    path is `P1-C`'s job (a metadata-only platform Admin inventory); the
    real interactive break-glass mechanism is Phase 5's (`P5-B`). Neither
    exists yet -- an admin with no real membership is denied here, with no
    replacement access path, by this ticket's own explicit design.
    """
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
        return AccessDecision(False, None, REASON_PROJECT_NOT_FOUND, frozenset())
    role = project_role(session, identity, project)
    permissions = permissions_for_role(role)
    if role is not None and action in permissions:
        return AccessDecision(True, role, REASON_GRANTED, permissions)
    if role is None:
        return AccessDecision(False, None, REASON_NO_MEMBERSHIP, permissions)
    return AccessDecision(False, role, REASON_PERMISSION_DENIED, permissions)


def authorize(
    session: Session,
    principal: CaliberIdentity,
    action: str,
    workspace_id: str | None,
    *,
    resource: object | None = None,
    environment: object | None = None,
    release: object | None = None,
) -> AccessDecision:
    """The central authorization decision, per `docs/workspace-plan.md`
    section 5.4.

    Implements section 2.4's effective-decision AND-chain:

        authenticated principal
        AND credential/global-scope ceiling
        AND credential workspace ceiling, when present
        AND active workspace membership/role
        AND resource belongs to or is pinned by workspace
        AND environment policy
        AND release-instance rules

    The first three conjuncts are already satisfied by the time a route
    calls this function -- `require_scopes`/`require_all_scopes` and
    `resolve_identity` run first, at the route layer, exactly as they do
    today. This function is responsible for the rest.

    `resource`/`environment`/`release` are typed `object | None` -- no
    `ResourceContext`/`EnvironmentContext`/`ReleaseContext` class exists
    yet (no per-resource pinning model, no environment-scoped route, no
    release/Change-Request model -- Phase 4/5's job). No caller passes
    anything but `None` today. Passing a non-`None` value raises
    `NotImplementedError` naming the unmodeled conjunct -- an honest
    "not built yet" signal, not a silent no-op a future real caller could
    trip over without noticing nothing was actually checked.
    """
    if workspace_id is None:
        # Only `project.create`'s pre-membership bootstrap check has no
        # concrete workspace (section 5.4), and that check is
        # `require_all_scopes`, not this function -- every action reaching
        # here with no workspace id denies.
        return AccessDecision(False, None, REASON_PROJECT_NOT_FOUND, frozenset())
    if resource is not None:
        raise NotImplementedError(
            "authorize(): per-resource authorization context is not modeled yet"
        )
    if environment is not None:
        raise NotImplementedError(
            "authorize(): environment-policy authorization context is not modeled yet"
        )
    if release is not None:
        raise NotImplementedError(
            "authorize(): release-instance authorization context is not modeled yet"
        )
    project = session.get(CaliberProject, workspace_id)
    return decide_project_access(session, principal, project, action)


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

    A compatibility wrapper over `authorize()` (`P1-B`, section 5.4's
    decision service) -- existing callers keep this exact shape/signature;
    `authorize()` is the new canonical decision function underneath.
    """
    project = session.get(CaliberProject, project_id)
    decision = authorize(session, identity, action, project_id)
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
