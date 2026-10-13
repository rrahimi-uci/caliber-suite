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

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.auth import CaliberIdentity
from caliber.db.models import CaliberAgentConfig, CaliberRefinementJob
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
