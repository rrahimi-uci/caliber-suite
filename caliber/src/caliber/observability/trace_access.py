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
import random
import threading
import time
from collections import defaultdict
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import CaliberAgentConfig, CaliberWorkflow
from caliber.db.scoping import apply_visibility_filter
from caliber.profile import GOVERNED_TRACE_FIELDS
from caliber.trace_client import (
    TraceNotFoundError,
    TraceStoreUnavailableError,
    fetch_trace_detail,
)

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


class ExperimentLookupError(TraceStoreUnavailableError):
    """The tracking backend could not say whether an experiment reference exists, or which one it is.

    Claims, quarantine and visibility fail closed on it: an ownership decision is never made on a guess. It is a
    :class:`~caliber.trace_client.TraceStoreUnavailableError`, so every route that already answers 503 for an
    unreachable trace store answers it here. The message is deliberately generic: the backend's own error (a
    host, a port, a URL with credentials) is logged, never sent to a caller.
    """


_LOOKUP_FAILED = (
    "the tracking backend did not answer, so the experiment's identity could not be established"
)

#: How long a resolved reference is trusted, with up to +-20% jitter so a scan that filled the cache all at
#: once does not expire it all at once. Resolution is one MLflow call per distinct reference (or one listing,
#: below) and the conflict scan resolves every claim, so a short cache keeps a trace list from becoming an
#: N-call fan-out; it is short enough that a renamed experiment is noticed within seconds.
_RESOLUTION_TTL_SECONDS = 15.0
#: A failure is remembered briefly, so a backend that is down costs one probe per window, not one per request.
_FAILURE_TTL_SECONDS = 2.0
#: The cache is bounded: an editor can try arbitrary strings as claims.
_CACHE_LIMIT = 5000
#: At or above this many uncached references one listing of every experiment replaces one call per reference.
_BULK_THRESHOLD = 20
_LISTING_PAGE = 1000
_JITTER = 0.2

#: ``ref -> (expires_at, canonical id or None, failed)``
_resolutions: dict[str, tuple[float, str | None, bool]] = {}
_resolutions_lock = threading.Lock()


def clear_resolution_cache() -> None:
    """Forget every cached resolution (tests, and a caller that has just created an experiment)."""
    with _resolutions_lock:
        _resolutions.clear()


def _remember(ref: str, value: str | None, *, failed: bool = False) -> None:
    ttl = (
        _FAILURE_TTL_SECONDS
        if failed
        else _RESOLUTION_TTL_SECONDS * random.uniform(1 - _JITTER, 1 + _JITTER)  # noqa: S311
    )
    with _resolutions_lock:
        if ref not in _resolutions and len(_resolutions) >= _CACHE_LIMIT:
            _resolutions.pop(next(iter(_resolutions)))  # the oldest entry
        _resolutions[ref] = (time.monotonic() + ttl, value, failed)


def resolve_experiment(ref: str) -> str | None:
    """The canonical numeric experiment id of ``ref`` (a name or an id), or ``None`` when no such experiment.

    Canonical identity is the id the configured tracking backend assigns: a trace carries only that id, so
    ownership has to be decided on it, whichever alias an agent or workflow named. Raises
    :class:`ExperimentLookupError` when the backend cannot answer; "no such experiment" is an answer.
    """
    with _resolutions_lock:
        cached = _resolutions.get(ref)
    if cached is not None and time.monotonic() < cached[0]:
        if cached[2]:
            raise ExperimentLookupError(_LOOKUP_FAILED)
        return cached[1]
    try:
        resolved = _lookup_experiment(ref)
    except ExperimentLookupError:
        _remember(ref, None, failed=True)
        raise
    _remember(ref, resolved)
    return resolved


def resolve_many(refs: set[str] | frozenset[str]) -> None:
    """Warm the cache for ``refs``: one listing of every experiment when many are uncached, else nothing.

    The conflict scan resolves every claim in the database; on a deployment with thousands, one call per claim
    every time the cache expires is minutes of MLflow round trips. A complete listing answers all of them
    exactly (an id or a name the listing lacks does not exist). Any failure is left for the per-reference path,
    which reports it.
    """
    now = time.monotonic()
    with _resolutions_lock:
        missing = [r for r in refs if (_resolutions.get(r) or (0.0,))[0] <= now]
    if len(missing) < _BULK_THRESHOLD:
        return
    try:
        from mlflow import MlflowClient  # noqa: PLC0415
        from mlflow.entities import ViewType  # noqa: PLC0415

        client = MlflowClient()
        ids: set[str] = set()
        by_name: dict[str, str] = {}
        token = None
        while True:
            page = client.search_experiments(
                view_type=ViewType.ALL, max_results=_LISTING_PAGE, page_token=token
            )
            for experiment in page:
                ids.add(str(experiment.experiment_id))
                by_name[str(experiment.name)] = str(experiment.experiment_id)
            token = getattr(page, "token", None)
            if not token:
                break
    except Exception:
        logger.warning("could not list experiments to resolve claims in bulk", exc_info=True)
        return
    for ref in missing:
        _remember(ref, (ref if ref in ids else None) if ref.isdigit() else by_name.get(ref))


