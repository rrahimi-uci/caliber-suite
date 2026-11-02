"""Owner-fenced operator actions on effect proposals (B05.5d, #997; ADR 0003).

An effect proposal is settled by evidence from the downstream and by nothing else (:mod:`caliber.effects.reconcile`): a
verified receipt commits it, a connector that proves absence final fails it, and every other answer leaves it ``unknown``.
The owner loop (:class:`caliber.orchestrator.effect_reconciler.EffectReconcilerTask`) asks on an interval. This is the
operator's way to ask *now* (after the downstream's owner confirms the request is visible, say) and to leave a record of who
asked and why. The normal action is deliberately **not** a way to say what happened:

* **No say-so for reconcile.** There is no ``committed`` or ``failed`` an operator can write through the normal action. A
  receipt exists only when the platform read the effect back from the downstream by its key, and a ``failed`` only when the
  connector proves absence final. A proposal that cannot be settled stays ``unknown`` and blocks what depends on it; the
  receipt callback (``POST .../effects/callbacks/receipt``) is how downstream confirmation reaches the platform.
* **Evidence-bounded attestation.** The separate ``attest_failed`` action is the narrow exit for a permanent ``unknown``. An
  admin supplies an opaque reference to the downstream's own evidence, and the owner-fenced write records that reference in
  the same transaction as ``unknown`` -> ``reconciled`` -> ``failed``. It cannot commit a proposal; ``committed`` remains
  available only through a verified receipt.
* **Owner-fenced.** The lookup and every write run under the execution-owner lease the *serving process* holds right now
  (``reconcile_one``: the lease is compared, by owner, fencing token and the database's clock, in the transaction of every
  write). A process that does not hold the lease cannot ask or record anything (the caller answers 409), and a lease taken
  over mid-request stops the action (``lease_lost``) with nothing recorded.
* **Audited.** Every reconcile request leaves one ``effect_reconcile_requested`` audit row. A successful attestation writes one
  ``effect_attested_resolution`` row with the actor, reason, evidence reference and both states in the same transaction as the
  two existing CAS transitions. A concurrent receipt or reconciler wins by version and the attestation records nothing.
* **Evidence-preserving.** The outcome vocabulary is the reconciler's: a 404 from a connector that does not prove absence is
  ``unknown`` (never ``failed``), a 5xx or an unreadable body is ``unknown``, a lookup that cannot be made is ``lookup_failed``
  or ``not_bound``, and only a stored request that is this proposal's effect settles it.

The row is offered only where it can do something *every time*: the operator view lists ``reconcile`` and ``attest_failed`` for
``unknown`` and ``reconciled`` proposals, and only ``reconcile`` for a ``dispatched`` one whose lease was taken over. Neither
action is listed for a terminal or never-dispatched proposal. A
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
    wake_parked_runs,
)
from caliber.effects.records import (
    COMMITTED,
    FAILED,
    PRINTABLE,
    RECONCILED,
    UNKNOWN,
    ProposalError,
    fence_owner,
    proposal_state,
    transition,
)
from caliber.ownership import Lease

#: The audit action recorded for every operator request.
AUDIT_ACTION = "effect_reconcile_requested"
MAX_REASON_CHARS = 500
#: How many operator-triggered reconciliations a process runs at once (a lookup can take its executor timeout).
MAX_CONCURRENT_REQUESTS = 4
#: What ``safe_actions`` carries for a proposal this action applies to.
ACTION = "reconcile"
ATTEST_FAILURE_ACTION = "attest_failed"
ATTEST_FAILURE_AUDIT_ACTION = "effect_attested_resolution"
ATTESTED_RECONCILIATION_REASON = "operator_attested_absence"
ATTESTED_FAILURE_REASON = "operator_attested_absence"
MAX_EVIDENCE_REFERENCE_CHARS = 512


class NotReconcilable(Exception):  # noqa: N818 - a refusal, named for what it says
    """The proposal is not in a state this action applies to (it was never dispatched, or it is already settled)."""

    def __init__(self, proposal_id: str, state: str) -> None:
        super().__init__(f"proposal {proposal_id} is {state}: there is nothing to reconcile")
        self.proposal_id = proposal_id
        self.state = state


class NotAttestable(Exception):  # noqa: N818 - a refusal, named for what it says
    """The proposal moved out of the permanent-unknown states before attestation could win."""

    def __init__(self, proposal_id: str, state: str) -> None:
        super().__init__(f"proposal {proposal_id} is {state}: it is not attestable")
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


@dataclass(frozen=True)
class OperatorAttestation:
    """The one terminal resolution an operator may attest: absence, never a commit."""

    proposal_id: str
    resolution: str
    state_before: str
    state: str
    reason: str
    evidence_reference: str
    settled: bool


def _operator_text(value: object, *, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be 1 to {limit} printable characters")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit or not PRINTABLE.fullmatch(cleaned):
        raise ValueError(f"{name} must be 1 to {limit} printable characters")
    return cleaned


def attest_failure_as_operator(
    factory: SessionFactory,
    proposal_id: str,
    *,
    actor: str,
    reason: str,
    evidence_reference: str,
    lease: Lease,
) -> OperatorAttestation:
    """Attest downstream evidence of absence for a permanent ``unknown`` proposal.

    The operator cannot attest ``committed``. The evidence reference is recorded in the same transaction as the existing
    ``unknown`` -> ``reconciled`` -> ``failed`` CAS moves. A receipt callback or reconciler that wins the version race leaves
    this action with no audit row and no state write.
    """
    cleaned_reason = _operator_text(reason, name="reason", limit=MAX_REASON_CHARS)
    cleaned_reference = _operator_text(
        evidence_reference, name="evidence_reference", limit=MAX_EVIDENCE_REFERENCE_CHARS
    )
    with factory() as session:
        fence_owner(session, proposal_id, lease.owner_id, lease)
        before = proposal_state(session, proposal_id)
        if before.state not in (UNKNOWN, RECONCILED):
            raise NotAttestable(proposal_id, before.state)
        version = before.version
        if before.state == UNKNOWN:
            version = transition(
                session,
                proposal_id,
                RECONCILED,
                expected_version=version,
                reason=ATTESTED_RECONCILIATION_REASON,
            ).version
        transition(
            session,
            proposal_id,
            FAILED,
            expected_version=version,
            reason=ATTESTED_FAILURE_REASON,
        )
        audit_record(
            session,
            actor=actor,
            action=ATTEST_FAILURE_AUDIT_ACTION,
            entity_type="effect_proposal",
            entity_id=proposal_id,
            details={
                "resolution": "failed",
                "reason": cleaned_reason,
                "evidence_reference": cleaned_reference,
                "state_before": before.state,
                "state_after": FAILED,
                "owner_id": lease.owner_id,
                "fencing_token": lease.fencing_token,
            },
            severity="high",
        )
        session.commit()
    wake_parked_runs(factory, proposal_id, settled_state=FAILED)
    return OperatorAttestation(
        proposal_id=proposal_id,
        resolution="failed",
        state_before=before.state,
        state=FAILED,
        reason=cleaned_reason,
        evidence_reference=cleaned_reference,
        settled=True,
    )


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
    "ATTESTED_FAILURE_REASON",
    "ATTESTED_RECONCILIATION_REASON",
    "ATTEST_FAILURE_ACTION",
    "ATTEST_FAILURE_AUDIT_ACTION",
    "AUDIT_ACTION",
    "MAX_CONCURRENT_REQUESTS",
    "MAX_EVIDENCE_REFERENCE_CHARS",
    "MAX_REASON_CHARS",
    "NotAttestable",
    "NotReconcilable",
    "OperatorAttestation",
    "OperatorReconciliation",
    "attest_failure_as_operator",
    "reconcile_as_operator",
]
