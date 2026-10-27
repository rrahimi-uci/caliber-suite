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
  wildcard, no prefix and no ranking of actions by name or risk.
* **Authority is rechecked every time and fails closed.** :func:`authorize` re-reads the grant, then checks, in this
  order: revoked, expired, the subject's *current* active membership of the grant's project, the action, the resource.
  A database failure while reading any of it is :data:`AUTHORITY_UNAVAILABLE`, never an allow.
* **Concurrent revocation.** :func:`authorize` takes a row lock on the grant (``SELECT ... FOR UPDATE`` on
  PostgreSQL) in the *caller's* transaction, and :func:`revoke_grant` takes the same lock, so a revocation either
  commits before a check (the check denies) or waits for the checking transaction to finish; it cannot slip between a
  check and the transition that relies on it, provided the caller performs that transition in the same transaction.
  SQLite has no row locks; its single writer gives the same ordering.
* **Time** is the caller's clock (``now``), UTC, so tests use a fake clock; the grant's own expiry is stored as UTC.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProjectMember, CaliberRunGrant
from caliber.policy.principal import PrincipalError, Subject, Workload

#: Every refusal code :class:`GrantDenied` carries.
GRANT_NOT_FOUND: Final = "grant_not_found"
GRANT_REVOKED: Final = "grant_revoked"
GRANT_EXPIRED: Final = "grant_expired"
NOT_A_MEMBER: Final = "not_a_member"
ACTION_NOT_ALLOWED: Final = "action_not_allowed"
RESOURCE_NOT_ALLOWED: Final = "resource_not_allowed"
AUTHORITY_UNAVAILABLE: Final = "authority_unavailable"
DENIAL_CODES: Final = frozenset(
    {
        GRANT_NOT_FOUND,
        GRANT_REVOKED,
        GRANT_EXPIRED,
        NOT_A_MEMBER,
        ACTION_NOT_ALLOWED,
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


def _utc(moment: datetime) -> datetime:
    """Naive UTC, which is how the database stores it."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _now(now: datetime | None) -> datetime:
    return _utc(now if now is not None else datetime.now(timezone.utc))


def _members(values: Iterable[str], what: str) -> list[str]:
    items = list(values)
    if not items:
        raise GrantError(f"{what} must not be empty")
    for item in items:
        if type(item) is not str or not item or item != item.strip():
            raise GrantError(
                f"every entry of {what} must be a non-empty string with no surrounding whitespace"
            )
    return sorted(set(items))


def _text(name: str, value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise GrantError(f"{name} must be a non-empty string with no surrounding whitespace")
    return value


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
) -> Grant:
    """Issue a grant for ``run_id``. Flushes, does not commit: it belongs in the admission transaction.

    The grant id is supplied by the caller (admission allocates it); a duplicate id violates the primary key. The
    subject's own ``project_id`` (server-side context) must agree with the grant's, so a grant cannot be issued for a
    project the subject did not act in.
    """
    if type(subject) is not Subject or type(workload) is not Workload:
        raise GrantError("subject and workload must be a Subject and a Workload")
    if subject.project_id is not None and subject.project_id != project_id:
        raise GrantError("the grant's project must be the project the subject acted in")
    moment = _now(now)
    expiry = _utc(expires_at)
    if expiry <= moment:
        raise GrantError("expires_at must be in the future")
    row = CaliberRunGrant(
        grant_id=_text("grant_id", grant_id),
        workflow_run_id=_text("run_id", run_id),
        subject_user_id=subject.user_id,
        subject_auth_method=subject.auth_method,
        workload_id=workload.workload_id,
        workload_name=workload.name,
        workflow_version_digest=workload.workflow_version_digest,
        project_id=_text("project_id", project_id),
        allowed_actions=_members(allowed_actions, "allowed_actions"),
        allowed_resources=_members(allowed_resources, "allowed_resources"),
        policy_version=_text("policy_version", policy_version),
        expires_at=expiry,
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


def _is_active_member(session: Session, project_id: str, user_id: str) -> bool:
    member = session.execute(
        select(CaliberProjectMember.status, CaliberProjectMember.deactivated_at).where(
            CaliberProjectMember.project_id == project_id, CaliberProjectMember.user_id == user_id
        )
    ).first()
    return member is not None and member.status == "active" and member.deactivated_at is None


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
) -> Grant:
    """Return the grant if, right now, it authorizes ``action`` on ``resource`` for ``run_id``; else raise.

    Rechecks revocation, expiry and the subject's current project membership on every call and holds a row lock on
    the grant until the caller's transaction ends, so run the transition this check guards in the same transaction.
    Any database failure is :data:`AUTHORITY_UNAVAILABLE`.
    """
    try:
        row = _locked_row(session, run_id, grant_id)
        grant = _to_grant(row)
        if row.revoked_at is not None:
            raise GrantDenied(GRANT_REVOKED)
        if _utc(row.expires_at) <= _now(now):
            raise GrantDenied(GRANT_EXPIRED)
        if not _is_active_member(session, row.project_id, row.subject_user_id):
            raise GrantDenied(NOT_A_MEMBER)
    except SQLAlchemyError as exc:
        raise GrantDenied(AUTHORITY_UNAVAILABLE) from exc
    if action not in grant.allowed_actions:
        raise GrantDenied(ACTION_NOT_ALLOWED)
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
    try:
        row = _locked_row(session, run_id, grant_id)
        if row.revoked_at is not None:
            raise GrantDenied(GRANT_REVOKED)
        row.revoked_at = _now(now)
        row.revoked_by = _text("by", by)
        row.revoke_reason = _text("reason", reason)
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
    "Grant",
    "GrantDenied",
    "GrantError",
    "authorize",
    "create_grant",
    "load_grant",
    "revoke_grant",
]
