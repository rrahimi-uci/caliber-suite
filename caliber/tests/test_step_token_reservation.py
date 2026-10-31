"""B08.4 (#761): a governed step's tokens are reserved before its adapter is called, charged conservatively, settled once.

SQLite throughout (the development dialect, and the migrated schema with its triggers); the PostgreSQL races, where the run's row
lock is what serializes concurrent workers, live in ``tests/egi_scenarios/test_b08_token_reservation.py``.

What is pinned, and what each pin is for:

* the reservation is the step's ``max_tokens`` ceiling, taken before the adapter runs, once per ``(run, node, occurrence, attempt)``;
  a duplicate returns the same row (whatever the budget is by then) and the budget boundary is exact;
* a refusal is typed and payload-free, writes nothing and never reaches the adapter;
* a charge is lowered only on a result the platform trusts, raised on any figure above the reservation, and the whole reservation
  for everything else; a result is settled exactly once and the stored row, not the caller's copy, decides the ceiling;
* an open reservation counts in full and is never released: a crash, a cancellation or a failed settlement leaves it counted, and
  the owner-only janitor finalizes it at its whole reservation;
* the database holds the line the code draws (CHECK constraints, immutable final rows).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.budget import step_reservation
from caliber.budget.step_reservation import (
    ABANDONED,
    BASIS_CONSERVATIVE,
    BASIS_REPORTED,
    BUDGET_INVALID,
    REQUEST_INVALID,
    RESERVATION_MISMATCH,
    RESERVATION_NOT_FOUND,
    RESERVED,
    RUN_NOT_FOUND,
    SETTLED,
    TOKEN_BUDGET_EXHAUSTED,
    ReservationRefused,
    StepReservation,
    abandon_stale,
    charge_for,
    reserve_step_tokens,
    run_token_usage,
    settle_step_tokens,
)
from caliber.config import (
    AGENT_RESERVATION_GRACE_BOUNDS,
    AGENT_RUN_MAX_TOKENS_BOUNDS,
    AGENT_RUN_MAX_TOKENS_DEFAULT,
    CaliberConfig,
    ConfigError,
)
from caliber.contracts.agent_step import StepLimits, StepUsage
from caliber.db.immutable import ImmutableRecordError
from caliber.db.models import CaliberStepTokenReservation
from caliber.ownership import LeaseLost, release
from caliber.workflows.agent_step import AgentStepResult, ScriptedStep, answer
from caliber.workflows.runtime import FakeWorkflowExecutor, execute
from tests.effect_helpers import leased
from tests.test_governed_agent_step import (  # noqa: F401 - the fixtures and builders the governed-step tests share
    RUN,
    USAGE,
    Canaries,
    _another_run,
    _context,
    _migrated_template,
    _plan,
    _proposing,
    _request,
    _rows,
)
from tests.test_governed_agent_step import world as _shared_world  # noqa: F401 - the fixture, under a name this module's own can wrap

pytestmark = pytest.mark.usefixtures("fixed_step_clock")


@pytest.fixture
def world(_shared_world: Any) -> Any:  # noqa: F811 - wraps the imported fixture
    """A migrated SQLite database with the governed-step run and tool registry seeded (a session factory)."""
    return _shared_world


CEILING = 20000  # the shared fixtures' ``max_tokens``
FUTURE = "2099-01-01T00:00:00Z"
PAST = "2026-10-26T09:10:00Z"


def _step(**changes: Any) -> Any:
    """A request for the seeded run; the identity fields are the default occurrence's."""
    limits = StepLimits(max_turns=8, max_tool_calls=4, max_tokens=CEILING, deadline=FUTURE)
    fields: dict[str, Any] = {"limits": limits}
    fields.update(changes)
    return _request(**fields)


def _with_ceiling(tokens: int, **changes: Any) -> Any:
    limits = StepLimits(max_turns=8, max_tool_calls=4, max_tokens=tokens, deadline=FUTURE)
    return _step(limits=limits, **changes)


def _reserve(factory: Callable[[], Session], request: Any, budget: int = 10**6) -> StepReservation:
    return reserve_step_tokens(factory, request, run_budget_tokens=budget)


def _row(factory: Callable[[], Session], reservation_id: str) -> CaliberStepTokenReservation:
    with factory() as session:
        row = session.get(CaliberStepTokenReservation, reservation_id)
        assert row is not None
        session.expunge(row)
        return row


def _all(factory: Callable[[], Session]) -> list[CaliberStepTokenReservation]:
    with factory() as session:
        return list(session.execute(select(CaliberStepTokenReservation)).scalars())


def _ran(request: Any, total: int, **changes: Any) -> AgentStepResult:
    """A step that ran to an answer and reported ``total`` tokens (all of them completion), from the provider unless told."""
    usage = StepUsage(
        prompt_tokens=0, completion_tokens=total, total_tokens=total, turns=1, tool_calls=0
    )
    fields: dict[str, Any] = {"usage": usage, "usage_source": "provider"}
    fields.update(changes)
    stop = fields.pop("stop_reason", "completed")
    return answer(request, stop, **fields)


# ------------------------------------------------------------------------------------------------- the reservation


