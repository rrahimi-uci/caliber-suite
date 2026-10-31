"""Project visibility and role gates for refinement-job routes.

``CaliberRefinementJob`` has no ``project_id`` or ``visibility`` column of its
own. A job belongs to the agent it refines (``job.agent_id``), and for a prompt,
skill or workflow target that agent row carries the owning project and the
visibility tier. So a job is visible to exactly the callers that can see its
agent, and the project role needed to act on it is read from the agent's
project.

Before this module existed, ``/jobs`` list, detail, targets, Apply,
request-changes and ``/quality-reviews`` checked only a global scope. An
operator who was not a member of project A could read A's candidate text and
diagnosis and could Apply A's candidate to production, and a viewer-role member
of A could do the same (see ``docs/caliber-alpha-gaps.md``, P0-1).

An out-of-scope job answers 404, never 403, so the response does not reveal
that the id exists. A visible job whose project role is too weak answers 403,
which lets the UI explain the missing role.

A job whose agent row is missing is treated as personal/global, the same
carve-out the prompt routes apply to a bare provider-only prompt with no
CALIBER target. A real database enforces the foreign key, so this is only
reachable on a legacy or hand-seeded SQLite file; there is no project to
protect there, and the caller's global-scope check still applies.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberProject,
    CaliberProjectMember,
    CaliberRefinementJob,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.resource_access import require_project_access_if_scoped

#: Project action needed to apply a candidate to production.
ACTION_APPLY = "release.apply"
#: Project action needed to review a candidate (request changes, quality review).
#: Owners, editors and reviewers hold it; viewers do not.
ACTION_REVIEW = "release.evaluate"


def _not_found(job_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"refinement job {job_id!r} not found")


def load_visible_job(
    session: Session,
    identity: CaliberIdentity,
    job_id: str,
    *,
    action: str | None = None,
) -> CaliberRefinementJob:
    """Return the job, or raise 404 when it is missing or not visible to ``identity``.

    When ``action`` is given, also require that project role on the job's
    agent's project (403 when the caller is a member without that role). Pass
    ``None`` for read-only routes. The check runs before any state change, so a
    refused caller never reaches a claim, a promoter call or an audit write.
    """
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise _not_found(job_id)
    agent = session.get(CaliberAgentConfig, job.agent_id)
    if agent is None:
        return job
    visible = get_visible(
        session, CaliberAgentConfig, CaliberAgentConfig.agent_id, job.agent_id, identity
    )
    if visible is None:
        raise _not_found(job_id)
    if action is not None:
        require_project_access_if_scoped(session, identity, agent.project_id, action)
    return job


#: How long, on PostgreSQL, the Apply claim waits for a row lock before it gives up (``SET LOCAL lock_timeout``). A lock
#: is held by a transaction that is about to commit (another claim, a membership or candidate edit), so a wait is short;
#: a stuck one must not pin a request handler. Fixed, not a setting: it only bounds a wait, it never changes an outcome.
LOCK_TIMEOUT_MS = 5000
_LOCK_NOT_AVAILABLE = "55P03"


def is_lock_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is PostgreSQL's ``lock_not_available`` (SQLSTATE 55P03), what ``lock_timeout`` raises."""
    if not isinstance(exc, OperationalError):
        return False
    orig = exc.orig
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == _LOCK_NOT_AVAILABLE


def lock_job_and_authority(
    session: Session, job_id: str, *, user_id: str
) -> CaliberRefinementJob | None:
    """Row-lock a job and every row that decides ``user_id``'s access to it, then drop stale session state (B06.4, #753).

    Returns the job as the database holds it now, or ``None`` when it no longer exists. Call it inside the transaction
    that will write the job, then re-run :func:`load_visible_job`: that check now reads rows that cannot change until
    the transaction ends, so a membership revoked, a role lowered, a project handed to another owner or an agent moved
    to another project is either seen here or waits behind this transaction. There is no gap between the check and the
    write for a revocation to land in.

    Locks are taken in a fixed order: the job (``FOR UPDATE``), its agent, the agent's project, then the caller's
    membership row in that project (each ``FOR SHARE``, so concurrent applies do not serialise on them while any writer of
    those rows does). SQLite has no row locks and ignores the clause; its single-writer file lock is the equivalent.
    On PostgreSQL the waits are bounded by :data:`LOCK_TIMEOUT_MS`; a wait that runs out raises ``OperationalError``
    (:func:`is_lock_timeout`), which the caller maps to a retryable refusal.
    Every statement refreshes the identity map first, otherwise ``session.get`` in :func:`load_visible_job` would answer
    from objects loaded before the lock.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}"))
    job = session.execute(
        select(CaliberRefinementJob)
        .where(CaliberRefinementJob.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if job is None:
        return None
    agent = session.execute(
        select(CaliberAgentConfig)
        .where(CaliberAgentConfig.agent_id == job.agent_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if agent is not None and agent.project_id is not None:
        session.execute(
            select(CaliberProject.project_id)
            .where(CaliberProject.project_id == agent.project_id)
            .with_for_update(read=True)
        ).all()
        session.execute(
            select(CaliberProjectMember.member_id)
            .where(
                CaliberProjectMember.project_id == agent.project_id,
                CaliberProjectMember.user_id == user_id,
            )
            .with_for_update(read=True)
        ).all()
    session.expire_all()
    return job


def scope_jobs_to_visible_agents(stmt: Select[Any], identity: CaliberIdentity) -> Select[Any]:
    """Restrict a ``CaliberRefinementJob`` SELECT to jobs ``identity`` may see.

    Jobs whose agent row does not exist stay listed (see the module docstring).
    Admins see every job, matching every other visibility-filtered list.
    """
    visible_agents = apply_visibility_filter(
        select(CaliberAgentConfig.agent_id),
        CaliberAgentConfig,
        identity,
        identity.active_project_id,
    )
    all_agents = select(CaliberAgentConfig.agent_id)
    return stmt.where(
        or_(
            CaliberRefinementJob.agent_id.in_(visible_agents),
            CaliberRefinementJob.agent_id.not_in(all_agents),
        )
    )
