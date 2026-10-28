"""The exclusive experiment owner registry behind agent and workflow claims (B11.registry, #665).

Who owns a tracking-backend experiment decides who sees its traces, so it is recorded once, exclusively, on the
backend's own canonical experiment id (:mod:`caliber.observability.trace_access` resolves names and ids to it) in
``caliber_experiment_owners``, keyed by ``(tracking_backend, experiment_id)``. The primary key is the exclusivity:
two projects racing to claim one experiment cannot both insert, whichever alias each used, and the loser is refused
(never overwritten, never "last writer wins").

* **Provenance.** In the reference profile a claim must come from a trusted first claim: either CALIBER *creates* the
  experiment server-side for the claimant (the registry row is written in the same transaction), or an administrator
  *adopts* an existing experiment. Anyone else naming an experiment that already exists and nobody has registered is
  refused (:class:`ExperimentNotClaimable`): an arbitrary pre-existing experiment id cannot be appropriated by the
  first editor to type it. Outside the reference profile an existing experiment is adopted by its first claimant, as
  before, and an experiment that does not exist yet stays a plain reference (no registry row, nothing to own).
* **Legacy claims.** :func:`migrate_legacy_claims` resolves every agent and workflow claim that predates the registry:
  an experiment claimed by one scope becomes a ``legacy`` row; one claimed by two or more becomes a ``quarantined`` row
  with **no owner** (no winner is chosen) until :func:`resolve_quarantine` records an administrator's decision. Until
  the migration has run, the claim-derived visibility of ``trace_access`` still applies, so a foreign payload is denied
  before, during and after it.
* **Authority.** When the registry has a row for an experiment it is authoritative for visibility: a quarantined
  experiment is hidden from everyone but an administrator, and an experiment owned by another scope is hidden from the
  caller even if one of the caller's own agents or workflows names it under another alias
  (:func:`registry_hidden_ids`).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import CaliberExperimentOwner
from caliber.db.transactions import begin_sqlite_transaction
from caliber.observability.trace_access import (
    ExperimentClaimConflict,
    ExperimentLookupError,
    _claim_rows,
    assert_experiment_claim_allowed,
    claim_keys,
    clear_resolution_cache,
    resolve_experiment,
    resolve_many,
)

logger = logging.getLogger("caliber.observability.experiment_registry")

CREATED: Final = "created"
ADOPTED: Final = "adopted"
LEGACY: Final = "legacy"
RESOLVED: Final = "resolved"
ACTIVE: Final = "active"
QUARANTINED: Final = "quarantined"
SCOPE_KINDS: Final = frozenset({"project", "user", "public"})

_DEFAULT_BACKEND = "mlflow"


class ExperimentNotClaimable(ExperimentClaimConflict):
    """The experiment cannot be claimed this way: it is quarantined, or it already exists and has no trusted owner."""


@dataclass(frozen=True)
class ExperimentClaim:
    """A registered claim: the canonical experiment, its owner scope and how the first claim was made."""

    tracking_backend: str
    experiment_id: str
    scope: tuple[str, str]
    provenance: str
    created: bool  # True when this call wrote the row (a repeat claim by the owner is False)


def tracking_backend() -> str:
    """The configured tracking backend's identity: its URI, normalised, with any credentials and query removed.

    Scheme and host are lower-cased and a trailing slash dropped, so two spellings of one server are one backend.
    An error reading the URI is :class:`ExperimentLookupError`: with no identity there is no registry to consult, and
    treating that as "no rows" would fail open.
    """
    try:
        import mlflow  # noqa: PLC0415

        uri = str(mlflow.get_tracking_uri() or "")
    except Exception as exc:
        raise ExperimentLookupError(
            "the tracking backend's identity could not be established, so ownership cannot be decided"
        ) from exc
    if not uri:
        return _DEFAULT_BACKEND
    parts = urlsplit(uri)
    host = (parts.hostname or "").lower()
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))[:512] or _DEFAULT_BACKEND


def _scope_of(visibility: str, project_id: str | None, owner: str) -> tuple[str, str]:
    if visibility == "project":
        return ("project", project_id or "")
    if visibility == "public":
        return ("public", "")
    return ("user", owner)


# ------------------------------------------------------------------ claiming


class _CreateRace(Exception):  # noqa: N818 - control flow inside this module
    """The experiment already existed when we tried to create it."""


def _create_server_side(name: str, scope: tuple[str, str], claimant: str) -> str:
    """Create the experiment in the tracking backend for ``scope`` and return its canonical id."""
    try:
        import mlflow  # noqa: PLC0415
        from mlflow.exceptions import MlflowException  # noqa: PLC0415
    except Exception as exc:
        raise ExperimentLookupError(
            "the tracking backend did not answer, so the experiment's identity could not be established"
        ) from exc
    try:
        created = mlflow.create_experiment(
            name,
            tags={
                "caliber.owner_kind": scope[0],
                "caliber.owner_id": scope[1],
                "caliber.created_by": claimant,
            },
        )
    except MlflowException as exc:
        if getattr(exc, "error_code", "") == "RESOURCE_ALREADY_EXISTS":
            raise _CreateRace from exc
        logger.warning("could not create experiment %r: %s", name, exc)
        raise ExperimentLookupError(
            "the tracking backend did not answer, so the experiment's identity could not be established"
        ) from exc
    return str(created)


def _row(
    backend: str, experiment_id: str, scope: tuple[str, str], provenance: str, claimant: str
) -> CaliberExperimentOwner:
    return CaliberExperimentOwner(
        tracking_backend=backend,
        experiment_id=experiment_id,
        scope_kind=scope[0],
        scope_id=scope[1],
        status=ACTIVE,
        provenance=provenance,
        claimed_by=claimant,
        claimed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _created_for(canonical: str, scope: tuple[str, str]) -> bool:
    """Whether the experiment carries the owner tags CALIBER wrote when it created it for ``scope``."""
    try:
        import mlflow  # noqa: PLC0415

        tags = getattr(mlflow.get_experiment(canonical), "tags", None) or {}
    except Exception:
        return False
    return tags.get("caliber.owner_kind") == scope[0] and tags.get("caliber.owner_id") == scope[1]


def claim_experiment(
    session: Session,
    ref: str,
    *,
    scope: tuple[str, str],
    claimant: str,
    create_if_missing: bool,
    may_adopt: bool,
) -> ExperimentClaim | None:
    """Register ``scope`` as the owner of the experiment ``ref`` names, atomically and exclusively.

    Returns the claim, or ``None`` when ``ref`` names no experiment yet and ``create_if_missing`` is false (a plain
    reference, as outside the reference profile). Raises :class:`ExperimentClaimConflict` when another scope owns
    it, :class:`ExperimentNotClaimable` when it is quarantined or exists without a trusted owner and ``may_adopt`` is
    false, and :class:`ExperimentLookupError` when the backend cannot answer (fail closed). Flushes inside a
    savepoint, never commits: the registry row lives or dies with the caller's transaction, so a claim whose agent
    or workflow row is rolled back leaves no owner behind.
    """
    if scope[0] not in SCOPE_KINDS:
        raise ValueError(f"unknown scope kind {scope[0]!r}")
    canonical = resolve_experiment(ref)  # raises ExperimentLookupError: fail closed
    provenance = ADOPTED
    if canonical is None:
        if not create_if_missing:
            return None
        if ref.isdigit():
            raise ExperimentNotClaimable(
                f"there is no experiment {ref!r}; name the experiment to create"
            )
        try:
            canonical = _create_server_side(ref, scope, claimant)
            provenance = CREATED
        except _CreateRace:
            # It exists after all (a cached "no such experiment" from before it was created, or a concurrent
            # creator): decide as for any existing experiment, by the registry and the caller's right to adopt.
            clear_resolution_cache()
            canonical = resolve_experiment(ref)
            if canonical is None:
                raise ExperimentNotClaimable(
                    f"experiment {ref!r} was created by someone else a moment ago; ask an administrator to adopt it"
                ) from None
        else:
            clear_resolution_cache()  # the cached "no such experiment" is stale now
    backend = tracking_backend()
    existing = session.get(CaliberExperimentOwner, (backend, canonical), populate_existing=True)
    if existing is not None:
        return _answer(existing, scope, ref)
    if provenance == ADOPTED and not may_adopt and _created_for(canonical, scope):
        # CALIBER created this experiment for exactly this scope but the request that did so never committed (the
        # experiment is outside the database's transaction): the same scope may register it, nobody else can.
        provenance = CREATED
    if provenance == ADOPTED and not may_adopt:
        raise ExperimentNotClaimable(
            f"experiment {ref!r} already exists and nobody has registered it; ask CALIBER to create it by name, "
            "or ask an administrator to adopt it"
        )
    begin_sqlite_transaction(session)
    try:
        with session.begin_nested():
            session.add(_row(backend, canonical, scope, provenance, claimant))
    except IntegrityError:
        lost = session.get(CaliberExperimentOwner, (backend, canonical), populate_existing=True)
        if (
            lost is None
        ):  # pragma: no cover - the conflicting row vanished between the two statements
            raise
        return _answer(lost, scope, ref)
    return ExperimentClaim(backend, canonical, scope, provenance, created=True)


def _answer(existing: CaliberExperimentOwner, scope: tuple[str, str], ref: str) -> ExperimentClaim:
    if existing.status == QUARANTINED:
        raise ExperimentNotClaimable(
            f"experiment {ref!r} is quarantined: it was claimed by more than one project or user; "
            "an administrator must resolve it"
        )
    owner = (existing.scope_kind, existing.scope_id)
    if owner != scope:
        raise ExperimentClaimConflict(
            f"experiment {ref!r} is already claimed by another project or user"
        )
    return ExperimentClaim(
        existing.tracking_backend, existing.experiment_id, owner, existing.provenance, created=False
    )


def claim_for_writer(
    session: Session,
    experiment_id: str | None,
    *,
    visibility: str,
    project_id: str | None,
    owner: str,
    agent_id: str | None = None,
    workflow_id: str | None = None,
    reference_profile: bool = False,
    is_admin: bool = False,
) -> ExperimentClaim | None:
    """What an agent or workflow writer calls before it stores an experiment reference.

    First the text-claim check of ``trace_access`` (another scope already binding the experiment under any alias),
    then the registry claim. In the reference profile a missing experiment is created server-side and an existing
    unregistered one only an administrator may adopt; elsewhere an existing experiment is adopted by its claimant.
    """
    if not experiment_id:
        return None
    assert_experiment_claim_allowed(
        session,
        experiment_id,
        visibility=visibility,
        project_id=project_id,
        owner=owner,
        agent_id=agent_id,
        workflow_id=workflow_id,
    )
    return claim_experiment(
        session,
        experiment_id,
        scope=_scope_of(visibility, project_id, owner),
        claimant=owner,
        create_if_missing=reference_profile,
        may_adopt=is_admin or not reference_profile,
    )


# ------------------------------------------------------------------ reading the registry


def _allowed_scopes(identity: Any) -> set[tuple[str, str]]:
    scopes = {("public", ""), ("user", identity.user_id)}
    if identity.active_project_id:
        scopes.add(("project", identity.active_project_id))
    return scopes


def registry_view(session: Session, identity: Any) -> tuple[set[str], set[str], set[str]]:
    """``(hidden, owned, active)`` canonical ids for ``identity`` from one read of the registry.

    ``hidden``: quarantined, or owned by a scope the caller is not acting as. ``owned``: active and owned by a scope
    the caller is acting as. ``active``: has an active owner (the registry has decided it).
    """
    allowed = _allowed_scopes(identity)
    hidden: set[str] = set()
    owned: set[str] = set()
    active: set[str] = set()
    for row in _rows(session):
        experiment = str(row.experiment_id)
        if row.status == QUARANTINED:
            hidden.add(experiment)
            continue
        active.add(experiment)
        if (row.scope_kind, row.scope_id) in allowed:
            owned.add(experiment)
        else:
            hidden.add(experiment)
    return hidden, owned, active


def registry_hidden_ids(session: Session, identity: Any) -> set[str]:
    """Canonical ids the registry says ``identity`` may not see: quarantined, or owned by another scope."""
    allowed = _allowed_scopes(identity)
    hidden: set[str] = set()
    for row in _rows(session):
        if row.status == QUARANTINED or (row.scope_kind, row.scope_id) not in allowed:
            hidden.add(str(row.experiment_id))
    return hidden


def registry_row(session: Session, experiment_id: str) -> CaliberExperimentOwner | None:
    """The registry row of the canonical ``experiment_id`` on the configured backend, of any status."""
    return session.get(
        CaliberExperimentOwner, (tracking_backend(), experiment_id), populate_existing=True
    )


def registry_active_ids(session: Session) -> set[str]:
    """Canonical ids that have an active registry owner: the registry has decided them, whatever the text claims."""
    return {str(row.experiment_id) for row in _rows(session) if row.status == ACTIVE}


def _rows(session: Session) -> list[CaliberExperimentOwner]:
    return list(
        session.execute(
            select(CaliberExperimentOwner).where(
                CaliberExperimentOwner.tracking_backend == tracking_backend()
            )
        ).scalars()
    )


def registry_quarantined(session: Session) -> list[dict[str, object]]:
    """The quarantined registry rows for the configured backend, for an administrator."""
    backend = tracking_backend()
    return [
        {
            "experiment": str(row.experiment_id),
            "claimed_by": row.claimed_by,
            "provenance": row.provenance,
        }
        for row in session.execute(
            select(CaliberExperimentOwner)
            .where(
                CaliberExperimentOwner.tracking_backend == backend,
                CaliberExperimentOwner.status == QUARANTINED,
            )
            .order_by(CaliberExperimentOwner.experiment_id)
        ).scalars()
    ]


# ------------------------------------------------------------------ the legacy migration and the operator's decision


def migrate_legacy_claims(session: Session, *, actor: str) -> dict[str, Any]:
    """Register every pre-registry agent and workflow claim, quarantining the ambiguous ones. Idempotent.

    Returns ``{"registered", "quarantined", "skipped_existing", "mismatched", "unresolved"}`` lists of canonical ids
    (``mismatched``: already registered, but a legacy claim names another scope too; ``unresolved``: references that
    name no experiment yet). Raises
    :class:`ExperimentLookupError` when the backend cannot answer: nothing is registered on a guess. Flushes, never
    commits. No winner is ever chosen among conflicting claims.
    """
    backend = tracking_backend()
    rows = _claim_rows(session)
    resolve_many({ref for ref, *_ in rows})
    by_id: dict[str, set[tuple[str, str]]] = defaultdict(set)
    unresolved: list[str] = []
    for ref, scope, _agent, _workflow in rows:
        canonical = resolve_experiment(ref)
        if canonical is None:
            unresolved.append(ref)
            continue
        # Any alias that intersects another claim's keys is the same experiment, which ``canonical`` already is.
        claim_keys(ref)
        by_id[canonical].add(scope)
    report: dict[str, Any] = {
        "registered": [],
        "quarantined": [],
        "skipped_existing": [],
        "mismatched": [],
        "unresolved": sorted(set(unresolved)),
    }
    begin_sqlite_transaction(session)
    for canonical in sorted(by_id):
        scopes = by_id[canonical]
        existing = session.get(CaliberExperimentOwner, (backend, canonical), populate_existing=True)
        if existing is None:
            if len(scopes) == 1:
                row = _row(backend, canonical, next(iter(scopes)), LEGACY, actor)
            else:
                row = _row(backend, canonical, ("", ""), LEGACY, actor)
                row.status = QUARANTINED
            try:
                with session.begin_nested():
                    session.add(row)
            except IntegrityError:  # another run of the migration (or a claim) registered it first
                existing = session.get(
                    CaliberExperimentOwner, (backend, canonical), populate_existing=True
                )
            else:
                report["quarantined" if row.status == QUARANTINED else "registered"].append(
                    canonical
                )
                continue
        report["skipped_existing"].append(canonical)
        if (
            existing is not None
            and existing.status == ACTIVE
            and scopes != {(existing.scope_kind, existing.scope_id)}
        ):
            report["mismatched"].append(
                canonical
            )  # a claim by another scope than the registered owner
    session.flush()
    return report


def resolve_quarantine(
    session: Session, experiment_id: str, *, scope: tuple[str, str], actor: str
) -> ExperimentClaim:
    """Record an administrator's decision for a quarantined experiment: ``scope`` becomes its owner.

    The decision is explicit and attributed (``resolved_by``); the registry never decides it. A conditional UPDATE
    (``status = 'quarantined'``) makes two administrators racing a decision one winner and one ``LookupError``.
    Raises ``LookupError`` when the experiment is not (or no longer) quarantined, ``ValueError`` for an unknown scope
    kind, an empty project or user id, or a public owner with an id.
    """
    kind, scope_id = scope
    if kind not in SCOPE_KINDS or (kind == "public") != (scope_id == ""):
        raise ValueError("the owner must be a project or user id, or public (with no id)")
    backend = tracking_backend()
    begin_sqlite_transaction(session)
    result = session.execute(
        update(CaliberExperimentOwner)
        .where(
            CaliberExperimentOwner.tracking_backend == backend,
            CaliberExperimentOwner.experiment_id == experiment_id,
            CaliberExperimentOwner.status == QUARANTINED,
        )
        .values(
            status=ACTIVE,
            scope_kind=kind,
            scope_id=scope_id,
            provenance=RESOLVED,
            resolved_by=actor,
            resolved_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        raise LookupError("that experiment is not quarantined")
    session.get(
        CaliberExperimentOwner, (backend, experiment_id), populate_existing=True
    )  # refresh the row
    return ExperimentClaim(backend, experiment_id, scope, RESOLVED, created=False)


__all__ = [
    "ACTIVE",
    "ADOPTED",
    "CREATED",
    "LEGACY",
    "QUARANTINED",
    "RESOLVED",
    "SCOPE_KINDS",
    "ExperimentClaim",
    "ExperimentNotClaimable",
    "claim_experiment",
    "claim_for_writer",
    "migrate_legacy_claims",
    "registry_active_ids",
    "registry_hidden_ids",
    "registry_quarantined",
    "registry_row",
    "registry_view",
    "resolve_quarantine",
    "tracking_backend",
]