def test_the_reservation_is_the_steps_ceiling_taken_before_anything_runs(world: Any) -> None:
    request = _step()
    reservation = _reserve(world, request)

    assert reservation.created is True
    assert (reservation.state, reservation.reserved_tokens) == (RESERVED, CEILING)
    assert (reservation.charged_tokens, reservation.charge_basis) == (None, None)
    assert reservation.counted_tokens == CEILING and not reservation.final
    row = _row(world, reservation.reservation_id)
    assert (row.workflow_run_id, row.node_id, row.occurrence_id, row.attempt) == (
        RUN,
        "agent",
        "occ-1",
        1,
    )
    assert row.step_request_id == "step-1" and row.budget_tokens == 10**6
    assert row.deadline_at == datetime(2099, 1, 1)
    assert run_token_usage(world, RUN).counted_tokens == CEILING


def test_a_duplicate_request_returns_the_same_reservation_and_never_reserves_twice(
    world: Any,
) -> None:
    first = _reserve(world, _step())
    again = _reserve(world, _step())

    assert again.created is False
    assert again.reservation_id == first.reservation_id
    assert len(_all(world)) == 1
    assert run_token_usage(world, RUN).counted_tokens == CEILING


def test_a_duplicate_is_returned_even_when_the_budget_is_gone_by_then(world: Any) -> None:
    budget = 2 * CEILING
    first = _reserve(world, _step(), budget)
    _reserve(world, _step(occurrence_id="occ-2", step_request_id="step-2"), budget)
    with pytest.raises(ReservationRefused):
        _reserve(world, _step(occurrence_id="occ-3", step_request_id="step-3"), budget)

    again = _reserve(
        world, _step(), budget
    )  # the budget is spent, and the first step is still the first step's
    assert (again.reservation_id, again.created) == (first.reservation_id, False)
    assert run_token_usage(world, RUN).counted_tokens == budget


def test_the_budget_boundary_is_exact(world: Any) -> None:
    _reserve(world, _step(), 2 * CEILING)
    exact = _reserve(world, _step(occurrence_id="occ-2", step_request_id="step-2"), 2 * CEILING)
    assert exact.created is True, "reserving exactly up to the budget is within it"

    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(occurrence_id="occ-3", step_request_id="step-3"), 2 * CEILING)
    assert refused.value.code == TOKEN_BUDGET_EXHAUSTED
    assert (refused.value.budget, refused.value.committed, refused.value.requested) == (
        2 * CEILING,
        2 * CEILING,
        CEILING,
    )
    assert len(_all(world)) == 2, "a refusal writes nothing"


def test_a_ceiling_one_token_over_the_budget_is_refused_and_writes_nothing(world: Any) -> None:
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _with_ceiling(CEILING), CEILING - 1)
    assert str(refused.value) == (
        f"{TOKEN_BUDGET_EXHAUSTED} (budget={CEILING - 1} committed=0 requested={CEILING})"
    )
    assert _all(world) == []
    assert _reserve(world, _with_ceiling(CEILING), CEILING).created is True


def test_the_attempts_of_one_occurrence_add_up_against_the_run(world: Any) -> None:
    """A retry is a new attempt with its own reservation: the first attempt's ceiling stays counted."""
    budget = 2 * CEILING
    one = _reserve(world, _step(), budget)
    two = _reserve(world, _step(attempt=2, step_request_id="step-1b"), budget)
    assert one.reservation_id != two.reservation_id and two.created is True
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(attempt=3, step_request_id="step-1c"), budget)
    assert refused.value.code == TOKEN_BUDGET_EXHAUSTED


def test_another_run_has_its_own_budget(world: Any) -> None:
    other = _another_run(world, "RUN-other")
    _reserve(world, _step(), CEILING)
    assert _reserve(world, _step(workflow_run_id=other), CEILING).created is True


def test_a_run_that_does_not_exist_reserves_nothing(world: Any) -> None:
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(workflow_run_id="RUN-nowhere"))
    assert refused.value.code == RUN_NOT_FOUND
    assert _all(world) == []


@pytest.mark.parametrize(
    "budget", [True, 0, -5, AGENT_RUN_MAX_TOKENS_BOUNDS[1] + 1, "5", 5.0, None]
)
def test_a_budget_outside_the_settings_bounds_is_refused(world: Any, budget: Any) -> None:
    with pytest.raises(ReservationRefused) as refused:
        reserve_step_tokens(world, _step(), run_budget_tokens=budget)
    assert refused.value.code == BUDGET_INVALID
    assert _all(world) == []


def test_the_budget_bounds_themselves_are_accepted(world: Any) -> None:
    low, high = AGENT_RUN_MAX_TOKENS_BOUNDS
    assert _reserve(world, _with_ceiling(low), low).created is True
    assert _reserve(world, _step(occurrence_id="occ-2", step_request_id="s2"), high).created is True


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": 0},
        {"occurrence_id": ""},
        {"limits": StepLimits(8, 4, 0, FUTURE)},
        {"limits": StepLimits(8, 4, CEILING, "tomorrow")},
    ],
    ids=["attempt-zero", "empty-occurrence", "zero-ceiling", "bad-deadline"],
)
def test_a_request_that_is_not_valid_reserves_nothing(world: Any, changes: dict[str, Any]) -> None:
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(**changes))
    assert refused.value.code == REQUEST_INVALID
    assert _all(world) == []


