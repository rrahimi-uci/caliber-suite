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

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import CaliberAgentConfig, CaliberWorkflow
from caliber.db.scoping import apply_visibility_filter


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


def is_visible(experiment_id: str | None, visible: set[str] | None) -> bool:
    """Whether a trace filed under ``experiment_id`` may be shown, given :func:`visible_experiment_ids`."""
    if visible is None:
        return True
    return experiment_id is not None and experiment_id in visible