def _lookup_experiment(ref: str) -> str | None:
    try:
        import mlflow  # noqa: PLC0415
        from mlflow.exceptions import MlflowException  # noqa: PLC0415
    except Exception as exc:
        logger.warning("the tracking client is not available", exc_info=True)
        raise ExperimentLookupError(_LOOKUP_FAILED) from exc
    experiment: Any
    try:
        if ref.isdigit():
            experiment = mlflow.get_experiment(ref)
        else:
            experiment = mlflow.get_experiment_by_name(ref)
    except MlflowException as exc:
        if getattr(exc, "error_code", "") == "RESOURCE_DOES_NOT_EXIST":
            return None
        logger.warning("could not resolve experiment %r: %s", ref, exc)
        raise ExperimentLookupError(_LOOKUP_FAILED) from exc
    except Exception as exc:
        logger.warning("could not resolve experiment %r: %s", ref, exc)
        raise ExperimentLookupError(_LOOKUP_FAILED) from exc
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
    rows = _claim_rows(session)
    resolve_many({ref for ref, *_ in rows})
    for ref, scope, _agent, _workflow in rows:
        for key in claim_keys(
            ref
        ):  # raises ExperimentLookupError: an unresolvable claim could alias anything
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
        for key in sorted(claim_keys(ref) & contested.keys()):
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
    claim under any alias is quarantined and absent for everyone but an admin. If the backend cannot resolve a
    claim, the answer is :class:`ExperimentLookupError` (a 503 on the routes): an unresolvable claim could alias
    any experiment, so neither visibility nor quarantine can be decided.
    """
    from caliber.observability.experiment_registry import registry_view  # noqa: PLC0415 - import cycle

    claims = visible_experiment_ids(session, identity)
    if claims is None:
        return None
    resolve_many(claims)
    contested = _contested_keys(session)
    # The registry, where it has a row, is authoritative: an experiment owned by another scope, or quarantined, is
    # not the caller's to see under any alias it claims; one the registry gave to the caller's scope is visible
    # even where old text claims still collide; and the owner needs no text claim at all.
    hidden, owned, active = registry_view(session, identity)
    visible: set[str] = set()
    for ref in claims:
        keys = claim_keys(ref)
        if keys & hidden:
            continue
        if keys & active or not keys & contested.keys():
            visible |= keys
    visible |= owned
    return visible


def _owner_scope(session: Session, experiment_id: str) -> tuple[str, str] | None:
    """The single scope that owns ``experiment_id`` (a name or an id), or ``None`` when none can be established.

    The reference must name an existing experiment (a claim on a name that does not exist yet gives no payload
    access: the experiment, once created, could be anyone's). The registry decides where it has a row (a quarantined
    one means no owner); for an experiment it has no row for (development, or before the legacy migration) the agent
    and workflow claims decide, but only when they all name one scope. No claim, or a claim by two scopes, is "no
    owner". Raises :class:`ExperimentLookupError` when the backend cannot say what the reference means.
    """
    from caliber.observability.experiment_registry import registry_row  # noqa: PLC0415 - import cycle

    canonical = resolve_experiment(experiment_id)
    if canonical is None:
        return None  # payloads exist only for an experiment that exists; a claim on a name is no ownership of it yet
    row = registry_row(session, canonical)
    if row is not None:
        return (row.scope_kind, row.scope_id) if row.status == "active" else None
    keys = claim_keys(experiment_id)
    scopes = {
        scope for ref, scope, _agent, _workflow in _claim_rows(session) if claim_keys(ref) & keys
    }
    return next(iter(scopes)) if len(scopes) == 1 else None


def can_read_payloads(session: Session, identity: CaliberIdentity, experiment_id: str) -> bool:
    """Whether ``identity`` may read the *payloads* (span inputs, outputs, arguments) of traces in ``experiment_id``.

    Seeing that a trace exists is :func:`visible_experiments`; reading what is in it is stricter, because payloads
    can hold personal data. The experiment's owner scope is resolved (:func:`_owner_scope`) and then:

    * a project: the caller must be *acting in that project* (the active project, and the project a project-bound
      credential is bound to, are both that project, as :func:`visible_experiments` already requires), and their
      *current* membership role must allow ``trace.payload.read`` (owner or editor), through
      :func:`caliber.resource_access.decide_project_access`. A reviewer, a viewer, a removed or inactive member, and
      ``caliber.admin`` without membership (not an implicit project role, the P1-B rule) are refused. An archived
      project keeps its members' access, as every other read path does;
    * a user: only that user;
    * public, no owner, a quarantined or contested experiment, a reference that names no existing experiment, a
      project that no longer exists, an unreachable tracking backend or database, and anything that is not a string
      are all refused: the answer is ``False``, never a guess. Platform scopes (``caliber.viewer`` and so on) are not
      checked here: the calling route must still require them.
    """
    if type(experiment_id) is not str or not experiment_id:
        return False
    try:
        owner = _owner_scope(session, experiment_id)
        return owner is not None and _owner_allows_payloads(session, identity, owner)
    except ExperimentLookupError:
        logger.warning("payload permission denied: the experiment could not be resolved")
    except Exception:  # a database error is a refusal, not a 500 and not an allow
        logger.warning(
            "payload permission denied: the owner could not be established", exc_info=True
        )
    return False


def _owner_allows_payloads(
    session: Session, identity: CaliberIdentity, owner: tuple[str, str]
) -> bool:
    from caliber.db.models import CaliberProject  # noqa: PLC0415
    from caliber.resource_access import decide_project_access  # noqa: PLC0415

    kind, scope_id = owner
    if kind == "user":
        return identity.user_id == scope_id
    if kind != "project":
        return False  # public (or an unknown kind): no payload readers
    bound_elsewhere = (
        identity.credential_project_id is not None and identity.credential_project_id != scope_id
    )
    if identity.active_project_id != scope_id or bound_elsewhere:
        return False  # acting in another project, or on a credential bound to another one
    project = session.get(CaliberProject, scope_id)
    return decide_project_access(session, identity, project, "trace.payload.read").allowed


#: Keys of a trace's detail or summary that carry a request or response as a whole (not span fields).
_ROOT_PAYLOAD_KEYS: Final = ("request", "response", "request_preview", "response_preview")


def redacted(field: str) -> str:
    """The placeholder that stands for a governed field's value."""
    return f"[redacted:{field}]"