def test_the_same_step_under_another_request_id_is_a_mismatch_not_a_second_reservation(
    world: Any,
) -> None:
    _reserve(world, _step())
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(step_request_id="step-forged"))
    assert refused.value.code == RESERVATION_MISMATCH
    assert len(_all(world)) == 1


def test_a_first_reservation_that_loses_a_race_for_the_key_returns_the_winners_row(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite has no row lock; the unique key decides, and the loser reads the winner's row instead of failing."""
    winner = _reserve(world, _step())
    real = step_reservation._existing
    calls: list[int] = []

    def blind_the_first_time(session: Session, request: Any) -> Any:
        calls.append(1)
        return None if len(calls) == 1 else real(session, request)

    monkeypatch.setattr(step_reservation, "_existing", blind_the_first_time)
    loser = _reserve(world, _step())
    assert (loser.reservation_id, loser.created) == (winner.reservation_id, False)
    assert len(calls) == 2 and len(_all(world)) == 1


def test_a_key_collision_that_persists_is_not_swallowed(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reserve(world, _step())
    monkeypatch.setattr(step_reservation, "_existing", lambda _session, _request: None)
    with pytest.raises(IntegrityError):
        _reserve(world, _step())


# --------------------------------------------------------------------------------------------------- the charge


def _charge(reserved: int, result: Any) -> tuple[int, str, int | None]:
    charge = charge_for(reserved, result)
    return charge.tokens, charge.basis, charge.reported


def test_a_provider_reported_usage_below_the_reservation_is_charged_as_reported() -> None:
    request = _step()
    assert _charge(CEILING, _ran(request, 300)) == (300, BASIS_REPORTED, 300)
    assert _charge(CEILING, _ran(request, CEILING)) == (CEILING, BASIS_REPORTED, CEILING)
    assert _charge(CEILING, _ran(request, 1)) == (1, BASIS_REPORTED, 1)
    absent = _ran(request, 300, usage_source=None)
    assert _charge(CEILING, absent) == (300, BASIS_REPORTED, 300), (
        "the contract reads no source as the provider"
    )
    for stop in ("proposed_action", "max_turns", "max_tool_calls", "max_tokens", "deadline"):
        assert _charge(CEILING, _ran(request, 300, stop_reason=stop))[1] == BASIS_REPORTED


def test_a_figure_above_the_reservation_is_charged_in_full_whoever_reported_it() -> None:
    request = _step()
    over = CEILING + 7
    for result in (
        _ran(request, over),
        _ran(request, over, usage_source="estimated"),
        _ran(request, over, usage_source="unknown", stop_reason="error"),
        _ran(request, over, stop_reason="max_tokens"),
    ):
        assert _charge(CEILING, result) == (over, BASIS_REPORTED, over)


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"usage_source": "estimated"}, "an estimate may undercount"),
        ({"usage_source": "unknown"}, "nothing is known"),
        ({"stop_reason": "error", "usage_source": "unknown"}, "an error's outcome is unknown"),
        ({"stop_reason": "refused", "usage_source": "unknown"}, "a refusal's outcome is unknown"),
        ({"stop_reason": "error"}, "an error never shrinks a charge, whatever it claims"),
        ({"stop_reason": "refused"}, "a refusal never shrinks a charge, whatever it claims"),
    ],
)
def test_a_result_the_platform_does_not_trust_is_charged_the_whole_reservation(
    changes: dict[str, Any], why: str
) -> None:
    result = _ran(_step(), 300, **changes)
    assert _charge(CEILING, result) == (CEILING, BASIS_CONSERVATIVE, 300), why


def test_a_step_that_answered_with_zero_tokens_is_not_believed() -> None:
    zero = _ran(_step(), 0)
    assert _charge(CEILING, zero) == (CEILING, BASIS_CONSERVATIVE, 0)


def test_no_result_and_a_result_that_is_not_a_result_are_charged_the_whole_reservation() -> None:
    assert _charge(CEILING, None) == (CEILING, BASIS_CONSERVATIVE, None)
    assert _charge(CEILING, {"usage": {"total_tokens": 5}}) == (CEILING, BASIS_CONSERVATIVE, None)

    class Lookalike(AgentStepResult):
        pass

    real = _ran(_step(), 5)
    lookalike = Lookalike(**{f: getattr(real, f) for f in real.__dataclass_fields__})
    assert _charge(CEILING, lookalike) == (CEILING, BASIS_CONSERVATIVE, None)


def _forged_usage(**figures: Any) -> AgentStepResult:
    """A result whose usage carries ``figures`` straight into the dataclass (bypassing no validation the platform relies on)."""
    request = _step()
    usage = StepUsage(0, 5, 5, 1, 0)
    for name, value in figures.items():
        object.__setattr__(usage, name, value)
    return replace(_ran(request, 5), usage=usage)


@pytest.mark.parametrize(
    "figures",
    [
        {"total_tokens": True},
        {"total_tokens": -1},
        {"total_tokens": 2_147_483_648},
        {"total_tokens": 5.0},
        {"total_tokens": "5"},
        {"prompt_tokens": -3, "total_tokens": 2},
        {"completion_tokens": 9, "total_tokens": 5},
        {"prompt_tokens": 9, "total_tokens": 5},
    ],
    ids=[
        "boolean",
        "negative",
        "past-the-contract",
        "float",
        "string",
        "negative-prompt",
        "completion-over-total",
        "prompt-over-total",
    ],
)
def test_a_malformed_or_inconsistent_usage_has_no_usable_figure(figures: dict[str, Any]) -> None:
    assert _charge(CEILING, _forged_usage(**figures)) == (CEILING, BASIS_CONSERVATIVE, None)


