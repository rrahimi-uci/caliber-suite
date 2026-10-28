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

Not here: moving a proposal through its states (B05.2), the connector capability record (B05.3), receipts (B05.4) and
the dispatcher itself. ``state`` is recorded as ``proposed`` and nothing in this module changes it.
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
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.contracts import validate_record
from caliber.db.models import CaliberEffectProposal, CaliberWorkflowRun
from caliber.db.transactions import begin_sqlite_transaction
from caliber.effects.inventory import EFFECT_CLASSES
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

MAX_INTENT_BYTES: Final = 256 * 1024
MAX_ARGS_DEPTH: Final = 64
#: No control characters, NUL or unpaired surrogates: ids reach the database and the digest as they are.
_PRINTABLE: Final = re.compile(r"[^\x00-\x1f\x7f-\x9f\ud800-\udfff]+")
_LIMITS: Final = {
    "node_id": 128,
    "occurrence_id": 128,
    "workflow_run_id": 64,
    "connector": 128,
    "connector_version": 64,
    "resource": 512,
    "expected_resource_version": 256,
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
    "BAD_FIELD",
    "CORRUPT",
    "INTENT_ALGORITHM",
    "INTENT_CONFLICT",
    "MAX_ARGS_DEPTH",
    "MAX_INTENT_BYTES",
    "PROPOSED",
    "RUN_NOT_FOUND",
    "STORE_FAILED",
    "UNSUPPORTED_ALGORITHM",
    "Intent",
    "ProposalConflict",
    "ProposalError",
    "RecordedProposal",
    "intent_digest",
    "logical_key",
    "new_proposal_id",
    "read_proposal",
    "record_proposal",
]
