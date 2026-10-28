"""Verified effect receipts: what the downstream said, after the platform read it back (B05.4a, #717; ADR 0003).

A downstream's 2xx answer to a dispatch is a *claim*. A **receipt** exists only once the platform has asked the
downstream's own authoritative query and what came back is the effect this proposal asked for. :class:`ReceiptFacts` is what a
connector extracts from that query (``connectors.document_request.facts_from_lookup``), and :func:`record_receipt` checks it
against the proposal before it is stored:

* the **idempotency key** the downstream stored is the proposal's logical key (the key that was emitted),
* the **intent** the downstream stored hashes to the proposal's ``intent_hash`` (connector identity and version, effect class,
  target resource, arguments, expected resource version: the one full digest, never a reduced one),
* the **connector identity and version** and the **target** are the proposal's, and
* the **dispatch attempt** is the one the proposal recorded.

Anything else is refused with the *names* of the fields that differ and never their values (:class:`ReceiptMismatch`).
Be exact about what this proves: :func:`record_receipt` can only compare the facts it is handed with the proposal, so a receipt
is as sound as the connector verifier that built the facts from the downstream's answer (``facts_from_lookup``); the
"target and version" check (the stored resource and the version the effect produced) is that verifier's, not this module's. A
receipt is immutable bytes (the canonical ``receipt`` v1 record and its SHA-256) with one authoritative receipt per
``(proposal, attempt)``; a downstream request id belongs to one proposal only, so a receipt cannot be borrowed by another
(:class:`ReceiptConflict`). Delivering the same facts twice is idempotent; different facts for the same attempt are a conflict
and never replace the first. :func:`read_receipt` recomputes the record from the stored columns, so a row edited around the
database's triggers is detected.

``caliber.effects.records.transition`` refuses ``committed`` without such a receipt. A query that answers 404, an unreadable
answer, or a stored request that is not this proposal's is *not* a receipt, and not a failure either: the proposal stays
``unknown`` (:func:`settle_dispatch`), because a downstream that does not know a key yet may still process it.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.contracts import validate_record
from caliber.db.models import CaliberEffectProposal, CaliberEffectReceipt
from caliber.db.transactions import begin_sqlite_transaction
from caliber.effects.connectors import Classified, TransportOutcome
from caliber.effects.records import (
    BAD_FIELD,
    COMMITTED,
    DISPATCHED,
    FAILED,
    ILLEGAL_TRANSITION,
    RECONCILED,
    STALE_OWNER,
    UNKNOWN,
    ProposalError,
    StaleVersion,
    Transitioned,
    fence_owner,
    proposal_state,
    transition,
)
from caliber.ownership import Lease
from caliber.run_manifest import canonical_bytes

RECEIPT_MISMATCH: Final = "receipt_mismatch"
RECEIPT_CONFLICT: Final = "receipt_conflict"
RECEIPT_STATE: Final = "receipt_state"
RECEIPT_CORRUPT: Final = "receipt_corrupt"
VERIFIED_BY_QUERY: Final = "query"
#: A receipt can be recorded while the outcome is still open: the dispatch is in flight, or its outcome is unknown or being
#: reconciled. Once the proposal is settled a receipt is too late to matter.
_OPEN_STATES: Final = frozenset({DISPATCHED, UNKNOWN, RECONCILED})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
#: No control characters, NUL or unpaired surrogates: these values reach the database and the digest as they are.
_PRINTABLE = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_LIMITS: Final = {
    "idempotency_key": 255,
    "connector": 128,
    "connector_version": 64,
    "resource": 512,
    "intent_hash": 71,
    "downstream_request_id": 256,
    "resource_version": 256,
}


class ReceiptMismatch(ProposalError):  # noqa: N818 - a refusal, named for what it says
    """The facts do not describe this proposal's effect; ``fields`` names what differs (never the values)."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__(
            RECEIPT_MISMATCH, "the receipt does not match the proposal: " + ", ".join(fields)
        )
        self.fields = fields


