"""Run-bound grants: what one run may do, for whom, in which project, until when (B03.2, #664).

A grant (``CaliberRunGrant``) is issued for an exact run and carries an explicit grant id, the run's subject and
workload (:mod:`caliber.policy.principal`), the project, the explicit sets of allowed actions and resources, the
policy version it was issued under and an expiry. This module issues, revokes and checks them. It makes no policy
*decision* (that is the typed ``PolicyDecision`` of B03.3) and records no decision (B03.5); it answers "is this exact
grant, for this exact run, still good for this action on this resource, right now?".

Rules:

* **Exact identifiers, no fallback.** The caller supplies the run id *and* the grant id. There is no lookup of "the
  newest grant for the run" and no ambient grant: a grant that is not the one named, or that names a different run, is
  :data:`GRANT_NOT_FOUND`, the same answer as an unknown id, so ids cannot be probed.
* **Explicit sets, no ordering.** ``allowed_actions`` and ``allowed_resources`` are exact-match sets. There is no
  wildcard, no prefix and no ranking of actions by name or risk. Actions come from the closed vocabulary of
  ``caliber.resource_access.PROJECT_ACTIONS`` (an unknown action cannot be granted), and each carries the explicit set
  of project roles that may perform it.
* **A grant never exceeds the subject's role.** :func:`create_grant` refuses an action the subject's current role is
  not in the set for, and :func:`authorize` rechecks the *current* role on every use (``role_not_allowed``): a
  demotion narrows a standing grant at once. The project owner counts as ``owner`` only when the caller supplies live
  scopes that still include the owner-role scopes (``resource_access.OWNER_ROLE_REQUIRED_SCOPES``); without them
  ``owner`` is treated as ``editor``, as ``project_role`` does.
* **Authority is rechecked every time and fails closed.** :func:`authorize` re-reads the grant, then checks, in this
  order: revoked, expired, the subject's *current* membership of (or ownership of) the grant's project, the action
  against the grant, the current role against the action, the resource. A database failure while reading any of it
  is :data:`AUTHORITY_UNAVAILABLE`, never an allow.
* **Concurrent revocation and membership change.** :func:`authorize` takes a row lock on the grant (``SELECT ...
  FOR UPDATE``) and a shared lock on the membership (and project) rows it relied on (``FOR SHARE``) in the *caller's*
  transaction. :func:`revoke_grant` takes the grant lock, and removing or demoting a member must update the locked
  member row, so on PostgreSQL either change waits for a check in flight or commits first (and the check sees it).
  Under REPEATABLE READ or SERIALIZABLE a change that committed after the transaction's snapshot is a serialization
  error, which is :data:`AUTHORITY_UNAVAILABLE`. The guarantee holds only if the caller performs the guarded
  transition in the same transaction. **SQLite gives no such guarantee** (the driver does not even begin a
  transaction before the first write); it is development only, and the reference profile refuses it.
* **Time** is the caller's clock (``now``), UTC, so tests use a fake clock; the grant's own expiry is stored as UTC.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberWorkflowRun,
)
from caliber.policy.principal import PrincipalError, Subject, Workload
from caliber.resource_access import (
    OWNER_ROLE_REQUIRED_SCOPES,
    PROJECT_ACTIONS,
    ROLE_EDITOR,
    ROLE_OWNER,
)

#: Every refusal code :class:`GrantDenied` carries.
GRANT_NOT_FOUND: Final = "grant_not_found"
GRANT_REVOKED: Final = "grant_revoked"
GRANT_EXPIRED: Final = "grant_expired"
NOT_A_MEMBER: Final = "not_a_member"
ACTION_NOT_ALLOWED: Final = "action_not_allowed"
ROLE_NOT_ALLOWED: Final = "role_not_allowed"
RESOURCE_NOT_ALLOWED: Final = "resource_not_allowed"
AUTHORITY_UNAVAILABLE: Final = "authority_unavailable"
DENIAL_CODES: Final = frozenset(
    {
        GRANT_NOT_FOUND,
        GRANT_REVOKED,
        GRANT_EXPIRED,
        NOT_A_MEMBER,
        ACTION_NOT_ALLOWED,
        ROLE_NOT_ALLOWED,
        RESOURCE_NOT_ALLOWED,
        AUTHORITY_UNAVAILABLE,
    }
)


class GrantError(ValueError):
    """A grant that cannot be issued as asked (malformed input)."""


class GrantDenied(PermissionError):  # noqa: N818 - a refusal, named for what it says
    """A grant does not authorize this. ``code`` is one of :data:`DENIAL_CODES`; the message never echoes an id."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Grant:
    """A stored grant, read back as typed principals and exact sets."""

    grant_id: str
    run_id: str
    subject: Subject
    workload: Workload
    project_id: str
    allowed_actions: frozenset[str]
    allowed_resources: frozenset[str]
    policy_version: str
    expires_at: datetime
    revoked_at: datetime | None = None

    def allows(self, action: str, resource: str) -> bool:
        return action in self.allowed_actions and resource in self.allowed_resources