def test_a_usage_that_is_not_a_step_usage_has_no_usable_figure() -> None:
    result = replace(_ran(_step(), 5), usage={"total_tokens": 5})  # type: ignore[arg-type]
    assert _charge(CEILING, result) == (CEILING, BASIS_CONSERVATIVE, None)


# -------------------------------------------------------------------------------------------------- settlement


def test_a_result_settles_its_reservation_once_with_the_charge(world: Any) -> None:
    reservation = _reserve(world, _step())
    settled = settle_step_tokens(world, reservation, _ran(_step(), 300))

    assert (settled.state, settled.charged_tokens, settled.charge_basis) == (
        SETTLED,
        300,
        BASIS_REPORTED,
    )
    assert settled.final and settled.counted_tokens == 300
    row = _row(world, reservation.reservation_id)
    assert (row.reported_tokens, row.usage_source, row.stop_reason, row.error_code) == (
        300,
        "provider",
        "completed",
        None,
    )
    assert row.settled_at is not None
    assert run_token_usage(world, RUN).counted_tokens == 300


def test_the_unused_part_of_a_reservation_is_given_back_to_the_run(world: Any) -> None:
    budget = CEILING + 100
    first = _reserve(world, _step(), budget)
    with pytest.raises(ReservationRefused):
        _reserve(world, _step(occurrence_id="occ-2", step_request_id="s2"), budget)
    settle_step_tokens(world, first, _ran(_step(), 50))
    assert _reserve(world, _step(occurrence_id="occ-2", step_request_id="s2"), budget).created


def test_a_second_settlement_changes_nothing_and_never_double_charges(world: Any) -> None:
    reservation = _reserve(world, _step())
    settle_step_tokens(world, reservation, _ran(_step(), 300))
    before = _row(world, reservation.reservation_id)

    for later in (_ran(_step(), 1), _ran(_step(), CEILING * 2), None):
        again = settle_step_tokens(world, reservation, later)
        assert (again.state, again.charged_tokens) == (SETTLED, 300)
    after = _row(world, reservation.reservation_id)
    assert (after.charged_tokens, after.settled_at, after.reported_tokens) == (
        before.charged_tokens,
        before.settled_at,
        before.reported_tokens,
    )
    assert run_token_usage(world, RUN).counted_tokens == 300


@pytest.mark.parametrize(
    "result",
    [
        None,
        answer(_step(), "error", usage_source="unknown", error_code="adapter_timeout"),
        answer(_step(), "refused", usage_source="unknown", error_code="adapter_error"),
        _ran(_step(), 7, usage_source="estimated"),
    ],
    ids=["no-result", "error", "refused", "estimated"],
)
def test_an_ambiguous_call_keeps_the_whole_reservation(world: Any, result: Any) -> None:
    reservation = _reserve(world, _step())
    settled = settle_step_tokens(world, reservation, result)
    assert (settled.state, settled.charged_tokens, settled.charge_basis) == (
        SETTLED,
        CEILING,
        BASIS_CONSERVATIVE,
    )
    assert run_token_usage(world, RUN).counted_tokens == CEILING


def test_an_overage_is_charged_and_blocks_the_next_reservation(world: Any) -> None:
    budget = CEILING + 100
    reservation = _reserve(world, _step(), budget)
    settled = settle_step_tokens(
        world, reservation, _ran(_step(), CEILING + 500, stop_reason="max_tokens")
    )
    assert (settled.charged_tokens, settled.charge_basis) == (CEILING + 500, BASIS_REPORTED)
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(occurrence_id="occ-2", step_request_id="s2"), budget)
    assert refused.value.committed == CEILING + 500


def test_the_stored_row_not_the_callers_copy_decides_the_ceiling(world: Any) -> None:
    """A forged reservation naming a smaller ceiling cannot make a conservative charge smaller than what was reserved."""
    reservation = _reserve(world, _step())
    forged = replace(reservation, reserved_tokens=1)
    settled = settle_step_tokens(world, forged, None)
    assert settled.charged_tokens == CEILING


def test_settling_a_reservation_that_is_not_there_is_refused(world: Any) -> None:
    ghost = replace(_reserve(world, _step()), reservation_id="tres-nothing")
    with pytest.raises(ReservationRefused) as refused:
        settle_step_tokens(world, ghost, None)
    assert refused.value.code == RESERVATION_NOT_FOUND


def test_what_an_adapter_wrote_is_stored_only_as_a_fixed_token(world: Any) -> None:
    reservation = _reserve(world, _step())
    hostile = _ran(
        _step(),
        9,
        stop_reason="error",
        usage_source="unknown",
        error_code="Boom: SELECT secret FROM users",
    )
    settle_step_tokens(world, reservation, hostile)
    row = _row(world, reservation.reservation_id)
    assert row.error_code is None and row.stop_reason == "error" and row.usage_source == "unknown"
    assert "secret" not in repr(
        [getattr(row, column.name) for column in CaliberStepTokenReservation.__table__.columns]
    )


# ------------------------------------------------------------------------------------------------ open reservations


