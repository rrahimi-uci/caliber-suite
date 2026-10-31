"""The one action an operator has on an effect proposal: ask the downstream now, as the owner (B05.5d, #997; ADR 0003).

An effect proposal is settled by evidence from the downstream and by nothing else (:mod:`caliber.effects.reconcile`): a
verified receipt commits it, a connector that proves absence final fails it, and every other answer leaves it ``unknown``.
The owner loop (:class:`caliber.orchestrator.effect_reconciler.EffectReconcilerTask`) asks on an interval. This is the
operator's way to ask *now* (after the downstream's owner confirms the request is visible, say) and to leave a record of who
asked and why. It is deliberately **not** a way to say what happened:

* **No say-so.** There is no ``committed`` or ``failed`` an operator can write. A receipt exists only when the platform read
  the effect back from the downstream by its key, and a ``failed`` only when the connector proves absence final. An operator
  who believes the effect did not happen has no evidence the platform can check, and a proposal marked failed on that belief
  unblocks everything that depends on it while the effect may in fact have happened. The legacy ledger's ``resolve``
  (:mod:`caliber.routes.system_effects`) does take the operator's word; a proposal never does. A proposal that cannot be
  settled (a connector with no lookup by key, a downstream that stays unreachable) stays ``unknown`` and blocks what depends
  on it, which is the safe state: the operator takes it to the downstream's owner, and the receipt callback
  (``POST .../effects/callbacks/receipt``) is how the downstream's own confirmation reaches the platform.
* **Owner-fenced.** The lookup and every write run under the execution-owner lease the *serving process* holds right now
  (``reconcile_one``: the lease is compared, by owner, fencing token and the database's clock, in the transaction of every
  write). A process that does not hold the lease cannot ask or record anything (the caller answers 409), and a lease taken
  over mid-request stops the action (``lease_lost``) with nothing recorded.
* **Audited.** Every request leaves one ``effect_reconcile_requested`` audit row naming the operator, their reason and what
  came of it (the outcome and the state afterwards), whether or not the proposal moved. When the lookup settles the proposal
  the reconciler's own ``effect_reconciled`` row is written in the same transaction as the move.
* **Evidence-preserving.** The outcome vocabulary is the reconciler's: a 404 from a connector that does not prove absence is
  ``unknown`` (never ``failed``), a 5xx or an unreadable body is ``unknown``, a lookup that cannot be made is ``lookup_failed``
  or ``not_bound``, and only a stored request that is this proposal's effect settles it.

The row is offered only where it can do something *every time*: the operator view lists ``reconcile`` in ``safe_actions`` for
``unknown`` and ``reconciled`` proposals and for a ``dispatched`` one whose lease was taken over, and for nothing else. A
``dispatched`` attempt of the lease that is still current is the dispatcher's and is not listed, but the route accepts it:
younger than ``CALIBER_EFFECT_IN_FLIGHT_MAX_SECONDS`` it answers ``in_flight`` and changes nothing, older it records
``unknown`` (``in_flight_expired``) exactly as the loop would and asks.

Concurrency: at most :data:`MAX_CONCURRENT_REQUESTS` operator requests run a lookup at once per process; the route answers
``429`` to the rest (the downstream is not to be hammered by a button).
"""

from __future__ import annotations

from dataclasses import dataclass

from caliber.audit import record as audit_record
from caliber.effects.reconcile import (
    OPEN_STATES,
    Lookup,
    SessionFactory,
    Targets,
    ask_downstream,
    reconcile_one,
)
from caliber.effects.records import (
    COMMITTED,
    FAILED,
    ProposalError,
    proposal_state,
)
from caliber.ownership import Lease

#: The audit action recorded for every operator request.
AUDIT_ACTION = "effect_reconcile_requested"
MAX_REASON_CHARS = 500
#: How many operator-triggered reconciliations a process runs at once (a lookup can take its executor timeout).
MAX_CONCURRENT_REQUESTS = 4
#: What ``safe_actions`` carries for a proposal this action applies to.
ACTION = "reconcile"


class NotReconcilable(Exception):  # noqa: N818 - a refusal, named for what it says
    """The proposal is not in a state this action applies to (it was never dispatched, or it is already settled)."""

    def __init__(self, proposal_id: str, state: str) -> None:
        super().__init__(f"proposal {proposal_id} is {state}: there is nothing to reconcile")
        self.proposal_id = proposal_id
        self.state = state


@dataclass(frozen=True)
class OperatorReconciliation:
    """What the request did: identifiers and the reconciler's own outcome vocabulary, never a payload."""

    proposal_id: str
    outcome: str
    state: str
    detail: str | None
    settled: bool


def reconcile_as_operator(
    factory: SessionFactory,
    proposal_id: str,
    *,
    actor: str,
    reason: str,
    lease: Lease,
    targets: Targets,
    in_flight_seconds: float | None = None,
    lookup: Lookup = ask_downstream,
) -> OperatorReconciliation:
    """Ask the downstream about ``proposal_id`` now, under ``lease``, and audit the request.

    ``KeyError`` for an unknown id; :class:`NotReconcilable` for a state outside ``dispatched``/``unknown``/``reconciled``;
    ``ProposalError`` ``lease_lost`` (the lease was taken over, nothing recorded), and any other failure, are raised after the
    attempt is audited (``outcome`` is the error code or the exception class name).
    ``reason`` is the operator's, 1 to :data:`MAX_REASON_CHARS` characters.
    """
    cleaned = reason.strip()
    if not cleaned or len(cleaned) > MAX_REASON_CHARS:
        raise ValueError(f"reason must be 1 to {MAX_REASON_CHARS} characters")
    with factory() as session:
        before = proposal_state(session, proposal_id)
    if before.state not in OPEN_STATES:
        raise NotReconcilable(proposal_id, before.state)
    try:
        result = reconcile_one(
            factory,
            proposal_id,
            lease=lease,
            targets=targets,
            lookup=lookup,
            in_flight_seconds=in_flight_seconds,
        )
    except ProposalError as exc:
        _audit(factory, actor, proposal_id, cleaned, before.state, lease, outcome=exc.code)
        raise
    except Exception as exc:
        # the request was made and failed (a store error, a connector bug): still on the record, by exception class only
        _audit(
            factory, actor, proposal_id, cleaned, before.state, lease, outcome=type(exc).__name__
        )
        raise
    _audit(
        factory,
        actor,
        proposal_id,
        cleaned,
        before.state,
        lease,
        outcome=result.outcome,
        state=result.state,
        detail=result.detail,
    )
    return OperatorReconciliation(
        proposal_id,
        result.outcome,
        result.state,
        result.detail,
        settled=result.state in (COMMITTED, FAILED),
    )


def _audit(
    factory: SessionFactory,
    actor: str,
    proposal_id: str,
    reason: str,
    state_before: str,
    lease: Lease,
    *,
    outcome: str,
    state: str | None = None,
    detail: str | None = None,
) -> None:
    with factory() as session:
        audit_record(
            session,
            actor=actor,
            action=AUDIT_ACTION,
            entity_type="effect_proposal",
            entity_id=proposal_id,
            details={
                "reason": reason,
                "state_before": state_before,
                "outcome": outcome,
                "state": state,
                "detail": detail,
                "owner_id": lease.owner_id,
                "fencing_token": lease.fencing_token,
            },
        )
        session.commit()


__all__ = [
    "ACTION",
    "AUDIT_ACTION",
    "MAX_CONCURRENT_REQUESTS",
    "MAX_REASON_CHARS",
    "NotReconcilable",
    "OperatorReconciliation",
    "reconcile_as_operator",
]
