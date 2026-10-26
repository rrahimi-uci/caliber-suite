"""Which MLflow experiments, and so which traces, an identity may see.

MLflow keeps traces by experiment and knows nothing of CALIBER's projects. CALIBER ties an experiment to
its owner in two places: a registered agent (``CaliberAgentConfig.experiment_id``) and, optionally, a
workflow (``CaliberWorkflow.default_experiment_id``). Both rows carry a visibility tier and a project, so
the same 3-tier rule that decides who sees an agent or a workflow decides who sees the traces filed under
its experiment (:func:`caliber.db.scoping.apply_visibility_filter`).

An experiment that no visible agent or workflow claims is **hidden from everyone but an admin**. That is
deliberate: an ownership we cannot establish must not read as "public". Workflow runs record their MLflow
experiment by *name* (``workflows/runtime.py`` passes ``ir.mlflow_experiment_name``) and the database keeps
no experiment id for a run, so a workflow's traces are visible to non-admins only when the workflow sets
``default_experiment_id``. Mapping the rest, and history, is the run-correlation work in #530.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import CaliberAgentConfig, CaliberWorkflow
from caliber.db.scoping import apply_visibility_filter
from caliber.trace_client import TraceNotFoundError, fetch_trace_detail

logger = logging.getLogger("caliber.observability.trace_access")


def visible_experiment_ids(session: Session, identity: CaliberIdentity) -> set[str] | None:
    """The MLflow experiment ids ``identity`` may see traces from; ``None`` means every experiment.

    ``None`` is for an admin only. Everyone else gets the experiments of the agents and workflows
    their visibility and project membership allow, which may be the empty set.
    """
    if identity.has_scope(SCOPE_ADMIN):
        return None
    project_id = identity.active_project_id
    agents = apply_visibility_filter(
        select(CaliberAgentConfig.experiment_id), CaliberAgentConfig, identity, project_id
    )
    workflows = apply_visibility_filter(
        select(CaliberWorkflow.default_experiment_id).where(
            CaliberWorkflow.default_experiment_id.is_not(None)
        ),
        CaliberWorkflow,
        identity,
        project_id,
    )
    found = set(session.execute(agents).scalars()) | set(session.execute(workflows).scalars())
    return {str(experiment_id) for experiment_id in found if experiment_id}


def visible_experiments(session: Session, identity: CaliberIdentity) -> set[str] | None:
    """:func:`visible_experiment_ids` plus the numeric id of every claim that is an experiment *name*.

    An agent may be registered by experiment name (``routes/agents.py`` documents that), while a trace
    always carries the numeric id, so a name claim has to be resolved or its owners would lose their own
    traces. A name MLflow cannot resolve stays a claim on that name only.
    """
    claims = visible_experiment_ids(session, identity)
    if claims is None:
        return None
    return claims | _numeric_ids_of_named_claims(claims)


def _numeric_ids_of_named_claims(claims: set[str]) -> set[str]:
    names = [claim for claim in claims if not claim.isdigit()]
    if not names:
        return set()
    try:
        import mlflow  # noqa: PLC0415

        getter = mlflow.get_experiment_by_name
    except Exception:
        return set()
    resolved: set[str] = set()
    for name in names:
        try:
            experiment = getter(name)
        except Exception:
            logger.debug("could not resolve experiment name %r to an id", name, exc_info=True)
            continue
        experiment_id = getattr(experiment, "experiment_id", None) if experiment else None
        if experiment_id is not None:
            resolved.add(str(experiment_id))
    return resolved


class TraceNotReadableError(ValueError):
    """The trace does not exist or is filed under an experiment the caller may not see.

    The two are deliberately one error with one message, so a foreign id is not confirmed to exist.
    """


def require_readable_trace(trace_id: str, visible: set[str] | None) -> None:
    """Raise :class:`TraceNotReadableError` unless ``trace_id`` exists and ``visible`` includes its experiment.

    ``visible=None`` (an admin) skips the lookup. A trace store that cannot be reached raises
    :class:`caliber.trace_client.TraceStoreUnavailableError`: an outage is not an answer about ownership.
    """
    if visible is None:
        return
    try:
        detail = fetch_trace_detail(trace_id, strict=True)
    except TraceNotFoundError as exc:
        raise TraceNotReadableError(f"trace {trace_id!r} not found") from exc
    if not is_visible(detail.experiment_id, visible):
        raise TraceNotReadableError(f"trace {trace_id!r} not found")


class ExperimentClaimConflict(ValueError):  # noqa: N818 (a conflict, not an error class by convention)
    """Another project or user already claims the experiment."""


def _claim_scope(visibility: str, project_id: str | None, owner: str) -> tuple[str, str]:
    """Who a claim belongs to: a project, one user, or everyone."""
    if visibility == "project":
        return ("project", project_id or "")
    if visibility == "public":
        return ("public", "")
    return ("user", owner)


def assert_experiment_claim_allowed(
    session: Session,
    experiment_id: str | None,
    *,
    visibility: str,
    project_id: str | None,
    owner: str,
    agent_id: str | None = None,
    workflow_id: str | None = None,
) -> None:
    """Refuse a claim on an experiment that an agent or workflow of another scope already holds.

    Ownership of an experiment is what decides who sees its traces, so it cannot be a free-text
    assertion anyone may overwrite: without this check, an editor in project A could name project B's
    experiment as a workflow's ``default_experiment_id`` and read B's traces. Two rows of the same
    project (or the same user) may share an experiment; a row of any other scope may not take it.
    ``agent_id`` / ``workflow_id`` name the row being updated so it does not conflict with itself.

    This closes taking an experiment that is already bound. An experiment nobody has bound yet goes to
    whoever claims it first: proving that it was created for a project needs CALIBER to create it,
    which is the run-correlation work in #530.
    """
    if not experiment_id:
        return
    mine = _claim_scope(visibility, project_id, owner)
    agent_rows = session.execute(
        select(
            CaliberAgentConfig.agent_id,
            CaliberAgentConfig.visibility,
            CaliberAgentConfig.project_id,
            CaliberAgentConfig.owner,
        ).where(CaliberAgentConfig.experiment_id == experiment_id)
    ).all()
    workflow_rows = session.execute(
        select(
            CaliberWorkflow.workflow_id,
            CaliberWorkflow.visibility,
            CaliberWorkflow.project_id,
            CaliberWorkflow.owner,
        ).where(CaliberWorkflow.default_experiment_id == experiment_id)
    ).all()
    others = [(vis, proj, own) for row_id, vis, proj, own in agent_rows if row_id != agent_id] + [
        (vis, proj, own) for row_id, vis, proj, own in workflow_rows if row_id != workflow_id
    ]
    if any(_claim_scope(*other) != mine for other in others):
        raise ExperimentClaimConflict(
            f"experiment {experiment_id!r} is already claimed by another project or user"
        )


def is_visible(experiment_id: str | None, visible: set[str] | None) -> bool:
    """Whether a trace filed under ``experiment_id`` may be shown, given :func:`visible_experiment_ids`."""
    if visible is None:
        return True
    return experiment_id is not None and experiment_id in visible