def test_an_open_reservation_counts_in_full_and_is_never_released(world: Any) -> None:
    """A worker that crashed after the reservation (or never settled) leaves its ceiling counted."""
    budget = CEILING + 100
    _reserve(world, _step(), budget)  # never settled
    usage = run_token_usage(world, RUN)
    assert (usage.counted_tokens, usage.open_reservations, usage.final_reservations) == (
        CEILING,
        1,
        0,
    )
    with pytest.raises(ReservationRefused) as refused:
        _reserve(world, _step(occurrence_id="occ-2", step_request_id="s2"), budget)
    assert refused.value.committed == CEILING


# ------------------------------------------------------------------------------------------------ the janitor


def _open(factory: Callable[[], Session], *, deadline: str, **changes: Any) -> StepReservation:
    limits = StepLimits(max_turns=8, max_tool_calls=4, max_tokens=CEILING, deadline=deadline)
    return _reserve(factory, _step(limits=limits, **changes))


def test_the_janitor_finalizes_a_stale_reservation_at_its_whole_reservation(world: Any) -> None:
    stale = _open(world, deadline=PAST)
    fresh = _open(world, deadline=FUTURE, occurrence_id="occ-2", step_request_id="s2")
    before = run_token_usage(world, RUN).counted_tokens
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()

    assert abandon_stale(world, lease, grace_seconds=300) == 1

    row = _row(world, stale.reservation_id)
    assert (row.state, row.charged_tokens, row.charge_basis) == (
        ABANDONED,
        CEILING,
        BASIS_CONSERVATIVE,
    )
    assert row.settled_at is not None
    assert _row(world, fresh.reservation_id).state == RESERVED
    assert run_token_usage(world, RUN).counted_tokens == before, "abandoning gives nothing back"
    assert abandon_stale(world, lease, grace_seconds=300) == 0, "a second pass finds nothing"


def test_the_grace_keeps_a_reservation_that_is_only_just_past_its_deadline(world: Any) -> None:
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    young = _open(world, deadline=recent)
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    assert abandon_stale(world, lease, grace_seconds=300) == 0
    assert _row(world, young.reservation_id).state == RESERVED
    assert abandon_stale(world, lease, grace_seconds=AGENT_RESERVATION_GRACE_BOUNDS[0]) == 1


def test_a_worker_that_returns_after_the_janitor_cannot_lower_the_charge(world: Any) -> None:
    stale = _open(world, deadline=PAST)
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    abandon_stale(world, lease, grace_seconds=300)

    late = settle_step_tokens(world, stale, _ran(_step(), 5))
    assert (late.state, late.charged_tokens, late.charge_basis) == (
        ABANDONED,
        CEILING,
        BASIS_CONSERVATIVE,
    )


def test_a_settled_reservation_is_not_abandoned(world: Any) -> None:
    settled = settle_step_tokens(world, _open(world, deadline=PAST), _ran(_step(), 5))
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    assert abandon_stale(world, lease, grace_seconds=300) == 0
    assert (_row(world, settled.reservation_id).state, settled.charged_tokens) == (SETTLED, 5)


def test_only_the_execution_owner_may_run_the_janitor(world: Any) -> None:
    stale = _open(world, deadline=PAST)
    with world() as session:
        old = leased(session, "owner-1")
        session.commit()
        leased(session, "owner-2")  # takeover: the old lease is stale
        session.commit()
    with pytest.raises(LeaseLost):
        abandon_stale(world, old, grace_seconds=300)
    assert _row(world, stale.reservation_id).state == RESERVED

    with world() as session:
        lease = leased(session, "owner-2")
        assert release(session, lease)
        session.commit()
    with pytest.raises(LeaseLost):
        abandon_stale(world, lease, grace_seconds=300)
    assert _row(world, stale.reservation_id).state == RESERVED


def test_the_janitor_is_bounded_and_oldest_first(world: Any) -> None:
    older = _open(world, deadline="2026-10-01T00:00:00Z", occurrence_id="o1", step_request_id="s1")
    newer = _open(world, deadline="2026-10-02T00:00:00Z", occurrence_id="o2", step_request_id="s2")
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    assert abandon_stale(world, lease, grace_seconds=300, limit=1) == 1
    assert _row(world, older.reservation_id).state == ABANDONED
    assert _row(world, newer.reservation_id).state == RESERVED


@pytest.mark.parametrize(
    ("grace", "limit"),
    [
        (0.5, 10),
        (AGENT_RESERVATION_GRACE_BOUNDS[1] + 1, 10),
        (float("nan"), 10),
        (True, 10),
        ("300", 10),
        (300, 0),
        (300, step_reservation.MAX_SWEEP_LIMIT + 1),
        (300, True),
    ],
)
def test_the_janitor_refuses_unbounded_settings(world: Any, grace: Any, limit: Any) -> None:
    with world() as session:
        lease = leased(session, "owner-1")
        session.commit()
    with pytest.raises(ValueError, match="out of bounds"):
        abandon_stale(world, lease, grace_seconds=grace, limit=limit)


# --------------------------------------------------------------------------------------- what the database enforces


def _sql(factory: Callable[[], Session], statement: str, **params: Any) -> None:
    with factory() as session:
        session.execute(text(statement), params)
        session.commit()