def _redact_governed(target: dict[str, Any]) -> dict[str, Any]:
    """``target`` with every governed field that is *present* replaced by its placeholder (keys are never removed
    or added, so the shape of the trace is unchanged)."""
    out = dict(target)
    for path in GOVERNED_TRACE_FIELDS:
        head, _, leaf = path.rpartition(".")
        if not head:
            if path in out:
                out[path] = redacted(path)
            continue
        container = out.get(head)
        if isinstance(container, dict) and leaf in container:
            out[head] = {**container, leaf: redacted(path)}
    return out


def redact_trace_detail(detail: dict[str, Any], allowed: bool) -> dict[str, Any]:
    """The trace detail (or list summary) as ``allowed`` says it may be seen.

    ``allowed`` is :func:`can_read_payloads` for the trace's experiment. When it is true the detail is returned
    unchanged. When it is false every governed field (``profile.GOVERNED_TRACE_FIELDS``) of the detail and of each
    of its spans, and the whole-trace ``request``/``response`` and their previews, become ``[redacted:<field>]``;
    keys are never deleted, and everything else (ids, names, times, status, tokens, cost, run id) is untouched. A
    new dict is returned: the argument is not modified.
    """
    if allowed:
        return detail
    out = _redact_governed(detail)
    for key in _ROOT_PAYLOAD_KEYS:
        if key in out:
            out[key] = redacted(key)
    spans = out.get("spans")
    if isinstance(spans, list):
        out["spans"] = [
            _redact_governed(span) if isinstance(span, dict) else span for span in spans
        ]
    return out


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
    rows = _claim_rows(session)
    resolve_many({ref for ref, *_ in rows})
    for ref, scope, existing_agent, existing_workflow in rows:
        if (agent_id and existing_agent == agent_id) or (
            workflow_id and existing_workflow == workflow_id
        ):
            continue
        if scope == mine:
            continue
        # Raises ExperimentLookupError when another scope's claim cannot be resolved: it could be the very
        # experiment being claimed, so the claim is refused rather than compared as written.
        if wanted & claim_keys(ref):
            raise ExperimentClaimConflict(
                f"experiment {experiment_id!r} is already claimed by another project or user"
            )


def is_visible(experiment_id: str | None, visible: set[str] | None) -> bool:
    """Whether a trace filed under ``experiment_id`` may be shown, given :func:`visible_experiment_ids`."""
    if visible is None:
        return True
    return experiment_id is not None and experiment_id in visible
