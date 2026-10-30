"""Effect proposals: one immutable intent per logical effect, found again by a durable occurrence (B05.1, #700; ADR 0003).

A run that asks to change something outside CALIBER first records a **proposal**. Two things identify it and they are
kept apart on purpose:

* the **logical key**: ``(deployment, run, node, occurrence)``. A retry of the same effect (a restarted run, a redelivered
  request) presents the same occurrence and finds the proposal that is already there; a legitimate repeat of the same call
  (a loop that notifies the same endpoint twice) presents the next occurrence and gets a new one. The occurrence id is
  allocated by the caller and persisted by it before the effect is attempted (the dispatcher, B05.3 onwards); this module
  only records it and never invents one.
* the **intent**: what exactly is to be done. :class:`Intent` is the one place the canonical form is defined, over the
  connector's identity *and version*, the effect class, the target resource, the arguments and the expected resource
  version, as canonical JSON (sorted keys, no whitespace, UTF-8, no ``NaN``) with an algorithm version
  (:data:`INTENT_ALGORITHM`). Its ``sha256:`` digest is the proposal's ``intent_hash``; **every consumer that has to say
  "this is what was approved / what the receipt answers" reuses that digest** and never computes a reduced one over fewer
  fields, so changing the connector version or the effect class changes the digest and voids an approval of the old one.

:func:`record_proposal` is idempotent for the same logical key and the same intent, and refuses a different intent under
the same key with :class:`ProposalConflict` (the route that calls it answers 409): the stored intent is never replaced
and an attempt to do so is the signal that something upstream is wrong. It runs in the caller's transaction and never
commits, like the other immutable-record writers. :func:`read_proposal` rebuilds the ``proposal`` v1 record (ADR 0003) and
recomputes the intent from the stored columns, so a row edited around the database's triggers is detected, and an
algorithm this build does not know is refused instead of being read as the nearest one.

**States (B05.2, #705).** A proposal moves through the seven states of ADR 0003 only along :data:`TRANSITIONS`, by
:func:`transition`, a compare-and-set on ``version``: the caller names the version it read, the UPDATE is conditioned on
it, and a caller that read an old version is refused (:class:`StaleVersion`) instead of overwriting a newer state. Every
move carries the evidence that justifies it (see :func:`transition`), the database refuses the same illegal moves from raw
SQL, and a ``committed`` or ``failed`` proposal never changes again. ``unknown`` is left only through ``reconciled``: there
is no edge from ``unknown`` (or anywhere) back to ``dispatched``, so recovery is a question to the downstream and never a
blind second dispatch.

Not here: the connector capability record (B05.3), receipts (B05.4), the callback, the reconciler and the dispatcher
itself. A proposal is recorded as ``proposed``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Final, cast

from sqlalchemy import Table, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.contracts import validate_record
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberEffectReceipt,
    CaliberPolicyDecision,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowRun,
)
from caliber.db.transactions import begin_sqlite_transaction
from caliber.effects.inventory import EFFECT_CLASSES
from caliber.ownership import DEFAULT_LEASE, Lease, LeaseLost, assert_fenced
from caliber.run_manifest import FIXED_REGION, FIXED_TENANT_ID, canonical_bytes

INTENT_ALGORITHM: Final = "intent-v1"
PROPOSED: Final = "proposed"
SCHEMA_VERSION: Final = "v1"

BAD_FIELD: Final = "bad_field"
RUN_NOT_FOUND: Final = "run_not_found"
INTENT_CONFLICT: Final = "intent_conflict"
CORRUPT: Final = "corrupt"
UNSUPPORTED_ALGORITHM: Final = "unsupported_algorithm"
STORE_FAILED: Final = "store_failed"
ILLEGAL_TRANSITION: Final = "illegal_transition"
STALE_VERSION: Final = "stale_version"
STALE_OWNER: Final = "stale_owner"
LEASE_LOST: Final = "lease_lost"
NOT_ABANDONED: Final = "not_abandoned"
ABANDONED_REASON: Final = "abandoned_dispatch"
EVIDENCE_REQUIRED: Final = "evidence_required"
APPROVAL_REFUSED: Final = "approval_refused"
RECEIPT_REFUSED: Final = "receipt_refused"
RECEIPT_EXISTS: Final = "receipt_exists"

APPROVED: Final = "approved"
DISPATCHED: Final = "dispatched"
COMMITTED: Final = "committed"
FAILED: Final = "failed"
UNKNOWN: Final = "unknown"
RECONCILED: Final = "reconciled"
#: The seven states of ADR 0003.
STATES: Final = (PROPOSED, APPROVED, DISPATCHED, COMMITTED, FAILED, UNKNOWN, RECONCILED)
#: The legal moves, and the only ones. No state can be skipped (``proposed`` to ``dispatched`` is refused: even a
#: policy-approved read is recorded ``approved`` first), ``unknown`` has one way out (``reconciled``) and no way back to
#: ``dispatched``, and ``committed`` and ``failed`` are final. A test keeps migration 0124's trigger identical to this.
TRANSITIONS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        PROPOSED: frozenset({APPROVED, FAILED}),
        APPROVED: frozenset({DISPATCHED, FAILED}),
        DISPATCHED: frozenset({COMMITTED, FAILED, UNKNOWN}),
        UNKNOWN: frozenset({RECONCILED}),
        RECONCILED: frozenset({COMMITTED, FAILED}),
        COMMITTED: frozenset(),
        FAILED: frozenset(),
    }
)
SETTLED: Final = frozenset({COMMITTED, FAILED})
POLICY_DECISION: Final = "policy_decision"
#: The approval evidence written by the dispatcher after B06.3 has rechecked the
#: exact-intent request and the current authority.  ``approval_ref`` is the
#: runtime approval request id; the dispatcher passes the validator's
#: ``ApprovalCommit`` object so this low-level writer cannot be used as a
#: substitute for that recheck.
EXACT_INTENT_APPROVAL: Final = "exact_intent"
#: The policy action a decision must be for to approve a read effect, asked of the resource ``effect:<proposal id>``.
EFFECT_ACTION: Final = "resource.execute"

MAX_INTENT_BYTES: Final = 256 * 1024
MAX_ARGS_DEPTH: Final = 64
#: No control characters, NUL or unpaired surrogates: ids reach the database and the digest as they are.
_PRINTABLE: Final = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
#: The same rule, public for the other records that take an id or a name from a caller (approval requests, B06.1).
PRINTABLE: Final = _PRINTABLE
_LIMITS: Final = {
    "node_id": 128,
    "occurrence_id": 128,
    "workflow_run_id": 64,
    "connector": 128,
    "connector_version": 64,
    "resource": 512,
    "expected_resource_version": 256,
    "owner_id": 128,
    "reason": 128,
    "approval_ref": 128,
    "receipt_id": 64,
}


class ProposalError(ValueError):
    """A proposal cannot be recorded or read; ``code`` says why (never carries arguments or other payload)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProposalConflict(ProposalError):  # noqa: N818 - a conflict, named for what it says
    """The same logical key was presented with a different intent. The stored intent stands; answer 409."""

    def __init__(self, logical_key: str, stored_intent_hash: str, offered_intent_hash: str) -> None:
        super().__init__(
            INTENT_CONFLICT,
            f"logical effect {logical_key} already has intent {stored_intent_hash}; "
            f"a different intent {offered_intent_hash} was offered",
        )
        self.logical_key = logical_key
        self.stored_intent_hash = stored_intent_hash
        self.offered_intent_hash = offered_intent_hash