def test_a_final_row_cannot_be_updated_deleted_or_edited_through_the_orm(world: Any) -> None:
    reservation = _reserve(world, _step())
    settle_step_tokens(world, reservation, _ran(_step(), 300))
    rid = reservation.reservation_id

    for statement in (
        "UPDATE caliber_step_token_reservations SET charged_tokens = 0 WHERE reservation_id = :r",
        "UPDATE caliber_step_token_reservations SET state = 'reserved' WHERE reservation_id = :r",
        "UPDATE caliber_step_token_reservations SET stop_reason = 'x' WHERE reservation_id = :r",
        "DELETE FROM caliber_step_token_reservations WHERE reservation_id = :r",
    ):
        with pytest.raises(IntegrityError):
            _sql(world, statement, r=rid)
    assert _row(world, rid).charged_tokens == 300

    with world() as session:
        row = session.get(CaliberStepTokenReservation, rid)
        assert row is not None
        row.charged_tokens = 1
        with pytest.raises(ImmutableRecordError):
            session.flush()
        session.rollback()
        row = session.get(CaliberStepTokenReservation, rid)
        assert row is not None
        session.delete(row)
        with pytest.raises(ImmutableRecordError):
            session.flush()


@pytest.mark.parametrize(
    "column",
    [
        "workflow_run_id",
        "node_id",
        "occurrence_id",
        "attempt",
        "step_request_id",
        "reserved_tokens",
    ],
)
def test_the_identity_and_reserved_figures_of_an_open_row_cannot_change(
    world: Any, column: str
) -> None:
    reservation = _reserve(world, _step())
    value = {"workflow_run_id": "RUN-x", "attempt": 9, "reserved_tokens": 1}.get(column, "other")
    with pytest.raises(IntegrityError):
        _sql(
            world,
            f"UPDATE caliber_step_token_reservations SET {column} = :v WHERE reservation_id = :r",
            v=value,
            r=reservation.reservation_id,
        )
    with pytest.raises(IntegrityError):
        _sql(
            world,
            "UPDATE caliber_step_token_reservations SET budget_tokens = 1 WHERE reservation_id = :r",
            r=reservation.reservation_id,
        )
    assert _row(world, reservation.reservation_id).reserved_tokens == CEILING


def test_an_open_row_cannot_be_deleted(world: Any) -> None:
    reservation = _reserve(world, _step())
    with pytest.raises(IntegrityError):
        _sql(
            world,
            "DELETE FROM caliber_step_token_reservations WHERE reservation_id = :r",
            r=reservation.reservation_id,
        )


_FINAL = (
    "state = 'settled', charged_tokens = {charged}, charge_basis = '{basis}', "
    "reported_tokens = {reported}, settled_at = CURRENT_TIMESTAMP"
)


@pytest.mark.parametrize(
    "assignment",
    [
        # a charge below the reservation on a conservative basis
        _FINAL.format(charged=1, basis="conservative", reported="NULL"),
        # a reported basis with nothing reported, or a charge that is not the report
        _FINAL.format(charged=5, basis="reported", reported="NULL"),
        _FINAL.format(charged=5, basis="reported", reported=6),
        # a negative charge or report
        _FINAL.format(charged=-1, basis="reported", reported=-1),
        # an unknown basis
        _FINAL.format(charged=CEILING, basis="forgiven", reported="NULL"),
        # a settled row without its charge, or an open one with it
        "state = 'settled', settled_at = CURRENT_TIMESTAMP",
        f"charged_tokens = {CEILING}",
        # an unknown state
        "state = 'released'",
        # an abandoned row on any basis but the conservative one
        "state = 'abandoned', charged_tokens = 5, charge_basis = 'reported', reported_tokens = 5, "
        "settled_at = CURRENT_TIMESTAMP",
    ],
    ids=[
        "conservative-below-reservation",
        "reported-nothing-reported",
        "reported-not-the-report",
        "negative",
        "unknown-basis",
        "settled-without-charge",
        "open-with-charge",
        "unknown-state",
        "abandoned-on-a-reported-basis",
    ],
)
def test_the_constraints_refuse_a_charge_the_code_would_never_write(
    world: Any, assignment: str
) -> None:
    reservation = _reserve(world, _step())
    with pytest.raises(IntegrityError):
        _sql(
            world,
            f"UPDATE caliber_step_token_reservations SET {assignment} WHERE reservation_id = :r",
            r=reservation.reservation_id,
        )
    assert _row(world, reservation.reservation_id).state == RESERVED


@pytest.mark.parametrize(
    "overrides",
    [
        {"reserved_tokens": 0},
        {"reserved_tokens": -1},
        {"budget_tokens": 0},
        {"attempt": 0},
        {"node_id": ""},
        {"occurrence_id": ""},
        {"step_request_id": ""},
        {"state": "settled"},
    ],
    ids=[
        "zero-reserved",
        "negative-reserved",
        "zero-budget",
        "attempt-zero",
        "empty-node",
        "empty-occurrence",
        "empty-request",
        "born-final",
    ],
)
def test_the_constraints_refuse_a_row_that_is_not_a_reservation(
    world: Any, overrides: dict[str, Any]
) -> None:
    fields: dict[str, Any] = {
        "reservation_id": "tres-raw",
        "workflow_run_id": RUN,
        "node_id": "agent",
        "occurrence_id": "occ-9",
        "attempt": 1,
        "step_request_id": "step-9",
        "reserved_tokens": CEILING,
        "budget_tokens": CEILING,
        "deadline_at": datetime(2099, 1, 1),
    }
    fields.update(overrides)
    with world() as session, pytest.raises(IntegrityError):
        session.add(CaliberStepTokenReservation(**fields))
        session.commit()


