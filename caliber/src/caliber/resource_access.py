"""Centralized project/resource authorization for CALIBER.

Authentication answers who the caller is. This module answers what that caller
may do inside a project. It intentionally stays in-process: routes and workers
can share the same decision function without adding a network dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.auth import SCOPE_APPROVER, SCOPE_OPERATOR, CaliberIdentity, scopes_for_user
from caliber.db.models import CaliberProject, CaliberProjectMember

ROLE_OWNER: Final[str] = "owner"
ROLE_EDITOR: Final[str] = "editor"
ROLE_REVIEWER: Final[str] = "reviewer"
ROLE_VIEWER: Final[str] = "viewer"
PROJECT_ROLES: Final[frozenset[str]] = frozenset(
    {ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER}
)

#: `P1-D` (section 16 item 5): the full closed action vocabulary from section
#: 2.4's target table, keyed by the exact literal every route/worker/SDK must
#: use -- not a Python `Enum` (see `ACCESS_REASONS`'s own comment: no
#: precedent for that pattern in this codebase, and `routes/scope_inference.py`'s
#: AST-based inventory only recognizes a bare string literal as an action;
#: an enum member/`.value` access is an `ast.Attribute`, not `ast.Constant`,
#: and would silently vanish from that inventory instead of failing loudly).
#: Values are the stored role literals from section 2.1's mapping (Developer
#: = `editor`, QA = `reviewer`, Admin = `owner`, Viewer = `viewer` -- the
#: *product labels* Developer/QA/Admin haven't been renamed onto these
#: strings anywhere in code yet, but the stored roles themselves already are
#: exactly these four people, so every row below can be modeled today with
#: no role rename blocking it.
#:
#: Most of these keys are reserved, not live: their routes don't exist yet
#: (Change Request/version-tag/release/environment machinery is Phase 2-5's
#: job) or, for `resource.write.evidence`/`rework.update`, wiring a real
#: project-role check onto their routes is deliberately deferred (see the
#: two comments below) rather than done here. A reserved key still closes
#: the registry -- `decide_project_access` denies any action not present as
#: a key at all, so adding a key now means a route that starts using this
#: literal next is instantly covered by an already-correct role set, not an
#: implicit allow. `test_the_live_vs_reserved_action_partition_is_pinned`
#: pins exactly which of these are live vs. reserved today.
PROJECT_ACTIONS: Final[dict[str, frozenset[str]]] = {
    "read": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER, ROLE_VIEWER}),
    "project.update": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "project.manage_members": frozenset({ROLE_OWNER}),
    # `P1-C`: the primary-owner invariant and multi-Admin membership work
    # (section 16, item 6/10). Admin-only (`ROLE_OWNER`), same as
    # `project.manage_members` -- these are the three actions section 2.4's
    # target registry adds alongside it.
    "project.archive": frozenset({ROLE_OWNER}),
    "project.restore": frozenset({ROLE_OWNER}),
    "project.transfer_owner": frozenset({ROLE_OWNER}),
    # Reserved: no `PUT/POST /projects/{id}/source*` route exists yet
    # (Phase 4's job -- git-provider import/review).
    "source.manage": frozenset({ROLE_OWNER}),
    # `P1-D`: `resource.write` (section 2.4's own "current registry" name)
    # retired in favor of this split -- see section 6.4's resource-family
    # taxonomy ("authored runtime asset" vs. "quality definition"). Live,
    # unchanged role set: today's 5 generic project-file-storage call sites
    # (`routes/files.py::staging_upload`, `routes/projects.py`'s
    # folder/upload/delete routes, `routes/object_store.py`'s project
    # import) gate CALIBER's generic file tree, not a typed Prompt/Workflow/
    # Test-set/Judge row -- none of them cleanly maps to "runtime" vs.
    # "evidence" under the section 6.4 taxonomy, since that tree holds
    # inputs/outputs for both. They're migrated to `.runtime` here (the
    # closer fit: `object_store.py`'s own docstring already calls this tree
    # "accepted by workflow file-input nodes"), not left on a retired name.
    "resource.write.runtime": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    # Reserved: wiring this onto the actual Prompt/Workflow/Tool/Skill/
    # Test-set/Judge/Scorer CRUD routes is section 2.3's own explicit
    # warning -- "isolation closure is a hard prerequisite for shipping QA"
    # (Phase 2, not this slice). Those routes check only global scope today;
    # adding a project-role check on top of that now, before Phase 2 makes
    # resource-to-workspace ownership consistent, is exactly the premature
    # tightening section 2.3 says not to do yet.
    "resource.write.evidence": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER}),
    "resource.publish": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    # Reserved (never wired to a route in this codebase's history) --
    # section 2.4's target table has no `resource.approve` row at all; kept
    # only because removing a `PROJECT_ACTIONS` key outright, rather than
    # documenting it as retired-and-unused, would be a silent behavior
    # change for any caller that somehow still checks it.
    "resource.approve": frozenset({ROLE_OWNER, ROLE_REVIEWER}),
    # `P1-D`: now wired -- `routes/workflow_runs.py` (create/trigger-event/
    # cancel/retry/resume/resume-by-event) and `routes/evaluations.py`
    # (create) call `require_project_access_if_scoped` with this action
    # whenever the run/evaluation's workflow/dataset actually has a
    # `project_id` (both are *optionally* project-scoped -- a personal/
    # global one has no workspace to check a role against, so the check is
    # skipped rather than 404ing every unscoped run).
    "resource.execute": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER}),
    # `P1-D`: new, and now wired -- `routes/review_queues.py::submit_item`
    # (the route section 2.4's own prose names: "there is no existing action
    # for it to ride on ... submitting review-queue feedback today calls
    # neither `require_project_access` nor any project action"). Same
    # optionally-scoped treatment as `resource.execute` above: a personal
    # review queue (`project_id is None`) skips the project-role check.
    "feedback.submit": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER}),
    # Reserved: `caliber_rework_tasks` is a global table today, not yet
    # project-scoped (the project-scoped `/projects/{id}/rework-tasks` API
    # section 3.6 describes still doesn't exist) -- there is no `project_id`
    # to check a role against yet, so wiring this would mean building that
    # scoping first, not just adding a call here.
    "rework.update": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    # Everything below is reserved for a Phase 2-5 route family that does
    # not exist in this codebase yet (Change Requests, version tags,
    # environments, releases/operations). Closing the registry over the
    # full target vocabulary now means whichever of those routes lands
    # first is instantly gated correctly, not added to `PROJECT_ACTIONS` as
    # an afterthought alongside its own PR.
    "revision.import": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "revision.create": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "change_request.create": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "change_request.update": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "change_request.comment": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER}),
    "change_request.review": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "change_request.manage": frozenset({ROLE_OWNER}),
    "environment.manage": frozenset({ROLE_OWNER}),
    "release.request": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "release.evaluate": frozenset({ROLE_OWNER, ROLE_EDITOR, ROLE_REVIEWER}),
    "release.quality_signoff": frozenset({ROLE_REVIEWER}),
    "release.approve": frozenset({ROLE_OWNER}),
    "release.apply": frozenset({ROLE_OWNER, ROLE_EDITOR}),
    "release.rollback": frozenset({ROLE_OWNER}),
    "release.reconcile": frozenset({ROLE_OWNER}),
    # `release.break_glass_apply` is deliberately absent: section 2.4 names
    # it "no ordinary role grant" -- it needs `caliber.admin`, an
    # interactive credential, and an explicit recovery policy, not a project
    # role at all. Modeling it as a normal `{role: ...}` entry here would
    # misrepresent it as grantable through ordinary membership. Phase 5
    # (`P5-B`) designs its real, separately-audited check.
}

#: `P1-B`/`P1-C`/`P1-D`: a hand-bumped marker for `AccessDecision.policy_version`
#: (section 5.4). Bump this string whenever this module's decision policy
#: changes in a way an auditor reading old decisions would need to know
#: about (e.g. the admin-owner-bypass removal `P1-B` made, `P1-C` adding
#: the archive/restore/transfer_owner actions, or `P1-D` closing the full
#: action registry and wiring `resource.execute`/`feedback.submit`) -- not
#: on every unrelated edit to this file.
POLICY_VERSION: Final[str] = "p1d-2026-09-12"

#: `P1-C` (section 2.4/19.1 item 4): granting the `owner` role (Admin) --
#: whether via `add_project_member`/`update_project_member` or as the
#: target of `transfer-ownership` -- requires the target's live platform
#: scopes to include **both** of these, the same conjunction
#: `project.create`'s pre-membership bootstrap check enforces for the
#: creator. A project-role holder cannot self-certify this: it is checked
#: against the target user's actual global-scope grants, independent of
#: who is making the request.
OWNER_ROLE_REQUIRED_SCOPES: Final[frozenset[str]] = frozenset({SCOPE_OPERATOR, SCOPE_APPROVER})

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

    "Authenticated principal" is the route layer's job (`require_user`/
    `resolve_identity` run before this function is ever reached). The
    "credential/global-scope ceiling" conjunct is *supposed* to be the
    calling route's responsibility too (`require_scopes`/`require_all_scopes`
    before this function runs) for every **workspace-backed** action -- but
    that is a convention this function cannot itself enforce or verify for
    those, and a GitHub Copilot review of this same PR found several
    existing routes that called `require_project_access` without a
    preceding scope check at all, letting a project-role holder with only
    `caliber.viewer` perform an action section 2.4 says needs
    `caliber.operator`. Those call sites were fixed directly
    (`routes/projects.py`'s member-mutation and `project.update` routes;
    `routes/openapi_integrations.py`'s publish route) rather than papering
    over the gap here: the fix belongs at each call site. `P1-D` closes the
    action registry itself (`PROJECT_ACTIONS` now covers section 2.4's full
    target vocabulary) but does not change this function to also carry a
    central action-to-scope mapping -- that remains each call site's job,
    same as before. The one exception is the
    `project.create` bootstrap case below (`workspace_id=None`), which this
    function *does* check directly, since no workspace/route exists yet to
    delegate that check to.

    `principal.scopes` is assumed already fully resolved (`auth.py`'s
    `current_scopes()` expands `caliber.admin` into every scope it implies
    before a `CaliberIdentity` is ever constructed) -- the same assumption
    every other scope check in this codebase makes (`CaliberIdentity.has_scope`
    is a bare containment check with no hierarchy logic of its own). A
    `CaliberIdentity` built directly with only the literal `SCOPE_ADMIN`
    value, bypassing `resolve_identity`, would not satisfy the bootstrap
    check below even though a real admin identity always does.

    `resource`/`environment`/`release` are typed `object | None` -- no
    `ResourceContext`/`EnvironmentContext`/`ReleaseContext` class exists
    yet (no per-resource pinning model, no environment-scoped route, no
    release/Change-Request model -- Phase 4/5's job). No caller passes
    anything but `None` today. Passing a non-`None` value raises
    `NotImplementedError` naming the unmodeled conjunct -- an honest
    "not built yet" signal, not a silent no-op a future real caller could
    trip over without noticing nothing was actually checked. Checked
    first, unconditionally, so the `project.create` bootstrap branch below
    cannot short-circuit past this guard.
    """
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
    if workspace_id is None:
        if action == "project.create":
            # The one documented exception (section 5.4): the pre-membership
            # bootstrap check has no concrete workspace yet. `create_project`
            # itself still enforces this via `require_all_scopes` at the
            # route layer (unchanged) -- this branch exists so `authorize()`
            # is not self-contradictory for the one case section 5.4 names
            # as valid with `workspace_id=None`, for any caller that reaches
            # this function directly instead.
            required = frozenset({SCOPE_OPERATOR, SCOPE_APPROVER})
            if required <= principal.scopes:
                return AccessDecision(True, None, REASON_GRANTED, frozenset())
            return AccessDecision(False, None, REASON_PERMISSION_DENIED, frozenset())
        # Every other action with no concrete workspace denies (section 5.4).
        return AccessDecision(False, None, REASON_PROJECT_NOT_FOUND, frozenset())
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