class ReceiptConflict(ProposalError):  # noqa: N818 - a refusal, named for what it says
    """A different receipt is already the authoritative one for this attempt, or the request id belongs to another proposal."""

    def __init__(self, message: str) -> None:
        super().__init__(RECEIPT_CONFLICT, message)


def _text(name: str, value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _LIMITS[name]
        or not _PRINTABLE.fullmatch(value)
    ):
        raise ProposalError(
            BAD_FIELD, f"{name} must be 1..{_LIMITS[name]} printable characters and not blank"
        )
    return value


@dataclass(frozen=True)
class ReceiptFacts:
    """What a connector read back from the downstream's authoritative query about one dispatch attempt."""

    attempt: int
    idempotency_key: str
    connector: str
    connector_version: str
    resource: str
    intent_hash: str
    downstream_request_id: str
    response_digest: str
    resource_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.attempt) is not int or not 1 <= self.attempt <= 2**31 - 1:
            raise ProposalError(BAD_FIELD, "attempt must be a positive integer")
        for name in (
            "idempotency_key",
            "connector",
            "connector_version",
            "resource",
            "intent_hash",
            "downstream_request_id",
        ):
            _text(name, getattr(self, name))
        _text("resource_version", self.resource_version, nullable=True)
        for name in ("intent_hash", "response_digest"):
            if not isinstance(getattr(self, name), str) or not _DIGEST.fullmatch(
                getattr(self, name)
            ):
                raise ProposalError(
                    BAD_FIELD, f"{name} must be 'sha256:' and 64 lowercase hex digits"
                )


@dataclass(frozen=True)
class RecordedReceipt:
    """The outcome of :func:`record_receipt`: ``created`` is ``False`` when the same receipt was already recorded."""

    receipt_id: str
    proposal_id: str
    attempt: int
    created: bool


def new_receipt_id() -> str:
    return f"rcpt-{uuid.uuid4().hex}"


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc(moment: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if moment is None else moment
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _record_dict(row: CaliberEffectReceipt) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "receipt_id": row.receipt_id,
        "proposal_id": row.proposal_id,
        "downstream_request_id": row.downstream_request_id,
        "response_digest": row.response_digest,
        "verified_by": row.verified_by,
        "verified_at": _rfc3339(_utc(row.verified_at)),
        "attempt": row.attempt,
        "logical_key": row.logical_key,
        "intent_hash": row.intent_hash,
        "connector": row.connector,
        "connector_version": row.connector_version,
        "resource": row.resource,
        "resource_version": row.resource_version,
    }


def _mismatches(row: CaliberEffectProposal, facts: ReceiptFacts) -> tuple[str, ...]:
    pairs = (
        ("attempt", row.attempt, facts.attempt),
        ("idempotency_key", row.logical_key, facts.idempotency_key),
        ("connector", row.connector, facts.connector),
        ("connector_version", row.connector_version, facts.connector_version),
        ("resource", row.resource, facts.resource),
        ("intent_hash", row.intent_hash, facts.intent_hash),
    )
    return tuple(name for name, expected, actual in pairs if expected != actual)