def test_the_unique_key_refuses_a_second_row_for_one_step(world: Any) -> None:
    reservation = _reserve(world, _step())
    with world() as session, pytest.raises(IntegrityError):
        session.add(
            CaliberStepTokenReservation(
                reservation_id="tres-dup",
                workflow_run_id=RUN,
                node_id=reservation.node_id,
                occurrence_id=reservation.occurrence_id,
                attempt=reservation.attempt,
                step_request_id="step-dup",
                reserved_tokens=1,
                budget_tokens=1,
                deadline_at=datetime(2099, 1, 1),
            )
        )
        session.commit()


# ------------------------------------------------------------------------------------- the settings and the context


def test_the_settings_default_to_bounded_values_and_read_the_environment() -> None:
    config = CaliberConfig()
    assert config.agent_run_max_tokens == AGENT_RUN_MAX_TOKENS_DEFAULT == 2_000_000
    assert config.agent_reservation_grace_seconds == 300.0

    loaded = CaliberConfig.load(
        environ={
            "CALIBER_AGENT_RUN_MAX_TOKENS": "12345",
            "CALIBER_AGENT_RESERVATION_GRACE_SECONDS": "42.5",
        }
    )
    assert (loaded.agent_run_max_tokens, loaded.agent_reservation_grace_seconds) == (12345, 42.5)


@pytest.mark.parametrize(
    ("variable", "bad"),
    [
        ("CALIBER_AGENT_RUN_MAX_TOKENS", "0"),
        ("CALIBER_AGENT_RUN_MAX_TOKENS", "1000000001"),
        ("CALIBER_AGENT_RESERVATION_GRACE_SECONDS", "0.5"),
        ("CALIBER_AGENT_RESERVATION_GRACE_SECONDS", "86401"),
        ("CALIBER_AGENT_RESERVATION_GRACE_SECONDS", "inf"),
        ("CALIBER_AGENT_RESERVATION_GRACE_SECONDS", "nan"),
    ],
)
def test_a_setting_outside_its_bounds_stops_the_service_at_startup(variable: str, bad: str) -> None:
    with pytest.raises(ConfigError, match=variable.removeprefix("CALIBER_").lower()):
        CaliberConfig.load(environ={variable: bad})


@pytest.mark.parametrize("budget", [True, 0, -1, AGENT_RUN_MAX_TOKENS_BOUNDS[1] + 1, "5", 5.0])
def test_a_governed_context_refuses_a_run_budget_outside_the_bounds(
    world: Any, budget: Any
) -> None:
    with pytest.raises(ValueError, match="run_token_budget"):
        _context(world, run_token_budget=budget)


def test_a_governed_context_without_a_budget_is_bounded_not_unbounded(world: Any) -> None:
    assert _context(world).run_token_budget == AGENT_RUN_MAX_TOKENS_DEFAULT
    assert (
        _context(world, run_token_budget=AGENT_RUN_MAX_TOKENS_BOUNDS[1]).run_token_budget
        == (AGENT_RUN_MAX_TOKENS_BOUNDS[1])
    )


# ------------------------------------------------------------------------------- through the governed step's runtime


