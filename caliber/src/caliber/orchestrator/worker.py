"""Background worker for refinement jobs.

The worker is the consumer side of the queue the verify endpoint produces.
Each tick it:

1. **Atomically claims** the oldest queued job by transitioning its status
   from ``queued`` to ``running`` in a single SQL UPDATE that returns the
   claimed ``job_id`` (or nothing if no work is available). This is what
   makes multi-replica deployments safe — two workers running simultaneously
   never claim the same job because only one's UPDATE returns a row.
2. Runs the **stage loop**: reads the job's ``current_stage`` and dispatches
   to the matching stage function. After each stage commits, re-reads the
   row and dispatches the next one. Loop exits when the status leaves
   ``running`` (terminal states: ``candidate_ready``, ``rejected``,
   ``completed``, ``failed``) or when the stage isn't in the dispatch table.
3. On any stage failure marks the job ``failed`` with the error and writes
   an audit row.

The stage loop also handles the **retry path**: the eval stage's
self-correction loop resets a job to ``running/candidate`` (with gate
feedback in ``review_notes``) so the worker re-runs the candidate stage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import ExitStack, nullcontext, suppress
from datetime import datetime, timezone
from types import ModuleType
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.artifact_store import ArtifactStore
from caliber.audit import record as audit_record
from caliber.config import CaliberConfig
from caliber.db.models import CaliberAgentConfig, CaliberRefinementJob
from caliber.eval.provider import EvalProvider, EvalProviderError
from caliber.events.bus import EventBus
from caliber.llm.circuit_breaker import LLMCircuitOpenError
from caliber.llm.provider import LLMProvider, LLMProviderError
from caliber.observability import metrics
from caliber.observability.trace import bind_trace_id
from caliber.orchestrator.candidate import CandidateStateError, run_candidate
from caliber.orchestrator.diagnosis import DiagnosisStateError, run_diagnosis
from caliber.orchestrator.eval_stage import EvalStateError, run_eval
from caliber.orchestrator.evidence import EvidenceStateError, run_evidence
from caliber.orchestrator.triage import TriageStateError, run_triage
from caliber.trace_client import TraceClient

logger = logging.getLogger("caliber.orchestrator.worker")

# Floor for the number of stages a single ``_advance_job`` call will run before
# giving up — a defense against a buggy stage that fails to advance
# ``current_stage`` (without it the loop would spin forever). The effective cap
# is scaled up by the configured self-correction retries (see
# ``_max_stages_per_job``): a fresh job spends ~5 stages (triage, evidence,
# diagnosis, candidate, eval) and each retry adds 2 (candidate + eval), so a
# fixed 16 falsely failed jobs once ``refinement_max_iterations`` reached 6.
_MAX_STAGES_PER_JOB = 16
_BASE_STAGES_PER_JOB = 5


def _dispatch_triage(session: Session, job_id: str, worker: RefinementWorker) -> None:
    run_triage(session, job_id, llm=worker._llm_provider)


def _dispatch_evidence(session: Session, job_id: str, worker: RefinementWorker) -> None:
    run_evidence(session, job_id, trace_client=worker._trace_client)


def _dispatch_diagnosis(session: Session, job_id: str, worker: RefinementWorker) -> None:
    run_diagnosis(session, job_id, worker._llm_provider)


def _dispatch_candidate(session: Session, job_id: str, worker: RefinementWorker) -> None:
    run_candidate(
        session,
        job_id,
        worker._llm_provider,
        worker._artifact_store,
        config=worker._config,
    )


def _dispatch_eval(session: Session, job_id: str, worker: RefinementWorker) -> None:
    run_eval(
        session,
        job_id,
        worker._eval_provider,
        artifact_store=worker._artifact_store,
        config=worker._config,
    )


# Stage name → callable that runs that stage in a uniform shape.
# Adding a stage is one new entry here; nothing else changes.
_STAGE_DISPATCH: dict[str, Callable[[Session, str, RefinementWorker], None]] = {
    "triage": _dispatch_triage,
    "evidence": _dispatch_evidence,
    "diagnosis": _dispatch_diagnosis,
    "candidate": _dispatch_candidate,
    "eval": _dispatch_eval,
}


class RefinementWorker:
    """Background task that advances queued refinement jobs through stages.

    Safe to run with multiple replicas as of Phase 3.1 — the
    :meth:`_claim_next_job` UPDATE-RETURNING transaction guarantees that a
    given job is claimed by exactly one worker even when ticks overlap.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        llm_provider: LLMProvider,
        artifact_store: ArtifactStore,
        eval_provider: EvalProvider,
        interval_seconds: float = 5.0,
        event_bus: EventBus | None = None,
        config: CaliberConfig | None = None,
        trace_client: TraceClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm_provider = llm_provider
        self._artifact_store = artifact_store
        self._eval_provider = eval_provider
        # Optional trace-fetch client: the evidence stage uses it to enrich a
        # job with the real execution trace. ``None`` (tests) → trace omitted.
        self._trace_client = trace_client
        # Optional config: when set with a real ``llm_provider``, workflow
        # calibration/refinement eval replays candidates through the real
        # executor (golden-path roadmap, Wave 5) instead of the fake one.
        self._config = config
        self._interval_seconds = interval_seconds
        # Optional SSE bus: when the worker advances a job through a stage,
        # it publishes a ``job.advanced`` event so the UI's progress bar
        # updates in real time. Tests that don't care about events leave
        # this as ``None``; the publish helper no-ops in that case.
        self._event_bus = event_bus
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    def _publish(self, event: dict[str, object]) -> None:
        """Forward an event to the SSE bus when one is attached."""
        if self._event_bus is not None:
            self._event_bus.publish(event)

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("RefinementWorker.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.refinement_worker")
        logger.info("refinement worker started (interval=%.1fs)", self._interval_seconds)

    async def stop(self, *, grace_seconds: float = 30.0) -> None:
        """Signal stop and drain the current tick before cancelling.

        The worker may be mid-stage (an LLM call, an MLflow eval run)
        when shutdown fires. Cancelling immediately would leave the
        job pinned to ``status='running'`` with no terminal write —
        the janitor would eventually reap it, but that's a sloppy
        shutdown. We wait up to ``grace_seconds`` for the current
        tick to finish, then cancel if it hasn't.
        """
        if self._task is None:
            return
        self._stopped.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning(
                "refinement worker did not finish within %.1fs; cancelling",
                grace_seconds,
            )
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        except asyncio.CancelledError:
            # The outer shutdown cancelled us — make sure the inner
            # task is cancelled too before we return.
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            raise
        self._task = None
        logger.info("refinement worker stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("refinement worker tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    # ---- Sync work below ----

    def _tick(self) -> None:
        """Process one queued job, if any.

        Each tick is bound to its own trace ID so every log line emitted
        while a single job advances correlates under one ``trace_id``
        field — handy when the JSON log stream is interleaved with the
        poller's output.
        """
        with bind_trace_id() as trace_id:
            job_id = self._claim_next_job()
            if job_id is None:
                return
            self._advance_job(job_id, trace_id=trace_id)

    def _claim_next_job(self) -> str | None:
        """Atomically claim the oldest queued job whose agent is enabled.

        Implemented as ``UPDATE ... WHERE job_id = (SELECT ... LIMIT 1)
        AND status='queued' SET status='running' RETURNING job_id``. The
        double ``status='queued'`` check — once in the subquery, once on
        the outer UPDATE — defends against the race where the subquery
        resolves to a row another worker has claimed between the SELECT
        and UPDATE phases.

        The subquery joins ``caliber_agent_config.enabled = True`` so a
        paused agent's queued jobs sit in the queue without blocking
        other agents. Resuming the agent (PATCH /agents/{id} with
        ``enabled: true``) makes the same row eligible on the next tick.

        Works on both SQLite (3.35+) and Postgres without a
        backend-specific code path. Postgres can additionally use
        ``FOR UPDATE SKIP LOCKED`` for better throughput at high
        concurrency; that's a future optimization, not a correctness
        prerequisite.
        """
        with self._session_factory() as session:
            subq = (
                select(CaliberRefinementJob.job_id)
                .join(
                    CaliberAgentConfig,
                    CaliberAgentConfig.agent_id == CaliberRefinementJob.agent_id,
                )
                .where(CaliberRefinementJob.status == "queued")
                .where(CaliberAgentConfig.enabled.is_(True))
                .order_by(CaliberRefinementJob.created_at)
                .limit(1)
                .scalar_subquery()
            )
            stmt = (
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == subq)
                .where(CaliberRefinementJob.status == "queued")
                .values(
                    status="running",
                    # Seed the heartbeat on claim so the janitor doesn't
                    # immediately reap a freshly-claimed retry job
                    # whose ``last_heartbeat_at`` is from its prior
                    # attempt.
                    last_heartbeat_at=datetime.now(timezone.utc),
                )
                .returning(CaliberRefinementJob.job_id)
            )
            result = session.execute(stmt).scalar_one_or_none()
            session.commit()
            return result

    def _max_stages_per_job(self) -> int:
        """Per-``_advance_job`` stage cap, scaled by the configured retries.

        A fresh job spends ~``_BASE_STAGES_PER_JOB`` stages and each
        self-correction retry adds 2 (candidate + eval), so the runaway guard
        must allow for ``refinement_max_iterations`` retries — otherwise a
        legitimate retry loop is falsely failed as "stage loop exceeded". Never
        drops below the fixed floor.
        """
        iterations = self._config.refinement_max_iterations if self._config is not None else 0
        return max(_MAX_STAGES_PER_JOB, _BASE_STAGES_PER_JOB + 2 * (iterations + 1))

    def _advance_job(self, job_id: str, *, trace_id: str) -> None:
        """Run every eligible stage for this job in a stage-driven loop.

        Reads ``current_stage`` after each stage so the dispatch picks up
        whatever the prior stage transitioned to. This is what lets retry
        jobs (entered at ``queued/candidate``) skip triage/evidence/
        diagnosis and start at the candidate stage.
        """
        mlflow_mod: ModuleType | None = None
        with ExitStack() as stack:
            mlflow_mod = self._start_mlflow_run(job_id, trace_id=trace_id, stack=stack)
            _ = self._open_mlflow_span(
                mlflow_mod,
                stack=stack,
                name="caliber.refinement_job",
                attributes={
                    "caliber.job_id": job_id,
                    "caliber.trace_id": trace_id,
                },
            )

            exceeded_stage_limit = True
            for _ in range(self._max_stages_per_job()):
                try:
                    stage = self._current_stage(job_id)
                except LookupError:
                    exceeded_stage_limit = False
                    break
                if stage is None or stage not in _STAGE_DISPATCH:
                    # Status left ``running`` (terminal) or unknown stage —
                    # either way the loop has nothing left to do.
                    exceeded_stage_limit = False
                    break
                if not self._run_stage(job_id, stage, mlflow_mod=mlflow_mod):
                    exceeded_stage_limit = False
                    break

            if exceeded_stage_limit:
                # Loop exhausted — only reachable if a stage doesn't advance
                # ``current_stage``. Bail rather than spin forever.
                logger.error("job=%s exceeded max stages per advance; bailing", job_id)
                self._mark_failed(job_id, "stage loop exceeded max iterations")

            self._log_mlflow_job_end(mlflow_mod, job_id, trace_id=trace_id)

        if mlflow_mod is not None:
            self._flush_mlflow_traces(mlflow_mod)

    def _run_stage(
        self,
        job_id: str,
        stage: str,
        *,
        mlflow_mod: ModuleType | None = None,
    ) -> bool:
        """Run a single stage by name. Returns True iff the loop should continue.

        Failures are caught here so the caller doesn't have to know the
        full set of exception types. The job is marked ``failed`` and
        ``False`` returned so the caller exits the loop.
        """
        self._heartbeat(job_id)
        span: Any | None = None
        try:
            with self._stage_span_context(mlflow_mod, job_id=job_id, stage=stage) as opened_span:
                span = opened_span
                with self._session_factory() as session:
                    _STAGE_DISPATCH[stage](session, job_id, self)
            self._set_span_attribute(span, "caliber.stage.status", "completed")
            self._publish(
                {
                    "type": "job.advanced",
                    "job_id": job_id,
                    "completed_stage": stage,
                }
            )
            return True
        except (
            TriageStateError,
            EvidenceStateError,
            DiagnosisStateError,
            CandidateStateError,
            EvalStateError,
            LookupError,
        ) as exc:
            self._set_span_attribute(span, "caliber.stage.status", "failed")
            self._set_span_attribute(span, "caliber.stage.error", str(exc))
            logger.warning("job=%s ineligible for next stage: %s", job_id, exc)
            self._mark_failed(job_id, str(exc))
        except LLMCircuitOpenError as exc:
            self._set_span_attribute(span, "caliber.stage.status", "deferred")
            self._set_span_attribute(span, "caliber.stage.error", str(exc))
            # Distinct from a normal provider error: the breaker has
            # already decided the provider is unhealthy, so the
            # right move is to defer the job, not consume retry
            # budget on it. Re-queue at the current stage; the next
            # tick (or some later tick once the breaker closes)
            # picks it back up.
            logger.warning("job=%s deferred: llm circuit open (%s)", job_id, exc)
            self._requeue_for_circuit(job_id, stage, str(exc))
        except LLMProviderError as exc:
            self._set_span_attribute(span, "caliber.stage.status", "failed")
            self._set_span_attribute(span, "caliber.stage.error", str(exc))
            logger.warning("job=%s LLM provider failed: %s", job_id, exc)
            self._mark_failed(job_id, f"llm provider error: {exc}")
        except EvalProviderError as exc:
            self._set_span_attribute(span, "caliber.stage.status", "failed")
            self._set_span_attribute(span, "caliber.stage.error", str(exc))
            logger.warning("job=%s eval provider failed: %s", job_id, exc)
            self._mark_failed(job_id, f"eval provider error: {exc}")
        except Exception as exc:
            self._set_span_attribute(span, "caliber.stage.status", "failed")
            self._set_span_attribute(span, "caliber.stage.error", repr(exc))
            logger.exception("job=%s failed during stage run", job_id)
            self._mark_failed(job_id, f"unhandled error: {exc!r}")
        return False

    def _mlflow_job_context(self, job_id: str) -> dict[str, str | None] | None:
        """Return the MLflow-relevant context for a refinement job."""
        with self._session_factory() as session:
            job = session.get(CaliberRefinementJob, job_id)
            if job is None:
                return None
            agent = session.get(CaliberAgentConfig, job.agent_id)
            experiment_ref = agent.experiment_id if agent is not None else None
            return {
                "job_id": job.job_id,
                "agent_id": job.agent_id,
                "artifact_type": job.artifact_type,
                "optimizer_type": job.optimizer_type,
                "experiment_ref": experiment_ref,
                "run_id": job.mlflow_run_id,
            }

    def _start_mlflow_run(
        self,
        job_id: str,
        *,
        trace_id: str,
        stack: ExitStack,
    ) -> ModuleType | None:
        """Best-effort MLflow run binding for one refinement job.

        Returns ``None`` when MLflow is unavailable or misconfigured; the
        orchestrator still runs normally in that case.
        """
        mlflow_mod = self._import_mlflow()
        if mlflow_mod is None:
            return None

        context = self._mlflow_job_context(job_id)
        if context is None:
            return None

        start_run = getattr(mlflow_mod, "start_run", None)
        if not callable(start_run):
            return None

        run_kwargs: dict[str, Any] = {}
        existing_run_id = context.get("run_id")
        if isinstance(existing_run_id, str) and existing_run_id:
            run_kwargs["run_id"] = existing_run_id
        else:
            experiment_id = self._resolve_mlflow_experiment_id(
                mlflow_mod,
                context.get("experiment_ref"),
            )
            if experiment_id is None:
                logger.info(
                    "job=%s skipping mlflow run binding: experiment %r is not resolvable",
                    job_id,
                    context.get("experiment_ref"),
                )
                return None
            run_kwargs["experiment_id"] = experiment_id
            run_kwargs["run_name"] = f"caliber-refinement-{job_id}"

        tags = {
            "caliber.job_id": job_id,
            "caliber.agent_id": str(context.get("agent_id") or ""),
            "caliber.artifact_type": str(context.get("artifact_type") or ""),
            "caliber.optimizer_type": str(context.get("optimizer_type") or ""),
            "caliber.trace_id": trace_id,
        }

        try:
            run = stack.enter_context(start_run(tags=tags, **run_kwargs))
        except Exception:
            logger.warning(
                "job=%s unable to start MLflow run; continuing without run binding",
                job_id,
                exc_info=True,
            )
            return None

        run_info = getattr(run, "info", None)
        resolved_run_id = str(getattr(run_info, "run_id", "") or "")
        if resolved_run_id and resolved_run_id != existing_run_id:
            self._set_job_mlflow_run_id(job_id, resolved_run_id)

        self._set_mlflow_tags(mlflow_mod, tags)
        self._log_mlflow_dict(
            mlflow_mod,
            {
                "event": "refinement_job_started",
                "job_id": job_id,
                "trace_id": trace_id,
                "run_id": resolved_run_id,
                "agent_id": context.get("agent_id"),
                "artifact_type": context.get("artifact_type"),
                "optimizer_type": context.get("optimizer_type"),
            },
            artifact_file="caliber/job_start.json",
        )

        return mlflow_mod

    def _resolve_mlflow_experiment_id(  # noqa: PLR0911
        self,
        mlflow_mod: ModuleType,
        experiment_ref: str | None,
    ) -> str | None:
        """Resolve configured experiment ref (id or name) to an id string."""
        if not isinstance(experiment_ref, str):
            return None
        normalized = experiment_ref.strip()
        if not normalized:
            return None
        if normalized.isdigit():
            return normalized

        resolver = getattr(mlflow_mod, "get_experiment_by_name", None)
        if callable(resolver):
            try:
                experiment = resolver(normalized)
            except Exception:
                logger.warning(
                    "mlflow.get_experiment_by_name failed for %r",
                    normalized,
                    exc_info=True,
                )
                return None
            if experiment is None:
                return None
            experiment_id = str(getattr(experiment, "experiment_id", "") or "")
            return experiment_id or None

        return None

    def _set_job_mlflow_run_id(self, job_id: str, run_id: str) -> None:
        """Persist the MLflow run id attached to a job."""
        with self._session_factory() as session:
            session.execute(
                update(CaliberRefinementJob)
                .where(CaliberRefinementJob.job_id == job_id)
                .values(mlflow_run_id=run_id)
            )
            session.commit()

    def _stage_span_context(
        self,
        mlflow_mod: ModuleType | None,
        *,
        job_id: str,
        stage: str,
    ) -> Any:
        """Return a context manager for a stage span (or a no-op manager)."""
        if mlflow_mod is None:
            return nullcontext(None)

        start_span = getattr(mlflow_mod, "start_span", None)
        if not callable(start_span):
            return nullcontext(None)

        try:
            return start_span(
                name=f"caliber.stage.{stage}",
                span_type="CHAIN",
                attributes={
                    "caliber.job_id": job_id,
                    "caliber.stage": stage,
                },
            )
        except Exception:
            logger.debug(
                "job=%s stage=%s unable to create MLflow span",
                job_id,
                stage,
                exc_info=True,
            )
            return nullcontext(None)

    def _open_mlflow_span(
        self,
        mlflow_mod: ModuleType | None,
        *,
        stack: ExitStack,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Any | None:
        """Open a span in the provided stack, returning the live span object."""
        if mlflow_mod is None:
            return None
        start_span = getattr(mlflow_mod, "start_span", None)
        if not callable(start_span):
            return None
        try:
            return stack.enter_context(
                start_span(
                    name=name,
                    span_type="CHAIN",
                    attributes=dict(attributes or {}),
                )
            )
        except Exception:
            logger.debug("unable to open MLflow span %s", name, exc_info=True)
            return None

    def _set_span_attribute(self, span: Any | None, key: str, value: Any) -> None:
        """Best-effort span attribute setter."""
        if span is None:
            return
        setter = getattr(span, "set_attribute", None)
        if not callable(setter):
            return
        try:
            setter(key, value)
        except Exception:
            logger.debug("failed setting span attribute %s", key, exc_info=True)

    def _log_mlflow_job_end(
        self,
        mlflow_mod: ModuleType | None,
        job_id: str,
        *,
        trace_id: str,
    ) -> None:
        """Emit end-of-job state to the active MLflow run."""
        if mlflow_mod is None:
            return

        snapshot = self._job_snapshot(job_id)
        if snapshot is None:
            return

        self._set_mlflow_tags(
            mlflow_mod,
            {
                "caliber.job_status": str(snapshot.get("status") or ""),
                "caliber.current_stage": str(snapshot.get("current_stage") or ""),
                "caliber.trace_id": trace_id,
            },
        )

        total_tokens = snapshot.get("total_tokens")
        if isinstance(total_tokens, int | float):
            self._log_mlflow_metric(mlflow_mod, "caliber.total_tokens", float(total_tokens))

        cost_usd = snapshot.get("cost_usd")
        if isinstance(cost_usd, int | float):
            self._log_mlflow_metric(mlflow_mod, "caliber.cost_usd", float(cost_usd))

        self._log_mlflow_dict(
            mlflow_mod,
            {
                "event": "refinement_job_finished",
                "trace_id": trace_id,
                **snapshot,
            },
            artifact_file="caliber/job_end.json",
        )

    def _job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        """Read a compact refinement-job snapshot for MLflow artifacts."""
        with self._session_factory() as session:
            job = session.get(CaliberRefinementJob, job_id)
            if job is None:
                return None
            return {
                "job_id": job.job_id,
                "agent_id": job.agent_id,
                "artifact_type": job.artifact_type,
                "optimizer_type": job.optimizer_type,
                "status": job.status,
                "current_stage": job.current_stage,
                "attempt_count": job.attempt_count,
                "total_tokens": job.total_tokens,
                "cost_usd": job.cost_usd,
                "mlflow_run_id": job.mlflow_run_id,
                "error_message": job.error_message,
            }

    def _import_mlflow(self) -> ModuleType | None:
        try:
            import mlflow  # noqa: PLC0415

            return mlflow
        except ImportError:
            return None

    def _set_mlflow_tags(self, mlflow_mod: ModuleType, tags: dict[str, Any]) -> None:
        setter = getattr(mlflow_mod, "set_tags", None)
        if not callable(setter):
            return
        try:
            setter(tags)
        except Exception:
            logger.debug("failed to set MLflow tags", exc_info=True)

    def _log_mlflow_dict(
        self,
        mlflow_mod: ModuleType,
        payload: dict[str, Any],
        *,
        artifact_file: str,
    ) -> None:
        logger_fn = getattr(mlflow_mod, "log_dict", None)
        if not callable(logger_fn):
            return
        try:
            logger_fn(payload, artifact_file=artifact_file)
        except Exception:
            logger.debug("failed to log MLflow dict artifact %s", artifact_file, exc_info=True)

    def _log_mlflow_metric(self, mlflow_mod: ModuleType, key: str, value: float) -> None:
        logger_fn = getattr(mlflow_mod, "log_metric", None)
        if not callable(logger_fn):
            return
        try:
            logger_fn(key, value)
        except Exception:
            logger.debug("failed to log MLflow metric %s", key, exc_info=True)

    def _flush_mlflow_traces(self, mlflow_mod: ModuleType) -> None:
        """Flush async trace exports so traces appear in MLflow immediately."""
        flush_fn = getattr(mlflow_mod, "flush_trace_async_logging", None)
        if not callable(flush_fn):
            flush_fn = getattr(mlflow_mod, "flush_async_logging", None)
        if not callable(flush_fn):
            return
        try:
            # ``terminate=False`` keeps the global queue alive for future jobs.
            flush_fn(terminate=False)
        except TypeError:
            try:
                flush_fn()
            except Exception:
                logger.debug("failed to flush MLflow trace queue", exc_info=True)
        except Exception:
            logger.debug("failed to flush MLflow trace queue", exc_info=True)

    def _current_stage(self, job_id: str) -> str | None:
        """Return ``current_stage`` if the job is running, else ``None``.

        ``None`` signals "stop the stage loop" — the job left the
        ``running`` status, which means a stage marked it terminal (e.g.
        ``candidate_ready`` after a passing eval gate).
        """
        with self._session_factory() as session:
            job = session.get(CaliberRefinementJob, job_id)
            if job is None:
                raise LookupError(f"job {job_id!r} vanished mid-advance")
            if job.status != "running":
                return None
            return job.current_stage

    def _heartbeat(self, job_id: str) -> None:
        """Bump ``last_heartbeat_at`` on the job row.

        Fired before each stage so the janitor's "stale heartbeat → reap"
        check is keyed off a recent timestamp. Failures here are
        intentionally swallowed: a transient DB hiccup shouldn't fail
        the stage, and the next heartbeat will recover. The worst case
        is the janitor reaping a job that's actually fine — but a
        reaped job's idempotency properties match the retry-changes
        flow, so we can absorb the cost.
        """
        try:
            with self._session_factory() as session:
                session.execute(
                    update(CaliberRefinementJob)
                    .where(CaliberRefinementJob.job_id == job_id)
                    .values(last_heartbeat_at=datetime.now(timezone.utc))
                )
                session.commit()
        except Exception:
            logger.warning("heartbeat for job=%s failed; continuing", job_id, exc_info=True)

    def _requeue_for_circuit(self, job_id: str, stage: str, reason: str) -> None:
        """Reset a job to ``queued`` so it'll be retried after the breaker closes.

        The job stays at its current stage — the breaker is a
        transient upstream-health condition, not a state-machine
        regression — and lands back in the queue for the next tick to
        claim. An audit row records the deferral so the trail shows
        *why* the job ran later than expected.

        Note that the next claim could race the breaker still being
        open (especially at short intervals + long open durations).
        That just trips this same path again — the cost is one extra
        UPDATE per tick, which is acceptable for a transient
        condition.
        """
        with self._session_factory() as session:
            job = session.get(CaliberRefinementJob, job_id)
            if job is None:
                return
            # Only requeue jobs that were running under this worker.
            # A terminal/parked status here means another actor (admin
            # action, eval gate, completion) already transitioned the
            # job — resurrecting it back to ``queued`` would re-run a
            # stage on a job nobody asked us to revisit.
            if job.status != "running":
                logger.info(
                    "skipping _requeue_for_circuit for job=%s: status=%s (not running)",
                    job_id,
                    job.status,
                )
                return
            job.status = "queued"
            audit_record(
                session,
                actor="refinement_worker",
                action="defer_job",
                entity_type="refinement_job",
                entity_id=job_id,
                details={"stage": stage, "reason": reason},
            )
            session.commit()
        self._publish(
            {
                "type": "job.deferred",
                "job_id": job_id,
                "stage": stage,
                "reason": reason,
            }
        )

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        agent_id: str | None = None
        artifact_type: str | None = None
        with self._session_factory() as session:
            job = session.get(CaliberRefinementJob, job_id)
            if job is None:
                return
            # Only the worker's own ``running`` jobs are eligible to be
            # transitioned to ``failed`` here. A stage may have committed
            # a different status (e.g. ``candidate_ready`` after the
            # eval gate clears, or ``completed`` if the stage
            # finished before the error fired) and we must not clobber
            # that with a generic failure record. Same applies to admin-
            # driven transitions to ``rejected`` between commits.
            if job.status != "running":
                logger.info(
                    "skipping _mark_failed for job=%s: status=%s (not running)",
                    job_id,
                    job.status,
                )
                return
            previous_status = job.status
            job.status = "failed"
            job.error_message = error_message
            agent_id = job.agent_id
            artifact_type = job.artifact_type
            audit_record(
                session,
                actor="refinement_worker",
                action="fail_job",
                entity_type="refinement_job",
                entity_id=job_id,
                details={"from_status": previous_status, "error": error_message},
            )
            session.commit()
        if agent_id is not None and artifact_type is not None:
            metrics.record_job_terminal(
                agent_id=agent_id, artifact_type=artifact_type, status="failed"
            )
        self._publish(
            {
                "type": "job.failed",
                "job_id": job_id,
                "error": error_message,
            }
        )
