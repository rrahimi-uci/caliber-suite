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
import logging
import os
import socket
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


def run_job(session: Any, job: Any, *, config: Any | None) -> None:
    """Execute one claimed job and record its outcome.

    Runs synchronously and is expected to be called off the event loop. Failures are
    recorded on the row rather than raised: a drain that dies on one bad tool would stop
    every later calibration, and the point of the record is that an outcome always exists.
    """
    from caliber.calibration import aggregate  # noqa: PLC0415
    from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415
    from caliber.routes.tools import _score_tool_cases  # noqa: PLC0415
    from caliber.schemas import ToolSchema  # noqa: PLC0415

    try:
        tool = session.get(CaliberToolRegistry, job.tool_id)
        if tool is None:
            raise ValueError(f"tool {job.tool_id!r} no longer exists")
        tool_data = ToolSchema.model_validate(tool)
        cases = list(job.test_cases or [])
        if not cases:
            raise ValueError("no test cases were captured for this job")

        scored = _score_tool_cases(tool_data, cases, config=config)
        result = aggregate(scored)
        result["ran_at"] = datetime.now(timezone.utc).isoformat()

        # The tool's own snapshot is only updated when its cases still match the ones this
        # job scored. Writing a result derived from cases an author has since edited would
        # attach a pass rate to a definition that never produced it.
        current = session.get(CaliberToolRegistry, job.tool_id)
        if current is not None and list(current.test_cases or []) == cases:
            current.last_calibration = result
        else:
            result["stale"] = True
            logger.info(
                "calibration job %s finished after its tool's cases changed; "
                "result recorded on the job but not on the tool",
                job.job_id,
            )
        job.result = result
        job.status = STATUS_COMPLETED
    except Exception as exc:
        job.status = STATUS_FAILED
        job.error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.warning("calibration job %s failed", job.job_id, exc_info=True)
    finally:
        job.finished_at = _now()
        session.commit()


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

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("CalibrationDrain.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="caliber.calibration_drain")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
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
            with self._session_factory() as session:
                job = claim_next_job(session, claimed_by=worker_identity())
                if job is None:
                    return ran
                job_id = job.job_id
            # Off the event loop: scoring spawns and waits on subprocesses, and holding
            # the loop for minutes would stall every request the app is serving.
            await asyncio.to_thread(self._execute, job_id)
            ran += 1

    def _execute(self, job_id: str) -> None:
        from caliber.db.models import CaliberCalibrationJob  # noqa: PLC0415

        with self._session_factory() as session:
            job = session.get(CaliberCalibrationJob, job_id)
            if job is None:  # pragma: no cover - claimed rows are not deleted
                return
            run_job(session, job, config=self._config)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "STATUS_RUNNING",
    "CalibrationDrain",
    "claim_next_job",
    "run_job",
    "worker_identity",
]
