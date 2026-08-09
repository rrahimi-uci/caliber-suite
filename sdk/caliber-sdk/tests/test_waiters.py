"""Waiters: termination, timeout, backoff, and failure semantics."""

from __future__ import annotations

import pytest

from caliber_sdk import WaitFailed, WaitTimeout, wait_for, wait_for_terminal_state
from caliber_sdk.waiters import state_of


class Clock:
    """Injectable time so tests do not spend real seconds."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def test_returns_as_soon_as_the_predicate_passes() -> None:
    clock = Clock()
    seen = iter([{"status": "running"}, {"status": "running"}, {"status": "succeeded"}])
    result = wait_for(
        lambda: next(seen),
        is_done=lambda item: item["status"] == "succeeded",
        sleep=clock.sleep,
        now=clock.monotonic,
    )
    assert result["status"] == "succeeded"
    assert len(clock.slept) == 2


def test_interval_backs_off_and_is_capped() -> None:
    """A fixed short interval turns a slow job into thousands of requests."""
    clock = Clock()
    with pytest.raises(WaitTimeout):
        wait_for(
            lambda: {"status": "running"},
            is_done=lambda item: False,
            timeout=100.0,
            interval=1.0,
            backoff=2.0,
            max_interval=8.0,
            sleep=clock.sleep,
            now=clock.monotonic,
        )
    assert clock.slept[:5] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_never_sleeps_past_the_deadline() -> None:
    """Reporting a timeout later than asked matters in CI with its own limits."""
    clock = Clock()
    with pytest.raises(WaitTimeout) as caught:
        wait_for(
            lambda: {"status": "running"},
            is_done=lambda item: False,
            timeout=5.0,
            interval=10.0,
            sleep=clock.sleep,
            now=clock.monotonic,
        )
    assert sum(clock.slept) <= 5.0
    assert caught.value.last == {"status": "running"}


def test_timeout_reports_the_last_observation() -> None:
    clock = Clock()
    with pytest.raises(WaitTimeout) as caught:
        wait_for(
            lambda: {"status": "queued", "id": "JOB-1"},
            is_done=lambda item: False,
            timeout=1.0,
            interval=1.0,
            sleep=clock.sleep,
            now=clock.monotonic,
        )
    assert caught.value.last["id"] == "JOB-1"


def test_terminal_state_helper_accepts_status_or_state() -> None:
    assert state_of({"status": "SUCCEEDED"}) == "succeeded"
    assert state_of({"state": "failed"}) == "failed"
    assert state_of({"nothing": 1}) == ""


def test_a_failed_terminal_state_raises_by_default() -> None:
    clock = Clock()
    with pytest.raises(WaitFailed) as caught:
        wait_for_terminal_state(
            lambda: {"status": "failed"}, sleep=clock.sleep, now=clock.monotonic
        )
    assert caught.value.state == "failed"


def test_failure_can_be_returned_instead_of_raised() -> None:
    clock = Clock()
    result = wait_for_terminal_state(
        lambda: {"status": "failed"},
        raise_on_failure=False,
        sleep=clock.sleep,
        now=clock.monotonic,
    )
    assert result["status"] == "failed"


@pytest.mark.parametrize(("timeout", "interval"), [(0, 1.0), (-1, 1.0), (10, 0)])
def test_invalid_budgets_are_refused(timeout: float, interval: float) -> None:
    with pytest.raises(ValueError):
        wait_for(lambda: 1, is_done=lambda _: True, timeout=timeout, interval=interval)
