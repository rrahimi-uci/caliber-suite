"""Periodic settlement of incomplete external prompt release operations.

The provider alias and CALIBER's SQL row cannot commit atomically. Routes make
that boundary observable by persisting an intent before the provider call; this
task closes the operational loop by periodically observing aliases for rows in
``applying`` or ``reconcile_required``. It never guesses about ``prepared`` rows:
those prove no provider call started and require the explicit retry/abandon API.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

from caliber.release_operations import AliasResolver, reconcile_prompt_alias_releases

logger = logging.getLogger("caliber.orchestrator.release_reconciler")

DEFAULT_INTERVAL_SECONDS = 60.0


class ReleaseReconcilerTask:
    """Periodically reconcile incomplete prompt-alias release intents."""

    def __init__(
        self,
        session_factory: Any,
        *,
        resolve_alias: AliasResolver,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._resolve_alias = resolve_alias
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("ReleaseReconcilerTask.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.release_reconciler")
        logger.info("release reconciler started (interval=%.1fs)", self._interval_seconds)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopped.set()
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("release reconciler stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("release reconciliation tick failed; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    def _tick(self) -> int:
        with self._session_factory() as session:
            rows = reconcile_prompt_alias_releases(
                session,
                resolve_alias=self._resolve_alias,
            )
        if rows:
            logger.info("release reconciler inspected %d incomplete operation(s)", len(rows))
        return len(rows)
