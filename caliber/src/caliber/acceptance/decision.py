"""Acceptance decisions: what was decided about one candidate of a run, kept as immutable evidence (B09.2a, #784).

:func:`judge` is pure: it runs a predicate set (:mod:`caliber.acceptance.predicates`) over the observations a caller read and
returns a :class:`Verdict`. :func:`record_decision` judges and stores that verdict in the caller's transaction, in
``caliber_acceptance_decisions`` (migration 0135). The two are separate so a replay (B09.4, #805) can judge a recorded
observation again without writing anything.

**What identifies a decision.** ``(workflow_run_id, candidate_id, criteria_version, observation_digest)`` is unique. The digest is
the SHA-256 of the canonical JSON of the observations the predicates saw. Recording the same decision again returns the stored
row (``created`` false) and changes nothing; a new candidate, a new criteria version or any other observation is a new decision,
so a run can hold several (a retry's observations differ from the first attempt's). A different verdict under one key is a
:data:`DECISION_CONFLICT` and never replaces the first (it means the predicates changed without a new criteria version). The
observation bytes are not stored here (B09.3, #799 records them); only their digest is.

**What decides the outcome.** Only the criteria. Every predicate is critical: the decision is ``accepted`` if and only if
none failed, and there is no argument through which an HTTP status, a model's text or a transport's success could change that.
The table's CHECK constraints refuse an ``accepted`` row with a failed criterion even from raw SQL.

**Unreadable observations are a rejection, not a crash.** ``Observations(...)`` refuses what is not JSON-shaped (NaN, a
``bytes``, a hostile container, nesting past 32 levels). A downstream that answered with such a value did not prove the effect, so
:func:`judge` returns a ``rejected`` verdict with no criteria, ``reason_code`` ``invalid_observations`` and the contract error's
message as ``reason_detail`` (a JSON pointer and a type, never a value; non-ASCII is replaced and the text cut at 512 characters).
A non-mapping (a failed read that produced ``None``) is the same. Every *other* contract error (an empty or mixed predicate set,
a predicate that raises or returns another predicate's result) is raised as :class:`PredicateError` and nothing is stored, so a
bug cannot become an accepted run or a silent rejection.

**Readers verify.** :func:`read_decision`, :func:`find_decision` and :func:`decisions_for_run` rebuild the record from the stored
columns and refuse a row whose criteria do not match its counts or its outcome (:data:`DECISION_CORRUPT`), so a row inserted
around the application cannot be read as a different decision than the one it states.

The package root (``caliber.acceptance``) stays I/O-free: import this module explicitly.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.acceptance.predicates import (
    INVALID_OBSERVATIONS,
    Observations,
    Predicate,
    PredicateError,
    check_predicate_set,
    evaluate_all,
)
from caliber.db.models import CaliberAcceptanceDecision, CaliberWorkflowRun
from caliber.db.transactions import begin_sqlite_write_transaction
from caliber.run_manifest import canonical_bytes

ACCEPTED: Final = "accepted"
REJECTED: Final = "rejected"
#: The reason of a rejection recorded without criteria because the observations could not be frozen.
REASON_INVALID_OBSERVATIONS: Final = INVALID_OBSERVATIONS

BAD_FIELD: Final = "bad_field"
UNKNOWN_RUN: Final = "unknown_run"
DECISION_CONFLICT: Final = "decision_conflict"
DECISION_CORRUPT: Final = "decision_corrupt"
STORE_FAILED: Final = "store_failed"
#: Every code an :class:`AcceptanceError` carries.
CODES: Final = frozenset(
    {BAD_FIELD, UNKNOWN_RUN, DECISION_CONFLICT, DECISION_CORRUPT, STORE_FAILED}
)

MAX_RUN_ID: Final = 64
MAX_REASON_DETAIL: Final = 512
_CANDIDATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_NOT_ASCII_PRINTABLE = re.compile(r"[^\x20-\x7e]")
#: Domain separator of the digest of unreadable observations: canonical JSON starts with ``{``, so no readable observation
#: can have the same digest as an unreadable one.
_UNREADABLE_PREFIX: Final = b"caliber.acceptance.unreadable-observations\n"
_CRITERION_KEYS: Final = frozenset({"predicate_id", "passed", "evidence_ref", "rule_ids"})


class AcceptanceError(ValueError):
    """Recording or reading a decision failed; ``code`` is one of :data:`CODES`. The message never carries an observation value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class CriterionRecord:
    """One criterion of a decision: the predicate, its verdict, the pointer that decided it and the rules it enforces."""

    predicate_id: str
    passed: bool
    evidence_ref: str
    rule_ids: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "predicate_id": self.predicate_id,
            "passed": self.passed,
            "evidence_ref": self.evidence_ref,
            "rule_ids": list(self.rule_ids),
        }


