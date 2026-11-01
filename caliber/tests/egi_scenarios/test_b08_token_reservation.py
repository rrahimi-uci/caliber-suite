"""B08.4 (#761) on PostgreSQL: concurrent workers cannot overspend a run's token budget, and a step is reserved and settled once.

The reference-profile database, where the run's row lock is what serializes the writers. Every race here is forced to overlap:
a barrier releases the workers together, and the budget read is slowed after the fact so that, without the lock, every worker
would have read the same total before any of them wrote. Each test asserts the **persisted** state, not only what the workers
were told.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from caliber.budget import step_reservation
from caliber.budget.step_reservation import (
    ABANDONED,
    BASIS_CONSERVATIVE,
    BASIS_REPORTED,
    RESERVATION_REPLAYED,
    RESERVED,
    SETTLED,
    TOKEN_BUDGET_EXHAUSTED,
    ReservationRefused,
    abandon_stale,
    reserve_step_tokens,
    run_token_usage,
    settle_step_tokens,
)
from caliber.contracts.agent_step import StepLimits, StepUsage
from caliber.db.models import CaliberEffectProposal, CaliberStepTokenReservation
from caliber.effects import step_proposal
from caliber.ownership import acquire
from caliber.workflows.agent_step import ScriptedStep, answer
from caliber.workflows.runtime import FakeWorkflowExecutor, execute
from tests.egi_scenarios.conftest import ReferenceApp, Workspaces
from tests.egi_scenarios.test_v04_stale_approval import NODE, _waiting_run
from tests.test_governed_agent_step import (
    USAGE,
    Canaries,
    _plan,
    _proposing,
    _request,
    _seed_registry,
)

CEILING = 20000
WORKERS = 8
ROUNDS = 5


def _postgres_only(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip(
            "the row lock is proven on PostgreSQL; the SQLite race is in tests/test_step_token_reservation.py"
        )


def _step(run_id: str, occurrence: str, attempt: int = 1) -> Any:
    limits = StepLimits(
        max_turns=8, max_tool_calls=4, max_tokens=CEILING, deadline="2099-01-01T00:00:00Z"
    )
    return _request(
        workflow_run_id=run_id,
        node_id=NODE,
        occurrence_id=occurrence,
        attempt=attempt,
        step_request_id=f"step-{occurrence}-{attempt}",
        limits=limits,
    )


def _in_threads(*targets: Callable[[], Any]) -> list[Any]:
    """Run each target on its own thread, released together by a barrier; return what each returned (or raised)."""
    barrier = threading.Barrier(len(targets))
    outcomes: list[Any] = [None] * len(targets)

    def wrap(index: int, target: Callable[[], Any]) -> Callable[[], None]:
        def run() -> None:
            barrier.wait(30)
            try:
                outcomes[index] = target()
            except BaseException as exc:
                outcomes[index] = exc

        return run

    threads = [threading.Thread(target=wrap(i, t)) for i, t in enumerate(targets)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads), "a worker is stuck"
    return outcomes


def _widen_the_race(monkeypatch: pytest.MonkeyPatch, pause: float = 0.15) -> None:
    """Hold every worker between reading the run's total and writing its reservation, so an unlocked reader would overspend."""
    real = step_reservation._counted

    def slow(session: Any, workflow_run_id: str) -> int:
        total = real(session, workflow_run_id)
        time.sleep(pause)
        return total

    monkeypatch.setattr(step_reservation, "_counted", slow)


def _rows(app: ReferenceApp, run_id: str) -> list[CaliberStepTokenReservation]:
    with app.session_factory() as session:
        return list(
            session.execute(
                select(CaliberStepTokenReservation).where(
                    CaliberStepTokenReservation.workflow_run_id == run_id
                )
            ).scalars()
        )


def _counted(app: ReferenceApp, run_id: str) -> int:
    with app.session_factory() as session:
        return int(
            session.execute(
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
                ).where(CaliberStepTokenReservation.workflow_run_id == run_id)
            ).scalar_one()
        )


