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
import threading
import time
from collections import defaultdict

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


class ExperimentLookupError(RuntimeError):
    """The tracking backend could not say whether an experiment reference exists, or which one it is.

    Claims fail closed on it: an ownership decision is never made on a guess.
    """


#: How long a resolved reference is trusted. Resolution is one MLflow call per distinct reference and the
#: conflict scan resolves every claim in the database, so a short cache keeps a trace list from becoming an
#: N-call fan-out; it is also short enough that a renamed experiment is noticed within seconds.
_RESOLUTION_TTL_SECONDS = 15.0
_resolutions: dict[str, tuple[float, str | None]] = {}
_resolutions_lock = threading.Lock()


def clear_resolution_cache() -> None:
    """Forget every cached resolution (tests, and a caller that has just created an experiment)."""
    with _resolutions_lock:
        _resolutions.clear()


def resolve_experiment(ref: str) -> str | None:
    """The canonical numeric experiment id of ``ref`` (a name or an id), or ``None`` when no such experiment.

    Canonical identity is the id the configured tracking backend assigns: a trace carries only that id, so
    ownership has to be decided on it, whichever alias an agent or workflow named. Raises
    :class:`ExperimentLookupError` when the backend cannot answer; "no such experiment" is an answer.
    """
    now = time.monotonic()
    with _resolutions_lock:
        cached = _resolutions.get(ref)
        if cached is not None and now - cached[0] < _RESOLUTION_TTL_SECONDS:
            return cached[1]
    resolved = _lookup_experiment(ref)
    with _resolutions_lock:
        _resolutions[ref] = (now, resolved)
    return resolved


def _lookup_experiment(ref: str) -> str | None:
    try:
        import mlflow  # noqa: PLC0415
        from mlflow.exceptions import MlflowException  # noqa: PLC0415
    except Exception as exc:
        raise ExperimentLookupError("the tracking client is not available") from exc
    try:
        if ref.isdigit():
            experiment = mlflow.get_experiment(ref)
        else:
            experiment = mlflow.get_experiment_by_name(ref)
    except MlflowException as exc:
        if getattr(exc, "error_code", "") == "RESOURCE_DOES_NOT_EXIST":
            return None
        raise ExperimentLookupError(f"could not resolve experiment {ref!r}: {exc}") from exc
    except Exception as exc:
        raise ExperimentLookupError(f"could not resolve experiment {ref!r}: {exc}") from exc
    experiment_id = getattr(experiment, "experiment_id", None) if experiment else None
    return None if experiment_id is None else str(experiment_id)


def claim_keys(ref: str) -> frozenset[str]:
    """Every key a claim on ``ref`` answers to: the reference as written plus its canonical id.

    Two claims name the same experiment exactly when their keys intersect, which is how a name and an id (or
    two names that resolve to one id) are caught. A reference that does not resolve (an experiment nobody has
    created yet) is just its own text. Raises :class:`ExperimentLookupError` when the backend cannot answer.
    """
    resolved = resolve_experiment(ref)
    return frozenset({ref} if resolved is None else {ref, resolved})


def _claim_rows(session: Session) -> list[tuple[str, tuple[str, str], str | None, str | None]]:
    """Every agent and workflow experiment claim: ``(reference, scope, agent_id, workflow_id)``."""
    rows: list[tuple[str, tuple[str, str], str | None, str | None]] = []
    for agent_id, ref, visibility, project_id, owner in session.execute(
        select(
            CaliberAgentConfig.agent_id,
            CaliberAgentConfig.experiment_id,
            CaliberAgentConfig.visibility,
            CaliberAgentConfig.project_id,
            CaliberAgentConfig.owner,
        ).where(CaliberAgentConfig.experiment_id.is_not(None))
    ).all():
        if ref:
            rows.append((str(ref), _claim_scope(visibility, project_id, owner), agent_id, None))
    for workflow_id, ref, visibility, project_id, owner in session.execute(
        select(
            CaliberWorkflow.workflow_id,
            CaliberWorkflow.default_experiment_id,
            CaliberWorkflow.visibility,
            CaliberWorkflow.project_id,
            CaliberWorkflow.owner,
        ).where(CaliberWorkflow.default_experiment_id.is_not(None))
    ).all():
        if ref:
            rows.append((str(ref), _claim_scope(visibility, project_id, owner), None, workflow_id))
    return rows


