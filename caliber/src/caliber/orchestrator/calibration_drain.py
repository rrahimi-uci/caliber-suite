"""Background execution of queued tool calibrations.

Calibration scores every saved test case for a tool through the sandbox. With up to 200
cases each paying a cold subprocess start, that is minutes of work, and it used to be one
synchronous HTTP request. An earlier pass fixed the sharp edges — the waits blocked the
event loop, and a database session was held across execution — but the shape remained: the
client holds a connection open for the whole run, a proxy timeout or a closed lid loses the
result, and nothing recorded that the work happened at all.

This drains ``caliber_calibration_jobs``: claim a queued job, run it off the event loop,
write the outcome. Submitting returns ``202`` and the client polls.

## Claiming, not locking

A job is claimed with a conditional ``UPDATE ... WHERE status = 'queued'`` — the same
arbitration the webhook dead-letter replay uses. Two drains racing means one wins the row
and the other moves on. A lock held across execution would instead be stranded by exactly
the crash this durability exists to survive.

## A crashed drain leaves a claimed job, deliberately

Nothing re-queues a ``running`` job automatically. Calibration invokes tools, and a tool
may have side effects, so silently re-running one after an ambiguous failure is the wrong
default — the same reasoning the effect ledger applies to webhook and API nodes. The row
stays visible with its claim, and re-submitting is an operator decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import socket
import threading
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger("caliber.orchestrator.calibration_drain")

#: Long enough that an idle deployment is not polling constantly, short enough that a
#: submitted calibration starts feeling immediate rather than scheduled.
DEFAULT_INTERVAL_SECONDS = 2.0

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def worker_identity() -> str:
    """Host and pid, so a claimed job says which process took it."""
    return f"{socket.gethostname()}:{os.getpid()}"[:128]


def snapshot_tool(tool: Any) -> dict[str, Any]:
    """Return the JSON-safe executable Tool definition captured by a job.

    The snapshot deliberately uses the public schema rather than copying selected ORM
    attributes ad hoc. Adding a new Tool definition field to the schema then makes it part of
    calibration identity automatically instead of silently leaving queued work on old inputs.
    """
    from caliber.schemas import ToolSchema  # noqa: PLC0415

    # Cases have their own immutable job column, and the previous calibration is output
    # history rather than executable input. Excluding both avoids duplicating potentially
    # large (and output-bearing) payloads in every queued job.
    snapshot = ToolSchema.model_validate(tool).model_dump(
        mode="json", exclude={"test_cases", "last_calibration"}
    )
    # This is intentionally worker metadata rather than part of the public Tool schema.
    # Pydantic ignores it when reconstructing the executable ToolSchema, while the worker
    # retains it for the atomic ``UPDATE ... WHERE calibration_revision =`` fence.
    snapshot["calibration_revision"] = int(tool.calibration_revision or 1)
    return snapshot


def _definition_only(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Definition/revision fields, excluding cases and the prior result payload."""
    definition = copy.deepcopy(snapshot)
    definition.pop("test_cases", None)
    definition.pop("last_calibration", None)
    # Timestamps are registry bookkeeping, not executable identity. In particular, writing
    # another valid calibration advances ``updated_at`` and must not stale a result produced
    # from the same definition and fixtures.
    definition.pop("created_at", None)
    definition.pop("updated_at", None)
    # Revision is diagnosed separately, so an edit-and-revert is still stale without also
    # claiming the final definition bytes differ.
    definition.pop("calibration_revision", None)
    return definition


def _snapshot_revision(snapshot: dict[str, Any]) -> int:
    """Read a submitted revision, treating pre-0078 queued snapshots as revision 1."""
    value = snapshot.get("calibration_revision", 1)
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def invalidate_tool_calibration(session: Any, tool: Any) -> None:
    """Invalidate current evidence and advance the tool's calibration identity.

    Call this in the same transaction as every supported definition or fixture mutation.
    The revision advance is what makes a worker's later conditional attribution atomic;
    clearing the payload prevents clients from displaying evidence for the old identity.

    Both assignments must be database expressions, not Python changes on a previously
    loaded ORM object. If that object saw ``last_calibration = NULL``, a worker can attach a
    result before the mutation commits; assigning Python ``None`` again is then considered
    unchanged and SQLAlchemy omits it from the UPDATE. Likewise, two editors that both read
    revision N must produce N+2, not overwrite each other with N+1.
    """
    from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415

    # Flush the definition/fixture edit first so it and the fence remain one transaction.
    # The Core UPDATE then unconditionally writes NULL and increments from the value in the
    # database, serializing correctly on both SQLite and PostgreSQL.
    session.flush()
    invalidated = session.execute(
        update(CaliberToolRegistry)
        .where(CaliberToolRegistry.tool_id == tool.tool_id)
        .values(
            calibration_revision=CaliberToolRegistry.calibration_revision + 1,
            last_calibration=None,
        )
        .execution_options(synchronize_session=False)
    )
    if invalidated.rowcount != 1:
        raise RuntimeError(f"tool {tool.tool_id!r} disappeared during calibration invalidation")
    # Avoid returning the stale identity-map values after commit (the app's sessions use
    # expire_on_commit=False). Access while the session is open reloads the fenced values.
    session.expire(tool, ["calibration_revision", "last_calibration"])


