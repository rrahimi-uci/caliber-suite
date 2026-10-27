"""Who runs the background loops: the execution owner, or a secondary that only serves requests (B13.3, #673; ADR 0002).

In the reference profile exactly one process holds the execution-owner lease (:mod:`caliber.ownership`) and only the
holder runs the owner-only loops (every loop in ``observability/worker_inventory.py`` is ``owner_only``). Every other
replica is a *secondary*: it serves HTTP, never starts a loop and keeps trying to acquire the lease.

:class:`ExecutionRoleController` runs that protocol:

* at start it makes one attempt, so the role is known before the first request, then ticks every
  ``lease_seconds / 3``;
* **owner:** each tick renews the exact lease it captured (same token). The controller keeps a *local* deadline of
  two thirds of the lease after the start of the last successful acquire or renew: a renewal that fails, hangs past
  that deadline or finds the lease gone means the lease may be gone, so the loops are stopped (the role reads
  ``secondary`` from the moment the stop begins) and later ticks try to acquire again (a fresh acquisition has a new,
  higher token). The lease calls run on a dedicated thread, never the loops' executor, and on PostgreSQL carry a
  ``lock_timeout`` and ``statement_timeout``;
* **secondary:** each tick tries to acquire; on success it starts the loops and reports ``owner``. If the loops fail
  to start, the ones that did start are stopped, the lease is released and the process stays ``secondary``;
* at shutdown it stops the loops *while it keeps renewing the lease*, then releases it, so a rolling restart hands
  over without waiting for expiry and without a second owner starting before this one has stopped. A stop that is
  already running is never cancelled: the loops are always stopped.

``role`` is ``owner`` exactly while this process's loops are meant to be running. Outside the reference profile (or with
background tasks off) there is no lease: :meth:`start` runs the loops directly when asked to and reports ``owner`` if it
did, ``secondary`` if it did not, which is what a process that serves HTTP only should say.

The loops themselves still claim work row by row; fencing each transition with :func:`caliber.ownership.assert_fenced`
is the outbox and effect work that follows (#685). Until then a loop mid-transition when the lease is lost finishes
that transition, and a stop can take up to ``CALIBER_SHUTDOWN_GRACE_SECONDS`` (the loops stop concurrently), so an old
owner's last transition can overlap a new owner's first one by up to that grace.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final, TypeVar

from sqlalchemy import text

from caliber import ownership
from caliber.ownership import Lease

logger = logging.getLogger("caliber.execution_role")

_T = TypeVar("_T")

#: The part of the lease this process trusts: it stops the loops after this fraction of the lease has passed
#: since the start of the last successful acquire or renew, whatever the database is doing.
LOCAL_DEADLINE_FRACTION: Final = 2.0 / 3.0
_MIN_WAIT_SECONDS: Final = 0.05

OWNER: Final = "owner"
SECONDARY: Final = "secondary"
ROLES: Final = frozenset({OWNER, SECONDARY})


def new_owner_id() -> str:
    """An owner id that is unique per process *incarnation*: host, pid and a random suffix."""
    return (
        f"{socket.gethostname().split('.', 1)[0] or 'host'}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
    )


class ExecutionRoleController:
    """Acquire, renew and lose the execution-owner lease, starting and stopping the owner-only loops with it."""

    def __init__(
        self,
        session_factory: Any,
        *,
        lease_seconds: float,
        start_loops: Callable[[], Awaitable[None]],
        stop_loops: Callable[[], Awaitable[None]],
        owner_id: str | None = None,
        leased: bool = True,
        run_loops: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._lease_seconds = float(lease_seconds)
        self._start_loops = start_loops
        self._stop_loops = stop_loops
        self.owner_id = owner_id or new_owner_id()
        self._leased = leased
        self._run_loops = run_loops
        self._lease: Lease | None = None
        self._loops_running = False
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._ending: asyncio.Future[None] | None = None
        self._deadline = 0.0
        # Lease calls never queue behind the loops' threads (or the default executor's).
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="caliber-lease")

    # -- state ---------------------------------------------------------------------------------

    @property
    def role(self) -> str:
        """``owner`` while this process's owner-only loops are running, else ``secondary``."""
        return OWNER if self._loops_running else SECONDARY

    @property
    def lease(self) -> Lease | None:
        return self._lease

    @property
    def tick_seconds(self) -> float:
        return self._lease_seconds / 3.0

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        """Take the first decision synchronously, then keep it up in the background."""
        if self._task is not None:
            raise RuntimeError("ExecutionRoleController.start() called while already running")
        self._stopping = False
        if not self._run_loops:
            return  # an HTTP-only process: no loops, no lease, role stays secondary
        if not self._leased:
            await (
                self._begin_loops()
            )  # no lease outside the reference profile: this process is the owner
            return
        await self.tick()
        self._task = asyncio.create_task(self._run(), name="caliber.execution_role")

    async def stop(self, *, release: bool = True) -> None:
        """Stop the loops (still renewing the lease meanwhile), stop ticking, then give the lease up (best effort)."""
        self._stopping = True
        try:
            await self._end_loops()  # joins a stop a lost lease already began; never cancels it
        finally:
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            lease, self._lease = self._lease, None
            if release and lease is not None:
                await self._release_quietly(lease, "at shutdown")
            self._executor.shutdown(wait=False)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                await self.tick()
            except (
                Exception
            ):  # a failing tick must never end the protocol; the next one tries again
                logger.exception("execution-owner tick failed")

    async def tick(self) -> str:
        """One renew-or-acquire step; returns the role afterwards."""
        if self._lease is not None:
            started = time.monotonic()
            try:
                self._lease = await self._call(
                    self._renew, self._lease, timeout=self._deadline - started
                )
                self._deadline = started + self._lease_seconds * LOCAL_DEADLINE_FRACTION
            except Exception as lost:
                logger.warning(
                    "execution-owner lease lost (%s); stopping the owner-only loops",
                    lost or type(lost).__name__,
                    exc_info=lost.__cause__ or lost,
                )
                self._lease = None
                await self._end_loops()
            return self.role
        if self._stopping:
            return self.role  # shutting down: never take a lease we are about to give up
        started = time.monotonic()
        try:
            lease = await self._call(self._acquire, timeout=self._lease_seconds / 3.0)
        except Exception as lost:
            logger.warning(
                "cannot acquire the execution-owner lease (%s)",
                lost or type(lost).__name__,
                exc_info=lost.__cause__ or lost,
            )
            return self.role
        if lease is not None:
            self._lease = lease
            self._deadline = started + self._lease_seconds * LOCAL_DEADLINE_FRACTION
            logger.info(
                "execution-owner lease acquired (owner=%s token=%d)",
                self.owner_id,
                lease.fencing_token,
            )
            try:
                await self._begin_loops()
            except Exception:
                logger.exception(
                    "the owner-only loops failed to start; stopping the ones that did and giving the lease up"
                )
                self._lease = None
                await self._end_loops(force=True)
                await self._release_quietly(lease, "after the loops failed to start")
        return self.role

    # -- loops ---------------------------------------------------------------------------------

    async def _begin_loops(self) -> None:
        await self._start_loops()
        self._loops_running = True

    async def _end_loops(self, *, force: bool = False) -> None:
        """Stop the loops once, however many callers ask, and never let a caller's cancellation cut the stop short.

        ``force`` stops them even if they were never marked as running (a failed start may have started some).
        """
        if self._ending is None and (self._loops_running or force):
            # Report secondary first: from here on the loops are being stopped.
            self._loops_running = False
            self._ending = asyncio.ensure_future(self._stop_loops_once())
        if self._ending is not None:
            await asyncio.shield(self._ending)

    async def _stop_loops_once(self) -> None:
        try:
            await self._stop_loops()
        except Exception:
            logger.exception("stopping the owner-only loops failed")
        finally:
            self._ending = None

    # -- database (run off the event loop, on the lease's own thread) --------------------------

    async def _call(self, fn: Callable[..., _T], *args: Any, timeout: float) -> _T:
        """Run a lease call on the lease thread and give up on it after ``timeout`` seconds (an unanswered call)."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, lambda: fn(*args))
        done, _ = await asyncio.wait({future}, timeout=max(timeout, _MIN_WAIT_SECONDS))
        if not done:
            future.add_done_callback(_discard_result)
            raise TimeoutError(f"no answer from the database within {timeout:.1f}s")
        return future.result()

    def _bounded(self, session: Any) -> None:
        """Bound how long any lease statement may wait on PostgreSQL (SQLite has no such timeouts)."""
        if session.get_bind().dialect.name == "postgresql":
            millis = max(int(self._lease_seconds / 3.0 * 1000), 100)
            session.execute(text(f"SET LOCAL lock_timeout = {millis}"))
            session.execute(text(f"SET LOCAL statement_timeout = {millis}"))

    def _acquire(self) -> Lease | None:
        with self._session_factory() as session:
            self._bounded(session)
            lease = ownership.acquire(session, self.owner_id, ttl_seconds=self._lease_seconds)
            session.commit()
            return lease

    def _renew(self, lease: Lease) -> Lease:
        with self._session_factory() as session:
            self._bounded(session)
            renewed = ownership.renew(session, lease, ttl_seconds=self._lease_seconds)
            session.commit()
            return renewed

    def _release(self, lease: Lease) -> None:
        with self._session_factory() as session:
            self._bounded(session)
            ownership.release(session, lease)
            session.commit()

    async def _release_quietly(self, lease: Lease, when: str) -> None:
        try:
            await self._call(self._release, lease, timeout=self._lease_seconds / 3.0)
        except Exception:  # best effort: the lease simply expires
            logger.warning("could not release the execution-owner lease %s", when, exc_info=True)


def _discard_result(future: Any) -> None:
    """Consume the outcome of a call we stopped waiting for, so it is not reported as never retrieved."""
    with contextlib.suppress(asyncio.CancelledError):
        future.exception()


__all__ = ["OWNER", "ROLES", "SECONDARY", "ExecutionRoleController", "new_owner_id"]
