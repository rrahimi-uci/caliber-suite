"""Polling helpers for CALIBER's long-running operations.

Runs, jobs, evaluations, and report generation are all asynchronous and all
poll differently today (gap P1-11 in the SDK plan). Until the server
standardises terminal states, the SDK owns one polling implementation so every
caller does not write their own ``while True`` with a bare sleep.

The design rule: a waiter never decides what "finished" means. The caller
supplies the predicate, because only they know whether a `failed` run is a
successful outcome for their script.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from .errors import CaliberError

T = TypeVar("T")

#: States CALIBER uses for work that has stopped. Provided as a default so the
#: common case needs no argument, and exported so callers can extend it rather
#: than retype it.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"succeeded", "success", "completed", "complete", "failed", "error", "cancelled", "canceled",
     "rejected", "timed_out", "expired"}
)

#: Terminal states that mean the work did not succeed.
FAILURE_STATES: frozenset[str] = frozenset(
    {"failed", "error", "cancelled", "canceled", "rejected", "timed_out", "expired"}
)


class WaitTimeout(CaliberError):
    """The operation did not reach a terminal state within the budget."""

    def __init__(self, message: str, *, last: Any = None, elapsed: float = 0.0) -> None:
        super().__init__(message)
        #: The most recent observation, so a caller can report *where* it stalled
        #: rather than only that it did.
        self.last = last
        self.elapsed = elapsed


class WaitFailed(CaliberError):
    """The operation reached a terminal state that indicates failure."""

    def __init__(self, message: str, *, state: str, last: Any = None) -> None:
        super().__init__(message)
        self.state = state
        self.last = last


def wait_for(
    poll: Callable[[], T],
    *,
    is_done: Callable[[T], bool],
    timeout: float = 300.0,
    interval: float = 2.0,
    max_interval: float = 15.0,
    backoff: float = 1.5,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> T:
    """Poll until ``is_done`` or the timeout expires.

    The interval grows geometrically to ``max_interval``. A fixed short interval
    is what turns a slow job into thousands of requests; a fixed long one makes
    a fast job feel slow. ``sleep`` and ``now`` are injectable so tests do not
    have to spend real seconds proving this.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if interval <= 0:
        raise ValueError("interval must be positive")

    started = now()
    delay = interval
    last: T | None = None
    while True:
        current: T = poll()
        last = current
        if is_done(current):
            return current
        elapsed = now() - started
        if elapsed >= timeout:
            raise WaitTimeout(
                f"operation did not finish within {timeout:g}s", last=last, elapsed=elapsed
            )
        # Never sleep past the deadline: doing so reports a timeout later than
        # the caller asked for, which matters in CI with its own limits.
        sleep(min(delay, max(0.0, timeout - elapsed)))
        delay = min(delay * backoff, max_interval)


def state_of(payload: Any, *, keys: Sequence[str] = ("status", "state")) -> str:
    """Read a status field from a payload, tolerating either spelling."""
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value.lower()
    return ""


def wait_for_terminal_state(
    poll: Callable[[], Any],
    *,
    terminal: frozenset[str] = TERMINAL_STATES,
    failure: frozenset[str] = FAILURE_STATES,
    raise_on_failure: bool = True,
    **kwargs: Any,
) -> Any:
    """Poll until the payload's status is terminal.

    ``raise_on_failure`` is on by default because the common script wants a
    failed job to stop it; a caller inspecting the outcome themselves turns it
    off rather than wrapping every call in a try.
    """
    result = wait_for(poll, is_done=lambda item: state_of(item) in terminal, **kwargs)
    state = state_of(result)
    if raise_on_failure and state in failure:
        raise WaitFailed(f"operation finished in state {state!r}", state=state, last=result)
    return result


__all__ = [
    "FAILURE_STATES",
    "TERMINAL_STATES",
    "WaitFailed",
    "WaitTimeout",
    "state_of",
    "wait_for",
    "wait_for_terminal_state",
]