def claim_next_job(session: Any, *, claimed_by: str) -> Any | None:
    """Claim one queued job, or return ``None``.

    The conditional ``UPDATE`` is the arbitration: whichever process changes the row from
    ``queued`` wins it. Selecting then updating without the ``status`` predicate would let
    two drains both believe they owned the same job.
    """
    from caliber.db.models import CaliberCalibrationJob  # noqa: PLC0415

    candidate = (
        session.execute(
            select(CaliberCalibrationJob)
            .where(CaliberCalibrationJob.status == STATUS_QUEUED)
            .order_by(CaliberCalibrationJob.created_at.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if candidate is None:
        return None
    claimed = session.execute(
        update(CaliberCalibrationJob)
        .where(
            CaliberCalibrationJob.job_id == candidate.job_id,
            CaliberCalibrationJob.status == STATUS_QUEUED,
        )
        .values(status=STATUS_RUNNING, claimed_at=_now(), claimed_by=claimed_by)
    )
    session.commit()
    if not claimed.rowcount:
        return None  # another drain won it
    return session.get(CaliberCalibrationJob, candidate.job_id)


def _load_job_inputs(
    session_factory: Any, job_id: str
) -> tuple[str, dict[str, Any], list[dict[str, Any]]] | None:
    """Copy all executable inputs out of a short-lived read session."""
    from caliber.db.models import CaliberCalibrationJob  # noqa: PLC0415

    with session_factory() as session:
        job = session.get(CaliberCalibrationJob, job_id)
        if job is None:
            return None
        return (
            str(job.tool_id),
            copy.deepcopy(job.tool_snapshot or {}),
            copy.deepcopy(list(job.test_cases or [])),
        )


def _stale_reasons(
    current: Any | None,
    submitted_snapshot: dict[str, Any],
    cases: list[dict[str, Any]],
) -> list[str]:
    if current is None:
        return ["tool_missing"]
    reasons: list[str] = []
    current_snapshot = snapshot_tool(current)
    if int(current.calibration_revision or 1) != _snapshot_revision(submitted_snapshot):
        reasons.append("tool_revision_changed")
    if _definition_only(current_snapshot) != _definition_only(submitted_snapshot):
        reasons.append("tool_definition_changed")
    if list(current.test_cases or []) != cases:
        reasons.append("test_cases_changed")
    return reasons


def attach_calibration_if_current(
    session: Any,
    tool_id: str,
    submitted_snapshot: dict[str, Any],
    cases: list[dict[str, Any]],
    result: dict[str, Any],
) -> list[str]:
    """Conditionally attach ``result`` and return stale reasons when it was refused.

    The full comparison catches legacy/direct database drift. The revision predicate closes
    the remaining check-then-write window: on both SQLite and PostgreSQL, an edit either
    commits first and makes this UPDATE miss, or commits second and clears this result.
    """
    from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415

    current = session.get(CaliberToolRegistry, tool_id)
    reasons = _stale_reasons(current, submitted_snapshot, cases)
    if reasons:
        return reasons

    attached = session.execute(
        update(CaliberToolRegistry)
        .where(
            CaliberToolRegistry.tool_id == tool_id,
            CaliberToolRegistry.calibration_revision == _snapshot_revision(submitted_snapshot),
        )
        .values(last_calibration=result)
        .execution_options(synchronize_session=False)
    )
    if attached.rowcount:
        return []

    # A mutation won after the comparison. Reload for useful diagnostics, but retain a
    # revision reason even if the author changed a field and then changed it back.
    session.expire_all()
    current = session.get(CaliberToolRegistry, tool_id)
    reasons = _stale_reasons(current, submitted_snapshot, cases)
    if current is not None and "tool_revision_changed" not in reasons:
        reasons.append("tool_revision_changed")
    return reasons


def _persist_result(
    session_factory: Any,
    job_id: str,
    tool_id: str,
    submitted_snapshot: dict[str, Any],
    cases: list[dict[str, Any]],
    result: dict[str, Any],
) -> None:
    """Attach a result and settle its job in one revision-fenced transaction."""
    from caliber.db.models import CaliberCalibrationJob  # noqa: PLC0415

    with session_factory() as session:
        reasons = attach_calibration_if_current(session, tool_id, submitted_snapshot, cases, result)
        if reasons:
            result["stale"] = True
            result["stale_reasons"] = reasons
            logger.info(
                "calibration job %s finished against stale inputs (%s); "
                "result recorded on the job but not on the tool",
                job_id,
                ", ".join(reasons),
            )
        settled = session.execute(
            update(CaliberCalibrationJob)
            .where(
                CaliberCalibrationJob.job_id == job_id,
                CaliberCalibrationJob.status == STATUS_RUNNING,
            )
            .values(
                result=result,
                error=None,
                status=STATUS_COMPLETED,
                finished_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )
        if not settled.rowcount:
            # This rollback also undoes a Tool attachment. A shutdown or operator action
            # that already settled the job must not leave an orphaned late result behind.
            session.rollback()
            logger.warning(
                "calibration job %s finished after leaving running state; ignoring", job_id
            )
            return
        session.commit()


def _persist_failure(session_factory: Any, job_id: str, exc: Exception) -> None:
    """Record a terminal failure without reusing a transaction touched by execution."""
    from caliber.db.models import CaliberCalibrationJob  # noqa: PLC0415

    try:
        with session_factory() as session:
            session.execute(
                update(CaliberCalibrationJob)
                .where(
                    CaliberCalibrationJob.job_id == job_id,
                    CaliberCalibrationJob.status == STATUS_RUNNING,
                )
                .values(
                    status=STATUS_FAILED,
                    error=f"{type(exc).__name__}: {exc}"[:2000],
                    finished_at=_now(),
                )
                .execution_options(synchronize_session=False)
            )
            session.commit()
    except Exception:
        # A database outage can make persistence impossible. Keep the original claim visible
        # as an explicitly recoverable ambiguous outcome instead of masking the execution
        # error with a second exception from the bookkeeping path.
        logger.exception("could not persist failure for calibration job %s", job_id)


def run_job(
    session_factory: Any,
    job_id: str,
    *,
    config: Any | None,
    stop_requested: threading.Event | None = None,
) -> None:
    """Execute one claimed job and record its outcome.

    Runs synchronously and is expected to be called off the event loop. Ordinary failures
    are recorded on the row rather than raised, so one bad tool does not stop every later
    calibration. A generation-fenced shutdown is intentionally different: it returns
    without guessing an outcome and leaves the claimed row visibly ``running``/ambiguous.

    Database sessions intentionally bracket only reads/writes. Sandbox execution can take
    minutes; holding a transaction and pooled connection across it both starves requests and
    makes the final stale check read the identity-mapped Tool object loaded before execution.
    """
    from caliber.calibration import aggregate  # noqa: PLC0415
    from caliber.routes.tools import _score_tool_cases  # noqa: PLC0415
    from caliber.schemas import ToolSchema  # noqa: PLC0415

    try:
        # A claim may complete in an executor thread after its drain generation has been
        # fenced. Do not even open a read session for that late completion, much less run
        # authored code or write a terminal result.
        inputs = (
            None
            if stop_requested is not None and stop_requested.is_set()
            else _load_job_inputs(session_factory, job_id)
        )
        if inputs is None:
            return
        tool_id, submitted_snapshot, cases = inputs
        if not submitted_snapshot:
            raise ValueError("job has no captured tool snapshot; resubmit it after upgrading")
        tool_data = ToolSchema.model_validate(submitted_snapshot)
        if tool_data.tool_id != tool_id:
            raise ValueError("captured tool snapshot does not match the job's tool id")
        if not cases:
            raise ValueError("no test cases were captured for this job")

        if stop_requested is not None and stop_requested.is_set():
            return
        scored = _score_tool_cases(
            tool_data,
            cases,
            config=config,
            should_stop=stop_requested.is_set if stop_requested is not None else None,
        )
        if stop_requested is not None and stop_requested.is_set():
            return
        result = aggregate(scored)
        result["ran_at"] = datetime.now(timezone.utc).isoformat()
        # A fresh session is the correctness boundary. The old implementation called
        # ``session.get`` twice in one identity map, so a concurrent edit was invisible.
        if stop_requested is not None and stop_requested.is_set():
            return
        _persist_result(
            session_factory,
            job_id,
            tool_id,
            submitted_snapshot,
            cases,
            result,
        )
    except Exception as exc:
        if stop_requested is not None and stop_requested.is_set():
            return
        if stop_requested is not None and stop_requested.is_set():
            return
        _persist_failure(session_factory, job_id, exc)
        logger.warning("calibration job %s failed", job_id, exc_info=True)


class CalibrationDrain:
    """Periodically execute queued calibration jobs.

    Modelled on :class:`~caliber.orchestrator.janitor.JanitorTask` so the lifecycle,
    cancellation, and error containment behave the way the other background tasks do
    rather than inventing a third convention.
    """

    def __init__(
        self,
        session_factory: Any,
        *,
        config: Any | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._interval_seconds = max(0.1, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._stop_requested = threading.Event()
        # Serializes generation capture and claim publication inside the event loop.
        # The database claim itself deliberately runs outside it: stop fences the retained
        # generation Event immediately and must never wait on a wedged executor thread.
        self._state_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("CalibrationDrain.start() called while already running")
        self._stopped.clear()
        async with self._state_lock:
            # Do not clear/reuse the old Event: a thread left over from a bounded stop must
            # retain a permanently-set signal even if this drain is started again.
            self._stop_requested = threading.Event()
        self._task = asyncio.create_task(self._loop(), name="caliber.calibration_drain")

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        if self._task is None:
            return
        task = self._task
        grace = max(0.0, float(grace_seconds))
        # Fence this generation before waiting for *anything*. In particular, `_claim_one`
        # runs in an executor and can wedge inside a database driver. It used to hold the
        # asyncio state lock across that await, which made stop() wait forever merely to set
        # this flag. threading.Event is safe to set from the event-loop thread without that
        # lock, and every worker retains its own generation's Event across restart.
        self._stopped.set()
        self._stop_requested.set()
        # Deliberately perform no stop-time database settlement. `asyncio.to_thread`
        # cancellation does not cancel its executor thread; a best-effort settlement can
        # start after its wait times out and touch an engine the lifespan has disposed.
        # An interrupted claim therefore remains visibly `running`/ambiguous for explicit
        # operator resolution. The retained generation Event prevents its scorer from
        # attaching a late result when a non-cooperative call eventually returns.
        try:
            if grace > 0:
                await asyncio.wait_for(asyncio.shield(task), timeout=grace)
            elif not task.done():
                raise asyncio.TimeoutError
        except asyncio.TimeoutError:
            # Cancelling a to_thread await leaves its executor thread alive. The captured
            # stop Event and persistence fence above make that safe.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            self._task = None

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad tick must not stop every future calibration.
                logger.exception("calibration drain tick failed; continuing")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)

    async def tick(self) -> int:
        """Drain the queue once. Returns how many jobs ran."""
        ran = 0
        while True:
            # Capture the generation under the short state critical section, but never hold
            # that lock across the database claim: an executor thread stuck in the driver
            # must not prevent stop() from fencing this generation within its grace period.
            async with self._state_lock:
                if self._stopped.is_set() or self._stop_requested.is_set():
                    return ran
                stop_requested = self._stop_requested
            job_id = await asyncio.to_thread(self._claim_one)
            async with self._state_lock:
                # stop() may have run while the claim was in flight, or this drain may have
                # been restarted with a new Event after a bounded stop. A late claim is
                # never published as active and therefore can neither execute nor persist a
                # result in the newer generation.
                if (
                    self._stopped.is_set()
                    or stop_requested.is_set()
                    or stop_requested is not self._stop_requested
                ):
                    return ran
                if job_id is None:
                    return ran
            # Off the event loop: scoring spawns and waits on subprocesses, and holding
            # the loop for minutes would stall every request the app is serving.
            await asyncio.to_thread(self._execute, job_id, stop_requested)
            ran += 1

    def _claim_one(self) -> str | None:
        with self._session_factory() as session:
            job = claim_next_job(session, claimed_by=worker_identity())
            return str(job.job_id) if job is not None else None

    def _execute(self, job_id: str, stop_requested: threading.Event) -> None:
        run_job(
            self._session_factory,
            job_id,
            config=self._config,
            stop_requested=stop_requested,
        )


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "CalibrationDrain",
    "attach_calibration_if_current",
    "claim_next_job",
    "invalidate_tool_calibration",
    "run_job",
    "snapshot_tool",
    "worker_identity",
]