def _contested_keys(session: Session) -> dict[str, set[tuple[str, str]]]:
    """``{key: scopes}`` for every key claimed by two or more scopes, under any alias."""
    scopes: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for ref, scope, _agent, _workflow in _claim_rows(session):
        try:
            keys = claim_keys(ref)
        except ExperimentLookupError:
            keys = frozenset({ref})
            logger.warning("could not resolve experiment claim %r; comparing it as written", ref)
        for key in keys:
            scopes[key].add(scope)
    return {key: claimed for key, claimed in scopes.items() if len(claimed) > 1}


def experiment_claim_conflicts(session: Session) -> list[dict[str, object]]:
    """The experiments claimed by more than one project or user, for an operator to resolve.

    Such an experiment is **quarantined**: no non-admin sees its traces until one of the claims is removed.
    Nothing here picks a winner; a last-writer-wins rule would hand one project another's traces.
    """
    rows = _claim_rows(session)
    contested = _contested_keys(session)
    found: dict[str, list[dict[str, object]]] = defaultdict(list)
    for ref, scope, agent_id, workflow_id in rows:
        try:
            keys = claim_keys(ref)
        except ExperimentLookupError:
            keys = frozenset({ref})
        for key in sorted(keys & contested.keys()):
            found[key].append(
                {
                    "claim": ref,
                    "scope": {"kind": scope[0], "id": scope[1]},
                    "agent_id": agent_id,
                    "workflow_id": workflow_id,
                }
            )
    return [{"experiment": key, "claims": claims} for key, claims in sorted(found.items())]


def visible_experiments(session: Session, identity: CaliberIdentity) -> set[str] | None:
    """The experiment keys ``identity`` may see traces from; ``None`` means every experiment (an admin).

    The claims of :func:`visible_experiment_ids` (names or ids, as written) plus the canonical id of each, so a
    trace, which carries only the id, is found whichever alias its owner used. An experiment that two scopes
    claim under any alias is quarantined and absent for everyone but an admin. A claim the backend cannot
    resolve contributes only its own text: it can hide traces from its owner, never reveal them to others.
    """
    claims = visible_experiment_ids(session, identity)
    if claims is None:
        return None
    expanded = set(claims)
    for ref in claims:
        try:
            expanded |= claim_keys(ref)
        except ExperimentLookupError:
            logger.warning(
                "could not resolve experiment claim %r; it matches only its own text", ref
            )
    return expanded - _contested_keys(session).keys()


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

    The comparison is on **canonical identity**, not on text: the claimed reference is resolved to the
    tracking backend's id, as is every existing claim, so an experiment named by id in one project and by name
    in another is one experiment. Raises :class:`ExperimentLookupError` when the backend cannot resolve the
    reference being claimed: no ownership decision is made on a guess.

    This closes taking an experiment that is already bound. An experiment nobody has bound yet goes to
    whoever claims it first: proving that it was created for a project needs CALIBER to create it,
    which is the registry work in B11.
    """
    if not experiment_id:
        return
    mine = _claim_scope(visibility, project_id, owner)
    wanted = claim_keys(experiment_id)  # raises ExperimentLookupError: fail closed
    for ref, scope, existing_agent, existing_workflow in _claim_rows(session):
        if (agent_id and existing_agent == agent_id) or (
            workflow_id and existing_workflow == workflow_id
        ):
            continue
        if scope == mine:
            continue
        try:
            keys = claim_keys(ref)
        except ExperimentLookupError:
            keys = frozenset({ref})
        if wanted & keys:
            raise ExperimentClaimConflict(
                f"experiment {experiment_id!r} is already claimed by another project or user"
            )


def is_visible(experiment_id: str | None, visible: set[str] | None) -> bool:
    """Whether a trace filed under ``experiment_id`` may be shown, given :func:`visible_experiment_ids`."""
    if visible is None:
        return True
    return experiment_id is not None and experiment_id in visible
