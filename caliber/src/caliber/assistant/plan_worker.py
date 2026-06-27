"""Background worker that advances Aria plans parked on async jobs.

Phase 4 of the agentic-orchestration plan. The synchronous executor parks a step
in ``waiting_job`` when a capability enqueues long-running work; this worker
periodically polls those in-flight jobs (via a :class:`JobStatusResolver`) and
resumes the plan when they finish — the DB-polling pattern the workflow-run
worker already uses. It is started/stopped by the app lifespan and gated by
``background_tasks_enabled`` (off in tests so it can't race seeded state).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from caliber.assistant.executor import (
    JobStatusResolver,
    MLflowJobStatusResolver,
    PlanExecutor,
)

logger = logging.getLogger(__name__)


class AriaPlanWorker:
    """Periodically resolves in-flight async plan steps and resumes their plans."""

    def __init__(
        self,
        session_factory: Any,
        *,
        config: Any,
        interval_seconds: float | None = None,
        resolver: JobStatusResolver | None = None,
        executor: PlanExecutor | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._interval_seconds = float(
            interval_seconds
            if interval_seconds is not None
            else getattr(config, "aria_plan_worker_interval_seconds", 10.0)
        )
        self._resolver = resolver or MLflowJobStatusResolver()
        self._executor = executor or PlanExecutor()
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def tick(self) -> list[str]:
        """One poll pass over all plans with a waiting_job step (sync; testable)."""
        return self._executor.poll_waiting_plans(
            session_factory=self._session_factory, config=self._config, resolver=self._resolver
        )

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("AriaPlanWorker.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.aria_plan_worker")
        logger.info("aria-plan worker started (interval=%.1fs)", self._interval_seconds)

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        if self._task is None:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("aria-plan worker stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self.tick)
                except Exception:
                    logger.exception("aria-plan worker tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise
