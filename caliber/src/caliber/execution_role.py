"""Who runs the background loops: the execution owner, or a secondary that only serves requests (B13.3, #673; ADR 0002).

In the reference profile exactly one process holds the execution-owner lease (:mod:`caliber.ownership`) and only the
holder runs the owner-only loops (every loop in ``observability/worker_inventory.py`` is ``owner_only``). Every other
replica is a *secondary*: it serves HTTP, never starts a loop and keeps trying to acquire the lease.

:class:`ExecutionRoleController` runs that protocol:

* at start it makes one attempt, so the role is known before the first request, then ticks every
  ``lease_seconds / 3``;
* **owner:** each tick renews the exact lease it captured (same token). A renewal that fails, for any reason
  including an unreachable database, means the lease may be gone: the controller stops the loops first and only then
  reports ``secondary``, and it tries to acquire again on later ticks (a fresh acquisition has a new, higher token);
* **secondary:** each tick tries to acquire; on success it starts the loops and reports ``owner``;
* at shutdown it stops the loops and releases the lease, so a rolling restart hands over without waiting for expiry.

``role`` is ``owner`` exactly while this process's loops are meant to be running. Outside the reference profile (or with
background tasks off) there is no lease: :meth:`start` runs the loops directly when asked to and reports ``owner`` if it
did, ``secondary`` if it did not, which is what a process that serves HTTP only should say.

The loops themselves still claim work row by row; fencing each transition with :func:`caliber.ownership.assert_fenced`
is the outbox and effect work that follows (#685), so a loop mid-transition when the lease is lost finishes that
transition (bounded by the stop grace) before it stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Final

from caliber import ownership
from caliber.ownership import Lease

logger = logging.getLogger("caliber.execution_role")

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
        """Stop ticking, stop the loops and give the lease up (best effort) so a successor need not wait for expiry."""
        self._stopping = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._end_loops()
        lease, self._lease = self._lease, None
        if release and lease is not None:
            try:
                await asyncio.to_thread(self._release, lease)
            except Exception:  # best effort: the lease simply expires
                logger.warning(
                    "could not release the execution-owner lease at shutdown", exc_info=True
                )

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
            try:
                self._lease = await asyncio.to_thread(self._renew, self._lease)
            except Exception as lost:
                logger.warning(
                    "execution-owner lease lost (%s); stopping the owner-only loops",
                    lost,
                    exc_info=lost.__cause__ or lost,
                )
                self._lease = None
                await self._end_loops()
            return self.role
        try:
            lease = await asyncio.to_thread(self._acquire)
        except Exception as lost:
            logger.warning(
                "cannot acquire the execution-owner lease (%s)",
                lost,
                exc_info=lost.__cause__ or lost,
            )
            return self.role
        if lease is not None:
            self._lease = lease
            logger.info(
                "execution-owner lease acquired (owner=%s token=%d)",
                self.owner_id,
                lease.fencing_token,
            )
            await self._begin_loops()
        return self.role

    # -- loops ---------------------------------------------------------------------------------

    async def _begin_loops(self) -> None:
        await self._start_loops()
        self._loops_running = True

    async def _end_loops(self) -> None:
        if self._loops_running:
            # Report secondary first: from here on the loops are being stopped.
            self._loops_running = False
            await self._stop_loops()

    # -- database (run off the event loop) -----------------------------------------------------

    def _acquire(self) -> Lease | None:
        with self._session_factory() as session:
            lease = ownership.acquire(session, self.owner_id, ttl_seconds=self._lease_seconds)
            session.commit()
            return lease

    def _renew(self, lease: Lease) -> Lease:
        with self._session_factory() as session:
            renewed = ownership.renew(session, lease, ttl_seconds=self._lease_seconds)
            session.commit()
            return renewed

    def _release(self, lease: Lease) -> None:
        with self._session_factory() as session:
            ownership.release(session, lease)
            session.commit()


__all__ = ["OWNER", "ROLES", "SECONDARY", "ExecutionRoleController", "new_owner_id"]
