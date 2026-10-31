"""Owner-only loop that drives the effect reconciler (B05.5d, #997; ADR 0002, ADR 0003).

:func:`caliber.effects.reconcile.reconcile_pass` is a function the execution-owner lease holder calls. This is the loop
that calls it, so an effect nobody recorded the outcome of is asked about by key (never sent again) without anyone
having to remember to. It adds no second lease protocol and no second queue:

* **Owner only, never a secondary.** The lifespan binds :meth:`EffectReconcilerTask.bind_lease_provider` to the
  :class:`~caliber.execution_role.ExecutionRoleController`'s ``lease`` and starts the loop with the other owner-only
  loops, so a process that does not hold the execution-owner lease never runs it. Every pass takes the exact lease the
  controller holds *now* and hands it to ``reconcile_pass``, which compares it (owner, fencing token, the database's clock)
  in the transaction of every write. A loop built without a provider (outside the reference profile there is no lease)
  does nothing: the reconciler cannot act without one.
* **Lease loss stops it, between proposals.** Before each proposal the pass checks that the controller still holds the
  *same* lease (same owner and fencing token); if the lease is gone, replaced or this loop is stopping, the pass ends
  there and nothing further is read, asked or written. The next tick takes whatever lease the controller holds then, so a
  re-acquired lease (a higher token) starts a fresh pass. The loop does not renew the lease: the controller renews it on
  its own thread every third of the lease, and a loop that also renewed would be a second renewer.
* **Bounded.** A pass stops starting lookups after ``effect_reconciler_budget_seconds`` and the next pass continues after
  the last proposal it looked at (a backlog of effects that stay ``unknown`` can neither starve the rest nor be looked at
  without end). No lock is held across a lookup (``reconcile_pass`` reads, asks and writes in separate transactions). On
  PostgreSQL every transaction of the pass carries ``lock_timeout`` and ``statement_timeout`` of a third of the execution
  lease, like the controller's own lease calls, so a stuck session cannot hold failover up.
* **Same-token dispatches.** The pass also moves an effect that stayed ``dispatched`` under the lease that is still
  current for longer than ``effect_in_flight_max_seconds`` to ``unknown`` (see
  :func:`caliber.effects.records.expire_in_flight`) and asks the downstream about it like any other; the dispatcher itself
  records ``unknown`` at once for a settle that fails while it still holds its lease, and this bound is the backstop for
  one that could not even do that (the store was down, the thread died).
* **Targets** are the connector bindings of ``CALIBER_EFFECT_CALLBACK_CONNECTORS`` (the same ``(connector, project)`` that
  may report a receipt is the one asked). With none bound the pass still abandons the dispatches of lost leases and expires
  stuck in-flight ones; lookups report ``not_bound`` and nothing is settled by guessing.
* **Liveness.** It writes its own ``caliber_worker_heartbeats`` row (kind ``effect_reconciler``) on every tick, idle ones
  included, and on progress inside a long pass; a clean stop removes it.

Stopping: :meth:`stop` ends the pass at the next proposal and waits up to ``grace_seconds`` for the thread. A lookup in
progress is not interrupted (its own timeout, 30 s by default, bounds it); if that outlasts the grace the stop returns with
a warning and the thread finishes that one proposal alone. Its writes are still fenced by the lease, so it cannot record
anything after the lease is released. There are no claims to give back: the reconciler holds none.

Errors and logs carry identifiers, outcome names and exception classes, never a request, response or payload.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from caliber.effects.callbacks import parse_bindings
from caliber.effects.reconcile import (
    Lookup,
    Reconciliation,
    ask_downstream,
    reconcile_pass_from,
    targets_from_bindings,
)
from caliber.effects.records import LEASE_LOST, ProposalError
from caliber.observability.trace import bind_trace_id
from caliber.observability.worker_registry import (
    KIND_EFFECT_RECONCILER,
    deregister,
    new_worker_id,
    reap_stale_workers,
    record_heartbeat,
    stale_after_seconds,
)
from caliber.ownership import Lease

logger = logging.getLogger("caliber.orchestrator.effect_reconciler")

DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_BUDGET_SECONDS = 20.0
DEFAULT_IN_FLIGHT_MAX_SECONDS = 900.0
_DEFAULT_EXECUTION_LEASE_SECONDS = 30.0
#: The most a statement of the pass waits on PostgreSQL, as a fraction of the execution lease (the controller's own bound).
_LOCK_WAIT_FRACTION = 1.0 / 3.0
_MIN_LOCK_WAIT_MILLIS = 100
_REAP_EVERY_SECONDS = 60.0

#: Who may reconcile right now: returns the exact lease the owner captured, ``None`` when this process does not hold it.
LeaseProvider = Callable[[], Lease | None]

#: How a pass ended.
COMPLETE = "complete"
BUDGET = "budget"
STOPPED = "stopped"
LEASE_CHANGED = "lease_changed"
LEASE_REFUSED = "lease_lost"
REFUSED = "refused"


@dataclass(frozen=True)
class PassReport:
    """What one tick did: identifiers and counts only."""

    fencing_token: int
    ended: str
    results: tuple[Reconciliation, ...]
    #: ``None`` unless the pass ended on an error code a ``ProposalError`` carried (``lease_lost``, ``store_failed``, ...).
    code: str | None = None


class _PassOver(Exception):  # noqa: N818 - control flow of one pass, not an error
    """Raised by the progress check to end a pass between proposals."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _bounded_seconds(config: Any, name: str, default: float, low: float, high: float) -> float:
    value = getattr(config, name, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be a finite number from {low:g} to {high:g}")
    return float(value)


class EffectReconcilerTask:
    """Calls ``reconcile_pass`` under the lease the execution owner holds, on an interval, for as long as it holds it."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: Any,
        *,
        lease_provider: LeaseProvider | None = None,
        lookup: Lookup = ask_downstream,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = _bounded_seconds(
            config, "effect_reconciler_interval_seconds", DEFAULT_INTERVAL_SECONDS, 1.0, 3600.0
        )
        self._budget_seconds = _bounded_seconds(
            config, "effect_reconciler_budget_seconds", DEFAULT_BUDGET_SECONDS, 1.0, 600.0
        )
        self._in_flight_seconds = _bounded_seconds(
            config,
            "effect_in_flight_max_seconds",
            DEFAULT_IN_FLIGHT_MAX_SECONDS,
            60.0,
            86400.0,
        )
        self._execution_lease_seconds = _bounded_seconds(
            config, "execution_lease_seconds", _DEFAULT_EXECUTION_LEASE_SECONDS, 1.0, 3600.0
        )
        # Parsed once: the setting is validated at startup, and a pass must not depend on re-reading it.
        self._targets = targets_from_bindings(
            parse_bindings(getattr(config, "effect_callback_connectors", "[]"))
        )
        self._lease_provider = lease_provider
        self._lookup = lookup
        self._worker_id = new_worker_id("effect-reconciler", self)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._stop_requested = threading.Event()
        self._last_reap = float("-inf")
        self._last_beat = float("-inf")
        self._cursor: tuple[int, str] | None = None  # (fencing token, last proposal id looked at)

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def in_flight_seconds(self) -> float:
        return self._in_flight_seconds

    def bind_lease_provider(self, provider: LeaseProvider | None) -> None:
        """Reconcile only under the execution-owner lease ``provider`` returns (``None``: no lease, no reconciliation).

        The lifespan binds it once its :class:`~caliber.execution_role.ExecutionRoleController` exists, because this loop
        is built before that controller is.
        """
        self._lease_provider = provider

    # -- lifecycle -----------------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("EffectReconcilerTask.start() called while already running")
        self._stopped.clear()
        self._stop_requested.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.effect_reconciler")
        logger.info(
            "effect reconciler started (interval=%.1fs budget=%.1fs in-flight bound=%.0fs bindings=%d worker=%s)",
            self._interval_seconds,
            self._budget_seconds,
            self._in_flight_seconds,
            len(self._targets),
            self._worker_id,
        )

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        """End the pass at its next proposal and wait (up to ``grace_seconds``) for the thread; then drop the liveness row."""
        if self._task is None:
            return
        self._stop_requested.set()
        self._stopped.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "effect reconciler did not stop within %.1fs; a lookup in progress finishes alone and the pass ends "
                "at its next proposal (its writes stay fenced by the lease)",
                grace_seconds,
            )
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        await asyncio.to_thread(self._deregister_liveness)
        logger.info("effect reconciler stopped")

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                logger.exception("effect reconciler tick raised; continuing")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)

    # -- one pass ------------------------------------------------------------------------------

    def tick(self) -> PassReport | None:
        """One pass under the lease the controller holds now (``None``: not the owner, nothing was done)."""
        if self._lease_provider is None:
            logger.debug("effect reconciler has no execution-owner lease provider; nothing to do")
            return None
        with bind_trace_id():
            self._record_liveness(
                force=True
            )  # first: an idle reconciler must prove it is alive too
            lease = self._lease_provider()
            if lease is None:
                logger.debug("effect reconciler is not the execution owner; nothing reconciled")
                return None
            return self._pass(lease)

    def _pass(self, lease: Lease) -> PassReport:
        after = self._cursor[1] if self._cursor and self._cursor[0] == lease.fencing_token else None
        try:
            outcome = reconcile_pass_from(
                self._open,
                lease=lease,
                targets=self._targets,
                lookup=self._lookup,
                budget_seconds=self._budget_seconds,
                on_progress=self._progress(lease),
                in_flight_seconds=self._in_flight_seconds,
                after=after,
            )
        except _PassOver as over:
            # the lease is gone or replaced, or this loop is stopping: nothing more is read, asked or written
            self._cursor = None
            ended = STOPPED if over.reason == STOPPED else LEASE_CHANGED
            logger.info(
                "effect reconciler pass ended (%s); no further proposal is looked at", ended
            )
            return PassReport(lease.fencing_token, ended, ())
        except ProposalError as exc:
            self._cursor = None
            if exc.code == LEASE_LOST:
                logger.warning(
                    "effect reconciler lost the execution-owner lease (token %d); the pass ended and the next owner's "
                    "pass picks up what is left",
                    lease.fencing_token,
                )
                return PassReport(lease.fencing_token, LEASE_REFUSED, (), exc.code)
            logger.warning("effect reconciler pass refused (%s); retrying next tick", exc.code)
            return PassReport(lease.fencing_token, REFUSED, (), exc.code)
        self._cursor = (lease.fencing_token, outcome.resume_after) if outcome.resume_after else None
        self._log(lease, outcome.results)
        return PassReport(
            lease.fencing_token,
            BUDGET if outcome.resume_after else COMPLETE,
            tuple(outcome.results),
        )

    def _progress(self, lease: Lease) -> Callable[[], None]:
        """Called before each proposal: end the pass if this loop is stopping or the controller no longer holds ``lease``."""

        def check() -> None:
            if self._stop_requested.is_set():
                raise _PassOver(STOPPED)
            held = self._lease_provider() if self._lease_provider is not None else None
            if (
                held is None
                or held.owner_id != lease.owner_id
                or held.fencing_token != lease.fencing_token
            ):
                raise _PassOver(LEASE_CHANGED)
            self._record_liveness(force=False)

        return check

    @staticmethod
    def _log(lease: Lease, results: list[Reconciliation]) -> None:
        if not results:
            logger.debug(
                "effect reconciler pass (token %d): nothing to reconcile", lease.fencing_token
            )
            return
        counts = Counter(r.outcome for r in results)
        logger.info(
            "effect reconciler pass (token %d): %d proposal(s) looked at: %s",
            lease.fencing_token,
            len(results),
            ", ".join(f"{name}={n}" for name, n in sorted(counts.items())),
        )

    # -- database ------------------------------------------------------------------------------

    def _open(self) -> Session:
        """A session whose every transaction is bounded on PostgreSQL (SQLite has no such timeouts)."""
        session = self._session_factory()
        if session.get_bind().dialect.name == "postgresql":
            millis = max(
                int(self._execution_lease_seconds * _LOCK_WAIT_FRACTION * 1000),
                _MIN_LOCK_WAIT_MILLIS,
            )

            @event.listens_for(session, "after_begin")
            def _bound(_session: Session, _transaction: Any, connection: Any) -> None:
                connection.execute(text(f"SET LOCAL lock_timeout = {millis}"))
                connection.execute(text(f"SET LOCAL statement_timeout = {millis}"))

        return session

    # -- liveness ------------------------------------------------------------------------------

    def _record_liveness(self, *, force: bool) -> None:
        """Write this worker's own heartbeat row (never raises); inside a pass at most once per interval."""
        now = time.monotonic()
        if not force and now - self._last_beat < self._interval_seconds:
            return
        self._last_beat = now
        with self._session_factory() as session:
            record_heartbeat(session, worker_id=self._worker_id, kind=KIND_EFFECT_RECONCILER)
            self._reap_replaced_workers(session, now)

    def _reap_replaced_workers(self, session: Session, now: float) -> None:
        """Drop heartbeat rows of reconcilers this one has outlived, at most once a minute."""
        if now - self._last_reap < _REAP_EVERY_SECONDS:
            return
        self._last_reap = now
        reap_stale_workers(
            session,
            worker_id=self._worker_id,
            kind=KIND_EFFECT_RECONCILER,
            stale_after=stale_after_seconds(self._interval_seconds, self._budget_seconds + 30.0),
        )

    def _deregister_liveness(self) -> None:
        """Drop the heartbeat row on a clean stop; an unclean exit leaves it to go stale, which is the signal."""
        with self._session_factory() as session:
            deregister(session, worker_id=self._worker_id)


__all__ = [
    "BUDGET",
    "COMPLETE",
    "DEFAULT_BUDGET_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_IN_FLIGHT_MAX_SECONDS",
    "LEASE_CHANGED",
    "LEASE_REFUSED",
    "REFUSED",
    "STOPPED",
    "EffectReconcilerTask",
    "LeaseProvider",
    "PassReport",
]