def record_receipt(
    session: Session, proposal_id: str, facts: ReceiptFacts, *, now: datetime | None = None
) -> RecordedReceipt:
    """Record the verified receipt for ``proposal_id`` in the caller's transaction (flushes, never commits).

    Raises ``KeyError`` for an unknown proposal, :class:`ProposalError` (``receipt_state``) if the proposal is not open
    (``dispatched``, ``unknown`` or ``reconciled``), :class:`ReceiptMismatch` if the facts are not this proposal's, and
    :class:`ReceiptConflict` if a different receipt is already the authoritative one for the attempt or the request id is
    another proposal's. The same facts delivered again return the existing receipt (``created=False``).

    This records a *verified statement about the downstream* and is not fenced by the execution-owner lease: only
    :func:`settle_dispatch` (receipt and state move under the lease) is the owner's commit path. A receipt recorded on its
    own makes the proposal impossible to fail but does not commit it.
    """
    if not isinstance(facts, ReceiptFacts):
        raise ProposalError(BAD_FIELD, "facts must be ReceiptFacts")
    begin_sqlite_transaction(session)
    row = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
    if row is None:
        raise KeyError(proposal_id)
    if row.state not in _OPEN_STATES:
        raise ProposalError(
            RECEIPT_STATE,
            f"proposal {proposal_id} is {row.state}: a receipt can no longer change its outcome",
        )
    wrong = _mismatches(row, facts)
    if wrong:
        raise ReceiptMismatch(wrong)
    existing = _for_attempt(session, proposal_id, facts.attempt)
    if existing is not None:
        return _same_or_conflict(existing, facts)
    _refuse_a_borrowed_request(session, proposal_id, facts)
    moment = _utc(now)
    candidate = CaliberEffectReceipt(
        receipt_id=new_receipt_id(),
        proposal_id=proposal_id,
        attempt=facts.attempt,
        logical_key=row.logical_key,
        intent_hash=row.intent_hash,
        connector=row.connector,
        connector_version=row.connector_version,
        resource=row.resource,
        downstream_request_id=facts.downstream_request_id,
        resource_version=facts.resource_version,
        response_digest=facts.response_digest,
        verified_by=VERIFIED_BY_QUERY,
        verified_at=moment.astimezone(timezone.utc).replace(tzinfo=None),
        receipt_bytes=b"-",
        receipt_digest="0" * 64,
    )
    record = _record_dict(candidate)
    validate_record("receipt", record)
    data = canonical_bytes(record)
    candidate.receipt_bytes = data
    candidate.receipt_digest = hashlib.sha256(data).hexdigest()
    try:
        with session.begin_nested():
            session.add(candidate)
    except IntegrityError:
        # Only the attempt or the request id means "another receipt got there first"; any other constraint failure is a defect
        # the caller must see, and the driver's text (which carries the parameters) is not passed on.
        winner = _for_attempt(session, proposal_id, facts.attempt, refresh=True)
        if winner is not None:
            return _same_or_conflict(winner, facts)
        if _request_owner(session, facts, refresh=True) is not None:
            raise ReceiptConflict(
                "the downstream request id already belongs to another proposal"
            ) from None
        raise ProposalError("store_failed", "the receipt could not be stored") from None
    return RecordedReceipt(candidate.receipt_id, proposal_id, facts.attempt, created=True)