@dataclass(frozen=True)
class Verdict:
    """What the predicates concluded about one set of observations (nothing is stored yet; see :func:`judge`)."""

    criteria_version: str
    observation_digest: str
    outcome: str
    criteria: tuple[CriterionRecord, ...]
    reason_code: str | None = None
    reason_detail: str | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED


@dataclass(frozen=True)
class AcceptanceDecision:
    """A stored decision (see :class:`caliber.db.models.CaliberAcceptanceDecision`)."""

    decision_id: str
    workflow_run_id: str
    candidate_id: str
    criteria_version: str
    observation_digest: str
    outcome: str
    criteria: tuple[CriterionRecord, ...]
    reason_code: str | None
    reason_detail: str | None
    decided_at: datetime

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        """The ids of the criteria that failed, in predicate order."""
        return tuple(c.predicate_id for c in self.criteria if not c.passed)


@dataclass(frozen=True)
class RecordedDecision:
    """The outcome of :func:`record_decision`: ``created`` is ``False`` when the same decision was already recorded."""

    decision: AcceptanceDecision
    created: bool


def observation_digest(observations: Observations) -> str:
    """The lowercase hex SHA-256 of the canonical JSON of ``observations`` (sorted keys, no whitespace, UTF-8).

    Raises :class:`PredicateError` (``invalid_observations``) for text that is not valid Unicode (an unpaired surrogate),
    which cannot be encoded and so cannot be recorded.
    """
    try:
        data = canonical_bytes(observations.as_dict())
    except UnicodeEncodeError:
        raise PredicateError(
            INVALID_OBSERVATIONS, "the observations contain text that is not valid Unicode"
        ) from None
    return hashlib.sha256(data).hexdigest()


def judge(predicates: Sequence[Predicate], observations: Mapping[str, Any]) -> Verdict:
    """The verdict of ``predicates`` over ``observations``, with no I/O and no storage.

    ``observations`` is an :class:`Observations`, or anything :class:`Observations` is built from. Unreadable observations
    (``invalid_observations``: not a mapping, not JSON-shaped, too deep, text that is not Unicode) give a rejected verdict
    with no criteria. Any other :class:`PredicateError` (a bad predicate set, a predicate that raises) propagates.
    """
    # A contract error is never a verdict, even for unreadable observations.
    items = check_predicate_set(predicates)
    version = items[0].criteria_version
    try:
        frozen = (
            observations if isinstance(observations, Observations) else Observations(observations)
        )
        digest = observation_digest(frozen)
    except PredicateError as exc:
        detail = _NOT_ASCII_PRINTABLE.sub("?", str(exc).removeprefix(f"{exc.code}: "))[
            :MAX_REASON_DETAIL
        ]
        return Verdict(
            criteria_version=version,
            observation_digest=hashlib.sha256(
                _UNREADABLE_PREFIX + detail.encode("ascii")
            ).hexdigest(),
            outcome=REJECTED,
            criteria=(),
            reason_code=exc.code,
            reason_detail=detail,
        )
    results = evaluate_all(items, frozen)
    criteria = tuple(
        CriterionRecord(
            result.predicate_id, result.passed, result.evidence_ref, tuple(predicate.rule_ids)
        )
        for result, predicate in zip(results, items, strict=True)
    )
    outcome = ACCEPTED if all(c.passed for c in criteria) else REJECTED
    return Verdict(version, digest, outcome, criteria)


