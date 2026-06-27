"""Cron scheduler for workflow Start triggers.

A periodic background task (same shape as :class:`~caliber.orchestrator.janitor.
JanitorTask`) that, each tick, scans active deployments, reads each deployed
version's Start-node ``cron`` trigger, and enqueues a run when the expression
fires for the current minute.

Duplicate suppression is idempotency-key based — ``cron:{alias}:{minute}`` — so
the unique ``(workflow_id, source, idempotency_key)`` constraint guarantees at
most one run per scheduled minute even if a tick overlaps a minute boundary or
runs on more than one process. No schedule table / migration is required: the
manifest is the source of truth.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.observability.trace import bind_trace_id
from caliber.workflows.cron import cron_matches
from caliber.workflows.manifest import StartNode, parse_manifest
from caliber.workflows.run_launch import enqueue_workflow_run

logger = logging.getLogger("caliber.orchestrator.scheduler")

DEFAULT_INTERVAL_SECONDS = 60.0


class WorkflowSchedulerTask:
    """Fires cron-triggered workflow runs from active deployments."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        publish: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = interval_seconds
        self._publish = publish
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("WorkflowSchedulerTask.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.workflow_scheduler")
        logger.info("workflow scheduler started (interval=%.1fs)", self._interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("workflow scheduler stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("workflow scheduler tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------
    # Sync work
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        with bind_trace_id():
            self._tick_inner(datetime.now(timezone.utc))

    def _tick_inner(self, now_utc: datetime) -> int:
        """Enqueue runs for every cron trigger that fires at ``now_utc``.

        Returns the number of runs enqueued (used by tests).
        """
        fired = 0
        with self._session_factory() as session:
            deployments = (
                session.execute(
                    select(CaliberWorkflowDeployment).where(
                        CaliberWorkflowDeployment.status == "active"
                    )
                )
                .scalars()
                .all()
            )
            for deployment in deployments:
                if self._maybe_fire(session, deployment, now_utc):
                    fired += 1
            if fired:
                session.commit()
        return fired

    def _maybe_fire(  # noqa: PLR0911 - flat guard sequence reads clearer than nesting
        self,
        session: Session,
        deployment: CaliberWorkflowDeployment,
        now_utc: datetime,
    ) -> bool:
        version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version is None:
            return False
        trigger = self._start_trigger(version)
        if trigger is None or trigger.mode != "cron" or not trigger.enabled:
            return False
        # A version deployed to several aliases fires only for its target alias.
        if trigger.alias != deployment.alias:
            return False

        local_now = self._localize(now_utc, trigger.timezone)
        try:
            if not cron_matches(trigger.cron, local_now):
                return False
        except ValueError:
            logger.warning(
                "invalid cron %r on workflow %s; skipping", trigger.cron, deployment.workflow_id
            )
            return False

        workflow = session.get(CaliberWorkflow, deployment.workflow_id)
        if workflow is None or workflow.status in {"paused", "archived"}:
            return False

        minute_key = local_now.strftime("%Y-%m-%dT%H:%M")
        _run, created = enqueue_workflow_run(
            session,
            workflow=workflow,
            version=version,
            alias=deployment.alias,
            source="cron",
            actor="@scheduler",
            idempotency_key=f"cron:{deployment.alias}:{minute_key}",
            publish=self._publish,
        )
        if created:
            logger.info(
                "scheduled run for workflow %s alias %s (cron=%r)",
                deployment.workflow_id,
                deployment.alias,
                trigger.cron,
            )
        return created

    @staticmethod
    def _start_trigger(version: CaliberWorkflowVersion):  # type: ignore[no-untyped-def]
        try:
            manifest = parse_manifest(version.manifest)
        except Exception:
            return None
        for node in manifest.nodes.values():
            if isinstance(node, StartNode):
                return node.trigger
        return None

    @staticmethod
    def _localize(now_utc: datetime, tz_name: str) -> datetime:
        try:
            return now_utc.astimezone(ZoneInfo(tz_name or "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            return now_utc