def _for_attempt(
    session: Session, proposal_id: str, attempt: int, *, refresh: bool = False
) -> CaliberEffectReceipt | None:
    statement = select(CaliberEffectReceipt).where(
        CaliberEffectReceipt.proposal_id == proposal_id, CaliberEffectReceipt.attempt == attempt
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def _request_owner(
    session: Session, facts: ReceiptFacts, *, refresh: bool = False
) -> CaliberEffectReceipt | None:
    statement = select(CaliberEffectReceipt).where(
        CaliberEffectReceipt.connector == facts.connector,
        CaliberEffectReceipt.downstream_request_id == facts.downstream_request_id,
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def _refuse_a_borrowed_request(session: Session, proposal_id: str, facts: ReceiptFacts) -> None:
    owner = _request_owner(session, facts)
    if owner is not None and owner.proposal_id != proposal_id:
        raise ReceiptConflict("the downstream request id already belongs to another proposal")


def _same_or_conflict(existing: CaliberEffectReceipt, facts: ReceiptFacts) -> RecordedReceipt:
    if (
        existing.downstream_request_id != facts.downstream_request_id
        or existing.resource_version != facts.resource_version
    ):
        raise ReceiptConflict(
            f"attempt {existing.attempt} of proposal {existing.proposal_id} already has a different authoritative receipt"
        )
    # The response digest is not compared: it is a hash of the whole body the lookup returned, which can gain a field between
    # two deliveries of the same effect; the first delivery's digest stays the recorded one.
    return RecordedReceipt(
        existing.receipt_id, existing.proposal_id, existing.attempt, created=False
    )


def read_receipt(session: Session, receipt_id: str) -> dict[str, Any]:
    """The ``receipt`` v1 record for ``receipt_id``, after recomputing it from the stored columns.

    ``KeyError`` for an unknown id; :class:`ProposalError` (``receipt_corrupt``) if the stored bytes or digest do not match the
    columns, the record is not a valid ``receipt`` v1, or it is not the proposal's own (key and intent).
    """
    row = session.get(CaliberEffectReceipt, receipt_id)
    if row is None:
        raise KeyError(receipt_id)
    record = _record_dict(row)
    proposal = session.get(CaliberEffectProposal, row.proposal_id)
    try:
        validate_record("receipt", record)
        stored = json.loads(bytes(row.receipt_bytes))
    except (ValueError, TypeError) as exc:
        raise ProposalError(
            RECEIPT_CORRUPT, f"receipt {receipt_id} does not hold a valid record"
        ) from exc
    if (
        stored != record
        or bytes(row.receipt_bytes) != canonical_bytes(record)
        or row.receipt_digest != hashlib.sha256(bytes(row.receipt_bytes)).hexdigest()
        or proposal is None
        or proposal.logical_key != row.logical_key
        or proposal.intent_hash != row.intent_hash
        or proposal.attempt != row.attempt
        or proposal.connector != row.connector
        or proposal.connector_version != row.connector_version
        or proposal.resource != row.resource
    ):
        raise ProposalError(
            RECEIPT_CORRUPT,
            f"receipt {receipt_id} does not match its recorded bytes or its proposal",
        )
    return record


@dataclass(frozen=True)
class Settled:
    """What :func:`settle_dispatch` recorded."""

    state: str
    version: int
    receipt_id: str | None
    reason: str | None
    applied: bool


def settle_dispatch(
    session: Session,
    proposal_id: str,
    *,
    expected_version: int,
    owner_id: str,
    attempt: int,
    lease: Lease,
    outcome: Classified,
    verify: Callable[[], ReceiptFacts | None],
    now: datetime | None = None,
) -> Settled:
    """Record what one dispatch attempt proved (``dispatched`` to ``committed``, ``failed`` or ``unknown``).

    * ``committed_receipt`` (a 2xx claim): ``verify`` runs the downstream's authoritative query and returns the facts if what it
      holds is this proposal's effect, or ``None`` (a 404, an unreadable answer, a stored request that is not this one's).
      Facts become a receipt and the proposal ``committed``; ``None`` leaves it ``unknown`` (``receipt_unverified``): a claim
      nobody could confirm is not a commit, and a query that finds nothing is not a failure.
    * ``definitive_rejection``: ``failed`` (``rejected:<code>``); ``proven_not_sent``: ``failed`` (``not_sent``).
    * ``ambiguous``: ``unknown`` (``ambiguous:<code>``); only a later lookup by key can say more.

    ``verify`` is not called for any other outcome, and not before the proposal is checked: it must still be ``dispatched`` at
    ``expected_version``, held by this ``owner_id`` and ``attempt``, under a lease that is this owner's. Another owner or attempt
    is refused *before* a query is made and records nothing, receipt included. Whether the lease is still *held* is decided at
    the write, never before the query, so the lease row is not locked across the network call. A repeat of a settle that
    already happened is ``stale_version``, so read the state. If ``verify`` raises, the exception propagates and the proposal
    stays ``dispatched``. The receipt and the commit are one unit: if the transition is refused, the receipt is not kept.
    Nothing is committed to the database (the caller's transaction).

    ``lease`` is the execution-owner lease ``owner_id`` captured when it acquired (B05.5a, #734). The writes made here, the
    receipt and the state move, are made in one transaction after the lease is compared (exact owner and fencing token, the
    database's clock, :func:`caliber.effects.records.fence_owner`), and that comparison comes first, so the lock order is the
    lease row, then the receipt, then the proposal. A worker whose lease expired or was taken over gets ``lease_lost``, the
    receipt is rolled back with the refused move, and the proposal stays ``dispatched``. Not even ``unknown`` is recorded by a
    stale worker: what the downstream did is for the reconciler, which asks it by key under the new lease.
    """
    _precheck(session, proposal_id, expected_version, owner_id, attempt, lease)
    if outcome.outcome is TransportOutcome.COMMITTED_RECEIPT:
        facts = verify()
        if facts is not None:
            with session.begin_nested():
                fence_owner(
                    session, proposal_id, owner_id, lease
                )  # lease row first: one lock order everywhere
                receipt = record_receipt(session, proposal_id, facts, now=now)
                moved = transition(
                    session,
                    proposal_id,
                    COMMITTED,
                    expected_version=expected_version,
                    owner_id=owner_id,
                    attempt=attempt,
                    receipt_id=receipt.receipt_id,
                    lease=lease,
                    now=now,
                )
            return Settled(COMMITTED, moved.version, receipt.receipt_id, None, moved.applied)
        return _settle(
            session,
            proposal_id,
            UNKNOWN,
            "receipt_unverified",
            expected_version,
            owner_id,
            attempt,
            lease,
            now,
        )
    if outcome.outcome is TransportOutcome.DEFINITIVE_REJECTION:
        return _settle(
            session,
            proposal_id,
            FAILED,
            f"rejected:{outcome.code}",
            expected_version,
            owner_id,
            attempt,
            lease,
            now,
        )
    if outcome.outcome is TransportOutcome.PROVEN_NOT_SENT:
        return _settle(
            session,
            proposal_id,
            FAILED,
            "not_sent",
            expected_version,
            owner_id,
            attempt,
            lease,
            now,
        )
    return _settle(
        session,
        proposal_id,
        UNKNOWN,
        f"ambiguous:{outcome.code}",
        expected_version,
        owner_id,
        attempt,
        lease,
        now,
    )


def _precheck(
    session: Session,
    proposal_id: str,
    expected_version: int,
    owner_id: str,
    attempt: int,
    lease: Lease,
) -> None:
    """Refuse a settle that could not succeed (stale version, not dispatched, another owner) before any query is made."""
    state = proposal_state(session, proposal_id)
    if state.version != expected_version:
        raise StaleVersion(proposal_id, expected_version, state.version, state.state)
    if state.state != DISPATCHED:
        raise ProposalError(
            ILLEGAL_TRANSITION,
            f"proposal {proposal_id} is {state.state}: only a dispatched proposal is settled here",
        )
    if state.owner_id != owner_id or state.attempt != attempt or lease.owner_id != owner_id:
        raise ProposalError(
            STALE_OWNER,
            f"proposal {proposal_id} was dispatched by another owner or attempt: nothing recorded",
        )


def _settle(
    session: Session,
    proposal_id: str,
    state: str,
    reason: str,
    expected_version: int,
    owner_id: str,
    attempt: int,
    lease: Lease,
    now: datetime | None,
) -> Settled:
    moved: Transitioned = transition(
        session,
        proposal_id,
        state,
        expected_version=expected_version,
        owner_id=owner_id,
        attempt=attempt,
        reason=reason[:128],
        lease=lease,
        now=now,
    )
    return Settled(state, moved.version, None, reason[:128], moved.applied)


__all__ = [
    "RECEIPT_CONFLICT",
    "RECEIPT_CORRUPT",
    "RECEIPT_MISMATCH",
    "RECEIPT_STATE",
    "VERIFIED_BY_QUERY",
    "ReceiptConflict",
    "ReceiptFacts",
    "ReceiptMismatch",
    "RecordedReceipt",
    "Settled",
    "new_receipt_id",
    "read_receipt",
    "record_receipt",
    "settle_dispatch",
]