def _text(name: str, value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    limit = _LIMITS[name]
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or not _PRINTABLE.fullmatch(value)
    ):
        raise ProposalError(
            BAD_FIELD,
            f"{name} must be 1..{limit} printable characters, not blank, with no control characters"
            + (" (or null)" if nullable else ""),
        )
    return value


def _plain_json(value: object, depth: int = 0) -> Any:
    """``value`` if it is plain JSON of exact Python types, else :class:`ProposalError`.

    Exact types only, so two different Python values cannot share one canonical form: a tuple is not a list, an
    integer key is not its string, a ``bool`` or ``str`` subclass is not the plain value. Nesting is capped so the
    outcome does not depend on the interpreter's recursion limit.
    """
    if depth > MAX_ARGS_DEPTH:
        raise ProposalError(BAD_FIELD, f"args nest deeper than {MAX_ARGS_DEPTH} levels")
    kind = type(value)
    if value is None or kind in (bool, str, int):
        return value
    if kind is float:
        if not math.isfinite(value):  # type: ignore[arg-type]
            raise ProposalError(BAD_FIELD, "args must be plain JSON (no NaN or Infinity)")
        return value
    if kind is list:
        return [_plain_json(item, depth + 1) for item in value]  # type: ignore[attr-defined]
    if kind is dict:
        if any(type(key) is not str for key in value):  # type: ignore[attr-defined]
            raise ProposalError(BAD_FIELD, "args object keys must be strings")
        return {key: _plain_json(item, depth + 1) for key, item in value.items()}  # type: ignore[attr-defined]
    raise ProposalError(
        BAD_FIELD, "args must be plain JSON: objects, arrays, strings, numbers, booleans and null"
    )


@dataclass(frozen=True, eq=False)
class Intent:
    """What exactly is to be done, in one canonical form; its digest is the ``intent_hash`` every consumer reuses.

    The canonical bytes are computed at construction, so a later change to the ``args`` mapping the caller passed in
    cannot change what is recorded or digested. ``args`` must be plain JSON of exact Python types (``dict`` with string
    keys, ``list``, ``str``, ``int``, finite ``float``, ``bool``, ``None``), so two different Python values never share
    a digest. The converse is not promised: ``intent-v1`` is not RFC 8785, so ``1`` and ``1.0``, ``0.0`` and ``-0.0``,
    and NFC and NFD spellings of a string are different intents (a retry that changes how it spells one gets a conflict,
    never a silent match). Two intents are equal when their canonical bytes are.
    """

    connector: str
    connector_version: str
    effect_class: str
    resource: str
    args: Mapping[str, Any]
    expected_resource_version: str | None = None
    algorithm: str = field(default=INTENT_ALGORITHM, init=False)
    canonical: bytes = field(init=False, repr=False)
    plain_args: dict[str, Any] = field(init=False, repr=False)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Intent) and self.canonical == other.canonical

    def __hash__(self) -> int:
        return hash(self.canonical)

    def __post_init__(self) -> None:
        _text("connector", self.connector)
        _text("connector_version", self.connector_version)
        _text("resource", self.resource)
        _text("expected_resource_version", self.expected_resource_version, nullable=True)
        if self.effect_class not in EFFECT_CLASSES:
            raise ProposalError(
                BAD_FIELD, f"effect_class must be one of {', '.join(EFFECT_CLASSES)}"
            )
        if not isinstance(self.args, Mapping):
            raise ProposalError(BAD_FIELD, "args must be a JSON object")
        args = _plain_json(dict(self.args))
        try:
            data = canonical_bytes(
                {
                    "algorithm": INTENT_ALGORITHM,
                    "connector": {"name": self.connector, "version": self.connector_version},
                    "effect_class": self.effect_class,
                    "target": {"resource": self.resource},
                    "args": args,
                    "expected_resource_version": self.expected_resource_version,
                }
            )
            plain = json.loads(data)["args"]
        except (TypeError, ValueError, RecursionError) as exc:
            raise ProposalError(BAD_FIELD, "args must be plain JSON") from exc
        if len(data) > MAX_INTENT_BYTES:
            raise ProposalError(BAD_FIELD, f"the intent exceeds {MAX_INTENT_BYTES} bytes")
        object.__setattr__(self, "canonical", data)
        object.__setattr__(self, "plain_args", plain)

    @property
    def digest(self) -> str:
        """``sha256:`` and the lowercase hex SHA-256 of :attr:`canonical`."""
        return intent_digest(self.canonical)


