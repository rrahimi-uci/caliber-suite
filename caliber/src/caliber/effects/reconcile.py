"""The reconciler: only the execution owner asks the downstream about unknown and abandoned effects (B05.5b, #742; ADR 0003).

An effect whose outcome nobody recorded is ``unknown``, and an attempt whose dispatcher died (or lost its lease) while it was
``dispatched`` is *abandoned*: it may have been sent, and the process that knew is gone. Neither is ever dispatched again.
They are settled the one way ADR 0003 allows, by asking the downstream by the **original logical key** and believing only
what it can prove:

* **Owner-only.** Every pass needs the :class:`~caliber.ownership.Lease` the owner captured, and every write it makes
  re-compares that lease (exact owner and fencing token, the database's clock) in the transaction that writes
  (:func:`caliber.effects.records.fence_owner`). A reconciler whose lease was taken over records nothing; the new owner's pass
  picks the same proposals up.
* **Abandoned means a prior lease.** A ``dispatched`` proposal is abandoned exactly when its ``dispatch_token`` (the fencing
  token it was dispatched under, migration 0126) is lower than the token of the lease the reconciler holds now: the lease that
  dispatched it has been taken over. One dispatched under the current token is in flight in this very process and is left
  alone (``in_flight``) *until it has been in flight longer than the caller's bound* (``in_flight_seconds``, B05.5d, #997): a
  dispatcher that kept its lease but could not record the outcome would otherwise leave it ``dispatched`` for good, so past the
  bound it becomes ``unknown`` too (:func:`caliber.effects.records.expire_in_flight`); one with no recorded token proves nothing
  and is never abandoned. An abandoned attempt becomes ``unknown`` (:func:`caliber.effects.records.abandon_dispatch`) and is
  then handled like any other unknown one.
* **Only authoritative terminal evidence moves it.** The lookup runs outside any transaction, by the proposal's original
  logical key (the idempotency key the dispatch emitted), against the connector's own query, and its answer is read by the
  connector's capability record (:func:`caliber.effects.connectors.interpret_lookup`):

  - a stored request that is *this proposal's effect* (key, intent, target, expected version: the same check as a receipt
    callback) becomes a verified receipt and the proposal ``committed`` (``unknown`` goes through ``reconciled``);
  - a 404 is ``failed`` only for a connector that **proves absence is final** (a tombstone or a cancel it honours, and a
    lookup that reads its own writes). For every other connector, the document-request one included, a 404 is delayed
    visibility as far as anyone can tell and the proposal stays ``unknown``;
  - anything else (a 5xx, an unreadable or mismatching body, a downstream that cannot be reached, a connector with no
    binding) leaves it ``unknown``, retried on the next pass.
* **No blind redrive.** Nothing here sends the effect: there is no edge from ``unknown`` back to ``dispatched``, so a crash
  before the send, after the downstream committed, or before the receipt was persisted all end the same way: a lookup by key,
  never a second request.
* **Unknown blocks what depends on it.** :func:`effect_gate` is the check the runtime makes before it runs a node (at every
  attempt, and before a guardrail re-runs its agents): while an effect the node, anything upstream of it, or any of their inline
  children (a fan-out, loop or boundary's target and compensation, an agent's handoff targets) asked for is ``dispatched``,
  ``unknown`` or ``reconciled``, the node does not run (:class:`UnknownEffectError`, naming what to reconcile) and nothing
  absorbs the refusal. No write that depends on an effect nobody can vouch for is made. The queued-run worker persists a
  same-run ``waiting_effect`` checkpoint and the reconciler wakes it only after every dependency is terminal.

Where the connector lives is the callback binding's ``base_url`` (``CALIBER_EFFECT_CALLBACK_CONNECTORS``): the same
``(connector, project)`` that may report a receipt is the one the reconciler asks. The generic ``http`` connector (governed
webhook and API-request nodes) has no such binding: its downstream is the lookup endpoint an operator listed for the
proposal's own resource in ``CALIBER_EFFECT_HTTP_VERIFIERS`` (:mod:`caliber.effects.connectors.http_lookup`, #1055), so an
``http`` effect that ended ``unknown`` (a timeout after the send, a 2xx nobody could confirm, a failed settle, an abandoned
dispatch) is settled by the same lookup by key that would have confirmed it, and one whose resource is not listed stays
``not_bound``. A lookup that finds nothing is never a failure for this connector. Scheduling a pass is the owner loop's
(:class:`caliber.orchestrator.effect_reconciler.EffectReconcilerTask`, B05.5d, #997: it holds the lease the controller holds,
calls :func:`reconcile_pass_from` with a budget and a lease check before each proposal, and stops when the lease is gone); the
dispatch itself is the dispatcher's (:mod:`caliber.effects.dispatcher`).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
)
from caliber.effects.callbacks import CallbackBinding, parse_bindings
from caliber.effects.connectors import ConnectorCapability, interpret_lookup
from caliber.effects.connectors import document_request as _document_request
from caliber.effects.connectors import http_lookup as _http_lookup
from caliber.effects.http_verifiers import parse_endpoints, target_key, targets_from_endpoints
from caliber.effects.receipts import RECEIPT_STATE, ReceiptFacts, record_receipt
from caliber.effects.records import (
    COMMITTED,
    DISPATCHED,
    FAILED,
    IN_FLIGHT_EXPIRED_REASON,
    LEASE_LOST,
    NOT_ABANDONED,
    NOT_EXPIRED,
    RECONCILED,
    UNKNOWN,
    ProposalError,
    ProposalState,
    ProposalView,
    StaleVersion,
    abandon_dispatch,
    expire_in_flight,
    fence_owner,
    proposal_state,
    proposal_view,
    transition,
)
from caliber.integrations.openapi.executor import OpenApiExecutionError, execute_openapi_http_tool
from caliber.ownership import Lease
from caliber.workflows.run_events import append_run_event
from caliber.workflows.run_state import (
    RUN_STATUS_QUEUED,
    RUN_STATUS_WAITING_EFFECT,
    assert_run_transition,
)

logger = logging.getLogger("caliber.effects.reconcile")

#: Outcomes of reconciling one proposal.
RECONCILED_COMMITTED: Final = "committed"
RECONCILED_FAILED: Final = "failed"
STILL_UNKNOWN: Final = "unknown"
UNVERIFIED: Final = "unverified"
LOOKUP_FAILED: Final = "lookup_failed"
NOT_BOUND: Final = "not_bound"
IN_FLIGHT: Final = "in_flight"
MOVED: Final = "moved"

#: The reason recorded when a connector proved absence final.
ABSENCE_PROVEN_REASON: Final = "absence_proven"
#: The reason recorded on the ``reconciled`` move the reconciler makes.
RECONCILER_REASON: Final = "reconciler"
#: The states in which an effect has been (or may have been) asked for and nobody has the final word.
OPEN_STATES: Final = (DISPATCHED, UNKNOWN, RECONCILED)
DEFAULT_PASS_LIMIT: Final = 50
_PROOF_REASON: Final = f"{RECONCILER_REASON}:proof"


class UnknownEffectError(RuntimeError):
    """An effect this node depends on (or asked for earlier) has no final outcome yet: reconcile it first."""

    def __init__(
        self,
        open_effects: Sequence[tuple[str, str, str]],
        *,
        effect_details: Sequence[EffectDependency] | None = None,
    ) -> None:
        self.open_effects = tuple(open_effects)
        self.effect_details = tuple(effect_details or ())
        self.blocked_node_id: str | None = None
        self.blocked_inputs: dict[str, Any] | None = None
        listing = ", ".join(
            f"{pid} (node {node}, {state})" for pid, node, state in self.open_effects
        )
        super().__init__(
            f"effect {listing} has no final outcome: reconcile it (a lookup by its key) before any step that depends on it runs"
        )


@dataclass(frozen=True)
class EffectDependency:
    """The identity evidence a parked run must retain for one open proposal."""

    proposal_id: str
    node_id: str
    state: str
    logical_key: str
    intent_hash: str


def _by_connector(view: ProposalView) -> str:
    return view.connector


@dataclass(frozen=True)
class ConnectorLookup:
    """What the reconciler needs to ask one connector and read its answer.

    ``target_key`` names where, in :data:`Targets`, the downstream of one proposal is: the connector's name for a connector with
    one downstream per project (the callback binding's ``base_url``), or a name that carries the proposal's own resource for the
    generic ``http`` connector, whose lookups are listed endpoint by endpoint (:mod:`caliber.effects.connectors.http_lookup`).
    """

    capability: ConnectorCapability
    config: Callable[[str], dict[str, Any]]
    arguments: Callable[[str], dict[str, Any]]
    facts: Callable[..., ReceiptFacts | None]
    target_key: Callable[[ProposalView], str] = _by_connector


#: The connectors the platform can reconcile: the document-request connector (one downstream per project) and the generic
#: ``http`` connector (the endpoints an operator listed, #1055).
CONNECTORS: Final[Mapping[str, ConnectorLookup]] = {
    _document_request.CONNECTOR: ConnectorLookup(
        capability=_document_request.CAPABILITY,
        config=_document_request.lookup_config,
        arguments=_document_request.lookup_input,
        facts=_document_request.facts_from_lookup,
    ),
    _http_lookup.CONNECTOR: ConnectorLookup(
        capability=_http_lookup.CAPABILITY,
        config=_http_lookup.lookup_config,
        arguments=_http_lookup.lookup_input,
        facts=_http_lookup.facts_from_lookup,
        target_key=lambda view: target_key(view.resource),
    ),
}


@dataclass(frozen=True)
class Answer:
    """What the downstream said to a lookup by key."""

    status_code: int
    body: Any


Lookup = Callable[[ConnectorLookup, str, str], Answer]
#: What opens a session: a ``sessionmaker``, or any callable that returns one (the owner loop bounds each transaction's waits).
SessionFactory = Callable[[], Session]
Targets = Mapping[tuple[str, str], str]


@dataclass(frozen=True)
class Reconciliation:
    """What one proposal's reconciliation did."""

    proposal_id: str
    outcome: str
    state: str
    detail: str | None = None


def targets_from_bindings(bindings: Sequence[CallbackBinding]) -> Targets:
    """``(connector, project)`` to the base URL the downstream is asked at."""
    return {(b.connector, b.project_id): b.base_url for b in bindings}


def targets_from_config(config: object) -> Targets:
    """Every downstream the reconciler may ask, from the operator's settings and nothing else: the callback bindings
    (``CALIBER_EFFECT_CALLBACK_CONNECTORS``) and the generic ``http`` endpoints (``CALIBER_EFFECT_HTTP_VERIFIERS``).

    A setting that does not parse is ``ValueError`` (the configuration validates both at startup). A missing attribute is the
    empty list, so a configuration that predates a setting still works.
    """
    targets = dict(
        targets_from_bindings(parse_bindings(getattr(config, "effect_callback_connectors", "[]")))
    )
    targets.update(
        targets_from_endpoints(parse_endpoints(getattr(config, "effect_http_verifiers", "[]")))
    )
    return targets


def ask_downstream(connector: ConnectorLookup, base_url: str, key: str) -> Answer:
    """The lookup by key through the connector's own query (the OpenAPI executor, so egress policy applies)."""
    found = execute_openapi_http_tool(
        execution_config=connector.config(base_url),
        input_schema=None,
        input_data=connector.arguments(key),
    )
    return Answer(found["status_code"], found["json"])


def commit_found(
    session: Session,
    proposal_id: str,
    facts: ReceiptFacts,
    *,
    reason: str,
    now: datetime | None = None,
) -> tuple[str, bool]:
    """Record the verified receipt and commit (``unknown`` goes through ``reconciled`` first), in the caller's transaction.

    Returns the receipt id and whether *this* call did the commit (``False`` if another actor already had).
    """
    state = proposal_state(session, proposal_id)
    if state.state == COMMITTED and state.receipt_id is not None:
        return state.receipt_id, False
    receipt = record_receipt(session, proposal_id, facts, now=now)
    version = state.version
    if state.state == UNKNOWN:
        version = transition(
            session, proposal_id, RECONCILED, expected_version=version, reason=reason, now=now
        ).version
    transition(
        session,
        proposal_id,
        COMMITTED,
        expected_version=version,
        receipt_id=receipt.receipt_id,
        now=now,
    )
    return receipt.receipt_id, True


def _candidate_page(
    session: Session, lease: Lease, limit: int, after: str | None, *, in_flight: bool = False
) -> list[str]:
    """Up to ``limit`` candidate ids greater than ``after``, in id order (a keyset on the primary key: never the same row twice,
    never skipping one, and no comparison of timestamps, which SQLite stores at another precision than it binds).

    ``in_flight`` also lists the ``dispatched`` rows of the lease's *own* token, which :func:`_take_over` then expires or
    leaves alone by their age (the age is compared in Python, not here)."""
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    token = CaliberEffectProposal.dispatch_token
    query = select(CaliberEffectProposal.proposal_id).where(
        or_(
            CaliberEffectProposal.state.in_((UNKNOWN, RECONCILED)),
            and_(
                CaliberEffectProposal.state == DISPATCHED,
                token <= lease.fencing_token if in_flight else token < lease.fencing_token,
            ),
        )
    )
    if after is not None:
        query = query.where(CaliberEffectProposal.proposal_id > after)
    return [
        str(proposal_id)
        for proposal_id in session.execute(
            query.order_by(CaliberEffectProposal.proposal_id).limit(limit)
        ).scalars()
    ]


def candidates(
    session: Session, lease: Lease, *, limit: int = DEFAULT_PASS_LIMIT, in_flight: bool = False
) -> list[str]:
    """The first ``limit`` proposals this owner should look at, in id order: ``unknown`` and ``reconciled`` ones, and
    ``dispatched`` ones whose dispatching lease has been taken over (a lower ``dispatch_token``); with ``in_flight`` also
    the ``dispatched`` ones of the current token (see :func:`reconcile_pass`)."""
    return _candidate_page(session, lease, limit, None, in_flight=in_flight)


def _project_of(session: Session, proposal_id: str) -> str | None:
    return session.execute(
        select(CaliberWorkflowRun.project_id)
        .join(
            CaliberEffectProposal,
            CaliberEffectProposal.workflow_run_id == CaliberWorkflowRun.workflow_run_id,
        )
        .where(CaliberEffectProposal.proposal_id == proposal_id)
    ).scalar_one_or_none()


def _in_flight_cutoff(in_flight_seconds: float, now: datetime | None) -> datetime:
    moment = datetime.now(timezone.utc) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment - timedelta(seconds=in_flight_seconds)


def _check_in_flight(in_flight_seconds: float | None) -> float | None:
    if in_flight_seconds is None:
        return None
    if (
        isinstance(in_flight_seconds, bool)
        or not isinstance(in_flight_seconds, int | float)
        or not math.isfinite(in_flight_seconds)
        or in_flight_seconds <= 0
    ):
        raise ValueError("in_flight_seconds must be a positive, finite number of seconds")
    return float(in_flight_seconds)


def _take_over(
    factory: SessionFactory,
    proposal_id: str,
    lease: Lease,
    now: datetime | None,
    in_flight_seconds: float | None = None,
) -> Reconciliation | None:
    """Abandon a dispatch whose lease is gone, or expire one of the current lease that has been in flight too long (``unknown``
    from here on), or say why it is left alone; ``None`` to go on."""
    with factory() as session:
        state = proposal_state(session, proposal_id)
        if state.state in (COMMITTED, FAILED):
            return Reconciliation(proposal_id, MOVED, state.state)
        if state.state != DISPATCHED:
            return None
        try:
            try:
                abandon_dispatch(
                    session, proposal_id, expected_version=state.version, lease=lease, now=now
                )
            except ProposalError as exc:
                # (StaleVersion is a ProposalError: the code decides, so it falls through to the outer handler)
                if exc.code != NOT_ABANDONED or in_flight_seconds is None:
                    raise
                session.rollback()
                expire_in_flight(
                    session,
                    proposal_id,
                    expected_version=state.version,
                    lease=lease,
                    older_than=_in_flight_cutoff(in_flight_seconds, now),
                    now=now,
                )
                _audit_in_flight_expiry(session, lease, proposal_id, state)
                logger.warning(
                    "effect %s was dispatched under the current lease (token %s) and nobody settled it within %.0fs: "
                    "recorded unknown (%s); a lookup by key follows",
                    proposal_id,
                    lease.fencing_token,
                    in_flight_seconds,
                    IN_FLIGHT_EXPIRED_REASON,
                )
            session.commit()
        except StaleVersion:  # (a ProposalError too: it is caught first)
            session.rollback()
            return Reconciliation(proposal_id, MOVED, proposal_state(session, proposal_id).state)
        except ProposalError as exc:
            session.rollback()
            if exc.code not in (NOT_ABANDONED, NOT_EXPIRED):
                raise
            return Reconciliation(proposal_id, IN_FLIGHT, DISPATCHED)
    return None


def reconcile_one(
    factory: SessionFactory,
    proposal_id: str,
    *,
    lease: Lease,
    targets: Targets,
    lookup: Lookup = ask_downstream,
    in_flight_seconds: float | None = None,
    now: datetime | None = None,
) -> Reconciliation:
    """Settle one proposal if the downstream can prove its outcome, under ``lease``, or leave it ``unknown`` and say why.

    ``ProposalError`` ``lease_lost`` (the lease was taken over) is raised, not reported: the pass is over. Everything else a
    downstream can do is an outcome (see the module docstring). ``in_flight_seconds`` is the bound of :func:`reconcile_pass`.
    """
    in_flight_seconds = _check_in_flight(in_flight_seconds)
    over = _take_over(factory, proposal_id, lease, now, in_flight_seconds)
    if over is not None:
        return over
    with factory() as session:
        # no questions are asked on a lease that is gone
        fence_owner(session, proposal_id, lease.owner_id, lease)
        view = proposal_view(session, proposal_id)
        seen = proposal_state(session, proposal_id)
        project_id = _project_of(session, proposal_id)
    if seen.state not in (UNKNOWN, RECONCILED):
        return Reconciliation(proposal_id, MOVED, seen.state)
    connector = CONNECTORS.get(view.connector)
    base_url = (
        None if connector is None else targets.get((connector.target_key(view), project_id or ""))
    )
    if connector is None or base_url is None:
        return Reconciliation(proposal_id, NOT_BOUND, seen.state, view.connector)
    try:
        answer = lookup(connector, base_url, view.logical_key)
    except OpenApiExecutionError as exc:
        return Reconciliation(proposal_id, LOOKUP_FAILED, seen.state, type(exc).__name__)
    result = _settle(factory, view, seen.version, connector, answer, lease, now)
    if result.state in (COMMITTED, FAILED):
        wake_parked_runs(factory, view.proposal_id, settled_state=result.state)
    return result


def _settle(
    factory: SessionFactory,
    view: ProposalView,
    seen_version: int,
    connector: ConnectorLookup,
    answer: Answer,
    lease: Lease,
    now: datetime | None,
) -> Reconciliation:
    verdict = interpret_lookup(connector.capability, answer.status_code)
    with factory() as session:
        # compared in the transaction that writes
        fence_owner(session, view.proposal_id, lease.owner_id, lease)
        state = proposal_state(session, view.proposal_id)
        if state.version != seen_version or state.state not in (UNKNOWN, RECONCILED):
            return Reconciliation(view.proposal_id, MOVED, state.state)
        if verdict == "answered":
            facts = connector.facts(view, lookup_status=answer.status_code, lookup_body=answer.body)
            if facts is None:
                # a stored request that is not this effect (or none at all) is not a receipt
                return Reconciliation(view.proposal_id, UNVERIFIED, state.state)
            committed = _commit_unless_moved(session, view.proposal_id, facts, now)
            if (
                committed is None
            ):  # the callback route (or another reconciler) got there between the read and the write
                session.rollback()
                return Reconciliation(
                    view.proposal_id, MOVED, proposal_state(session, view.proposal_id).state
                )
            receipt_id, performed = committed
            if performed:
                _audit(session, lease, view.proposal_id, COMMITTED, {"receipt_id": receipt_id})
            session.commit()
            return Reconciliation(view.proposal_id, RECONCILED_COMMITTED, COMMITTED, receipt_id)
        if verdict == "not_committed":
            version = state.version
            if state.state == UNKNOWN:
                version = transition(
                    session,
                    view.proposal_id,
                    RECONCILED,
                    expected_version=version,
                    reason=_PROOF_REASON,
                    now=now,
                ).version
            transition(
                session,
                view.proposal_id,
                FAILED,
                expected_version=version,
                reason=ABSENCE_PROVEN_REASON,
                now=now,
            )
            _audit(session, lease, view.proposal_id, FAILED, {"reason": ABSENCE_PROVEN_REASON})
            session.commit()
            return Reconciliation(
                view.proposal_id, RECONCILED_FAILED, FAILED, ABSENCE_PROVEN_REASON
            )
    return Reconciliation(
        view.proposal_id, STILL_UNKNOWN, state.state, f"http_{answer.status_code}"
    )


def _commit_unless_moved(
    session: Session, proposal_id: str, facts: ReceiptFacts, now: datetime | None
) -> tuple[str, bool] | None:
    """:func:`commit_found`, or ``None`` if another actor settled the proposal after it was read (nothing is kept)."""
    try:
        return commit_found(session, proposal_id, facts, reason=RECONCILER_REASON, now=now)
    except StaleVersion:  # (a ProposalError too: it is caught first)
        return None
    except ProposalError as exc:
        if exc.code != RECEIPT_STATE:
            raise
        return None


def _audit_in_flight_expiry(
    session: Session, lease: Lease, proposal_id: str, seen: ProposalState
) -> None:
    audit_record(
        session,
        actor=f"reconciler:{lease.owner_id}",
        action="effect_in_flight_expired",
        entity_type="effect_proposal",
        entity_id=proposal_id,
        details={
            "state": UNKNOWN,
            "reason": IN_FLIGHT_EXPIRED_REASON,
            "dispatch_token": seen.dispatch_token,
            "dispatched_at": seen.dispatched_at.isoformat() if seen.dispatched_at else None,
        },
    )


def _audit(
    session: Session, lease: Lease, proposal_id: str, state: str, details: dict[str, Any]
) -> None:
    audit_record(
        session,
        actor=f"reconciler:{lease.owner_id}",
        action="effect_reconciled",
        entity_type="effect_proposal",
        entity_id=proposal_id,
        details={"state": state, **details},
    )


@dataclass(frozen=True)
class PassResult:
    """What one pass did and where the next one should start.

    ``resume_after`` is the id of the last proposal looked at when the pass ran out of ``budget_seconds`` with candidates
    still to come, else ``None`` (every candidate was looked at). A loop that passes it back as ``after`` reaches the rest
    instead of looking at the same first ``unknown`` proposals every time."""

    results: list[Reconciliation]
    resume_after: str | None = None


def reconcile_pass(
    factory: SessionFactory,
    *,
    lease: Lease,
    targets: Targets,
    lookup: Lookup = ask_downstream,
    limit: int = DEFAULT_PASS_LIMIT,
    budget_seconds: float | None = None,
    on_progress: Callable[[], None] | None = None,
    in_flight_seconds: float | None = None,
    now: datetime | None = None,
) -> list[Reconciliation]:
    """One pass over *every* candidate (in id order, ``limit`` at a time, so a backlog of proposals that stay ``unknown`` can
    never hide a newer resolvable one), under ``lease``. Stops (raising ``ProposalError`` ``lease_lost``) the moment the lease is
    not this owner's: whatever is left is the next owner's.

    A lookup can take its executor timeout (30 s by default) and a pass does not renew the lease, so a caller that owns the loop
    bounds it: ``budget_seconds`` ends the pass after the proposal that crossed it (the rest wait for the next pass, see
    :func:`reconcile_pass_from`), and ``on_progress`` is called before each proposal (renew the lease there, or raise to stop).
    No lock is held across a lookup: each proposal is read, asked about and written in separate transactions, and the lease is
    compared again in the one that writes. A proposal whose reconciliation raises anything but ``lease_lost`` is reported
    ``unknown`` with the exception's class name and the pass goes on to the next one.

    ``in_flight_seconds`` (off by default) is the bounded in-flight rule for the *current* lease (B05.5d, #997): a proposal
    ``dispatched`` under this lease's own token for longer than that is recorded ``unknown`` and then handled like any other
    (:func:`caliber.effects.records.expire_in_flight`); a younger one is ``in_flight`` and left alone. Without it only a
    dispatch of a lease that has been taken over is touched.
    """
    return reconcile_pass_from(
        factory,
        lease=lease,
        targets=targets,
        lookup=lookup,
        limit=limit,
        budget_seconds=budget_seconds,
        on_progress=on_progress,
        in_flight_seconds=in_flight_seconds,
        now=now,
    ).results


def reconcile_pass_from(
    factory: SessionFactory,
    *,
    lease: Lease,
    targets: Targets,
    lookup: Lookup = ask_downstream,
    limit: int = DEFAULT_PASS_LIMIT,
    budget_seconds: float | None = None,
    on_progress: Callable[[], None] | None = None,
    in_flight_seconds: float | None = None,
    after: str | None = None,
    now: datetime | None = None,
) -> PassResult:
    """:func:`reconcile_pass` that starts after proposal id ``after`` and says where to start next (:class:`PassResult`)."""
    in_flight_seconds = _check_in_flight(in_flight_seconds)
    in_flight = in_flight_seconds is not None
    started = time.monotonic()
    with factory() as session:
        fence_owner(session, "pass", lease.owner_id, lease)
        page = _candidate_page(session, lease, limit, after, in_flight=in_flight)
    results: list[Reconciliation] = []
    while page:
        for proposal_id in page:
            if budget_seconds is not None and time.monotonic() - started >= budget_seconds:
                return PassResult(results, results[-1].proposal_id if results else after)
            if on_progress is not None:
                on_progress()
            results.append(
                _reconcile_or_report(
                    factory, proposal_id, lease, targets, lookup, in_flight_seconds, now
                )
            )
        with factory() as session:
            page = _candidate_page(session, lease, limit, page[-1], in_flight=in_flight)
    return PassResult(results)


def _reconcile_or_report(
    factory: SessionFactory,
    proposal_id: str,
    lease: Lease,
    targets: Targets,
    lookup: Lookup,
    in_flight_seconds: float | None,
    now: datetime | None,
) -> Reconciliation:
    try:
        return reconcile_one(
            factory,
            proposal_id,
            lease=lease,
            targets=targets,
            lookup=lookup,
            in_flight_seconds=in_flight_seconds,
            now=now,
        )
    except ProposalError as exc:
        if exc.code == LEASE_LOST:
            raise
        logger.warning("reconcile %s: %s", proposal_id, exc.code)
        return Reconciliation(proposal_id, STILL_UNKNOWN, UNKNOWN, exc.code)
    except Exception as exc:
        # One proposal's failure (a statement timeout, a connector bug) must not abort the pass or pin the cursor in front of it,
        # or every row behind it would starve. Only the exception class is kept: never a message, which may carry a payload.
        logger.warning("reconcile %s: %s", proposal_id, type(exc).__name__)
        return Reconciliation(proposal_id, STILL_UNKNOWN, UNKNOWN, type(exc).__name__)


def unresolved_effects(
    session: Session, workflow_run_id: str, node_ids: Collection[str]
) -> list[tuple[str, str, str]]:
    """``(proposal id, node id, state)`` of the effects of ``node_ids`` in this run that have no final outcome."""
    if not node_ids:
        return []
    rows = session.execute(
        select(
            CaliberEffectProposal.proposal_id,
            CaliberEffectProposal.node_id,
            CaliberEffectProposal.state,
        )
        .where(
            CaliberEffectProposal.workflow_run_id == workflow_run_id,
            CaliberEffectProposal.node_id.in_(sorted(node_ids)),
            CaliberEffectProposal.state.in_(OPEN_STATES),
        )
        .order_by(CaliberEffectProposal.node_id, CaliberEffectProposal.proposal_id)
    ).all()
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def unresolved_effect_details(
    session: Session, workflow_run_id: str, node_ids: Collection[str]
) -> list[EffectDependency]:
    """Return the identity evidence needed to explain and resume an effect gate."""
    if not node_ids:
        return []
    rows = session.execute(
        select(
            CaliberEffectProposal.proposal_id,
            CaliberEffectProposal.node_id,
            CaliberEffectProposal.state,
            CaliberEffectProposal.logical_key,
            CaliberEffectProposal.intent_hash,
        )
        .where(
            CaliberEffectProposal.workflow_run_id == workflow_run_id,
            CaliberEffectProposal.node_id.in_(sorted(node_ids)),
            CaliberEffectProposal.state.in_(OPEN_STATES),
        )
        .order_by(CaliberEffectProposal.node_id, CaliberEffectProposal.proposal_id)
    ).all()
    return [
        EffectDependency(
            proposal_id=str(row[0]),
            node_id=str(row[1]),
            state=str(row[2]),
            logical_key=str(row[3]),
            intent_hash=str(row[4]),
        )
        for row in rows
    ]


def effect_gate(factory: SessionFactory, workflow_run_id: str) -> Callable[[Collection[str]], None]:
    """The check the runtime makes before a node runs: ``UnknownEffectError`` while an effect of any of ``node_ids`` (the node
    and everything upstream of it) in this run has no final outcome."""

    def gate(node_ids: Collection[str]) -> None:
        with factory() as session:
            details = unresolved_effect_details(session, workflow_run_id, node_ids)
        if details:
            raise UnknownEffectError(
                [(item.proposal_id, item.node_id, item.state) for item in details],
                effect_details=details,
            )

    return gate


def wake_parked_runs(
    factory: SessionFactory,
    proposal_id: str,
    *,
    settled_state: str,
) -> list[str]:
    """Queue parked runs once every dependency in their durable checkpoint is settled.

    This is deliberately called after the proposal settlement transaction commits. The
    run keeps its original id and checkpoint, so the worker resumes the gated node in
    place; it never creates a new logical occurrence. A checkpoint with several open
    proposals wakes only after all of them have reached ``committed`` or ``failed``.
    """
    if settled_state not in (COMMITTED, FAILED):
        return []
    woken: list[str] = []
    with factory() as session:
        runs = (
            session.execute(
                select(CaliberWorkflowRun)
                .where(CaliberWorkflowRun.status == RUN_STATUS_WAITING_EFFECT)
                .order_by(CaliberWorkflowRun.queued_at.asc(), CaliberWorkflowRun.workflow_run_id)
            )
            .scalars()
            .all()
        )
        for run in runs:
            summary = run.summary if isinstance(run.summary, dict) else {}
            checkpoint_id = summary.get("resume_checkpoint_id")
            if not isinstance(checkpoint_id, str) or not checkpoint_id:
                continue
            checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
            state_blob = checkpoint.state_blob if checkpoint is not None else None
            if (
                not isinstance(state_blob, dict)
                or state_blob.get("kind") != "effect_reconciliation"
            ):
                continue
            effects = state_blob.get("effects")
            if not isinstance(effects, list):
                continue
            proposal_ids = {
                item.get("proposal_id")
                for item in effects
                if isinstance(item, dict) and isinstance(item.get("proposal_id"), str)
            }
            if proposal_id not in proposal_ids or not proposal_ids:
                continue
            state_rows = session.execute(
                select(CaliberEffectProposal.proposal_id, CaliberEffectProposal.state).where(
                    CaliberEffectProposal.proposal_id.in_(sorted(proposal_ids))
                )
            ).all()
            states: dict[str, str] = {
                str(proposal_id): str(state) for proposal_id, state in state_rows
            }
            if set(states) != proposal_ids or any(
                state in OPEN_STATES for state in states.values()
            ):
                continue
            assert_run_transition(run.status, RUN_STATUS_QUEUED)
            now = datetime.now(timezone.utc)
            parked = dict(summary.get("parked_effects") or {})
            parked["state"] = RUN_STATUS_QUEUED
            parked["wake_reason"] = "all_effect_dependencies_settled"
            parked["settled_states"] = {str(key): str(value) for key, value in states.items()}
            new_summary = {**summary, "status": RUN_STATUS_QUEUED, "parked_effects": parked}
            updated = session.execute(
                update(CaliberWorkflowRun)
                .where(
                    CaliberWorkflowRun.workflow_run_id == run.workflow_run_id,
                    CaliberWorkflowRun.status == RUN_STATUS_WAITING_EFFECT,
                )
                .values(
                    status=RUN_STATUS_QUEUED,
                    claimed_by=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    last_heartbeat_at=now,
                    current_node_id=None,
                    error_code=None,
                    error_summary=None,
                    summary=new_summary,
                )
            )
            if getattr(updated, "rowcount", 0) != 1:
                continue
            append_run_event(
                session,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                event_type="workflow.run.effect_resumed",
                payload={
                    "proposal_id": proposal_id,
                    "settled_state": settled_state,
                    "node_id": state_blob.get("node_id"),
                },
                node_id=state_blob.get("node_id")
                if isinstance(state_blob.get("node_id"), str)
                else None,
            )
            audit_record(
                session,
                actor="reconciler",
                action="resume_workflow_run_after_effect_settlement",
                entity_type="workflow_run",
                entity_id=run.workflow_run_id,
                details={
                    "proposal_id": proposal_id,
                    "settled_state": settled_state,
                    "proposal_ids": sorted(proposal_ids),
                },
            )
            woken.append(run.workflow_run_id)
        if woken:
            session.commit()
    return woken


__all__ = [
    "ABSENCE_PROVEN_REASON",
    "CONNECTORS",
    "IN_FLIGHT",
    "LOOKUP_FAILED",
    "MOVED",
    "NOT_BOUND",
    "OPEN_STATES",
    "RECONCILED_COMMITTED",
    "RECONCILED_FAILED",
    "STILL_UNKNOWN",
    "UNVERIFIED",
    "Answer",
    "ConnectorLookup",
    "EffectDependency",
    "PassResult",
    "Reconciliation",
    "UnknownEffectError",
    "ask_downstream",
    "candidates",
    "commit_found",
    "effect_gate",
    "reconcile_one",
    "reconcile_pass",
    "reconcile_pass_from",
    "targets_from_bindings",
    "targets_from_config",
    "unresolved_effect_details",
    "unresolved_effects",
    "wake_parked_runs",
]