def record_decision(
    session: Session,
    *,
    workflow_run_id: str,
    candidate_id: str,
    predicates: Sequence[Predicate],
    observations: Mapping[str, Any],
    now: datetime | None = None,
) -> RecordedDecision:
    """Judge ``observations`` and record the decision for ``candidate_id`` of ``workflow_run_id``, in the caller's transaction.

    Flushes and never commits, so the decision lands (or rolls back) with whatever the caller does next, such as completing the
    run (B09.2b, #791). Raises :class:`AcceptanceError` for a malformed id (``bad_field``), an unknown run (``unknown_run``),
    a different verdict already stored under the same key (``decision_conflict``) and a row the database refused
    (``store_failed``); :class:`PredicateError` for a predicate-contract error (nothing is stored). The same decision recorded
    again, also by a concurrent writer, returns the stored row with ``created`` false.
    """
    run_id = _run_id(workflow_run_id)
    candidate = _candidate_id(candidate_id)
    verdict = judge(predicates, observations)
    moment = _naive_utc(now)
    begin_sqlite_write_transaction(session)
    if (
        session.execute(
            select(CaliberWorkflowRun.workflow_run_id).where(
                CaliberWorkflowRun.workflow_run_id == run_id
            )
        ).first()
        is None
    ):
        raise AcceptanceError(UNKNOWN_RUN, f"workflow run {run_id} does not exist")
    existing = _row_for_key(session, run_id, candidate, verdict)
    if existing is not None:
        return _same_or_conflict(existing, verdict)
    row = CaliberAcceptanceDecision(
        decision_id=f"acc-{uuid.uuid4().hex}",
        workflow_run_id=run_id,
        candidate_id=candidate,
        criteria_version=verdict.criteria_version,
        observation_digest=verdict.observation_digest,
        outcome=verdict.outcome,
        criteria=[c.as_json() for c in verdict.criteria],
        criteria_total=len(verdict.criteria),
        criteria_failed=sum(1 for c in verdict.criteria if not c.passed),
        reason_code=verdict.reason_code,
        reason_detail=verdict.reason_detail,
        decided_at=moment,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # Only the key means "another writer got there first"; any other constraint failure is a defect the caller must see,
        # and the driver's text (which carries the parameters) is not passed on.
        winner = _row_for_key(session, run_id, candidate, verdict, refresh=True)
        if winner is not None:
            return _same_or_conflict(winner, verdict)
        raise AcceptanceError(STORE_FAILED, "the decision could not be stored") from None
    return RecordedDecision(_decision(row), created=True)


def read_decision(session: Session, decision_id: str) -> AcceptanceDecision:
    """The stored decision ``decision_id``. ``KeyError`` if there is none; :class:`AcceptanceError` (``decision_corrupt``) if it is inconsistent."""
    row = session.get(CaliberAcceptanceDecision, decision_id)
    if row is None:
        raise KeyError(decision_id)
    return _decision(row)


def find_decision(
    session: Session,
    *,
    workflow_run_id: str,
    candidate_id: str,
    criteria_version: str,
    observation_digest: str,
) -> AcceptanceDecision | None:
    """The decision made under exactly this key, or ``None``."""
    row = session.execute(
        select(CaliberAcceptanceDecision).where(
            CaliberAcceptanceDecision.workflow_run_id == workflow_run_id,
            CaliberAcceptanceDecision.candidate_id == candidate_id,
            CaliberAcceptanceDecision.criteria_version == criteria_version,
            CaliberAcceptanceDecision.observation_digest == observation_digest,
        )
    ).scalar_one_or_none()
    return None if row is None else _decision(row)


def decisions_for_run(
    session: Session, workflow_run_id: str, *, candidate_id: str | None = None
) -> list[AcceptanceDecision]:
    """Every decision of ``workflow_run_id`` (of one candidate if given), oldest first (ties broken by decision id)."""
    statement = select(CaliberAcceptanceDecision).where(
        CaliberAcceptanceDecision.workflow_run_id == workflow_run_id
    )
    if candidate_id is not None:
        statement = statement.where(CaliberAcceptanceDecision.candidate_id == candidate_id)
    rows = session.execute(
        statement.order_by(
            CaliberAcceptanceDecision.decided_at, CaliberAcceptanceDecision.decision_id
        )
    ).scalars()
    return [_decision(row) for row in rows]


def _run_id(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_RUN_ID:
        raise AcceptanceError(BAD_FIELD, f"workflow_run_id must be 1..{MAX_RUN_ID} characters")
    return value


def _candidate_id(value: object) -> str:
    if not isinstance(value, str) or not _CANDIDATE.fullmatch(value):
        raise AcceptanceError(
            BAD_FIELD, "candidate_id must match [A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
        )
    return value


def _naive_utc(moment: datetime | None) -> datetime:
    if moment is not None and not isinstance(moment, datetime):
        raise AcceptanceError(BAD_FIELD, "now must be a datetime")
    value = datetime.now(timezone.utc) if moment is None else moment
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _row_for_key(
    session: Session, run_id: str, candidate: str, verdict: Verdict, *, refresh: bool = False
) -> CaliberAcceptanceDecision | None:
    statement = select(CaliberAcceptanceDecision).where(
        CaliberAcceptanceDecision.workflow_run_id == run_id,
        CaliberAcceptanceDecision.candidate_id == candidate,
        CaliberAcceptanceDecision.criteria_version == verdict.criteria_version,
        CaliberAcceptanceDecision.observation_digest == verdict.observation_digest,
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return session.execute(statement).scalar_one_or_none()


def _same_or_conflict(row: CaliberAcceptanceDecision, verdict: Verdict) -> RecordedDecision:
    stored = _decision(row)
    # The criteria determine the outcome and the reason, so equal criteria are the same verdict.
    if stored.criteria != verdict.criteria:
        raise AcceptanceError(
            DECISION_CONFLICT,
            f"decision {stored.decision_id} already holds a different verdict for this candidate, criteria version and observation",
        )
    return RecordedDecision(stored, created=False)


def _corrupt(row: CaliberAcceptanceDecision) -> AcceptanceError:
    return AcceptanceError(
        DECISION_CORRUPT, f"decision {row.decision_id} does not hold a consistent record"
    )


def _criterion(entry: object) -> CriterionRecord | None:
    if (
        not isinstance(entry, dict)
        or set(entry) != _CRITERION_KEYS
        or not isinstance(entry["predicate_id"], str)
        or type(entry["passed"]) is not bool
        or not isinstance(entry["evidence_ref"], str)
        or not isinstance(entry["rule_ids"], list)
        or not all(isinstance(rule, str) for rule in entry["rule_ids"])
    ):
        return None
    return CriterionRecord(
        entry["predicate_id"], entry["passed"], entry["evidence_ref"], tuple(entry["rule_ids"])
    )


def _decision(row: CaliberAcceptanceDecision) -> AcceptanceDecision:
    entries = row.criteria if isinstance(row.criteria, list) else None
    parsed = None if entries is None else [_criterion(entry) for entry in entries]
    if parsed is None or any(c is None for c in parsed):
        raise _corrupt(row)
    criteria = tuple(c for c in parsed if c is not None)
    failed = sum(1 for c in criteria if not c.passed)
    consistent = (
        len(criteria) == row.criteria_total
        and failed == row.criteria_failed
        and _DIGEST.fullmatch(row.observation_digest) is not None
        and (
            (
                row.outcome == ACCEPTED
                and failed == 0
                and bool(criteria)
                and row.reason_code is None
                and row.reason_detail is None
            )
            or (
                row.outcome == REJECTED
                and row.reason_code is None
                and row.reason_detail is None
                and failed > 0
            )
            or (
                row.outcome == REJECTED
                and row.reason_code == REASON_INVALID_OBSERVATIONS
                and bool(row.reason_detail)
                and not criteria
            )
        )
    )
    if not consistent:
        raise _corrupt(row)
    return AcceptanceDecision(
        decision_id=row.decision_id,
        workflow_run_id=row.workflow_run_id,
        candidate_id=row.candidate_id,
        criteria_version=row.criteria_version,
        observation_digest=row.observation_digest,
        outcome=row.outcome,
        criteria=criteria,
        reason_code=row.reason_code,
        reason_detail=row.reason_detail,
        decided_at=row.decided_at.replace(tzinfo=timezone.utc),
    )


__all__ = [
    "ACCEPTED",
    "BAD_FIELD",
    "CODES",
    "DECISION_CONFLICT",
    "DECISION_CORRUPT",
    "REASON_INVALID_OBSERVATIONS",
    "REJECTED",
    "STORE_FAILED",
    "UNKNOWN_RUN",
    "AcceptanceDecision",
    "AcceptanceError",
    "CriterionRecord",
    "RecordedDecision",
    "Verdict",
    "decisions_for_run",
    "find_decision",
    "judge",
    "observation_digest",
    "read_decision",
    "record_decision",
]