def intent_digest(canonical: bytes) -> str:
    """The ``intent_hash`` of canonical intent bytes: ``sha256:`` and 64 lowercase hex digits."""
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def logical_key(*, workflow_run_id: str, node_id: str, occurrence_id: str) -> str:
    """The stable key of one logical effect: the SHA-256 of ``(deployment, run, node, occurrence)``.

    The deployment is this installation's one configured organization (ADR 0001), never taken from a caller. Each part
    is a separate JSON string, so ``("a:b", "c")`` and ``("a", "b:c")`` cannot collide.
    """
    return hashlib.sha256(
        canonical_bytes(
            {
                "tenant_id": FIXED_TENANT_ID,
                "workflow_run_id": workflow_run_id,
                "node_id": node_id,
                "occurrence_id": occurrence_id,
            }
        )
    ).hexdigest()


def new_proposal_id() -> str:
    return f"prop-{uuid.uuid4().hex}"


@dataclass(frozen=True)
class RecordedProposal:
    """The outcome of :func:`record_proposal`: ``created`` is ``False`` when the same effect was already recorded."""

    proposal_id: str
    logical_key: str
    intent_hash: str
    state: str
    created: bool


def record_proposal(
    session: Session,
    *,
    workflow_run_id: str,
    node_id: str,
    occurrence_id: str,
    intent: Intent,
) -> RecordedProposal:
    """Record the proposal for this logical effect in the caller's transaction (flushes, never commits).

    The same ``(run, node, occurrence)`` with the same intent returns the proposal that is already there (``created`` is
    ``False``); with a different intent raises :class:`ProposalConflict`. A new occurrence records a new proposal even
    when its intent is identical to an earlier one's. Raises :class:`ProposalError` for a malformed field or an unknown
    run.
    """
    run_id = _text("workflow_run_id", workflow_run_id)
    node = _text("node_id", node_id)
    occurrence = _text("occurrence_id", occurrence_id)
    if not isinstance(intent, Intent):
        raise ProposalError(BAD_FIELD, "intent must be an Intent")
    assert (
        run_id is not None and node is not None and occurrence is not None
    )  # for the type checker
    key = logical_key(workflow_run_id=run_id, node_id=node, occurrence_id=occurrence)
    begin_sqlite_transaction(session)
    if session.get(CaliberWorkflowRun, run_id) is None:
        raise ProposalError(RUN_NOT_FOUND, f"workflow run {run_id!r} does not exist")
    existing = _by_key(session, key)
    if existing is not None:
        return _same_or_conflict(existing, intent)
    row = CaliberEffectProposal(
        proposal_id=new_proposal_id(),
        tenant_id=FIXED_TENANT_ID,
        workflow_run_id=run_id,
        node_id=node,
        occurrence_id=occurrence,
        logical_key=key,
        connector=intent.connector,
        connector_version=intent.connector_version,
        effect_class=intent.effect_class,
        resource=intent.resource,
        args=copy.deepcopy(intent.plain_args),
        expected_resource_version=intent.expected_resource_version,
        intent_algorithm=intent.algorithm,
        intent_bytes=intent.canonical,
        intent_hash=intent.digest,
        state=PROPOSED,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # Only the logical key means "another transaction recorded it first". Any other constraint failure is a defect
        # the caller must see: reporting it as an existing proposal would acknowledge an effect nobody recorded.
        winner = _by_key(session, key, refresh=True)
        if winner is None:
            # Not the key: a constraint the caller cannot fix by retrying. The driver's error text carries the
            # statement's parameters (the arguments), so it is not passed on.
            raise ProposalError(STORE_FAILED, "the proposal could not be stored") from None
        return _same_or_conflict(winner, intent)
    return RecordedProposal(row.proposal_id, key, row.intent_hash, row.state, created=True)


def _by_key(session: Session, key: str, *, refresh: bool = False) -> CaliberEffectProposal | None:
    statement = select(CaliberEffectProposal).where(CaliberEffectProposal.logical_key == key)
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def _same_or_conflict(existing: CaliberEffectProposal, intent: Intent) -> RecordedProposal:
    if existing.intent_hash != intent.digest:
        raise ProposalConflict(existing.logical_key, existing.intent_hash, intent.digest)
    return RecordedProposal(
        existing.proposal_id,
        existing.logical_key,
        existing.intent_hash,
        existing.state,
        created=False,
    )


# ---------------------------------------------------------------- states (B05.2)


class StaleVersion(ProposalError):  # noqa: N818 - a refusal, named for what it says
    """The caller's version of the proposal is not the current one (or the state it wanted was reached by another)."""

    def __init__(
        self, proposal_id: str, expected: int, current_version: int, current_state: str
    ) -> None:
        super().__init__(
            STALE_VERSION,
            f"proposal {proposal_id} is at version {current_version} ({current_state}); version {expected} was expected",
        )
        self.proposal_id = proposal_id
        self.expected_version = expected
        self.current_version = current_version
        self.current_state = current_state


@dataclass(frozen=True)
class Approval:
    """What approved a proposal. Only a recorded policy decision can approve a read; anything else is refused."""

    kind: str
    ref: str


@dataclass(frozen=True)
class ProposalState:
    """A proposal's state, its compare-and-set ``version`` and the evidence recorded with its moves."""

    proposal_id: str
    state: str
    version: int
    attempt: int | None
    owner_id: str | None
    dispatch_token: int | None
    reason: str | None
    approval_kind: str | None
    approval_ref: str | None
    receipt_id: str | None
    dispatched_at: datetime | None
    unknown_at: datetime | None
    settled_at: datetime | None


@dataclass(frozen=True)
class Transitioned:
    """The outcome of :func:`transition`: ``applied`` is ``False`` when the same move had already been recorded."""

    proposal_id: str
    state: str
    version: int
    applied: bool


@dataclass(frozen=True)
class ProposalView:
    """What a connector needs to check a downstream's stored request against a proposal."""

    proposal_id: str
    logical_key: str
    connector: str
    connector_version: str
    effect_class: str
    resource: str
    expected_resource_version: str | None
    intent_hash: str
    attempt: int | None
    state: str


def proposal_view(session: Session, proposal_id: str) -> ProposalView:
    """The identity and intent of ``proposal_id`` (``KeyError`` for an unknown id)."""
    row = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
    if row is None:
        raise KeyError(proposal_id)
    return ProposalView(
        proposal_id=row.proposal_id,
        logical_key=row.logical_key,
        connector=row.connector,
        connector_version=row.connector_version,
        effect_class=row.effect_class,
        resource=row.resource,
        expected_resource_version=row.expected_resource_version,
        intent_hash=row.intent_hash,
        attempt=row.attempt,
        state=row.state,
    )


def effect_resource(proposal_id: str) -> str:
    """The resource a policy decision must have been asked about to approve this proposal."""
    return f"effect:{proposal_id}"


def proposal_state(session: Session, proposal_id: str) -> ProposalState:
    """The current state and version of ``proposal_id`` (``KeyError`` for an unknown id): what a caller passes back as
    ``expected_version``."""
    row = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
    if row is None:
        raise KeyError(proposal_id)
    return ProposalState(
        proposal_id=row.proposal_id,
        state=row.state,
        version=row.version,
        attempt=row.attempt,
        owner_id=row.owner_id,
        dispatch_token=row.dispatch_token,
        reason=row.reason,
        approval_kind=row.approval_kind,
        approval_ref=row.approval_ref,
        receipt_id=row.receipt_id,
        dispatched_at=row.dispatched_at,
        unknown_at=row.unknown_at,
        settled_at=row.settled_at,
    )


def _naive_utc(moment: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if moment is None else moment
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


#: The largest attempt number: it is stored in a 32-bit column, and a larger value must be a refusal, not a database error.
MAX_ATTEMPT: Final = 2**31 - 1


def _count(name: str, value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_ATTEMPT:
        raise ProposalError(BAD_FIELD, f"{name} must be an integer from 1 to {MAX_ATTEMPT}")
    return value


def transition(
    session: Session,
    proposal_id: str,
    to_state: str,
    *,
    expected_version: int,
    approval: Approval | None = None,
    owner_id: str | None = None,
    attempt: int | None = None,
    reason: str | None = None,
    receipt_id: str | None = None,
    lease: Lease | None = None,
    now: datetime | None = None,
) -> Transitioned:
    """Move a proposal to ``to_state`` if ``expected_version`` is still current, in the caller's transaction (never commits).

    The UPDATE is conditioned on the version the caller read and the state it read, so two actors that both read the
    same version cannot both move it: the second is refused with :class:`StaleVersion`. The same actor repeating a move
    that was already recorded (same target, same evidence, version advanced by exactly that move) gets ``applied=False``
    and no second write. A move that is not in :data:`TRANSITIONS` (including to the same state, and any move out of
    ``committed`` or ``failed``) raises ``ProposalError`` with code ``illegal_transition``.

    Each move needs its evidence, and evidence that does not belong to the move is refused:

    * ``approved``: ``approval``, a ``policy_decision`` that is an *allow* for this proposal's run, asked about action
      ``resource.execute`` on ``effect:<proposal id>``, and only for a ``read``. Any other effect class needs an
      exact-intent approval (B06), which is not verifiable yet: ``approval_refused``, so nothing but a read reaches
      ``dispatched`` today.
    * ``dispatched``: ``owner_id`` and ``attempt`` (recorded with ``dispatched_at``).
    * out of ``dispatched`` (``committed``, ``failed``, ``unknown``): the same ``owner_id`` and ``attempt`` that dispatched
      it; a stale worker is ``stale_owner`` and records nothing. ``failed`` and ``unknown`` also need a ``reason``.
    * into and out of ``dispatched`` (B05.5a, #734): the ``lease`` the owner captured when it acquired the execution-owner
      lease, whose owner is ``owner_id``. It is compared, by equality on the persisted owner and fencing token and against
      the database's clock, in this transaction and before the write (:func:`caliber.ownership.assert_fenced`; on
      PostgreSQL the lease row stays locked until the transaction ends, so a takeover waits for the write instead of
      overtaking it). A lease that is expired, taken over (a newer token) or never issued (a future token) is
      ``lease_lost`` and nothing is recorded: a stale worker cannot commit, fail or mark ``unknown`` itself. The proposal
      stays ``dispatched`` for the reconciler, which asks the downstream by key. No lease is ``evidence_required``, and a
      lease that is not the execution-owner lease (any other name) is ``lease_lost``. A fence that could not be *asked* (the
      database failed, timed out or was serialized away) is ``store_failed``, not ``lease_lost``: roll back and retry.
    * ``failed`` (from any state), ``unknown`` and ``reconciled``: a short ``reason`` code (never a payload).
    * ``committed``: ``receipt_id``, a verified receipt (:mod:`caliber.effects.receipts`) recorded for *this* proposal and
      dispatch attempt, read back from the downstream and matching the proposal's key and intent. A proposal never
      becomes ``committed`` on the strength of a 2xx alone: with no receipt it stays ``unknown``.

    Under READ COMMITTED a conflicting move is :class:`StaleVersion` (or ``applied=False`` for a repeat). Under
    REPEATABLE READ or SERIALIZABLE PostgreSQL itself refuses an UPDATE of a row another transaction changed after the
    snapshot (a serialization failure): the caller rolls back and retries from a fresh read, as with the grants.
    A repeat is only recognised while the proposal is still where the move left it: once it has moved on, the retry is
    :class:`StaleVersion` and the caller reads ``current_state``. A repeat of a move into or out of ``dispatched`` is fenced
    like the move itself: a worker that lost its lease is not told "recorded, you own this attempt" (``applied=False``) and
    goes on to send the effect; it is ``lease_lost``.
    """
    if type(expected_version) is not int or expected_version < 1:
        raise ProposalError(BAD_FIELD, "expected_version must be a positive integer")
    if to_state not in STATES:
        raise ProposalError(BAD_FIELD, f"to_state must be one of {', '.join(STATES)}")
    owner = _text("owner_id", owner_id, nullable=True)
    why = _text("reason", reason, nullable=True)
    tries = _count("attempt", attempt)
    begin_sqlite_transaction(session)
    row = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
    if row is None:
        raise KeyError(proposal_id)
    offered = _offered(approval, owner, tries, why, _text("receipt_id", receipt_id, nullable=True))
    offered |= _dispatch_token(to_state, lease)  # a dispatch under another token is not a repeat
    if (
        row.version == expected_version + 1
        and row.state == to_state
        and _complete(row, offered)
        and _recorded(row, offered)
    ):
        _fence_a_repeat(session, row, to_state, owner, lease)
        return Transitioned(row.proposal_id, row.state, row.version, applied=False)
    if row.version != expected_version:
        raise StaleVersion(row.proposal_id, expected_version, row.version, row.state)
    if to_state not in TRANSITIONS[row.state]:
        raise ProposalError(
            ILLEGAL_TRANSITION,
            f"proposal {row.proposal_id} cannot move from {row.state} to {to_state}",
        )
    evidence = _evidence(session, row, to_state, approval, owner, tries, why, receipt_id)
    _check_owner(row, owner, tries)
    if DISPATCHED in (row.state, to_state):
        _fence(session, row, owner, lease)
    evidence |= _dispatch_token(to_state, lease)
    values: dict[str, Any] = {"state": to_state, "version": expected_version + 1, **evidence}
    moment = _naive_utc(now)
    for state, column in ((DISPATCHED, "dispatched_at"), (UNKNOWN, "unknown_at")):
        if to_state == state:
            values[column] = moment
    if to_state in SETTLED:
        values["settled_at"] = moment
    table = cast(Table, CaliberEffectProposal.__table__)
    result = session.execute(
        update(table)
        .where(
            table.c.proposal_id == row.proposal_id,
            table.c.version == expected_version,
            table.c.state == row.state,
        )
        .values(**values)
    )
    session.expire(row)
    if not getattr(
        result, "rowcount", 0
    ):  # another transaction moved it between the read and the update
        current = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
        if (
            current is not None
            and current.version == expected_version + 1
            and current.state == to_state
            and _complete(current, evidence)
            and _recorded(current, evidence)
        ):
            return Transitioned(current.proposal_id, current.state, current.version, applied=False)
        assert current is not None  # a proposal is never deleted
        raise StaleVersion(proposal_id, expected_version, current.version, current.state)
    return Transitioned(row.proposal_id, to_state, expected_version + 1, applied=True)


def abandon_dispatch(
    session: Session,
    proposal_id: str,
    *,
    expected_version: int,
    lease: Lease,
    now: datetime | None = None,
) -> Transitioned:
    """``dispatched`` to ``unknown`` for an attempt whose dispatching lease has since been taken over (B05.5b, #742).

    The reconciler's entry point, under *its own* lease (the lease it holds now, not the dead owner's, so the ``owner_id`` and
    ``attempt`` of the dispatch are not presented): in the caller's transaction it compares that lease
    (:func:`fence_owner`) and refuses unless the proposal is ``dispatched`` at ``expected_version`` and was dispatched under a
    lower fencing token (``dispatch_token``; a ``NULL`` one proves nothing and is never abandoned). It records
    ``unknown`` with the reason ``abandoned_dispatch``: the attempt may have been sent, and only a query by key can say.
    Never commits, and never re-dispatches: ``unknown`` has no edge back to ``dispatched``.
    """
    if type(expected_version) is not int or expected_version < 1:
        raise ProposalError(BAD_FIELD, "expected_version must be a positive integer")
    fence_owner(session, proposal_id, lease.owner_id if isinstance(lease, Lease) else None, lease)
    begin_sqlite_transaction(session)
    row = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
    if row is None:
        raise KeyError(proposal_id)
    if row.version != expected_version:
        raise StaleVersion(proposal_id, expected_version, row.version, row.state)
    if row.state != DISPATCHED:
        raise ProposalError(
            ILLEGAL_TRANSITION,
            f"proposal {proposal_id} is {row.state}: only a dispatched attempt is abandoned",
        )
    if row.dispatch_token is None or row.dispatch_token >= lease.fencing_token:
        raise ProposalError(
            NOT_ABANDONED,
            f"proposal {proposal_id} was dispatched under the lease that is still current (or under one with no recorded token)",
        )
    table = cast(Table, CaliberEffectProposal.__table__)
    result = session.execute(
        update(table)
        .where(
            table.c.proposal_id == proposal_id,
            table.c.version == expected_version,
            table.c.state == DISPATCHED,
        )
        .values(
            state=UNKNOWN,
            version=expected_version + 1,
            reason=ABANDONED_REASON,
            unknown_at=_naive_utc(now),
        )
    )
    session.expire(row)
    if not getattr(result, "rowcount", 0):  # moved between the read and the update
        current = session.get(CaliberEffectProposal, proposal_id, populate_existing=True)
        assert current is not None  # a proposal is never deleted
        raise StaleVersion(proposal_id, expected_version, current.version, current.state)
    return Transitioned(proposal_id, UNKNOWN, expected_version + 1, applied=True)


def _offered(
    approval: Approval | None,
    owner: str | None,
    attempt: int | None,
    reason: str | None,
    receipt: str | None = None,
) -> dict[str, Any]:
    """The evidence the caller put forward, as the columns it would be recorded in (nothing verified)."""
    offered: dict[str, Any] = {}
    if owner is not None:
        offered["owner_id"] = owner
    if attempt is not None:
        offered["attempt"] = attempt
    if reason is not None:
        offered["reason"] = reason
    if receipt is not None:
        offered["receipt_id"] = receipt
    columns = _approval_columns(approval)
    if columns is not None:
        offered.update(columns)
    return offered


def _dispatch_token(to_state: str, lease: Lease | None) -> dict[str, Any]:
    """The fencing token a dispatch records (nothing for any other move, or for a lease that is not one)."""
    if to_state == DISPATCHED and isinstance(lease, Lease):
        return {"dispatch_token": lease.fencing_token}
    return {}


def _complete(row: CaliberEffectProposal, offered: Mapping[str, Any]) -> bool:
    """Whether ``offered`` carries all the evidence the move into ``row.state`` records or requires.

    A repeat is recognised only by an actor that can present the same evidence, so one that cannot prove who it is (no
    owner, no reason, no approval) is never told "already recorded". Whether the move came out of ``dispatched`` is read
    from the timestamps: ``dispatched_at`` set and ``unknown_at`` not (a ``committed`` or ``failed`` reached through
    ``unknown`` came out of ``reconciled``, which needs no owner).
    """
    needed: set[str] = set()
    if row.state == APPROVED:
        needed |= {"approval_kind", "approval_ref"}
    if row.state in (FAILED, UNKNOWN, RECONCILED):
        needed.add("reason")
    if row.state == COMMITTED:
        needed.add("receipt_id")
    from_dispatched = (
        row.state == DISPATCHED
        or (
            row.state in (COMMITTED, FAILED)
            and row.dispatched_at is not None
            and row.unknown_at is None
        )
        or row.state == UNKNOWN
    )
    if from_dispatched:
        needed |= {"owner_id", "attempt"}
    return needed <= set(offered)


def _recorded(row: CaliberEffectProposal, evidence: Mapping[str, Any]) -> bool:
    return all(getattr(row, key) == value for key, value in evidence.items())


def _evidence(
    session: Session,
    row: CaliberEffectProposal,
    to_state: str,
    approval: Approval | None,
    owner: str | None,
    attempt: int | None,
    reason: str | None,
    receipt_id: str | None = None,
) -> dict[str, Any]:
    """The evidence columns this move writes; refuses a move without its evidence and evidence that is not its own."""
    values: dict[str, Any] = {}
    wants_reason = to_state in (FAILED, UNKNOWN, RECONCILED)
    if (approval is not None) != (to_state == APPROVED):
        raise ProposalError(
            EVIDENCE_REQUIRED if to_state == APPROVED else BAD_FIELD,
            "an approval is required to approve a proposal and belongs to no other move",
        )
    if (owner is not None or attempt is not None) and DISPATCHED not in (to_state, row.state):
        raise ProposalError(
            BAD_FIELD, "owner_id and attempt belong to the moves of a dispatched proposal"
        )
    if reason is not None and not wants_reason and to_state != COMMITTED:
        raise ProposalError(BAD_FIELD, f"a reason does not belong to a move to {to_state}")
    if wants_reason:
        if reason is None:
            raise ProposalError(EVIDENCE_REQUIRED, f"a move to {to_state} needs a reason")
        values["reason"] = reason
    elif reason is not None:
        values["reason"] = reason
    if (
        to_state == FAILED
        and session.execute(
            select(CaliberEffectReceipt.receipt_id)
            .where(CaliberEffectReceipt.proposal_id == row.proposal_id)
            .limit(1)
        ).first()
    ):
        raise ProposalError(
            RECEIPT_EXISTS,
            f"proposal {row.proposal_id} has a verified receipt: the downstream says it committed, so it cannot be failed",
        )
    if (receipt_id is not None) != (to_state == COMMITTED):
        raise ProposalError(
            EVIDENCE_REQUIRED if to_state == COMMITTED else BAD_FIELD,
            "a verified receipt is required to commit a proposal and belongs to no other move",
        )
    if receipt_id is not None:
        _verify_receipt(session, row, receipt_id)
        values["receipt_id"] = receipt_id
    if approval is not None:
        _verify_approval(session, row, approval)
        columns = _approval_columns(approval)
        assert columns is not None
        values.update(columns)
    if to_state == DISPATCHED:
        if owner is None or attempt is None:
            raise ProposalError(
                EVIDENCE_REQUIRED, "dispatching a proposal needs an owner_id and an attempt"
            )
        values["owner_id"] = owner
        values["attempt"] = attempt
    return values


def _check_owner(row: CaliberEffectProposal, owner: str | None, attempt: int | None) -> None:
    if row.state != DISPATCHED:
        return
    if owner is None or attempt is None:
        raise ProposalError(
            EVIDENCE_REQUIRED,
            "a move out of dispatched needs the owner_id and attempt that dispatched it",
        )
    if owner != row.owner_id or attempt != row.attempt:
        raise ProposalError(
            STALE_OWNER,
            f"proposal {row.proposal_id} was dispatched by another owner or attempt: nothing recorded",
        )


def fence_owner(session: Session, proposal_id: str, owner: str | None, lease: Lease | None) -> None:
    """Refuse unless ``lease`` is, right now, the held execution-owner lease of ``owner`` (B05.5a, #734).

    The check :func:`transition` makes before every write into or out of ``dispatched``, exposed so a writer that records
    something else first (a receipt) can take the lease row *first* and keep one lock order: lease, then receipt, then
    proposal. Never commits; on PostgreSQL the lease row stays locked until the caller's transaction ends.
    """
    if lease is None:
        raise ProposalError(
            EVIDENCE_REQUIRED,
            "a move into or out of dispatched needs the lease the owner captured when it acquired",
        )
    if not isinstance(lease, Lease):
        raise ProposalError(BAD_FIELD, "lease must be the Lease the owner captured")
    if lease.owner_id != owner:
        raise ProposalError(
            STALE_OWNER,
            f"the lease belongs to {lease.owner_id!r}, not to the owner {owner!r} of this move: nothing recorded",
        )
    if lease.name != DEFAULT_LEASE:
        raise ProposalError(
            LEASE_LOST,
            f"proposal {proposal_id}: {lease.name!r} is not the execution-owner lease; nothing recorded",
        )
    try:
        assert_fenced(session, lease)
    except LeaseLost as exc:
        store_failed = (
            exc.__cause__ is not None
        )  # the store failed: not the same as having lost the lease
        if store_failed:
            raise ProposalError(
                STORE_FAILED,
                f"proposal {proposal_id}: the lease could not be checked ({exc.reason}); nothing recorded, roll back and retry",
            ) from exc
        raise ProposalError(
            LEASE_LOST,
            f"proposal {proposal_id}: {exc.reason}; nothing recorded, the reconciler asks the downstream",
        ) from exc


def _fence(
    session: Session, row: CaliberEffectProposal, owner: str | None, lease: Lease | None
) -> None:
    fence_owner(session, row.proposal_id, owner, lease)


def _fence_a_repeat(
    session: Session,
    row: CaliberEffectProposal,
    to_state: str,
    owner: str | None,
    lease: Lease | None,
) -> None:
    """The owner's move repeated (dispatching, or settling what it dispatched) is fenced like the move itself."""
    if owner is not None or to_state == DISPATCHED:
        _fence(session, row, owner, lease)


def _verify_receipt(session: Session, row: CaliberEffectProposal, receipt_id: str) -> None:
    """The receipt exists, was read back from the downstream (``query``) and answers *this* proposal's attempt."""
    receipt = session.get(CaliberEffectReceipt, receipt_id)
    if (
        receipt is None
        or receipt.proposal_id != row.proposal_id
        or receipt.attempt != row.attempt
        or receipt.logical_key != row.logical_key
        or receipt.intent_hash != row.intent_hash
        or receipt.verified_by != "query"
    ):
        raise ProposalError(
            RECEIPT_REFUSED,
            f"receipt {receipt_id!r} is not a verified receipt for this proposal and attempt",
        )


def _approval_columns(approval: object | None) -> dict[str, str] | None:
    """Return the persisted approval evidence for a policy or exact-intent commit."""
    if isinstance(approval, Approval):
        return {"approval_kind": approval.kind, "approval_ref": approval.ref}
    # Import lazily: approval_policy already depends on this module for the
    # canonical proposal reader, so importing it at module load would cycle.
    from caliber.workflows.approval_policy import ApprovalCommit  # noqa: PLC0415

    if isinstance(approval, ApprovalCommit):
        return {"approval_kind": EXACT_INTENT_APPROVAL, "approval_ref": approval.approval_id}
    return None


def _verify_approval(session: Session, row: CaliberEffectProposal, approval: object) -> None:
    from caliber.workflows.approval_policy import ApprovalCommit  # noqa: PLC0415

    if isinstance(approval, ApprovalCommit):
        request = session.get(CaliberRuntimeApprovalRequest, approval.approval_id)
        if (
            request is None
            or request.runtime_approval_id != approval.approval_id
            or request.proposal_id != row.proposal_id
            or request.workflow_run_id != row.workflow_run_id
            or request.status != "approved"
            or request.consumed_at is not None
            or request.intent_hash != row.intent_hash
            or request.expected_resource_version != row.expected_resource_version
            or request.workflow_run_id != approval.run_id
            or request.decided_by != approval.approver_id
            or request.evidence_hash != approval.evidence_hash
            or request.policy_digest != approval.policy_digest
            or approval.proposal_id != row.proposal_id
            or approval.intent_hash != row.intent_hash
        ):
            raise ProposalError(
                APPROVAL_REFUSED,
                "the exact-intent approval is not bound to this proposal",
            )
        return
    if not isinstance(approval, Approval) or approval.kind != POLICY_DECISION:
        raise ProposalError(
            APPROVAL_REFUSED,
            "only a recorded policy decision can approve a proposal; exact-intent approvals arrive with B06",
        )
    _text("approval_ref", approval.ref)
    if row.effect_class != "read":
        raise ProposalError(
            APPROVAL_REFUSED,
            f"a {row.effect_class} effect needs an exact-intent approval (B06), which is not available yet",
        )
    decision = session.get(CaliberPolicyDecision, approval.ref)
    if (
        decision is None
        or decision.result != "allow"
        or decision.workflow_run_id != row.workflow_run_id
        or decision.action != EFFECT_ACTION
        or decision.resource != effect_resource(row.proposal_id)
    ):
        raise ProposalError(
            APPROVAL_REFUSED,
            f"decision {approval.ref!r} is not an allow of {EFFECT_ACTION} on {effect_resource(row.proposal_id)} for this run",
        )


def read_proposal(session: Session, proposal_id: str) -> dict[str, Any]:
    """The ``proposal`` v1 record (ADR 0003) for ``proposal_id``, after recomputing its intent from the stored columns.

    The record carries three optional v1 fields beside the contract's own: ``occurrence_id``, ``connector_version`` and
    ``intent_algorithm``. Raises ``KeyError`` for an unknown id, :class:`ProposalError` with code ``unsupported_algorithm``
    for an intent this build cannot recompute, and with code ``corrupt`` when the stored bytes or digest do not match the
    columns, or the logical key does not match the identity it names.
    """
    row = session.get(CaliberEffectProposal, proposal_id)
    if row is None:
        raise KeyError(proposal_id)
    if row.intent_algorithm != INTENT_ALGORITHM:
        raise ProposalError(
            UNSUPPORTED_ALGORITHM,
            f"proposal {proposal_id} uses intent algorithm {row.intent_algorithm!r}; supported: {INTENT_ALGORITHM}",
        )
    try:
        rebuilt = Intent(
            connector=row.connector,
            connector_version=row.connector_version,
            effect_class=row.effect_class,
            resource=row.resource,
            args=row.args,
            expected_resource_version=row.expected_resource_version,
        )
    except ProposalError as exc:
        raise ProposalError(
            CORRUPT, f"proposal {proposal_id} holds an invalid intent: {exc}"
        ) from exc
    if (
        bytes(row.intent_bytes) != rebuilt.canonical
        or row.intent_hash != rebuilt.digest
        or row.logical_key
        != logical_key(
            workflow_run_id=row.workflow_run_id,
            node_id=row.node_id,
            occurrence_id=row.occurrence_id,
        )
        or row.tenant_id != FIXED_TENANT_ID
    ):
        raise ProposalError(CORRUPT, f"proposal {proposal_id} does not match its recorded intent")
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": row.proposal_id,
        "workflow_run_id": row.workflow_run_id,
        "node_id": row.node_id,
        "logical_key": row.logical_key,
        "intent_hash": row.intent_hash,
        "effect_class": row.effect_class,
        "target": {"connector": row.connector, "resource": row.resource},
        "args": copy.deepcopy(rebuilt.plain_args),
        "expected_resource_version": row.expected_resource_version,
        "state": row.state,
        "tenant_id": row.tenant_id,
        "region": FIXED_REGION,
        "occurrence_id": row.occurrence_id,
        "connector_version": row.connector_version,
        "intent_algorithm": row.intent_algorithm,
    }
    validate_record("proposal", record)
    return record


__all__ = [
    "ABANDONED_REASON",
    "APPROVAL_REFUSED",
    "APPROVED",
    "BAD_FIELD",
    "COMMITTED",
    "CORRUPT",
    "DISPATCHED",
    "EFFECT_ACTION",
    "EVIDENCE_REQUIRED",
    "EXACT_INTENT_APPROVAL",
    "FAILED",
    "ILLEGAL_TRANSITION",
    "INTENT_ALGORITHM",
    "INTENT_CONFLICT",
    "LEASE_LOST",
    "MAX_ARGS_DEPTH",
    "MAX_INTENT_BYTES",
    "NOT_ABANDONED",
    "POLICY_DECISION",
    "PRINTABLE",
    "PROPOSED",
    "RECEIPT_EXISTS",
    "RECEIPT_REFUSED",
    "RECONCILED",
    "RUN_NOT_FOUND",
    "SETTLED",
    "STALE_OWNER",
    "STALE_VERSION",
    "STATES",
    "STORE_FAILED",
    "TRANSITIONS",
    "UNKNOWN",
    "UNSUPPORTED_ALGORITHM",
    "Approval",
    "Intent",
    "ProposalConflict",
    "ProposalError",
    "ProposalState",
    "ProposalView",
    "RecordedProposal",
    "StaleVersion",
    "Transitioned",
    "abandon_dispatch",
    "effect_resource",
    "fence_owner",
    "intent_digest",
    "logical_key",
    "new_proposal_id",
    "proposal_state",
    "proposal_view",
    "read_proposal",
    "record_proposal",
    "transition",
]