def require_project_access_if_scoped(
    session: Session,
    identity: CaliberIdentity,
    project_id: str | None,
    action: str,
) -> None:
    """Enforce a project-role check only when `project_id` is actually set.

    `P1-D`: several resource families this action registry now covers --
    workflows/workflow runs (`resource.execute`), review queues
    (`feedback.submit`) -- are *optionally* project-scoped: `project_id` is
    nullable on the row, and a `None` value is a genuine personal/global
    resource with no workspace to check a role against, not a data gap.
    Calling `require_project_access` unconditionally would 404 every one of
    those (no project to look up). This is the shared "only gate when
    there's a workspace to gate against" check those call sites need --
    a no-op, not a bypass, for the unscoped case; the caller's own
    `require_scopes`/`require_all_scopes` global-scope check still applies
    either way.
    """
    if project_id is None:
        return
    require_project_access(session, identity, project_id, action)


def is_eligible_for_owner_role(config: Any, user_id: str) -> bool:
    """Whether ``user_id``'s live platform scopes qualify them for the
    ``owner`` (Admin) project role -- both `caliber.operator` and
    `caliber.approver` (`OWNER_ROLE_REQUIRED_SCOPES`).

    Checked against the target's *current* grants (`auth.scopes_for_user`),
    not a snapshot taken when a role was first assigned -- a member granted
    `owner` while both-scoped, later demoted at the platform-identity level,
    must fail this check the next time it runs (e.g. before a `transfer-
    ownership`), even though their stored project role is untouched. A
    missing/unwired config (no app fully configured) fails closed rather
    than raising, matching this module's fail-closed default elsewhere.
    """
    if config is None:
        return False
    return scopes_for_user(config, user_id) >= OWNER_ROLE_REQUIRED_SCOPES


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
        "deactivated_at": member.deactivated_at.isoformat() if member.deactivated_at else None,
        "deactivated_by": member.deactivated_by,
    }