def test_concurrent_workers_cannot_reserve_more_than_the_run_budget(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    admitted_steps = 3
    budget = admitted_steps * CEILING
    _widen_the_race(monkeypatch)

    outcomes = _in_threads(
        *[
            lambda n=n: reserve_step_tokens(
                reference_app.session_factory, _step(run_id, f"occ-{n}"), run_budget_tokens=budget
            )
            for n in range(WORKERS)
        ]
    )

    admitted = [o for o in outcomes if not isinstance(o, BaseException)]
    refused = [o for o in outcomes if isinstance(o, ReservationRefused)]
    assert len(admitted) + len(refused) == WORKERS, f"an unexpected failure: {outcomes!r}"
    assert len(admitted) == admitted_steps and all(r.created for r in admitted)
    assert {r.code for r in refused} == {TOKEN_BUDGET_EXHAUSTED}
    rows = _rows(reference_app, run_id)
    assert len(rows) == admitted_steps and {r.reservation_id for r in rows} == {
        a.reservation_id for a in admitted
    }
    assert _counted(reference_app, run_id) == budget, "exactly the budget, never a token over"


def test_the_budget_holds_against_workers_settling_and_reserving_at_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settling a step frees its unused tokens for whoever reserves next, and the run is never over budget at any instant."""
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    budget = 2 * CEILING
    first = reserve_step_tokens(
        reference_app.session_factory, _step(run_id, "occ-first"), run_budget_tokens=budget
    )
    reserve_step_tokens(
        reference_app.session_factory, _step(run_id, "occ-second"), run_budget_tokens=budget
    )
    _widen_the_race(monkeypatch, pause=0.05)
    used = answer(
        _step(run_id, "occ-first"),
        "completed",
        usage=StepUsage(0, 100, 100, 1, 0),
        usage_source="provider",
    )

    def settle() -> Any:
        return settle_step_tokens(reference_app.session_factory, first, used)

    def reserve(n: int) -> Callable[[], Any]:
        return lambda: reserve_step_tokens(
            reference_app.session_factory, _step(run_id, f"occ-late-{n}"), run_budget_tokens=budget
        )

    outcomes = _in_threads(settle, reserve(1), reserve(2), reserve(3))

    assert not isinstance(outcomes[0], BaseException)
    late = outcomes[1:]
    created = [o for o in late if not isinstance(o, BaseException)]
    assert all(isinstance(o, ReservationRefused) for o in late if isinstance(o, BaseException))
    assert len(created) <= 1, (
        "only the 19900 the settlement freed can be reserved, and only one ceiling fits"
    )
    assert _counted(reference_app, run_id) <= budget
    assert _counted(reference_app, run_id) == 100 + CEILING + CEILING * len(created)


def test_concurrent_requests_for_one_step_reserve_it_once_and_all_get_the_same_row(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    _widen_the_race(monkeypatch)

    outcomes = _in_threads(
        *[
            lambda: reserve_step_tokens(
                reference_app.session_factory,
                _step(run_id, "occ-one"),
                run_budget_tokens=10 * CEILING,
            )
            for _ in range(WORKERS)
        ]
    )

    assert all(not isinstance(o, BaseException) for o in outcomes), outcomes
    assert sum(1 for o in outcomes if o.created) == 1, "exactly one worker made the reservation"
    assert len({o.reservation_id for o in outcomes}) == 1
    rows = _rows(reference_app, run_id)
    assert len(rows) == 1 and rows[0].state == RESERVED
    assert _counted(reference_app, run_id) == CEILING


def test_racing_settlements_charge_a_step_exactly_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    for round_number in range(ROUNDS):
        reservation = reserve_step_tokens(
            reference_app.session_factory,
            _step(run_id, f"occ-{round_number}"),
            run_budget_tokens=100 * CEILING,
        )
        request = _step(run_id, f"occ-{round_number}")
        results = [
            answer(
                request,
                "completed",
                usage=StepUsage(0, 100 + n, 100 + n, 1, 0),
                usage_source="provider",
            )
            for n in range(WORKERS)
        ]
        outcomes = _in_threads(
            *[
                lambda r=r, held=reservation: settle_step_tokens(
                    reference_app.session_factory, held, r
                )
                for r in results
            ]
        )
        assert all(not isinstance(o, BaseException) for o in outcomes), outcomes
        charged = {o.charged_tokens for o in outcomes}
        assert len(charged) == 1, "every worker is told the one charge that was written"
        (row,) = [
            r
            for r in _rows(reference_app, run_id)
            if r.reservation_id == reservation.reservation_id
        ]
        assert (row.state, row.charge_basis) == (SETTLED, BASIS_REPORTED)
        assert row.charged_tokens in {100 + n for n in range(WORKERS)} and charged == {
            row.charged_tokens
        }


def test_a_janitor_and_a_late_settlement_agree_on_one_final_row(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The janitor abandons a stale reservation while its slow worker settles: one wins, the loser changes nothing."""
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    with reference_app.session_factory() as session:
        lease = acquire(session, "janitor-owner", ttl_seconds=600)
        assert lease is not None
        session.commit()
    stale = StepLimits(
        max_turns=8, max_tool_calls=4, max_tokens=CEILING, deadline="2026-10-26T09:10:00Z"
    )
    for round_number in range(ROUNDS):
        occurrence = f"occ-{round_number}"
        request = _request(
            workflow_run_id=run_id,
            node_id=NODE,
            occurrence_id=occurrence,
            attempt=1,
            step_request_id=f"step-{occurrence}",
            limits=stale,
        )
        reservation = reserve_step_tokens(
            reference_app.session_factory, request, run_budget_tokens=100 * CEILING
        )
        used = answer(
            request, "completed", usage=StepUsage(0, 50, 50, 1, 0), usage_source="provider"
        )
        outcomes = _in_threads(
            lambda held=reservation, result=used: settle_step_tokens(
                reference_app.session_factory, held, result
            ),
            lambda: abandon_stale(reference_app.session_factory, lease, grace_seconds=1),
        )
        assert all(not isinstance(o, BaseException) for o in outcomes), outcomes
        (row,) = [
            r
            for r in _rows(reference_app, run_id)
            if r.reservation_id == reservation.reservation_id
        ]
        assert row.state in {SETTLED, ABANDONED}
        if row.state == SETTLED:
            assert (row.charged_tokens, row.charge_basis) == (50, BASIS_REPORTED)
        else:
            assert (row.charged_tokens, row.charge_basis) == (CEILING, BASIS_CONSERVATIVE)
        assert outcomes[0].state == row.state and outcomes[0].charged_tokens == row.charged_tokens
        assert row.settled_at is not None and isinstance(row.settled_at, datetime)


def test_two_workers_running_the_same_step_call_the_provider_once(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarted run and the one that was never stopped both reach the same step: one adapter call, one reservation."""
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    calls: list[str] = []
    real_reserve = step_reservation.reserve_step_tokens
    arrived = threading.Barrier(2)

    def both_arrive_first(*args: Any, **kwargs: Any) -> Any:
        arrived.wait(30)
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(step_reservation, "reserve_step_tokens", both_arrive_first)

    class Counting(FakeWorkflowExecutor):
        def run_step(self, request: Any, step_input: Any) -> Any:
            calls.append(threading.current_thread().name)
            return super().run_step(request, step_input)

    def worker() -> Any:
        return execute(
            _plan(reference_app.session_factory, workflow_run_id=run_id),
            "ask",
            executor=Counting(step_script=[ScriptedStep("completed", usage=USAGE)]),
            extra_tools=Canaries().tools(),
        )

    results = _in_threads(worker, worker)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert len(calls) == 1, "the provider was called by one worker only"
    assert [r.status for r in results] == ["error", "error"]
    replayed = [r for r in results if RESERVATION_REPLAYED in (r.error or "")]
    assert len(replayed) == 1, "the other worker was refused before it reached the provider"
    rows = _rows(reference_app, run_id)
    assert len(rows) == 1 and (rows[0].state, rows[0].charged_tokens) == (SETTLED, 15)
    usage = run_token_usage(reference_app.session_factory, run_id)
    assert (usage.open_reservations, usage.final_reservations, usage.counted_tokens) == (0, 1, 15)


# ------------------------------------------------------------------------------- resuming a crashed step (#1073)


class _Dies(FakeWorkflowExecutor):
    """An adapter whose process dies in the call (a simulated kill): a ``BaseException`` is not a result, so the reservation stays open.

    The kill is simulated (``KeyboardInterrupt`` in ``run_step`` or in ``record_step_proposal``); the persisted database state is real.
    """

    def run_step(self, request: Any, step_input: Any) -> Any:
        raise KeyboardInterrupt


class _Counting(FakeWorkflowExecutor):
    def __init__(self, calls: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._calls = calls

    def run_step(self, request: Any, step_input: Any) -> Any:
        self._calls.append(f"{request.step_request_id}@{request.attempt}")
        return super().run_step(request, step_input)


def _attempts(app: ReferenceApp, run_id: str) -> list[tuple[int, str, int | None]]:
    rows = sorted(_rows(app, run_id), key=lambda r: r.attempt)
    return [(r.attempt, r.state, r.charged_tokens) for r in rows]


def _effects(app: ReferenceApp) -> int:
    with app.session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(CaliberEffectProposal)).scalar_one()
        )


def _execute(app: ReferenceApp, run_id: str, executor: Any) -> Any:
    return execute(
        _plan(app.session_factory, workflow_run_id=run_id),
        "ask",
        executor=executor,
        extra_tools=Canaries().tools(),
    )


def test_a_simulated_kill_between_reserve_and_settle_is_resumed_without_double_spend(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The simulated-kill worker's reservation stays open and charged in full; the resume takes its own and the run's total adds up both."""
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    with pytest.raises(KeyboardInterrupt):
        _execute(reference_app, run_id, _Dies())
    assert _attempts(reference_app, run_id) == [(1, RESERVED, None)]

    calls: list[str] = []
    result = _execute(
        reference_app,
        run_id,
        _Counting(calls, step_script=[ScriptedStep("completed", usage=USAGE)]),
    )

    assert result.status == "error" and RESERVATION_REPLAYED not in (result.error or ""), (
        result.error
    )
    assert len(calls) == 1 and calls[0].endswith("@2"), "one provider call, for the resumed attempt"
    assert _attempts(reference_app, run_id) == [(1, RESERVED, None), (2, SETTLED, 15)]
    usage = run_token_usage(reference_app.session_factory, run_id)
    assert (usage.open_reservations, usage.final_reservations) == (1, 1)
    assert usage.counted_tokens == CEILING + 15, (
        "the dead worker's ceiling and the resumed call's usage"
    )
    assert _counted(reference_app, run_id) == usage.counted_tokens


def test_a_simulated_kill_after_settling_is_resumed_and_the_budget_sums_both_calls(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call settled and the process died before its proposal was recorded: the resume is a second, charged call."""
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    _seed_registry(reference_app.session_factory)
    real = step_proposal.record_step_proposal
    died: list[bool] = []

    def dies_once(*args: Any, **kwargs: Any) -> Any:
        if not died:
            died.append(True)
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(step_proposal, "record_step_proposal", dies_once)
    first_calls: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        _execute(reference_app, run_id, _Counting(first_calls, step_script=[_proposing()]))
    assert _attempts(reference_app, run_id) == [(1, SETTLED, 15)]
    assert _effects(reference_app) == 0, "nothing was recorded before the crash"

    calls: list[str] = []
    result = _execute(reference_app, run_id, _Counting(calls, step_script=[_proposing()]))

    assert result.status == "blocked", result.error
    assert len(first_calls) == 1 and len(calls) == 1 and calls[0].endswith("@2")
    assert _attempts(reference_app, run_id) == [(1, SETTLED, 15), (2, SETTLED, 15)]
    assert _counted(reference_app, run_id) == 30
    assert _effects(reference_app) == 1, "the downstream sees one proposed effect, not two"

    third_calls: list[str] = []
    again = _execute(reference_app, run_id, _Counting(third_calls, step_script=[_proposing()]))
    assert again.status == "blocked", again.error
    assert third_calls == [] and len(_attempts(reference_app, run_id)) == 2
    assert _effects(reference_app) == 1


def test_two_resumers_of_one_crashed_step_reserve_once_between_them(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both derive the same next attempt before either reserves it: the unique key lets one in and the other is refused as a replay."""
    _postgres_only(reference_app)
    run_id = _waiting_run(reference_app)
    with pytest.raises(KeyboardInterrupt):
        _execute(reference_app, run_id, _Dies())
    derived: list[int] = []
    real_next = step_reservation.next_attempt
    real_reserve = step_reservation.reserve_step_tokens
    arrived = threading.Barrier(2)

    def record_derivation(*args: Any, **kwargs: Any) -> int:
        attempt = real_next(*args, **kwargs)
        derived.append(attempt)
        return attempt

    def both_arrive_first(*args: Any, **kwargs: Any) -> Any:
        arrived.wait(30)
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(step_reservation, "next_attempt", record_derivation)
    monkeypatch.setattr(step_reservation, "reserve_step_tokens", both_arrive_first)
    calls: list[str] = []

    def resumer() -> Any:
        return _execute(
            reference_app,
            run_id,
            _Counting(calls, step_script=[ScriptedStep("completed", usage=USAGE)]),
        )

    results = _in_threads(resumer, resumer)

    assert all(not isinstance(r, BaseException) for r in results), results
    assert derived == [2, 2], "both resumers derived the same attempt"
    assert len(calls) == 1, "the provider was called by one resumer only"
    replayed = [r for r in results if RESERVATION_REPLAYED in (r.error or "")]
    assert len(replayed) == 1, "the other was refused before it reached the provider"
    assert _attempts(reference_app, run_id) == [(1, RESERVED, None), (2, SETTLED, 15)]
    assert _counted(reference_app, run_id) == CEILING + 15