_MAX_ID = 64
_MAX_ENTRY = 256
_MAX_ENTRIES = 256


def _utc(moment: datetime) -> datetime:
    """Naive UTC, which is how the database stores it. A naive input is refused: its zone is unknowable."""
    if moment.tzinfo is None:
        raise GrantError("datetimes must be timezone-aware (UTC is stored)")
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _now(now: datetime | None) -> datetime:
    return _utc(now if now is not None else datetime.now(timezone.utc))


def _clean(value: object, name: str, limit: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise GrantError(f"{name} must be a non-empty string with no surrounding whitespace")
    if len(value) > limit:
        raise GrantError(f"{name} must be at most {limit} characters")
    if any(
        unicodedata.category(ch)[0] == "C" or unicodedata.category(ch) in {"Zl", "Zp"}
        for ch in value
    ):
        raise GrantError(f"{name} must not contain control or format characters")
    return value


def _members(
    values: Iterable[str], what: str, *, vocabulary: Iterable[str] | None = None
) -> list[str]:
    items = list(values)
    if not items:
        raise GrantError(f"{what} must not be empty")
    if len(items) > _MAX_ENTRIES:
        raise GrantError(f"{what} may hold at most {_MAX_ENTRIES} entries")
    cleaned = {_clean(item, f"every entry of {what}", _MAX_ENTRY) for item in items}
    if vocabulary is not None:
        unknown = sorted(cleaned - set(vocabulary))
        if unknown:
            raise GrantError(f"{what} names actions outside the closed vocabulary: {unknown}")
    return sorted(cleaned)


def _text(name: str, value: str) -> str:
    return _clean(value, name, _MAX_ID)


def create_grant(
    session: Session,
    *,
    grant_id: str,
    run_id: str,
    subject: Subject,
    workload: Workload,
    project_id: str,
    allowed_actions: Iterable[str],
    allowed_resources: Iterable[str],
    policy_version: str,
    expires_at: datetime,
    now: datetime | None = None,
    scopes: frozenset[str] | None = None,
) -> Grant:
    """Issue a grant for ``run_id``. Flushes, does not commit: it belongs in the admission transaction.

    The grant id is supplied by the caller (admission allocates it); a duplicate id violates the primary key. The
    run must exist and, when it has a project, be in the grant's; the subject's own ``project_id`` (server-side
    context) must agree with the grant's; every action must be in the closed vocabulary and permitted to the
    subject's *current* role in that project (``scopes``: see the module docstring), so a grant never exceeds the
    role of the person it is issued for.
    """
    if type(subject) is not Subject or type(workload) is not Workload:
        raise GrantError("subject and workload must be a Subject and a Workload")
    if subject.project_id is not None and subject.project_id != project_id:
        raise GrantError("the grant's project must be the project the subject acted in")
    moment = _now(now)
    expiry = _utc(expires_at)
    if expiry <= moment:
        raise GrantError("expires_at must be in the future")
    actions = _members(allowed_actions, "allowed_actions", vocabulary=PROJECT_ACTIONS)
    resources = _members(allowed_resources, "allowed_resources")
    row = CaliberRunGrant(
        grant_id=_text("grant_id", grant_id),
        workflow_run_id=_text("run_id", run_id),
        subject_user_id=subject.user_id,
        subject_auth_method=subject.auth_method,
        workload_id=workload.workload_id,
        workload_name=workload.name,
        workflow_version_digest=workload.workflow_version_digest,
        project_id=_text("project_id", project_id),
        allowed_actions=actions,
        allowed_resources=resources,
        policy_version=_text("policy_version", policy_version),
        expires_at=expiry,
    )
    run_project = session.execute(
        select(CaliberWorkflowRun.project_id).where(
            CaliberWorkflowRun.workflow_run_id == row.workflow_run_id
        )
    ).first()
    if run_project is None:
        raise GrantError("the run does not exist")
    if run_project[0] is not None and run_project[0] != row.project_id:
        raise GrantError("the run belongs to a different project")
    role = _current_role(session, row.project_id, subject.user_id, scopes, lock=False)
    if role is None:
        raise GrantError("the subject is not a current member of the project")
    beyond = sorted(action for action in actions if role not in PROJECT_ACTIONS[action])
    if beyond:
        raise GrantError(
            f"the subject's role may not perform {beyond}; a grant never exceeds the role"
        )
    session.add(row)
    session.flush()
    return _to_grant(row)


def _to_grant(row: CaliberRunGrant) -> Grant:
    try:
        subject = Subject(
            user_id=row.subject_user_id,
            auth_method=row.subject_auth_method,
            project_id=row.project_id,
        )
        workload = Workload(
            workload_id=row.workload_id,
            name=row.workload_name,
            workflow_version_digest=row.workflow_version_digest,
        )
    except PrincipalError as exc:  # a stored principal that no longer validates is not trusted
        raise GrantDenied(AUTHORITY_UNAVAILABLE) from exc
    return Grant(
        grant_id=row.grant_id,
        run_id=row.workflow_run_id,
        subject=subject,
        workload=workload,
        project_id=row.project_id,
        allowed_actions=frozenset(row.allowed_actions),
        allowed_resources=frozenset(row.allowed_resources),
        policy_version=row.policy_version,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


def _locked_row(session: Session, run_id: str, grant_id: str) -> CaliberRunGrant:
    """The grant row for exactly this run and grant id, locked for the rest of the caller's transaction."""
    row = session.execute(
        select(CaliberRunGrant)
        .where(CaliberRunGrant.grant_id == grant_id, CaliberRunGrant.workflow_run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise GrantDenied(GRANT_NOT_FOUND)
    return row


def _current_role(
    session: Session, project_id: str, user_id: str, scopes: frozenset[str] | None, *, lock: bool
) -> str | None:
    """The user's current project role, or ``None``: the project's owner, else an *active* member's role.

    ``owner`` counts only with live ``scopes`` that still include the owner-role scopes; otherwise it narrows to
    ``editor`` (as ``resource_access.project_role`` does). With ``lock`` the rows read are share-locked for the
    caller's transaction, so removing or demoting the member waits for the check.
    """
    project = select(CaliberProject.owner).where(CaliberProject.project_id == project_id)
    member = select(
        CaliberProjectMember.role, CaliberProjectMember.status, CaliberProjectMember.deactivated_at
    ).where(CaliberProjectMember.project_id == project_id, CaliberProjectMember.user_id == user_id)
    if lock:
        project, member = project.with_for_update(read=True), member.with_for_update(read=True)
    owner = session.execute(project).scalar_one_or_none()
    role: str | None = None
    if owner == user_id:
        role = ROLE_OWNER
    else:
        row = session.execute(member).first()
        if row is not None and row.status == "active" and row.deactivated_at is None:
            role = str(row.role)
    if role == ROLE_OWNER and not (scopes is not None and scopes >= OWNER_ROLE_REQUIRED_SCOPES):
        return ROLE_EDITOR
    return role


def load_grant(session: Session, *, run_id: str, grant_id: str) -> Grant:
    """Read one grant by its exact ids, without checking that it is still good (see :func:`authorize`)."""
    try:
        row = session.execute(
            select(CaliberRunGrant).where(
                CaliberRunGrant.grant_id == grant_id, CaliberRunGrant.workflow_run_id == run_id
            )
        ).scalar_one_or_none()
    except SQLAlchemyError as exc:
        raise GrantDenied(AUTHORITY_UNAVAILABLE) from exc
    if row is None:
        raise GrantDenied(GRANT_NOT_FOUND)
    return _to_grant(row)


def authorize(
    session: Session,
    *,
    run_id: str,
    grant_id: str,
    action: str,
    resource: str,
    now: datetime | None = None,
    scopes: frozenset[str] | None = None,
) -> Grant:
    """Return the grant if, right now, it authorizes ``action`` on ``resource`` for ``run_id``; else raise.

    Rechecks revocation, expiry, the subject's current project role and the action against that role on every
    call, and holds row locks (the grant, the membership and project rows) until the caller's transaction ends, so
    run the transition this check guards in the same transaction. Any database failure is
    :data:`AUTHORITY_UNAVAILABLE`.
    """
    try:
        row = _locked_row(session, run_id, grant_id)
        grant = _to_grant(row)
        if row.revoked_at is not None:
            raise GrantDenied(GRANT_REVOKED)
        if row.expires_at <= _now(now):
            raise GrantDenied(GRANT_EXPIRED)
        role = _current_role(session, row.project_id, row.subject_user_id, scopes, lock=True)
        if role is None:
            raise GrantDenied(NOT_A_MEMBER)
    except SQLAlchemyError as exc:
        raise GrantDenied(AUTHORITY_UNAVAILABLE) from exc
    if action not in grant.allowed_actions:
        raise GrantDenied(ACTION_NOT_ALLOWED)
    if role not in PROJECT_ACTIONS.get(action, frozenset()):
        raise GrantDenied(ROLE_NOT_ALLOWED)
    if resource not in grant.allowed_resources:
        raise GrantDenied(RESOURCE_NOT_ALLOWED)
    return grant


def revoke_grant(
    session: Session,
    *,
    run_id: str,
    grant_id: str,
    by: str,
    reason: str,
    now: datetime | None = None,
) -> Grant:
    """Revoke the grant, once. Flushes, does not commit. Revoking a revoked grant is :data:`GRANT_REVOKED`."""
    revoker = _clean(by, "by", 256)
    why = _clean(reason, "reason", 1000)
    moment = _now(now)
    try:
        row = _locked_row(session, run_id, grant_id)
        if row.revoked_at is not None:
            raise GrantDenied(GRANT_REVOKED)
        row.revoked_at, row.revoked_by, row.revoke_reason = moment, revoker, why
        session.flush()
    except SQLAlchemyError as exc:
        raise GrantDenied(AUTHORITY_UNAVAILABLE) from exc
    return _to_grant(row)


__all__ = [
    "ACTION_NOT_ALLOWED",
    "AUTHORITY_UNAVAILABLE",
    "DENIAL_CODES",
    "GRANT_EXPIRED",
    "GRANT_NOT_FOUND",
    "GRANT_REVOKED",
    "NOT_A_MEMBER",
    "RESOURCE_NOT_ALLOWED",
    "ROLE_NOT_ALLOWED",
    "Grant",
    "GrantDenied",
    "GrantError",
    "authorize",
    "create_grant",
    "load_grant",
    "revoke_grant",
]
