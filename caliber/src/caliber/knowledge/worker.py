"""Background worker for queued knowledge-base builds."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import (
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseRun,
    CaliberKnowledgeBaseRunEvent,
    CaliberKnowledgeBaseVersion,
)
from caliber.knowledge.service import KnowledgeBaseService, _utcnow
from caliber.observability.trace import bind_trace_id

logger = logging.getLogger("caliber.knowledge.worker")


class KnowledgeBaseWorker:
    """Claims queued knowledge-base runs and executes them with lease recovery."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: Any,
        object_store_client: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._interval_seconds = float(config.knowledge_build_worker_interval_seconds)
        self._lease_duration = timedelta(seconds=float(config.knowledge_build_lease_seconds))
        self._worker_id = f"knowledge-build-worker-{id(self):x}"
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._service = KnowledgeBaseService(
            config=config,
            session_factory=session_factory,
            object_store_client=object_store_client,
        )

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("KnowledgeBaseWorker.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.knowledge_build_worker")
        logger.info(
            "knowledge-base worker started (interval=%.1fs lease=%.1fs worker=%s)",
            self._interval_seconds,
            self._lease_duration.total_seconds(),
            self._worker_id,
        )

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        if self._task is None:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "knowledge-base worker did not stop within %.1fs; cancelling",
                grace_seconds,
            )
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        logger.info("knowledge-base worker stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("knowledge-base worker tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    def _tick(self) -> None:
        with bind_trace_id():
            self._recover_expired_leases()
            run_id = self._claim_next_run()
            if run_id is None:
                return
            self._execute_run(run_id)

    def _append_event(
        self,
        session: Session,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        current = (
            session.execute(
                select(func.max(CaliberKnowledgeBaseRunEvent.sequence)).where(
                    CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == run_id
                )
            )
            .scalars()
            .first()
        )
        session.add(
            CaliberKnowledgeBaseRunEvent(
                knowledge_base_run_id=run_id,
                sequence=(current or 0) + 1,
                event_type=event_type,
                payload=payload,
                created_at=_utcnow(),
            )
        )

    def _recover_expired_leases(self) -> None:
        now = _utcnow()
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(CaliberKnowledgeBaseRun)
                    .where(CaliberKnowledgeBaseRun.status == "running")
                    .where(CaliberKnowledgeBaseRun.lease_expires_at.is_not(None))
                    .where(CaliberKnowledgeBaseRun.lease_expires_at < now)
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            for run in rows:
                run.status = "queued"
                run.queued_at = now
                run.claimed_by = None
                run.claimed_at = None
                run.lease_expires_at = None
                run.last_heartbeat_at = None
                run.error_summary = "worker lease expired; build re-queued for recovery"
                version = session.get(
                    CaliberKnowledgeBaseVersion,
                    run.knowledge_base_version_id,
                )
                if version is not None:
                    version.status = "queued"
                    version.error_summary = None
                    version.completed_at = None
                knowledge_base = session.get(CaliberKnowledgeBase, run.knowledge_base_id)
                if knowledge_base is not None:
                    knowledge_base.last_run_status = "queued"
                self._append_event(
                    session,
                    run_id=run.knowledge_base_run_id,
                    event_type="build_requeued",
                    payload={
                        "reason": "lease_expired",
                        "worker_id": self._worker_id,
                        "at": now.isoformat(),
                    },
                )
            session.commit()

    def _claim_next_run(self) -> str | None:
        now = _utcnow()
        with self._session_factory() as session:
            subquery = (
                select(CaliberKnowledgeBaseRun.knowledge_base_run_id)
                .where(CaliberKnowledgeBaseRun.status == "queued")
                .order_by(
                    func.coalesce(
                        CaliberKnowledgeBaseRun.queued_at,
                        CaliberKnowledgeBaseRun.created_at,
                    ).asc()
                )
                .limit(1)
                .scalar_subquery()
            )
            claimed = session.execute(
                update(CaliberKnowledgeBaseRun)
                .where(CaliberKnowledgeBaseRun.knowledge_base_run_id == subquery)
                .where(CaliberKnowledgeBaseRun.status == "queued")
                .values(
                    status="running",
                    started_at=func.coalesce(CaliberKnowledgeBaseRun.started_at, now),
                    claimed_by=self._worker_id,
                    claimed_at=now,
                    last_heartbeat_at=now,
                    lease_expires_at=now + self._lease_duration,
                )
                .returning(CaliberKnowledgeBaseRun.knowledge_base_run_id)
            ).scalar_one_or_none()
            session.commit()
            return claimed

    def _renew_lease(self, run_id: str) -> int:
        now = _utcnow()
        with self._session_factory() as session:
            result = session.execute(
                update(CaliberKnowledgeBaseRun)
                .where(CaliberKnowledgeBaseRun.knowledge_base_run_id == run_id)
                .where(CaliberKnowledgeBaseRun.claimed_by == self._worker_id)
                .where(CaliberKnowledgeBaseRun.status == "running")
                .values(
                    last_heartbeat_at=now,
                    lease_expires_at=now + self._lease_duration,
                )
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def _heartbeat_loop(self, run_id: str, stop_event: threading.Event) -> None:
        interval = max(5.0, self._lease_duration.total_seconds() / 3.0)
        while not stop_event.wait(interval):
            try:
                self._renew_lease(run_id)
            except Exception:
                logger.debug("knowledge-base heartbeat failed for %s", run_id, exc_info=True)

    def _execute_run(self, run_id: str) -> None:
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run_id, heartbeat_stop),
            name=f"caliber-kb-heartbeat-{run_id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            self._service.execute_run(
                run_id,
                worker_id=self._worker_id,
                heartbeat=lambda: self._renew_lease(run_id),
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=5.0)
