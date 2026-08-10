"""Async waiters, holding the same polling policy as the synchronous ones.

The policy worth getting right is: poll once before sleeping at all, grow the
interval geometrically so a slow job does not become thousands of requests, cap
that growth so a fast job does not feel slow, and never sleep past the deadline
so a caller's timeout means what they wrote.

:func:`caliber_sdk.waiters.wait_for` takes an injectable ``sleep``, but an
``await`` cannot be injected into a synchronous function, so this loop is
written out rather than wrapped. Everything it can share it does share -- the
exception type, the terminal-state sets, the status reader, and the default
timings, which are imported as the very same objects. ``test_async_parity.py``
asserts the defaults never diverge.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from ..waiters import (
    FAILURE_STATES,
    TERMINAL_STATES,
    WaitFailed,
    WaitTimeout,
    state_of,
)
from ..waiters import wait_for as _sync_wait_for

T = TypeVar("T")

#: Read off the synchronous implementation rather than retyped, so the two
#: cannot describe different behaviour while claiming to be the same policy.
_SYNC_DEFAULTS = _sync_wait_for.__kwdefaults__ or {}
DEFAULT_TIMEOUT: float = float(_SYNC_DEFAULTS.get("timeout", 300.0))
DEFAULT_INTERVAL: float = float(_SYNC_DEFAULTS.get("interval", 2.0))
DEFAULT_MAX_INTERVAL: float = float(_SYNC_DEFAULTS.get("max_interval", 15.0))
DEFAULT_BACKOFF: float = float(_SYNC_DEFAULTS.get("backoff", 1.5))


async def wait_for(
    poll: Callable[[], Awaitable[T]],
    *,
    is_done: Callable[[T], bool],
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    max_interval: float = DEFAULT_MAX_INTERVAL,
    backoff: float = DEFAULT_BACKOFF,
) -> T:
    """Poll until ``is_done`` or the timeout expires.

    A waiter never decides what "finished" means -- the caller supplies the
    predicate, because only they know whether a ``failed`` run is a successful
    outcome for their script.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if interval <= 0:
        raise ValueError("interval must be positive")

    started = time.monotonic()
    delay = interval
    last: T | None = None
    while True:
        current: T = await poll()
        last = current
        if is_done(current):
            return current
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            raise WaitTimeout(
                f"operation did not finish within {timeout:g}s", last=last, elapsed=elapsed
            )
        # ``asyncio.sleep``, not ``time.sleep``: blocking the loop would stall
        # every other coroutine sharing it, which defeats the point of being
        # async at all.
        await asyncio.sleep(min(delay, max(0.0, timeout - elapsed)))
        delay = min(delay * backoff, max_interval)


async def wait_for_terminal_state(
    poll: Callable[[], Awaitable[Any]],
    *,
    terminal: frozenset[str] = TERMINAL_STATES,
    failure: frozenset[str] = FAILURE_STATES,
    raise_on_failure: bool = True,
    **options: Any,
) -> Any:
    """Poll until a payload reports a terminal status.

    ``raise_on_failure`` defaults to True because a script that waited for work
    and got a failure almost always wants to stop there.
    """
    result = await wait_for(poll, is_done=lambda item: state_of(item) in terminal, **options)
    state = state_of(result)
    if raise_on_failure and state in failure:
        raise WaitFailed(f"operation finished as {state!r}", state=state, last=result)
    return result


__all__ = [
    "DEFAULT_BACKOFF",
    "DEFAULT_INTERVAL",
    "DEFAULT_MAX_INTERVAL",
    "DEFAULT_TIMEOUT",
    "WaitFailed",
    "WaitTimeout",
    "wait_for",
    "wait_for_terminal_state",
]