class _Watcher(FakeWorkflowExecutor):
    """An adapter that records what the database says about its own step the moment it is called."""

    def __init__(self, factory: Callable[[], Session], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._factory = factory
        self.seen: list[list[tuple[str, int, str]]] = []

    def run_step(self, request: Any, step_input: Any) -> Any:
        with self._factory() as session:
            rows = session.execute(select(CaliberStepTokenReservation)).scalars().all()
            self.seen.append([(r.state, int(r.reserved_tokens), r.step_request_id) for r in rows])
        return super().run_step(request, step_input)


def _go(plan: Any, executor: Any) -> Any:
    return execute(plan, "ask", executor=executor, extra_tools=Canaries().tools())


def test_the_reservation_is_open_in_the_database_when_the_adapter_is_called(world: Any) -> None:
    executor = _Watcher(world, step_script=[_proposing()])
    result = _go(_plan(world), executor)

    assert result.status == "blocked", result.error
    assert executor.seen == [[(RESERVED, CEILING, "step-agent-agent-1")]]
    (row,) = _all(world)
    assert (row.state, row.charged_tokens, row.charge_basis, row.reported_tokens) == (
        SETTLED,
        15,
        BASIS_REPORTED,
        15,
    )
    assert (row.stop_reason, row.usage_source) == ("proposed_action", None)
    assert row.budget_tokens == AGENT_RUN_MAX_TOKENS_DEFAULT


def test_a_step_that_would_pass_the_run_budget_is_refused_and_its_adapter_never_called(
    world: Any,
) -> None:
    executor = _Watcher(world, step_script=[_proposing()])
    result = _go(_plan(world, run_token_budget=CEILING - 1), executor)

    assert result.status == "error"
    assert result.error == (
        "ToolExecutionError: governed agent step token reservation refused: "
        f"token_budget_exhausted (budget={CEILING - 1} committed=0 requested={CEILING})"
    )
    assert executor.seen == [] and executor.step_requests == [], "the provider was not reached"
    assert _all(world) == [] and _rows(world) == ([], []), "nothing was reserved or proposed"


def test_a_step_whose_ceiling_exactly_fits_the_run_budget_runs(world: Any) -> None:
    executor = _Watcher(world, step_script=[_proposing()])
    result = _go(_plan(world, run_token_budget=CEILING), executor)
    assert result.status == "blocked", result.error
    assert len(executor.seen) == 1


def test_a_step_that_already_holds_a_reservation_is_not_called_again(world: Any) -> None:
    """A restart of the same step may not call the provider on the strength of a reservation that already may have spent."""
    first = FakeWorkflowExecutor(step_script=[ScriptedStep("completed", usage=USAGE)])
    assert _go(_plan(world), first).status == "error"
    (row,) = _all(world)

    second = _Watcher(world, step_script=[_proposing()])
    result = _go(_plan(world), second)
    assert result.error == (
        "ToolExecutionError: governed agent step token reservation refused: reservation_replayed"
    )
    assert second.seen == [] and second.step_requests == []
    assert [r.reservation_id for r in _all(world)] == [row.reservation_id]


def test_a_failed_provider_call_keeps_the_whole_reservation(world: Any) -> None:
    executor = FakeWorkflowExecutor(step_script=[TimeoutError()])
    result = _go(_plan(world), executor)
    assert result.status == "error"
    (row,) = _all(world)
    assert (row.state, row.charged_tokens, row.charge_basis) == (
        SETTLED,
        CEILING,
        BASIS_CONSERVATIVE,
    )
    assert (row.stop_reason, row.error_code) == ("error", "adapter_timeout")


def test_a_step_that_overran_its_tokens_is_charged_what_it_reported(world: Any) -> None:
    over = StepUsage(
        prompt_tokens=0,
        completion_tokens=CEILING + 9,
        total_tokens=CEILING + 9,
        turns=1,
        tool_calls=0,
    )
    result = _go(
        _plan(world), FakeWorkflowExecutor(step_script=[ScriptedStep("completed", usage=over)])
    )
    assert result.status == "error" and "max_tokens" in (result.error or "")
    (row,) = _all(world)
    assert (row.charged_tokens, row.charge_basis, row.stop_reason) == (
        CEILING + 9,
        BASIS_REPORTED,
        "max_tokens",
    )


def test_a_step_the_platform_stops_before_work_reserves_nothing(world: Any) -> None:
    class NoTurns(FakeWorkflowExecutor):
        pass

    plan = _plan(world, limits=lambda _agent: StepLimits(0, 4, CEILING, FUTURE))
    executor = NoTurns(step_script=[_proposing()])
    result = _go(plan, executor)
    assert "governed agent step stopped: max_turns" in (result.error or "")
    assert executor.step_requests == [] and _all(world) == []


def test_an_executor_that_is_not_an_adapter_reserves_nothing(world: Any) -> None:
    class Legacy:
        def run_agent(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - never reached
            raise AssertionError("a governed step never uses the legacy path")

    result = _go(_plan(world), Legacy())
    assert result.status == "error"
    assert _all(world) == []


def test_a_cancelled_step_leaves_its_reservation_open_and_counted(world: Any) -> None:
    """A BaseException out of the adapter (a cancellation, a SIGINT) is not a result: the provider may have been called."""

    class Cancelled(FakeWorkflowExecutor):
        def run_step(self, request: Any, step_input: Any) -> Any:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _go(_plan(world), Cancelled())
    (row,) = _all(world)
    assert (row.state, row.charged_tokens, row.charge_basis) == (RESERVED, None, None)
    assert run_token_usage(world, RUN).counted_tokens == CEILING


def test_a_settlement_the_store_cannot_take_leaves_the_reservation_open_and_the_step_goes_on(
    world: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("password=hunter2 INSERT INTO secrets")

    monkeypatch.setattr(step_reservation, "settle_step_tokens", broken)
    with caplog.at_level(logging.WARNING, logger="caliber.workflows.runtime"):
        result = _go(_plan(world), FakeWorkflowExecutor(step_script=[_proposing()]))

    assert result.status == "blocked", (
        "the provider's answer is not discarded for a settlement the store refused"
    )
    assert len(_rows(world)[0]) == 1
    (row,) = _all(world)
    assert (row.state, row.charged_tokens) == (RESERVED, None)
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "token settlement failed (RuntimeError)" in logged
    assert "hunter2" not in logged and "secrets" not in logged


def test_a_store_that_fails_to_reserve_refuses_the_step_with_a_fixed_code(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("password=hunter2 INSERT INTO secrets")

    monkeypatch.setattr(step_reservation, "reserve_step_tokens", broken)
    executor = _Watcher(world, step_script=[_proposing()])
    result = _go(_plan(world), executor)
    assert result.error == (
        "ToolExecutionError: governed agent step token reservation refused: store_failed"
    )
    assert executor.seen == [] and "hunter2" not in (result.error or "")


def test_two_runs_each_get_their_own_budget_through_the_runtime(world: Any) -> None:
    other = _another_run(world, "RUN-second")
    for run_id in (RUN, other):
        executor = FakeWorkflowExecutor(step_script=[_proposing()])
        result = _go(_plan(world, workflow_run_id=run_id, run_token_budget=CEILING), executor)
        assert result.status == "blocked", result.error
    assert len(_all(world)) == 2
