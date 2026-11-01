"""Atomic, persisted token reservations for governed agent steps (B08.4, #761).

A governed step spends tokens the platform cannot see: only the adapter reports usage, and a crash, a timeout or a refused
result leaves nobody knowing what the provider charged. So the platform reserves the step's **ceiling first**, charges
conservatively, and only ever *lowers* a charge on evidence it believes:

* **Reserve before the adapter is called.** :func:`reserve_step_tokens` takes the step's ``limits.max_tokens`` (already brought
  down to the platform's ceilings, B08.3) under the **run's row lock** (``FOR UPDATE`` on PostgreSQL), so two workers asking at
  once are serialized: the sum of what a run has reserved or been charged, plus the new ceiling, may not pass the run's
  budget (:data:`TOKEN_BUDGET_EXHAUSTED`, a typed refusal with no payload, and nothing is written). The budget is the
  caller's (``CALIBER_AGENT_RUN_MAX_TOKENS``), finite and bounded.
* **One reservation per step.** The row is unique per ``(run, node, occurrence, attempt)``: a duplicate request returns the same
  reservation (``created`` false) and never reserves twice, whatever the budget is by then. A caller that is handed a
  reservation it did not create must not call the provider again for it (the earlier call may have spent): the runtime
  refuses the step (:data:`RESERVATION_REPLAYED`). A retry of a node is a new attempt and takes its own reservation, so the
  attempts of one occurrence add up against the run.
* **Resume is a new attempt (#1073).** The runtime's attempt counter restarts at 1 on every process-level re-execution of a run,
  so a node that is resumed after a crash would meet its own earlier reservation. :func:`next_attempt` derives the attempt from
  what is stored: the highest attempt the occurrence has reserved plus one (never below the caller's own counter). The earlier
  attempt's reservation is untouched (open, settled or abandoned, it keeps counting in full), the resumed call takes its own,
  and the run's budget adds up both. Two resumers that derive the same attempt meet at the unique key: one reserves, the other
  is handed that row (``created`` false) and is refused as a replay, so a resume never calls the provider twice under one
  reservation. The derivation cannot tell a live earlier worker from a dead one: a worker that starts the step while another's call
  is still in flight makes its own, second call under its own reservation (both charged, the budget bounds the spend).
* **Open counts in full.** A reservation is ``reserved`` from the moment it is taken until it is settled. An open one counts at its
  whole ceiling. It is never released: a worker that crashed, was cancelled, or lost its store mid-call leaves the ceiling
  counted, because the provider may have been called. There is no operator action that lowers a charge (no say-so: nothing the
  platform can check would back it); a run that needs more budget gets a larger ``CALIBER_AGENT_RUN_MAX_TOKENS``.
* **Settle once, from the result.** :func:`settle_step_tokens` moves a ``reserved`` row to ``settled`` exactly once (a compare-and-set
  from ``reserved``; a second call, a racing janitor or a retry finds the final row and changes nothing, so a retry cannot
  double-charge). The charge is :func:`charge_for`'s: a figure **above** the reservation is always charged in full (believing it
  can only raise the charge); a figure at or below it is believed, and the unused reservation given back to the run, only when
  the result is one the platform trusts (a provider-sourced usage on a step that ran, with a non-zero total). A missing,
  zero, malformed, estimated or ``unknown`` usage, and every ``refused`` or ``error`` result, is charged the whole
  reservation. **Adapters are the platform's own code and the provider's usage is theirs to report: this module cannot verify
  it, and says so**; it only refuses to let a result that carries no usable figure shrink a charge.
* **Abandoned, not released.** :func:`abandon_stale` is the owner-only janitor (it holds the execution-owner lease, compared in
  the transaction that writes): an open reservation past the step's own deadline plus a grace moves to ``abandoned`` at its whole
  reservation, so a crashed worker's row stops being open without ever giving tokens back. A worker that returns after that
  finds a final row and its settlement changes nothing (the conservative charge stands).
* **Final means final.** The ORM refuses every edit, and database triggers refuse any UPDATE of a final row, a change to the
  identity or the reserved figures of an open one, DELETE and TRUNCATE; CHECK constraints keep every figure non-negative and a
  conservative charge equal to the reservation (migration 0133).

Refusals are :class:`ReservationRefused` with a fixed code; nothing a model or a provider said is in one.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, cast

from sqlalchemy import Table, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.config import AGENT_RESERVATION_GRACE_BOUNDS, AGENT_RUN_MAX_TOKENS_BOUNDS
from caliber.contracts.agent_step import (
    STOP_REASONS,
    USAGE_SOURCES,
    AgentStepRequest,
    AgentStepResult,
    StepUsage,
    parse_step_request,
)
from caliber.contracts.registry import ContractValidationError, UnsupportedSchemaVersion
from caliber.db.models import CaliberStepTokenReservation, CaliberWorkflowRun
from caliber.db.transactions import begin_sqlite_write_transaction
from caliber.ownership import Lease, assert_fenced, database_now

logger = logging.getLogger("caliber.budget.step_reservation")

SessionFactory = Callable[[], Session]

#: Why a reservation was refused (``ReservationRefused.code``): fixed tokens, never a value from a model or a provider.
TOKEN_BUDGET_EXHAUSTED: Final = "token_budget_exhausted"  # noqa: S105 - a refusal code, not a credential
#: The step already has a reservation (the caller did not create it): the platform will not call the provider again for it.
RESERVATION_REPLAYED: Final = "reservation_replayed"
REQUEST_INVALID: Final = "request_invalid"
BUDGET_INVALID: Final = "budget_invalid"
RUN_NOT_FOUND: Final = "run_not_found"
RESERVATION_NOT_FOUND: Final = "reservation_not_found"
RESERVATION_MISMATCH: Final = "reservation_mismatch"

RESERVED: Final = "reserved"
SETTLED: Final = "settled"
ABANDONED: Final = "abandoned"
#: How a final charge was reached: the usage a result reported, or the whole reservation.
BASIS_REPORTED: Final = "reported"
BASIS_CONSERVATIVE: Final = "conservative"

#: The largest usage figure the result contract allows (a 32-bit count); anything above it is malformed.
_MAX_REPORTED: Final = 2_147_483_647
#: Stops of a step whose adapter ran to an answer: only their provider-sourced usage may lower a charge. ``refused`` and ``error``
#: say the call's outcome is unknown, so they never do.
_RAN_STOPS: Final = frozenset(STOP_REASONS) - {"refused", "error"}
#: The largest attempt the step request contract allows (a 32-bit count).
_MAX_ATTEMPT: Final = 2_147_483_647
_SAFE_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
#: The most open reservations one janitor pass finalizes.
DEFAULT_SWEEP_LIMIT: Final = 500
MAX_SWEEP_LIMIT: Final = 5000


class ReservationRefused(Exception):  # noqa: N818 - a refusal, named for what it says
    """A step may not start, or its reservation cannot be settled: a fixed ``code`` and, for an exhausted budget, three numbers.

    The text is the code (and the platform's own figures for :data:`TOKEN_BUDGET_EXHAUSTED`): never a provider or model value.
    """

    def __init__(
        self,
        code: str,
        *,
        budget: int | None = None,
        committed: int | None = None,
        requested: int | None = None,
    ) -> None:
        text = code
        if budget is not None and committed is not None and requested is not None:
            text = f"{code} (budget={budget} committed={committed} requested={requested})"
        super().__init__(text)
        self.code = code
        self.budget = budget
        self.committed = committed
        self.requested = requested


@dataclass(frozen=True)
class StepReservation:
    """One reservation as stored. ``created`` is true only on the call that wrote it."""

    reservation_id: str
    workflow_run_id: str
    node_id: str
    occurrence_id: str
    attempt: int
    step_request_id: str
    reserved_tokens: int
    budget_tokens: int
    state: str
    charged_tokens: int | None
    charge_basis: str | None
    created: bool = False

    @property
    def final(self) -> bool:
        return self.state != RESERVED

    @property
    def counted_tokens(self) -> int:
        """What this reservation counts against its run now: the charge once final, the whole ceiling while open."""
        return self.reserved_tokens if self.charged_tokens is None else self.charged_tokens


@dataclass(frozen=True)
class Charge:
    """What a result is charged: ``tokens`` on ``basis``, and the figure the adapter reported when it was a valid one."""

    tokens: int
    basis: str
    reported: int | None


@dataclass(frozen=True)
class RunTokenUsage:
    """What one run has reserved or been charged: the sum of every step's :attr:`StepReservation.counted_tokens`."""

    workflow_run_id: str
    counted_tokens: int
    open_reservations: int
    final_reservations: int


def _view(row: CaliberStepTokenReservation, *, created: bool) -> StepReservation:
    return StepReservation(
        reservation_id=row.reservation_id,
        workflow_run_id=row.workflow_run_id,
        node_id=row.node_id,
        occurrence_id=row.occurrence_id,
        attempt=int(row.attempt),
        step_request_id=row.step_request_id,
        reserved_tokens=int(row.reserved_tokens),
        budget_tokens=int(row.budget_tokens),
        state=row.state,
        charged_tokens=None if row.charged_tokens is None else int(row.charged_tokens),
        charge_basis=row.charge_basis,
        created=created,
    )


def _is_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_REPORTED


def _reported_total(result: object) -> int | None:
    """The total tokens ``result`` reports, or ``None`` unless the whole usage is plain, non-negative and consistent.

    Nothing about ``result`` is trusted: an object that is not exactly an :class:`AgentStepResult` holding exactly a
    :class:`StepUsage` of plain integers (no subclass, no boolean) has no usable figure.
    """
    if type(result) is not AgentStepResult or type(result.usage) is not StepUsage:
        return None
    usage = result.usage
    figures = (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
    if not all(_is_count(figure) for figure in figures):
        return None
    if usage.total_tokens < usage.prompt_tokens + usage.completion_tokens:
        return None
    return usage.total_tokens


def charge_for(reserved_tokens: int, result: object) -> Charge:
    """The charge for a step that was reserved ``reserved_tokens`` and returned ``result`` (anything: it is not trusted).

    A reported total above the reservation is always charged in full, the overage included: believing it can only raise the
    charge. A total at or below it is charged as reported only for a result that ran to an answer, whose usage the provider
    reported (``usage_source`` ``provider``, or absent, which the contract reads as ``provider``) and is not zero (a step that
    answered cannot have used nothing). Everything else is the whole reservation: no result at all, a malformed or inconsistent
    usage, a zero total, an ``estimated`` or ``unknown`` source, a ``refused`` or ``error`` stop.
    """
    reported = _reported_total(result)
    if reported is None:
        return Charge(reserved_tokens, BASIS_CONSERVATIVE, None)
    if reported > reserved_tokens:
        return Charge(reported, BASIS_REPORTED, reported)
    assert type(result) is AgentStepResult  # _reported_total proved it
    trusted = (
        result.stop_reason in _RAN_STOPS
        and result.usage_source in (None, "provider")
        and reported > 0
    )
    if trusted:
        return Charge(reported, BASIS_REPORTED, reported)
    return Charge(reserved_tokens, BASIS_CONSERVATIVE, reported)


def _lock_run(session: Session, workflow_run_id: str) -> None:
    """Serialize on the run until the caller's transaction ends: its row ``FOR UPDATE`` on PostgreSQL, a write lock on SQLite.

    ``FOR UPDATE`` is a no-op on SQLite, and pysqlite runs a SELECT outside any transaction, so SQLite is made exclusive
    explicitly (``BEGIN IMMEDIATE``, before the first read) or two threads read the same total and both reserve.
    """
    begin_sqlite_write_transaction(session)
    found = session.execute(
        select(CaliberWorkflowRun.workflow_run_id)
        .where(CaliberWorkflowRun.workflow_run_id == workflow_run_id)
        .with_for_update()
    ).scalar_one_or_none()
    if found is None:
        raise ReservationRefused(RUN_NOT_FOUND)


def _existing(session: Session, request: AgentStepRequest) -> CaliberStepTokenReservation | None:
    return session.execute(
        select(CaliberStepTokenReservation)
        .where(
            CaliberStepTokenReservation.workflow_run_id == request.workflow_run_id,
            CaliberStepTokenReservation.node_id == request.node_id,
            CaliberStepTokenReservation.occurrence_id == request.occurrence_id,
            CaliberStepTokenReservation.attempt == request.attempt,
        )
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _counted(session: Session, workflow_run_id: str) -> int:
    total = session.execute(
        select(
            func.coalesce(
                func.sum(
                    func.coalesce(
                        CaliberStepTokenReservation.charged_tokens,
                        CaliberStepTokenReservation.reserved_tokens,
                    )
                ),
                0,
            )
        ).where(CaliberStepTokenReservation.workflow_run_id == workflow_run_id)
    ).scalar_one()
    return int(total)


def next_attempt(
    session_factory: SessionFactory,
    workflow_run_id: str,
    node_id: str,
    occurrence_id: str,
    *,
    floor: int,
) -> int:
    """The attempt a governed step takes now: ``floor``, or one past the highest attempt this occurrence has reserved, if larger.

    ``floor`` is the caller's own attempt counter (1 on a first execution, higher on an in-process retry). It restarts at 1 when a
    process re-executes the run, so the stored reservations decide: an occurrence that has reserved attempts up to ``n`` resumes as
    ``n + 1`` and takes its own reservation, leaving every earlier one as it is (still counted in full). With none stored the result
    is ``floor``. This only reads: it neither takes the run's lock nor writes, because the unique key on the reservation settles a
    race (two callers deriving the same attempt reserve once between them, see :func:`reserve_step_tokens`). Raises
    :class:`ReservationRefused` (:data:`REQUEST_INVALID`) for a ``floor`` that is not an integer within the request contract's
    attempt bounds, or when the next attempt would pass them.
    """
    if type(floor) is not int or not 1 <= floor <= _MAX_ATTEMPT:
        raise ReservationRefused(REQUEST_INVALID)
    with session_factory() as session:
        highest = session.execute(
            select(func.max(CaliberStepTokenReservation.attempt)).where(
                CaliberStepTokenReservation.workflow_run_id == workflow_run_id,
                CaliberStepTokenReservation.node_id == node_id,
                CaliberStepTokenReservation.occurrence_id == occurrence_id,
            )
        ).scalar_one()
    attempt = floor if highest is None else max(floor, int(highest) + 1)
    if attempt > _MAX_ATTEMPT:
        raise ReservationRefused(REQUEST_INVALID)
    return attempt


def _checked_budget(run_budget_tokens: object) -> int:
    low, high = AGENT_RUN_MAX_TOKENS_BOUNDS
    if type(run_budget_tokens) is not int or not low <= run_budget_tokens <= high:
        raise ReservationRefused(BUDGET_INVALID)
    return run_budget_tokens


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def reserve_step_tokens(
    session_factory: SessionFactory, request: AgentStepRequest, *, run_budget_tokens: int
) -> StepReservation:
    """Reserve ``request.limits.max_tokens`` against the run's budget, once per ``(run, node, occurrence, attempt)``.

    Run before the adapter is called. In one transaction under the run's row lock: an existing reservation for the step is
    returned as it is (``created`` false; whatever its state, whatever the budget is now), otherwise the ceiling is added if
    ``counted + ceiling <= run_budget_tokens`` and refused with :data:`TOKEN_BUDGET_EXHAUSTED` (nothing written) if not. The
    request is validated first (:data:`REQUEST_INVALID`), the budget must be a bounded integer (:data:`BUDGET_INVALID`), the run
    must exist (:data:`RUN_NOT_FOUND`), and a stored reservation of the same step for another ``step_request_id`` is a
    :data:`RESERVATION_MISMATCH`. A step whose ceiling is zero never reaches here (``before_work_stop`` stops it first); one
    that does is :data:`REQUEST_INVALID`.
    """
    budget = _checked_budget(run_budget_tokens)
    try:
        checked = parse_step_request(request.record())
        deadline = _naive_utc(checked.limits.deadline_at)
    except (ContractValidationError, UnsupportedSchemaVersion, ValueError, TypeError):
        raise ReservationRefused(REQUEST_INVALID) from None
    ceiling = checked.limits.max_tokens
    if ceiling <= 0:
        raise ReservationRefused(REQUEST_INVALID)
    for last_turn in (False, True):
        with session_factory() as session:
            _lock_run(session, checked.workflow_run_id)
            row = _existing(session, checked)
            if row is not None:
                if row.step_request_id != checked.step_request_id:
                    raise ReservationRefused(RESERVATION_MISMATCH)
                return _view(row, created=False)
            committed = _counted(session, checked.workflow_run_id)
            if committed + ceiling > budget:
                raise ReservationRefused(
                    TOKEN_BUDGET_EXHAUSTED, budget=budget, committed=committed, requested=ceiling
                )
            row = CaliberStepTokenReservation(
                reservation_id="tres-" + uuid.uuid4().hex,
                workflow_run_id=checked.workflow_run_id,
                node_id=checked.node_id,
                occurrence_id=checked.occurrence_id,
                attempt=checked.attempt,
                step_request_id=checked.step_request_id,
                reserved_tokens=ceiling,
                budget_tokens=budget,
                deadline_at=deadline,
                state=RESERVED,
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Defence in depth: the run lock already serializes two first reservations of one step, and the unique key
                # still refuses a second row if it ever did not. The loser reads the winner's row on the next turn and returns
                # it (created false).
                session.rollback()
                if last_turn:
                    raise
                continue
            return _view(row, created=True)
    raise AssertionError("unreachable")  # pragma: no cover


def settle_step_tokens(
    session_factory: SessionFactory, reservation: StepReservation, result: object
) -> StepReservation:
    """Settle ``reservation`` exactly once with the charge :func:`charge_for` gives ``result``; return the row as it stands.

    A compare-and-set from ``reserved``: the first caller writes the charge, every later one (a retry, a duplicate, a janitor that
    abandoned the row first) finds a final row, changes nothing and gets that row back, so a settlement is never charged twice and
    never lowered after the fact. ``result`` may be ``None`` or anything (an exception path that has no result settles
    conservatively); it is never trusted beyond :func:`charge_for`. Raises :class:`ReservationRefused`
    (:data:`RESERVATION_NOT_FOUND`) for a row that is not there.
    """
    stop_reason = getattr(result, "stop_reason", None) if type(result) is AgentStepResult else None
    source = getattr(result, "usage_source", None) if type(result) is AgentStepResult else None
    code = getattr(result, "error_code", None) if type(result) is AgentStepResult else None
    table = cast(Table, CaliberStepTokenReservation.__table__)
    with session_factory() as session:
        # The stored row, locked, is the authority for the ceiling: the caller's copy is only an identifier.
        row = session.execute(
            select(CaliberStepTokenReservation)
            .where(CaliberStepTokenReservation.reservation_id == reservation.reservation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if row is None:
            raise ReservationRefused(RESERVATION_NOT_FOUND)
        charge = charge_for(int(row.reserved_tokens), result)
        session.execute(
            update(table)
            .where(table.c.reservation_id == row.reservation_id, table.c.state == RESERVED)
            .values(
                state=SETTLED,
                charged_tokens=charge.tokens,
                charge_basis=charge.basis,
                reported_tokens=charge.reported,
                usage_source=source if source in USAGE_SOURCES else None,
                stop_reason=stop_reason if stop_reason in STOP_REASONS else None,
                error_code=code if isinstance(code, str) and _SAFE_CODE.fullmatch(code) else None,
                settled_at=_naive_utc(datetime.now(timezone.utc)),
            )
        )
        session.commit()
        final = session.execute(
            select(CaliberStepTokenReservation)
            .where(CaliberStepTokenReservation.reservation_id == reservation.reservation_id)
            .execution_options(populate_existing=True)
        ).scalar_one()
        return _view(final, created=False)


def run_token_usage(session_factory: SessionFactory, workflow_run_id: str) -> RunTokenUsage:
    """What ``workflow_run_id`` has reserved or been charged so far (an open reservation counts at its whole ceiling)."""
    with session_factory() as session:
        counted = _counted(session, workflow_run_id)
        rows = session.execute(
            select(CaliberStepTokenReservation.state, func.count())
            .where(CaliberStepTokenReservation.workflow_run_id == workflow_run_id)
            .group_by(CaliberStepTokenReservation.state)
        ).all()
    by_state = {state: int(count) for state, count in rows}
    open_count = by_state.get(RESERVED, 0)
    return RunTokenUsage(
        workflow_run_id=workflow_run_id,
        counted_tokens=counted,
        open_reservations=open_count,
        final_reservations=sum(by_state.values()) - open_count,
    )


def abandon_stale(
    session_factory: SessionFactory,
    lease: Lease,
    *,
    grace_seconds: float,
    limit: int = DEFAULT_SWEEP_LIMIT,
) -> int:
    """Move open reservations past their step's deadline plus ``grace_seconds`` to ``abandoned``, at their whole reservation.

    Owner only: ``lease`` is the execution-owner lease, compared (owner, fencing token, the database's clock) in the transaction
    that writes, so a process that does not hold it changes nothing (:class:`~caliber.ownership.LeaseLost` is raised and
    propagates). It never releases tokens: an abandoned row's charge is its reservation, so the run's total does not move; the row only
    stops being open. At most ``limit`` rows per call (oldest deadline first); returns how many it moved. A row a worker
    settled in the meantime is final and is skipped (the update is a compare-and-set from ``reserved``).
    """
    low, high = AGENT_RESERVATION_GRACE_BOUNDS
    if (
        type(grace_seconds) not in (int, float)
        or not low <= grace_seconds <= high
        or type(limit) is not int
        or not 1 <= limit <= MAX_SWEEP_LIMIT
    ):
        raise ValueError("grace_seconds and limit are out of bounds")
    table = cast(Table, CaliberStepTokenReservation.__table__)
    with session_factory() as session:
        assert_fenced(session, lease)
        # A value read from the database's clock is only ever earlier than the true time, so this cutoff can only delay an
        # abandonment, never bring one forward: the direction ``ownership.database_now`` is safe in ("never for deciding
        # expiry" is about granting authority on a stale reading; here a stale reading abandons later, and nothing is released).
        cutoff = database_now(session) - timedelta(seconds=float(grace_seconds))
        stale = (
            session.execute(
                select(CaliberStepTokenReservation.reservation_id)
                .where(
                    CaliberStepTokenReservation.state == RESERVED,
                    CaliberStepTokenReservation.deadline_at < cutoff,
                )
                .order_by(CaliberStepTokenReservation.deadline_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        moved = 0
        for reservation_id in stale:
            result = session.execute(
                update(table)
                .where(table.c.reservation_id == reservation_id, table.c.state == RESERVED)
                .values(
                    state=ABANDONED,
                    charged_tokens=table.c.reserved_tokens,
                    charge_basis=BASIS_CONSERVATIVE,
                    settled_at=_naive_utc(datetime.now(timezone.utc)),
                )
            )
            assert isinstance(result, CursorResult)
            moved += result.rowcount
        session.commit()
    if moved:
        logger.warning(
            "finalized %d abandoned step token reservation(s) at their whole reservation", moved
        )
    return moved


__all__ = [
    "ABANDONED",
    "BASIS_CONSERVATIVE",
    "BASIS_REPORTED",
    "BUDGET_INVALID",
    "DEFAULT_SWEEP_LIMIT",
    "REQUEST_INVALID",
    "RESERVATION_MISMATCH",
    "RESERVATION_NOT_FOUND",
    "RESERVATION_REPLAYED",
    "RESERVED",
    "RUN_NOT_FOUND",
    "SETTLED",
    "TOKEN_BUDGET_EXHAUSTED",
    "Charge",
    "ReservationRefused",
    "RunTokenUsage",
    "StepReservation",
    "abandon_stale",
    "charge_for",
    "next_attempt",
    "reserve_step_tokens",
    "run_token_usage",
    "settle_step_tokens",
]
