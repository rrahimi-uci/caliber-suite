"""Background worker for queue-based workflow runs."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from caliber.audit import record as audit_record
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberProject,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowVersion,
)
from caliber.events.bus import EventBus
from caliber.ids import new_runtime_approval_id, new_workflow_run_checkpoint_id
from caliber.mcp_policy import deployment_blockers
from caliber.observability.trace import bind_trace_id
from caliber.secrets import resolve_secret
from caliber.storage import StorageError, WorkingDirectoryService, build_backend
from caliber.workflows.compiler import CompileError
from caliber.workflows.effect_ledger import SqlEffectLedger
from caliber.workflows.file_tools import (
    bind_managed_file_runtime,
    bind_run_read_tools,
    managed_file_tool_aliases,
)
from caliber.workflows.manifest import WorkflowManifestError
from caliber.workflows.memory_tools import bind_run_memory_tools
from caliber.workflows.promoter import build_executor, build_plan, workflow_run_summary
from caliber.workflows.run_events import append_run_event
from caliber.workflows.run_state import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_EXPIRED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_WAITING_EVENT,
    assert_run_transition,
    normalize_runtime_result_status,
)
from caliber.workflows.runtime import NodeStep, RuntimeResumeCheckpoint, WorkflowRunResult, execute

logger = logging.getLogger("caliber.orchestrator.workflow_run_worker")

_CANCEL_SENTINEL = "__CALIBER_WORKFLOW_RUN_CANCELLED__"


def _step_to_dict(step: NodeStep) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "node_id": step.node_id,
        "node_type": step.node_type,
        "status": step.status,
        "output": step.output,
        "tool_calls": step.tool_calls,
        "handoff_target": step.handoff_target,
        "detail": step.detail,
        "duration_ms": step.duration_ms,
        "input_by_port": dict(step.input_by_port or {}),
        "output_by_port": dict(step.output_by_port or {}),
    }
    if step.tokens > 0:
        payload["tokens"] = step.tokens
    if step.prompt_tokens > 0:
        payload["prompt_tokens"] = step.prompt_tokens
    if step.completion_tokens > 0:
        payload["completion_tokens"] = step.completion_tokens
    if step.cached_prompt_tokens > 0:
        payload["cached_prompt_tokens"] = step.cached_prompt_tokens
    if step.cost_usd > 0:
        payload["cost_usd"] = step.cost_usd
    if isinstance(step.model, str) and step.model:
        payload["model"] = step.model
    if isinstance(step.prompt_version, str) and step.prompt_version:
        payload["prompt_version"] = step.prompt_version
    return payload


def _node_started_to_dict(node_id: str, node_type: Any) -> dict[str, Any]:
    normalized_node_type = getattr(node_type, "value", node_type)
    return {
        "node_id": node_id,
        "node_type": str(normalized_node_type),
    }


def _input_from_summary(summary: dict[str, Any] | None) -> str:
    if not isinstance(summary, dict):
        return ""
    value = summary.get("input")
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _manifest_summary_metadata(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    copied: dict[str, Any] = {}
    manifest_mode = summary.get("manifest_mode")
    if manifest_mode in {"saved_version", "snapshot"}:
        copied["manifest_mode"] = manifest_mode
    manifest_hash = summary.get("manifest_hash")
    if isinstance(manifest_hash, str) and manifest_hash:
        copied["manifest_hash"] = manifest_hash
    workflow_version_number = summary.get("workflow_version_number")
    if isinstance(workflow_version_number, int):
        copied["workflow_version_number"] = workflow_version_number
    return copied


def _compile_error_summary(exc: CompileError) -> str:
    summary = str(exc)
    report = exc.report if isinstance(exc.report, dict) else None
    if not report:
        return summary
    errors = report.get("errors")
    if not isinstance(errors, list):
        return summary
    messages = [
        str(issue.get("message", "")).strip()
        for issue in errors
        if isinstance(issue, dict) and str(issue.get("message", "")).strip()
    ]
    if not messages:
        return summary
    return f"{summary}: {'; '.join(messages[:3])}"


def _manifest_parse_error_summary(exc: WorkflowManifestError | ValidationError) -> str:
    summary = "manifest is invalid and cannot be parsed"
    if isinstance(exc, WorkflowManifestError):
        detail = str(exc).strip()
        return f"{summary}: {detail}" if detail else summary
    messages: list[str] = []
    for issue in exc.errors()[:3]:
        if not isinstance(issue, dict):
            continue
        location = ".".join(str(part) for part in issue.get("loc", ()))
        detail = str(issue.get("msg", "")).strip()
        if location and detail:
            messages.append(f"{location}: {detail}")
        elif detail:
            messages.append(detail)
    if messages:
        return f"{summary}: {'; '.join(messages)}"
    detail = str(exc).strip()
    return f"{summary}: {detail}" if detail else summary


def _runtime_startup_error_summary(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"workflow runtime startup failed: {type(exc).__name__}: {detail}"
    return f"workflow runtime startup failed: {type(exc).__name__}"


def _runtime_execution_error_summary(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"workflow runtime raised unexpectedly: {type(exc).__name__}: {detail}"
    return f"workflow runtime raised unexpectedly: {type(exc).__name__}"


def _resume_checkpoint_preflight_error(  # noqa: PLR0911 - explicit fail-closed gate checks read clearer as early exits
    plan: Any,
    resume_checkpoint: RuntimeResumeCheckpoint | None,
) -> str | None:
    if resume_checkpoint is None:
        return None
    node = plan.ir.nodes.get(resume_checkpoint.node_id)
    if node is None:
        return f"references missing node {resume_checkpoint.node_id!r} in the current workflow plan"
    checkpoint_kind = resume_checkpoint.checkpoint_kind
    node_type = getattr(getattr(node, "node_type", None), "value", getattr(node, "node_type", None))
    binding = getattr(node, "binding", None)
    if checkpoint_kind == "wait_for_event" and node_type != "wait_for_event":
        return (
            f"kind {checkpoint_kind!r} does not match current node "
            f"{resume_checkpoint.node_id!r} type {node_type!r}"
        )
    if checkpoint_kind == "wait_until" and node_type != "wait_until":
        return (
            f"kind {checkpoint_kind!r} does not match current node "
            f"{resume_checkpoint.node_id!r} type {node_type!r}"
        )
    if checkpoint_kind == "human_approval" and node_type != "human_approval":
        return (
            f"kind {checkpoint_kind!r} does not match current node "
            f"{resume_checkpoint.node_id!r} type {node_type!r}"
        )
    if checkpoint_kind == "runtime_approval" and not (
        binding is not None and getattr(binding, "requires_approval", False)
    ):
        return (
            f"kind {checkpoint_kind!r} does not match current node "
            f"{resume_checkpoint.node_id!r} type {node_type!r}"
        )
    if not resume_checkpoint.replay_output:
        return None
    if node_type in {"wait_for_event", "wait_until", "human_approval"}:
        return (
            f"cannot replay output past gated node {resume_checkpoint.node_id!r}; "
            "an input snapshot resume is required"
        )
    if binding is not None and getattr(binding, "requires_approval", False):
        return (
            f"cannot replay output past gated node {resume_checkpoint.node_id!r}; "
            "an input snapshot resume is required"
        )
    return None


def _parse_checkpoint_resume_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_positive_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if parsed > 0 else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _waiting_event_step_context(
    result: WorkflowRunResult,
    node_id: str,
) -> tuple[str, str, dict[str, Any] | None]:
    step_output = ""
    step_node_type = ""
    checkpoint_state: dict[str, Any] | None = None
    for step in reversed(result.steps):
        if step.node_id != node_id:
            continue
        step_output = step.output
        step_node_type = step.node_type
        if isinstance(step.checkpoint_state, dict):
            checkpoint_state = dict(step.checkpoint_state)
        break
    if not step_output and result.steps:
        step_output = result.steps[-1].output
    return step_output, step_node_type, checkpoint_state


def _blocked_result_node_id(error: str | None, *, prefix: str) -> str | None:
    if not isinstance(error, str) or not error.startswith(prefix):
        return None
    node_id = error.split(":", 1)[1].strip()
    return node_id or None


def _checkpoint_input_by_port(checkpoint_state: dict[str, Any] | None) -> dict[str, Any]:
    if checkpoint_state is None:
        return {}
    raw = checkpoint_state.get("input_by_port", {})
    return dict(raw) if isinstance(raw, dict) else {}


def _wait_for_event_state_blob(
    *,
    node_id: str,
    step_output: str,
    checkpoint_state: dict[str, Any] | None,
) -> dict[str, Any]:
    state_blob: dict[str, Any] = {
        "kind": "wait_for_event",
        "node_id": node_id,
        "output": step_output,
        "input_by_port": _checkpoint_input_by_port(checkpoint_state),
    }
    expected_event_name = checkpoint_state.get("expected_event_name") if checkpoint_state else None
    if isinstance(expected_event_name, str) and expected_event_name.strip():
        state_blob["expected_event_name"] = expected_event_name
    correlation_key = checkpoint_state.get("correlation_key") if checkpoint_state else None
    if isinstance(correlation_key, str) and correlation_key.strip():
        state_blob["correlation_key"] = correlation_key
    correlation_value = checkpoint_state.get("correlation_value") if checkpoint_state else None
    if correlation_value not in (None, ""):
        state_blob["correlation_value"] = correlation_value
    timeout_seconds = checkpoint_state.get("timeout_seconds") if checkpoint_state else None
    parsed_timeout = _parse_positive_seconds(timeout_seconds)
    if parsed_timeout is not None:
        state_blob["timeout_seconds"] = parsed_timeout
    return state_blob


def _wait_until_state_blob(
    *,
    node_id: str,
    step_output: str,
    checkpoint_state: dict[str, Any] | None,
) -> dict[str, Any]:
    state_blob = {
        "kind": "wait_until",
        "node_id": node_id,
        "output": step_output,
        "input_by_port": _checkpoint_input_by_port(checkpoint_state),
    }
    resume_at = checkpoint_state.get("resume_at") if checkpoint_state else None
    if isinstance(resume_at, str) and resume_at.strip():
        state_blob["resume_at"] = resume_at
    wait_until = checkpoint_state.get("wait_until") if checkpoint_state else None
    if isinstance(wait_until, str) and wait_until.strip():
        state_blob["wait_until"] = wait_until
    timezone_name = checkpoint_state.get("timezone") if checkpoint_state else None
    if isinstance(timezone_name, str) and timezone_name.strip():
        state_blob["timezone"] = timezone_name
    return state_blob


def _waiting_event_state_blob(
    *,
    node_id: str,
    step_node_type: str,
    step_output: str,
    checkpoint_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if step_node_type == "wait_for_event":
        return _wait_for_event_state_blob(
            node_id=node_id,
            step_output=step_output,
            checkpoint_state=checkpoint_state,
        )
    if step_node_type == "wait_until":
        return _wait_until_state_blob(
            node_id=node_id,
            step_output=step_output,
            checkpoint_state=checkpoint_state,
        )
    return {
        "kind": "wait_event",
        "node_id": node_id,
        "output": step_output,
        "output_by_port": {"output": step_output},
    }


def _waiting_approval_checkpoint_payload(
    result: WorkflowRunResult,
) -> tuple[str, dict[str, Any]] | None:
    node_id = _blocked_result_node_id(result.error, prefix="waiting_approval:")
    if node_id is None:
        return None
    for step in reversed(result.steps):
        if step.node_id != node_id:
            continue
        if step.node_type == "human_approval":
            kind = "human_approval"
        elif step.node_type == "tool":
            kind = "runtime_approval"
        else:
            return None
        if not isinstance(step.input_by_port, dict):
            return None
        return node_id, {
            "kind": kind,
            "node_id": node_id,
            "output": step.output if isinstance(step.output, str) else str(step.output),
            "input_by_port": dict(step.input_by_port),
        }
    return None


def _waiting_event_checkpoint_payload(  # noqa: PLR0911 - gate-specific early exits keep malformed states explicit
    result: WorkflowRunResult,
) -> tuple[str, dict[str, Any]] | None:
    node_id = _blocked_result_node_id(result.error, prefix="waiting_event:")
    if node_id is None:
        return None
    step_output, step_node_type, checkpoint_state = _waiting_event_step_context(
        result,
        node_id,
    )
    if checkpoint_state is None:
        return None
    state_blob = _waiting_event_state_blob(
        node_id=node_id,
        step_node_type=step_node_type,
        step_output=step_output,
        checkpoint_state=checkpoint_state,
    )
    if step_node_type == "wait_for_event":
        expected_event_name = state_blob.get("expected_event_name")
        if not isinstance(expected_event_name, str) or not expected_event_name.strip():
            return None
        return node_id, state_blob
    if step_node_type == "wait_until":
        resume_at = state_blob.get("resume_at")
        if not isinstance(resume_at, str) or not resume_at.strip():
            return None
        return node_id, state_blob
    return None


class WorkflowRunWorker:
    """Claims queued workflow runs and executes them with lease recovery."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: CaliberConfig,
        event_bus: EventBus | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._event_bus = event_bus
        self._interval_seconds = float(config.workflow_run_worker_interval_seconds)
        self._lease_duration = timedelta(seconds=float(config.workflow_run_lease_seconds))
        from caliber.observability.worker_registry import new_worker_id  # noqa: PLC0415

        self._worker_id = new_worker_id("workflow-run-worker", self)
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._s3_client: Any = None  # lazily built for run-artifact persistence

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("WorkflowRunWorker.start() called while already running")
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="caliber.workflow_run_worker")
        logger.info(
            "workflow-run worker started (interval=%.1fs lease=%.1fs worker=%s)",
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
                "workflow-run worker did not stop within %.1fs; cancelling",
                grace_seconds,
            )
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        await asyncio.to_thread(self._deregister_liveness)
        logger.info("workflow-run worker stopped")

    async def _run(self) -> None:
        try:
            while not self._stopped.is_set():
                try:
                    await asyncio.to_thread(self._tick)
                except Exception:
                    logger.exception("workflow-run worker tick raised; continuing")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self._interval_seconds)
        except asyncio.CancelledError:
            raise

    def _tick(self) -> None:
        with bind_trace_id():
            # First, and outside the work below: a heartbeat recorded only when there
            # was something to do would reproduce the defect it closes — an idle
            # worker must prove it is alive. Never raises (see worker_registry).
            self._record_liveness()
            self._recover_expired_leases()
            self._expire_timed_out_wait_for_event_runs()
            self._resume_due_wait_until_runs()
            run_id = self._claim_next_run()
            if run_id is None:
                return
            self._execute_run(run_id)

    def _record_liveness(self) -> None:
        """Write this worker's own heartbeat row for the queue-health signal."""
        from caliber.observability.worker_registry import (  # noqa: PLC0415
            KIND_WORKFLOW_RUN,
            record_heartbeat,
        )

        with self._session_factory() as session:
            record_heartbeat(session, worker_id=self._worker_id, kind=KIND_WORKFLOW_RUN)

    def _deregister_liveness(self) -> None:
        """Drop the heartbeat row on a clean stop, so a planned shutdown is not
        reported as a dead worker. An unclean exit leaves the row to go stale, which
        is the signal we want."""
        from caliber.observability.worker_registry import deregister  # noqa: PLC0415

        with self._session_factory() as session:
            deregister(session, worker_id=self._worker_id)

    def _publish(self, payload: dict[str, Any]) -> None:
        if self._event_bus is None:
            return
        try:
            self._event_bus.publish(payload)
        except Exception:
            logger.warning(
                "failed to publish workflow-run event type=%r",
                payload.get("type"),
                exc_info=True,
            )

    def _append_event(
        self,
        session: Session,
        *,
        workflow_run_id: str,
        project_id: str | None,
        event_type: str,
        payload: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> None:
        append_run_event(
            session,
            workflow_run_id=workflow_run_id,
            project_id=project_id,
            event_type=event_type,
            payload=payload,
            node_id=node_id,
        )

    def _recover_expired_leases(self) -> None:
        now = datetime.now(timezone.utc)
        published: list[dict[str, Any]] = []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(CaliberWorkflowRun)
                    .where(CaliberWorkflowRun.status == RUN_STATUS_RUNNING)
                    .where(CaliberWorkflowRun.lease_expires_at.is_not(None))
                    .where(CaliberWorkflowRun.lease_expires_at < now)
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            ids = [row.workflow_run_id for row in rows]
            session.execute(
                update(CaliberWorkflowRun)
                .where(CaliberWorkflowRun.workflow_run_id.in_(ids))
                .where(CaliberWorkflowRun.status == RUN_STATUS_RUNNING)
                .values(
                    status=RUN_STATUS_QUEUED,
                    queued_at=func.coalesce(CaliberWorkflowRun.queued_at, now),
                    claimed_by=None,
                    claimed_at=None,
                    lease_expires_at=None,
                    last_heartbeat_at=None,
                    current_node_id=None,
                    error_code="lease_recovered",
                    error_summary="worker lease expired; run re-queued for recovery",
                )
            )
            for row in rows:
                self._append_event(
                    session,
                    workflow_run_id=row.workflow_run_id,
                    project_id=row.project_id,
                    event_type="workflow.run.recovered",
                    payload={"reason": "lease_expired", "worker_id": self._worker_id},
                )
                published.append(
                    {
                        "type": "workflow.run.recovered",
                        "workflow_id": row.workflow_id,
                        "workflow_version_id": row.workflow_version_id,
                        "workflow_run_id": row.workflow_run_id,
                        "status": RUN_STATUS_QUEUED,
                        "reason": "lease_expired",
                        "worker_id": self._worker_id,
                    }
                )
            session.commit()
        for payload in published:
            self._publish(payload)

    def _claim_next_run(self) -> str | None:
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            subquery = (
                select(CaliberWorkflowRun.workflow_run_id)
                .where(CaliberWorkflowRun.status == RUN_STATUS_QUEUED)
                .order_by(CaliberWorkflowRun.priority.desc(), CaliberWorkflowRun.queued_at.asc())
                .limit(1)
                .scalar_subquery()
            )
            claimed = session.execute(
                update(CaliberWorkflowRun)
                .where(CaliberWorkflowRun.workflow_run_id == subquery)
                .where(CaliberWorkflowRun.status == RUN_STATUS_QUEUED)
                .values(
                    status=RUN_STATUS_RUNNING,
                    started_at=func.coalesce(CaliberWorkflowRun.started_at, now),
                    claimed_by=self._worker_id,
                    claimed_at=now,
                    last_heartbeat_at=now,
                    lease_expires_at=now + self._lease_duration,
                )
                .returning(CaliberWorkflowRun.workflow_run_id)
            ).scalar_one_or_none()
            session.commit()
            return claimed

    def _approved_nodes(self, session: Session, run_id: str) -> set[str]:
        rows = (
            session.execute(
                select(CaliberRuntimeApprovalRequest.node_id)
                .where(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
                .where(CaliberRuntimeApprovalRequest.status == "approved")
            )
            .scalars()
            .all()
        )
        return {row for row in rows if row}

    def _resume_checkpoint_row(
        self,
        session: Session,
        run: CaliberWorkflowRun,
    ) -> CaliberWorkflowRunCheckpoint | None:
        summary = run.summary if isinstance(run.summary, dict) else {}
        checkpoint_id = summary.get("resume_checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            return None
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        if checkpoint is None or checkpoint.workflow_run_id != run.workflow_run_id:
            return None
        return checkpoint

    def _resume_checkpoint(  # noqa: PLR0911, PLR0912 - fail-closed checkpoint validation is intentionally branchy
        self,
        session: Session,
        run: CaliberWorkflowRun,
    ) -> RuntimeResumeCheckpoint | None:
        summary = run.summary if isinstance(run.summary, dict) else None
        if not isinstance(summary, dict):
            return None
        checkpoint_id = summary.get("resume_checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            return None
        source_run_id = summary.get("resume_checkpoint_run_id")
        if not isinstance(source_run_id, str) or not source_run_id:
            source_run_id = run.workflow_run_id
        row = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        if (
            row is None
            or row.workflow_run_id != source_run_id
            or not self._checkpoint_source_in_lineage(session, run, source_run_id)
            or not isinstance(row.state_blob, dict)
        ):
            return None
        kind = row.state_blob.get("kind")
        node_id = row.state_blob.get("node_id")
        row_node_id = row.node_id
        output = row.state_blob.get("output", "")
        output_by_port = row.state_blob.get("output_by_port")
        input_by_port = row.state_blob.get("input_by_port")
        injected_inputs = row.state_blob.get("resume_event_inputs")
        if not isinstance(node_id, str) or not node_id:
            return None
        if not isinstance(row_node_id, str) or not row_node_id or row_node_id != node_id:
            return None
        if not isinstance(output, str):
            output = str(output)
        if not isinstance(output_by_port, dict):
            output_by_port = None
        if not isinstance(input_by_port, dict):
            input_by_port = None
        if not isinstance(injected_inputs, dict):
            injected_inputs = None
        expected_event_name = (
            row.state_blob.get("expected_event_name")
            if isinstance(row.state_blob.get("expected_event_name"), str)
            else None
        )
        if kind == "wait_for_event":
            if expected_event_name is None or not expected_event_name.strip():
                return None
            injected_event_name = (
                injected_inputs.get("event_name")
                if isinstance(injected_inputs, dict)
                and isinstance(injected_inputs.get("event_name"), str)
                else None
            )
            if (
                isinstance(injected_event_name, str)
                and injected_event_name.strip()
                and injected_event_name != expected_event_name
            ):
                return None
            has_resume_payload = False
            if injected_inputs is not None:
                for key in ("resume_event", "event_payload", "event"):
                    if injected_inputs.get(key) not in (None, "", {}):
                        has_resume_payload = True
                        break
                if (
                    not has_resume_payload
                    and expected_event_name
                    and injected_inputs.get(expected_event_name) not in (None, "", {})
                ):
                    has_resume_payload = True
            if not has_resume_payload:
                return None
        if (
            kind in {"wait_for_event", "wait_until", "runtime_approval", "human_approval"}
            and input_by_port is None
        ):
            return None
        replay_output = not (
            kind in {"wait_for_event", "wait_until", "runtime_approval", "human_approval"}
            and input_by_port is not None
        )
        return RuntimeResumeCheckpoint(
            node_id=node_id,
            checkpoint_kind=kind if isinstance(kind, str) else None,
            output=output,
            output_by_port=output_by_port,
            input_by_port=input_by_port,
            injected_inputs=injected_inputs,
            replay_output=replay_output,
        )

    def _checkpoint_source_in_lineage(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        source_run_id: str,
    ) -> bool:
        current: CaliberWorkflowRun | None = run
        visited: set[str] = set()
        while current is not None and current.workflow_run_id not in visited:
            if current.workflow_run_id == source_run_id:
                return True
            visited.add(current.workflow_run_id)
            current = (
                session.get(CaliberWorkflowRun, current.parent_run_id)
                if current.parent_run_id
                else None
            )
        return False

    def _resume_due_wait_until_runs(self) -> None:
        now = datetime.now(timezone.utc)
        published: list[dict[str, Any]] = []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(CaliberWorkflowRun)
                    .where(CaliberWorkflowRun.status == RUN_STATUS_WAITING_EVENT)
                    .order_by(CaliberWorkflowRun.queued_at.asc())
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            for run in rows:
                checkpoint = self._maintenance_waiting_event_checkpoint(
                    session,
                    run,
                    published=published,
                )
                if checkpoint is None:
                    continue
                state_blob_raw = checkpoint.state_blob
                if not isinstance(state_blob_raw, dict):
                    continue
                state_blob: dict[str, Any] = state_blob_raw
                if state_blob.get("kind") != "wait_until":
                    continue
                resume_at = _parse_checkpoint_resume_at(state_blob.get("resume_at"))
                if resume_at is None:
                    self._fail_waiting_event_run_from_maintenance(
                        session,
                        run,
                        checkpoint=checkpoint,
                        published=published,
                        error_code="resume_checkpoint_unavailable",
                        error_summary=(
                            f"wait_until checkpoint {checkpoint.checkpoint_id!r} for node "
                            f"{checkpoint.node_id!r} is missing or has invalid resume_at"
                        ),
                        reason="invalid_wait_until_checkpoint",
                    )
                    continue
                if resume_at > now:
                    continue
                input_by_port = state_blob.get("input_by_port")
                if not isinstance(input_by_port, dict):
                    self._fail_waiting_event_run_from_maintenance(
                        session,
                        run,
                        checkpoint=checkpoint,
                        published=published,
                        error_code="resume_checkpoint_unavailable",
                        error_summary=(
                            f"wait_until checkpoint {checkpoint.checkpoint_id!r} for node "
                            f"{checkpoint.node_id!r} is missing its input snapshot"
                        ),
                        reason="invalid_wait_until_checkpoint",
                    )
                    continue
                assert_run_transition(run.status, RUN_STATUS_QUEUED)
                run.status = RUN_STATUS_QUEUED
                run.queued_at = run.queued_at or now
                run.claimed_by = None
                run.claimed_at = None
                run.lease_expires_at = None
                run.last_heartbeat_at = now
                run.current_node_id = None
                run.error_code = None
                run.error_summary = None
                summary = dict(run.summary or {})
                summary["status"] = RUN_STATUS_QUEUED
                run.summary = summary
                self._append_event(
                    session,
                    workflow_run_id=run.workflow_run_id,
                    project_id=run.project_id,
                    event_type="workflow.run.resumed",
                    payload={
                        "actor": "workflow_run_worker",
                        "auto": True,
                        "reason": "wait_until_due",
                        "resume_at": state_blob.get("resume_at"),
                    },
                    node_id=checkpoint.node_id,
                )
                audit_record(
                    session,
                    actor="workflow_run_worker",
                    action="auto_resume_workflow_run",
                    entity_type="workflow_run",
                    entity_id=run.workflow_run_id,
                    details={
                        "from_status": RUN_STATUS_WAITING_EVENT,
                        "reason": "wait_until_due",
                        "node_id": checkpoint.node_id,
                        "resume_at": state_blob.get("resume_at"),
                    },
                )
                published.append(
                    {
                        "type": "workflow.run.resumed",
                        "workflow_id": run.workflow_id,
                        "workflow_version_id": run.workflow_version_id,
                        "workflow_run_id": run.workflow_run_id,
                        "status": run.status,
                    }
                )
            if not published:
                return
            session.commit()
        for payload in published:
            self._publish(payload)

    def _expire_timed_out_wait_for_event_runs(  # noqa: PLR0912, PLR0915 - maintenance fail-closed branches are intentionally explicit
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        published: list[dict[str, Any]] = []
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(CaliberWorkflowRun)
                    .where(CaliberWorkflowRun.status == RUN_STATUS_WAITING_EVENT)
                    .order_by(CaliberWorkflowRun.queued_at.asc())
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            for run in rows:
                checkpoint = self._maintenance_waiting_event_checkpoint(
                    session,
                    run,
                    published=published,
                )
                if checkpoint is None:
                    continue
                state_blob_raw = checkpoint.state_blob
                if not isinstance(state_blob_raw, dict):
                    continue
                state_blob: dict[str, Any] = state_blob_raw
                if state_blob.get("kind") != "wait_for_event":
                    continue
                raw_timeout_seconds = state_blob.get("timeout_seconds")
                timeout_seconds = _parse_positive_seconds(state_blob.get("timeout_seconds"))
                if timeout_seconds is None and raw_timeout_seconds not in (None, ""):
                    self._fail_waiting_event_run_from_maintenance(
                        session,
                        run,
                        checkpoint=checkpoint,
                        published=published,
                        error_code="resume_checkpoint_unavailable",
                        error_summary=(
                            f"wait_for_event checkpoint {checkpoint.checkpoint_id!r} for node "
                            f"{checkpoint.node_id!r} has invalid timeout_seconds"
                        ),
                        reason="invalid_wait_for_event_checkpoint",
                    )
                    continue
                if timeout_seconds is None:
                    continue
                checkpoint_created = checkpoint.created_at
                if checkpoint_created.tzinfo is None:
                    checkpoint_created = checkpoint_created.replace(tzinfo=timezone.utc)
                deadline = checkpoint_created.astimezone(timezone.utc) + timedelta(
                    seconds=timeout_seconds
                )
                if deadline > now:
                    continue
                assert_run_transition(run.status, RUN_STATUS_EXPIRED)
                run.status = RUN_STATUS_EXPIRED
                run.completed_at = now
                run.claimed_by = None
                run.claimed_at = None
                run.lease_expires_at = None
                run.last_heartbeat_at = now
                run.current_node_id = None
                run.error_code = "wait_for_event_timeout"
                expected_event_name = state_blob.get("expected_event_name")
                if isinstance(expected_event_name, str) and expected_event_name.strip():
                    run.error_summary = (
                        f"wait_for_event node {checkpoint.node_id!r} timed out after "
                        f"{timeout_seconds:g}s waiting for {expected_event_name!r}"
                    )
                else:
                    run.error_summary = (
                        f"wait_for_event node {checkpoint.node_id!r} timed out after "
                        f"{timeout_seconds:g}s"
                    )
                summary = dict(run.summary or {})
                summary["status"] = RUN_STATUS_EXPIRED
                summary["error"] = run.error_summary
                summary["timeout_checkpoint_id"] = checkpoint.checkpoint_id
                summary["timeout_deadline"] = deadline.isoformat()
                run.summary = summary
                self._append_event(
                    session,
                    workflow_run_id=run.workflow_run_id,
                    project_id=run.project_id,
                    event_type="workflow.run.expired",
                    payload={
                        "status": run.status,
                        "error": run.error_summary,
                        "reason": "wait_for_event_timeout",
                        "node_id": checkpoint.node_id,
                        "timeout_seconds": timeout_seconds,
                        "deadline": deadline.isoformat(),
                        "event_name": expected_event_name,
                    },
                    node_id=checkpoint.node_id,
                )
                audit_record(
                    session,
                    actor="workflow_run_worker",
                    action="timeout_workflow_run_wait_for_event",
                    entity_type="workflow_run",
                    entity_id=run.workflow_run_id,
                    details={
                        "from_status": RUN_STATUS_WAITING_EVENT,
                        "node_id": checkpoint.node_id,
                        "timeout_seconds": timeout_seconds,
                        "deadline": deadline.isoformat(),
                        "event_name": expected_event_name,
                    },
                )
                published.append(
                    {
                        "type": "workflow.run.expired",
                        "workflow_id": run.workflow_id,
                        "workflow_version_id": run.workflow_version_id,
                        "workflow_run_id": run.workflow_run_id,
                        "status": run.status,
                        "error": run.error_summary,
                    }
                )
            if not published:
                return
            session.commit()
        for payload in published:
            self._publish(payload)

    def _maintenance_waiting_event_checkpoint(  # noqa: PLR0911 - maintenance fail-closed branches are intentionally explicit
        self,
        session: Session,
        run: CaliberWorkflowRun,
        *,
        published: list[dict[str, Any]],
    ) -> CaliberWorkflowRunCheckpoint | None:
        summary = run.summary if isinstance(run.summary, dict) else None
        if not isinstance(summary, dict):
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_node_id=run.current_node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary="waiting-event run is missing resume checkpoint reference",
                reason="missing_waiting_checkpoint",
            )
            return None
        checkpoint_id = summary.get("resume_checkpoint_id")
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_node_id=run.current_node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary="waiting-event run is missing resume checkpoint reference",
                reason="missing_waiting_checkpoint",
            )
            return None
        checkpoint = session.get(CaliberWorkflowRunCheckpoint, checkpoint_id)
        if checkpoint is None:
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id=checkpoint_id,
                checkpoint_node_id=run.current_node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary=(
                    f"waiting-event run references missing checkpoint {checkpoint_id!r}"
                ),
                reason="missing_waiting_checkpoint",
            )
            return None
        if checkpoint.workflow_run_id != run.workflow_run_id:
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_node_id=checkpoint.node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary=(
                    f"waiting-event run references foreign checkpoint {checkpoint.checkpoint_id!r}"
                ),
                reason="foreign_waiting_checkpoint",
            )
            return None
        if not isinstance(checkpoint.state_blob, dict):
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_node_id=checkpoint.node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary=(
                    f"waiting-event checkpoint {checkpoint.checkpoint_id!r} has corrupt state"
                ),
                reason="corrupt_waiting_checkpoint",
            )
            return None
        kind = checkpoint.state_blob.get("kind")
        if kind not in {"wait_event", "wait_for_event", "wait_until"}:
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_node_id=checkpoint.node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary=(
                    f"waiting-event checkpoint {checkpoint.checkpoint_id!r} has invalid kind {kind!r}"
                ),
                reason="invalid_waiting_checkpoint",
            )
            return None
        node_id = checkpoint.state_blob.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_node_id=checkpoint.node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary=(
                    f"waiting-event checkpoint {checkpoint.checkpoint_id!r} is missing node_id"
                ),
                reason="invalid_waiting_checkpoint",
            )
            return None
        current_node_id = run.current_node_id
        checkpoint_node_id = checkpoint.node_id
        if (
            not isinstance(current_node_id, str)
            or not current_node_id
            or not isinstance(checkpoint_node_id, str)
            or not checkpoint_node_id
            or checkpoint_node_id != current_node_id
            or node_id != current_node_id
            or checkpoint_node_id != node_id
        ):
            self._fail_waiting_event_run_from_maintenance(
                session,
                run,
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_node_id=checkpoint.node_id,
                published=published,
                error_code="resume_checkpoint_unavailable",
                error_summary=(
                    f"waiting-event checkpoint {checkpoint.checkpoint_id!r} does not match "
                    f"waiting run node {run.current_node_id!r}"
                ),
                reason="invalid_waiting_checkpoint",
            )
            return None
        return checkpoint

    def _fail_waiting_event_run_from_maintenance(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        *,
        checkpoint: CaliberWorkflowRunCheckpoint | None = None,
        checkpoint_id: str | None = None,
        checkpoint_node_id: str | None = None,
        published: list[dict[str, Any]],
        error_code: str,
        error_summary: str,
        reason: str,
    ) -> None:
        assert_run_transition(run.status, RUN_STATUS_FAILED)
        now = datetime.now(timezone.utc)
        run.status = RUN_STATUS_FAILED
        run.completed_at = now
        run.claimed_by = None
        run.claimed_at = None
        run.lease_expires_at = None
        run.last_heartbeat_at = now
        run.current_node_id = None
        run.error_code = error_code
        run.error_summary = error_summary
        effective_checkpoint_id = (
            checkpoint.checkpoint_id if checkpoint is not None else checkpoint_id
        )
        effective_node_id = checkpoint.node_id if checkpoint is not None else checkpoint_node_id
        summary = dict(run.summary or {})
        summary["status"] = RUN_STATUS_FAILED
        summary["error"] = error_summary
        if isinstance(effective_checkpoint_id, str) and effective_checkpoint_id:
            summary["failed_checkpoint_id"] = effective_checkpoint_id
        run.summary = summary
        self._append_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.failed",
            payload={
                "status": run.status,
                "error": error_summary,
                "reason": reason,
                "checkpoint_id": effective_checkpoint_id,
                "node_id": effective_node_id,
            },
            node_id=effective_node_id,
        )
        audit_record(
            session,
            actor="workflow_run_worker",
            action="fail_workflow_run_waiting_checkpoint",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={
                "from_status": RUN_STATUS_WAITING_EVENT,
                "reason": reason,
                "checkpoint_id": effective_checkpoint_id,
                "node_id": effective_node_id,
                "error": error_summary,
            },
        )
        published.append(
            {
                "type": "workflow.run.failed",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
                "error": error_summary,
            }
        )

    def _cancel_requested(self, session: Session, run_id: str) -> bool:
        row = session.get(CaliberWorkflowRun, run_id)
        return bool(row and row.cancel_requested_at is not None)

    def _renew_lease(self, run_id: str) -> int:
        """Extend the lease on a run we still hold; return rows updated.

        Uses its own session (safe to call from the heartbeat thread) and only
        touches the lease columns. The ``claimed_by`` + ``status`` guards make it
        a no-op (0 rows) once another worker has reclaimed the run or it left the
        running state, so a stale heartbeat can never resurrect a finished run.
        """
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            result = session.execute(
                update(CaliberWorkflowRun)
                .where(CaliberWorkflowRun.workflow_run_id == run_id)
                .where(CaliberWorkflowRun.claimed_by == self._worker_id)
                .where(CaliberWorkflowRun.status == RUN_STATUS_RUNNING)
                .values(last_heartbeat_at=now, lease_expires_at=now + self._lease_duration)
            )
            session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    def _heartbeat_loop(self, run_id: str, stop_event: threading.Event) -> None:
        """Renew the lease on a fixed cadence while a run executes.

        ``_on_step`` only renews at node boundaries, so a single long-running node
        (e.g. a large ForEach fan-out over a whole document) could let the lease
        expire mid-node and trip ``_recover_expired_leases`` into re-queuing the
        run — a duplicate execution. This independent thread keeps the lease alive
        regardless of node granularity, at one third of the lease duration so two
        renewals fit comfortably inside every lease window.
        """
        interval = max(5.0, self._lease_duration.total_seconds() / 3.0)
        while not stop_event.wait(interval):
            try:
                self._renew_lease(run_id)
            except Exception:
                logger.debug("workflow-run heartbeat failed for %s", run_id, exc_info=True)

    def _object_store_client(self) -> Any:
        """Lazily build a boto3 S3 client from the object-store config (same
        connection settings the Object Store UI uses)."""
        if self._s3_client is None:
            import boto3  # noqa: PLC0415
            from botocore.config import Config  # noqa: PLC0415

            cfg = self._config
            access_key = (
                resolve_secret(cfg.object_store_access_key_source)
                if cfg.object_store_access_key_source
                else None
            )
            secret_key = (
                resolve_secret(cfg.object_store_secret_key_source)
                if cfg.object_store_secret_key_source
                else None
            )
            self._s3_client = boto3.client(
                "s3",
                endpoint_url=cfg.object_store_endpoint_url or None,
                region_name=cfg.object_store_region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=Config(
                    s3={"addressing_style": "path" if cfg.object_store_force_path_style else "auto"}
                ),
            )
        return self._s3_client

    def _artifact_objects(
        self, run: CaliberWorkflowRun, result: WorkflowRunResult
    ) -> list[tuple[str, bytes, str]]:
        """(key, body, content_type) for a completed run's file artifacts plus a
        per-run JSON log. Pure (no IO) so it is unit-testable; empty list when
        artifact persistence is disabled (no configured bucket)."""
        if not self._config.workflow_run_artifact_bucket:
            return []
        rid = run.workflow_run_id
        ap = self._config.workflow_run_artifact_prefix.strip("/")
        lp = self._config.workflow_run_log_prefix.strip("/")
        out: list[tuple[str, bytes, str]] = []
        for name, content in (result.artifacts or {}).items():
            key = f"{ap}/{rid}/{name}" if ap else f"{rid}/{name}"
            body = (
                content.encode("utf-8")
                if isinstance(content, str)
                else json.dumps(content, default=str).encode("utf-8")
            )
            ctype = (
                "text/html"
                if name.endswith(".html")
                else "application/json"
                if name.endswith(".json")
                else "text/plain"
            )
            out.append((key, body, ctype))
        log = {
            "workflow_run_id": rid,
            "workflow_id": run.workflow_id,
            "workflow_version_id": run.workflow_version_id,
            "status": result.status,
            "mlflow_run_id": result.mlflow_run_id,
            "tokens": result.tokens,
            "error": result.error,
            "node_path": [s.node_id for s in result.steps],
            "steps": [_step_to_dict(s) for s in result.steps],
            "artifacts": sorted((result.artifacts or {}).keys()),
        }
        log_key = f"{lp}/{rid}.json" if lp else f"{rid}.json"
        out.append(
            (log_key, json.dumps(log, indent=2, default=str).encode("utf-8"), "application/json")
        )
        return out

    def _record_artifact_persistence_summary(
        self,
        run_id: str,
        *,
        status: str,
        bucket: str,
        object_count: int,
        artifact_names: list[str],
        error: str | None = None,
        persisted_object_count: int | None = None,
        recent_persisted_keys: list[str] | None = None,
        failed_object_key: str | None = None,
    ) -> None:
        try:
            with self._session_factory() as session:
                run = session.get(CaliberWorkflowRun, run_id)
                if run is None:
                    return
                summary = dict(run.summary or {})
                artifact_persistence = {
                    "status": status,
                    "bucket": bucket,
                    "object_count": object_count,
                    "artifact_names": list(artifact_names),
                }
                if isinstance(error, str) and error:
                    artifact_persistence["error"] = error
                if isinstance(persisted_object_count, int) and persisted_object_count >= 0:
                    artifact_persistence["persisted_object_count"] = persisted_object_count
                if isinstance(recent_persisted_keys, list):
                    artifact_persistence["recent_persisted_keys"] = [
                        str(item).strip() for item in recent_persisted_keys if str(item).strip()
                    ]
                if isinstance(failed_object_key, str) and failed_object_key.strip():
                    artifact_persistence["failed_object_key"] = failed_object_key
                summary["artifact_persistence"] = artifact_persistence
                run.summary = summary
                session.commit()
        except Exception:
            logger.warning(
                "failed to persist artifact-persistence summary for %s",
                run_id,
                exc_info=True,
            )

    def _persist_run_artifacts(self, run: CaliberWorkflowRun, result: WorkflowRunResult) -> None:
        """Best-effort: write a completed run's artifacts + JSON log to the
        configured object-store bucket. Failures are logged, never fatal."""
        if not self._config.workflow_run_artifact_bucket:
            return
        bucket = self._config.workflow_run_artifact_bucket
        artifact_names = sorted((result.artifacts or {}).keys())
        objs: list[tuple[str, bytes, str]] = []
        persisted_keys: list[str] = []
        failed_key: str | None = None
        try:
            objs = self._artifact_objects(run, result)
            if not objs:
                return
            client = self._object_store_client()
            for key, body, ctype in objs:
                failed_key = key
                client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype)
                persisted_keys.append(key)
                failed_key = None
            logger.info(
                "persisted %d artifact object(s) to s3://%s for run %s",
                len(objs),
                bucket,
                run.workflow_run_id,
            )
            self._record_artifact_persistence_summary(
                run.workflow_run_id,
                status="persisted",
                bucket=bucket,
                object_count=len(objs),
                artifact_names=artifact_names,
                persisted_object_count=len(persisted_keys),
                recent_persisted_keys=persisted_keys[-3:],
            )
        except Exception as exc:
            progress_suffix = (
                f" after storing {len(persisted_keys)} of {len(objs)} object(s)"
                if persisted_keys
                else f" before storing any of the {len(objs)} planned object(s)"
                if objs
                else ""
            )
            failing_key_suffix = f" while uploading {failed_key}" if failed_key else ""
            error_summary = f"{type(exc).__name__}: {exc}{failing_key_suffix}{progress_suffix}"
            logger_context = (
                f" after storing {len(persisted_keys)} of {len(objs)} object(s)"
                if persisted_keys
                else f" before storing any of the {len(objs)} planned object(s)"
                if objs
                else ""
            )
            if persisted_keys:
                preview = ", ".join(persisted_keys[-3:])
                logger_context = f"{logger_context} ({preview})"
            if failed_key:
                logger_context = f"{logger_context} while uploading {failed_key}"
            logger.warning(
                "failed to persist run artifacts for %s%s",
                run.workflow_run_id,
                logger_context,
                exc_info=True,
            )
            self._record_artifact_persistence_summary(
                run.workflow_run_id,
                status="failed",
                bucket=bucket,
                object_count=len(objs),
                artifact_names=artifact_names,
                error=error_summary,
                persisted_object_count=len(persisted_keys),
                recent_persisted_keys=persisted_keys[-3:],
                failed_object_key=failed_key,
            )

    def _execute_run(self, run_id: str) -> None:  # noqa: PLR0911, PLR0912, PLR0915 - run orchestration dispatch
        with self._session_factory() as session:
            run = session.get(CaliberWorkflowRun, run_id)
            if run is None or run.status != RUN_STATUS_RUNNING:
                return
            workflow = session.get(CaliberWorkflow, run.workflow_id)
            version_row = session.get(CaliberWorkflowVersion, run.workflow_version_id)
            manifest_snapshot = (
                json.loads(json.dumps(run.manifest_snapshot))
                if isinstance(run.manifest_snapshot, dict)
                else None
            )
            if workflow is None:
                self._mark_failed(
                    session,
                    run,
                    error_code="missing_dependencies",
                    error_summary="workflow not found",
                )
                return
            version_for_plan: Any = version_row
            if version_row is None:
                if manifest_snapshot is None:
                    self._mark_failed(
                        session,
                        run,
                        error_code="missing_dependencies",
                        error_summary="workflow version not found",
                    )
                    return
                summary = dict(run.summary or {})
                raw_version_number = summary.get("workflow_version_number")
                version_number = raw_version_number if isinstance(raw_version_number, int) else 0
                version_for_plan = SimpleNamespace(
                    version_id=run.workflow_version_id or "",
                    workflow_id=run.workflow_id,
                    version_number=version_number,
                    manifest=manifest_snapshot,
                    created_by=workflow.owner or "",
                )

            # Execute over the full input (input_payload); fall back to the summary
            # preview for legacy rows. Keep the summary's copy bounded so polling
            # responses don't carry a whole-document payload.
            input_text = run.input_payload or _input_from_summary(run.summary)
            if not input_text:
                input_text = ""
            run_summary = dict(run.summary or {})
            run_summary["input"] = input_text[:1000]
            run.summary = run_summary
            exact_manifest = manifest_snapshot or getattr(version_for_plan, "manifest", {})
            mcp_blockers = deployment_blockers(
                session,
                exact_manifest,
                alias=run.deployment_alias or "manual",
            )
            if mcp_blockers:
                self._mark_failed(
                    session,
                    run,
                    error_code="mcp_policy_blocked",
                    error_summary="MCP runtime preflight failed: " + "; ".join(mcp_blockers),
                )
                return
            requested_resume_checkpoint = run_summary.get("resume_checkpoint_id")
            resume_checkpoint = self._resume_checkpoint(session, run)
            if (
                isinstance(requested_resume_checkpoint, str)
                and requested_resume_checkpoint
                and resume_checkpoint is None
            ):
                source_run_id = run_summary.get("resume_checkpoint_run_id")
                if not isinstance(source_run_id, str) or not source_run_id:
                    source_run_id = run.workflow_run_id
                self._mark_failed(
                    session,
                    run,
                    error_code="resume_checkpoint_unavailable",
                    error_summary=(
                        "resume checkpoint "
                        f"{requested_resume_checkpoint!r} from run {source_run_id!r} "
                        "is missing, corrupt, or outside this run's lineage"
                    ),
                )
                return
            try:
                plan = build_plan(
                    session,
                    version_for_plan,
                    alias=run.deployment_alias,
                    manifest_override=manifest_snapshot,
                    config=self._config,
                    session_factory=self._session_factory,
                )
            except (WorkflowManifestError, ValidationError) as exc:
                self._mark_failed(
                    session,
                    run,
                    error_code="runtime_error",
                    error_summary=_manifest_parse_error_summary(exc),
                )
                return
            except CompileError as exc:
                self._mark_failed(
                    session,
                    run,
                    error_code="runtime_error",
                    error_summary=_compile_error_summary(exc),
                )
                return
            # At-most-once external effects. This is the only execution path that
            # can be *restarted*: an expired lease resets the run to ``queued`` and,
            # without a wait/approval checkpoint, execution begins again from the
            # start — so every effectful node would re-fire without this ledger.
            # The key is keyed on the run id, which the restart preserves.
            plan.effect_ledger = SqlEffectLedger(
                self._session_factory, workflow_run_id=run.workflow_run_id
            )
            preflight_error = _resume_checkpoint_preflight_error(plan, resume_checkpoint)
            if preflight_error is not None:
                source_run_id = run_summary.get("resume_checkpoint_run_id")
                if not isinstance(source_run_id, str) or not source_run_id:
                    source_run_id = run.workflow_run_id
                self._mark_failed(
                    session,
                    run,
                    error_code="resume_checkpoint_unavailable",
                    error_summary=(
                        "resume checkpoint "
                        f"{requested_resume_checkpoint!r} from run {source_run_id!r} "
                        f"{preflight_error}"
                    ),
                )
                return

            managed_snapshots = [
                file_ref
                for node in plan.ir.nodes.values()
                if (file_ref := getattr(node, "file_ref", None)) is not None
            ]
            raw_input_files = run_summary.get("input_files")
            if raw_input_files is not None and (
                not isinstance(raw_input_files, list)
                or any(not isinstance(item, dict) for item in raw_input_files)
            ):
                self._mark_failed(
                    session,
                    run,
                    error_code="managed_file_preflight",
                    error_summary="queued input_files snapshot is malformed",
                )
                return
            requested_input_files = raw_input_files if isinstance(raw_input_files, list) else []
            managed_file_resolver = None
            runtime_file_tools: dict[str, Any] = {}
            if managed_snapshots or requested_input_files:
                if managed_snapshots and not run.project_id:
                    self._mark_failed(
                        session,
                        run,
                        error_code="managed_file_preflight",
                        error_summary=(
                            "managed file inputs require the workflow to belong to a project"
                        ),
                    )
                    return
                project = session.get(CaliberProject, run.project_id) if run.project_id else None
                if run.project_id and project is None:
                    self._mark_failed(
                        session,
                        run,
                        error_code="managed_file_preflight",
                        error_summary=f"workflow project {run.project_id!r} no longer exists",
                    )
                    return
                try:
                    wd_service = WorkingDirectoryService(
                        build_backend(self._config.workflow_storage),
                        self._config.workflow_storage,
                    )
                    if project is not None:
                        wd_service = wd_service.for_backend(project.storage_backend)
                    wd_ctx = wd_service.create_run_workspace(
                        tenant_id=run.tenant_id or "local",
                        project_id=run.project_id or "default",
                        workflow_id=run.workflow_id,
                        workflow_run_id=run.workflow_run_id,
                    )
                    if requested_input_files and not run_summary.get("input_files_bound"):
                        bound_files = wd_service.materialize_input_files(
                            session,
                            wd_ctx,
                            requested_input_files,
                            actor="@workflow-run-worker",
                        )
                        run_summary["input_files_bound"] = True
                        run_summary["bound_input_file_refs"] = [
                            item.file_ref for item in bound_files
                        ]
                        run.summary = run_summary
                        session.commit()
                    runtime_file_tools = bind_run_read_tools(wd_service, session, wd_ctx)
                    managed_file_resolver, managed_tools = bind_managed_file_runtime(
                        wd_service,
                        session,
                        wd_ctx,
                        managed_snapshots,
                        extract_document_aliases=managed_file_tool_aliases(plan.ir),
                    )
                    if managed_file_resolver is not None:
                        # Fail before emitting workflow.run.started when a pinned
                        # row/object is missing, cross-project, or hash-mismatched.
                        managed_file_resolver.verify_all()
                    runtime_file_tools.update(managed_tools)
                except (StorageError, ValueError, RuntimeError) as exc:
                    self._mark_failed(
                        session,
                        run,
                        error_code="managed_file_preflight",
                        error_summary=f"managed file preflight failed: {exc}",
                    )
                    return
            try:
                executor = build_executor(self._config, ir=plan.ir)
                approved_nodes = self._approved_nodes(session, run.workflow_run_id)
            except Exception as exc:
                self._mark_failed(
                    session,
                    run,
                    error_code="runtime_error",
                    error_summary=_runtime_startup_error_summary(exc),
                )
                return
            self._append_event(
                session,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                event_type="workflow.run.started",
                payload={
                    "workflow_id": run.workflow_id,
                    "workflow_version_id": run.workflow_version_id,
                    "alias": run.deployment_alias,
                    "worker_id": self._worker_id,
                },
            )
            session.commit()
            self._publish(
                {
                    "type": "workflow.run.started",
                    "workflow_id": run.workflow_id,
                    "workflow_version_id": run.workflow_version_id,
                    "workflow_run_id": run.workflow_run_id,
                    "status": RUN_STATUS_RUNNING,
                    "alias": run.deployment_alias,
                }
            )
            steps: list[dict[str, Any]] = []

            def _on_node_start(node_id: str, node: Any, _inputs: dict[str, Any]) -> None:
                row = session.get(CaliberWorkflowRun, run.workflow_run_id)
                if row is None:
                    return
                now = datetime.now(timezone.utc)
                payload = _node_started_to_dict(node_id, node.node_type)
                row.status = RUN_STATUS_RUNNING
                row.current_node_id = node_id
                row.last_heartbeat_at = now
                row.lease_expires_at = now + self._lease_duration
                self._append_event(
                    session,
                    workflow_run_id=row.workflow_run_id,
                    project_id=row.project_id,
                    event_type="workflow.run.node_started",
                    payload=payload,
                    node_id=node_id,
                )
                session.commit()
                self._publish(
                    {
                        "type": "workflow.run.node_started",
                        "workflow_id": row.workflow_id,
                        "workflow_version_id": row.workflow_version_id,
                        "workflow_run_id": row.workflow_run_id,
                        "status": RUN_STATUS_RUNNING,
                        **payload,
                    }
                )

            def _on_step(step: NodeStep) -> None:
                payload = _step_to_dict(step)
                steps.append(payload)
                row = session.get(CaliberWorkflowRun, run.workflow_run_id)
                if row is None:
                    return
                now = datetime.now(timezone.utc)
                row.current_node_id = step.node_id
                row.last_heartbeat_at = now
                row.lease_expires_at = now + self._lease_duration
                summary = dict(row.summary or {})
                summary["node_path"] = [item["node_id"] for item in steps]
                summary["steps"] = steps
                row.summary = summary
                self._append_event(
                    session,
                    workflow_run_id=row.workflow_run_id,
                    project_id=row.project_id,
                    event_type="workflow.run.step",
                    payload={"step": payload},
                    node_id=step.node_id,
                )
                session.commit()
                self._publish(
                    {
                        "type": "workflow.run.step",
                        "workflow_id": row.workflow_id,
                        "workflow_version_id": row.workflow_version_id,
                        "workflow_run_id": row.workflow_run_id,
                        "step": payload,
                    }
                )
                if self._cancel_requested(session, row.workflow_run_id):
                    raise RuntimeError(_CANCEL_SENTINEL)

            # Independent lease heartbeat: keeps the lease alive across long nodes
            # (e.g. a whole-document ForEach) that would otherwise renew it only at
            # node boundaries. Stopped + joined in finally so it never outlives the
            # run or leaks a thread.
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(run.workflow_run_id, heartbeat_stop),
                name=f"caliber-wf-heartbeat-{run.workflow_run_id}",
                daemon=True,
            )
            heartbeat.start()
            result: WorkflowRunResult | None = None
            execution_exc: Exception | None = None
            managed_execute_kwargs: dict[str, Any] = {}
            if managed_file_resolver is not None:
                managed_execute_kwargs["managed_file_resolver"] = managed_file_resolver.read_text
            try:
                result = execute(
                    plan,
                    input_text,
                    executor=executor,
                    session_id=run.session_id,
                    preview=False,
                    on_step=_on_step,
                    on_node_start=_on_node_start,
                    runtime_approvals_enabled=bool(
                        self._config.workflow_run_runtime_approvals_enabled
                    ),
                    approved_human_approval_nodes=approved_nodes,
                    resume_checkpoint=resume_checkpoint,
                    # Agent long-term memory (mem0), scoped to the workflow so it
                    # persists across runs. Empty dict when memory is disabled.
                    extra_tools={
                        **runtime_file_tools,
                        **bind_run_memory_tools(self._config, agent_id=run.workflow_id),
                    },
                    **managed_execute_kwargs,
                )
            except Exception as exc:
                execution_exc = exc
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=5.0)
            row = session.get(CaliberWorkflowRun, run.workflow_run_id)
            if row is None:
                return
            if execution_exc is not None:
                self._mark_failed(
                    session,
                    row,
                    error_code="runtime_error",
                    error_summary=_runtime_execution_error_summary(execution_exc),
                )
                return
            assert result is not None
            # Link the run to its MLflow run for the trace UI (Wave 1). Set before
            # the terminal-state branches so every outcome persists it.
            if result.mlflow_run_id:
                row.mlflow_run_id = result.mlflow_run_id
            # ``GET /workflow-runs/{id}/trace`` resolves the span tree from
            # ``trace_id``, not ``mlflow_run_id``. Persisting only the latter
            # left the integrated trace panel empty even when the runtime had
            # produced a real trace.
            if result.mlflow_trace_id:
                row.trace_id = result.mlflow_trace_id
            if self._is_waiting_approval(result):
                approval_checkpoint = _waiting_approval_checkpoint_payload(result)
                if approval_checkpoint is None:
                    self._mark_failed(
                        session,
                        row,
                        result=result,
                        error_code="runtime_error",
                        error_summary=(
                            "workflow runtime returned waiting_approval without "
                            "resumable checkpoint context"
                        ),
                    )
                    return
                self._mark_waiting_approval(
                    session,
                    row,
                    result,
                    node_id=approval_checkpoint[0],
                    state_blob=approval_checkpoint[1],
                )
                return
            if self._is_waiting_event(result):
                waiting_event_checkpoint = _waiting_event_checkpoint_payload(result)
                if waiting_event_checkpoint is None:
                    self._mark_failed(
                        session,
                        row,
                        result=result,
                        error_code="runtime_error",
                        error_summary=(
                            "workflow runtime returned waiting_event without "
                            "resumable checkpoint context"
                        ),
                    )
                    return
                self._mark_waiting_event(
                    session,
                    row,
                    result,
                    node_id=waiting_event_checkpoint[0],
                    state_blob=waiting_event_checkpoint[1],
                )
                return
            if self._is_cancelled_result(result):
                self._mark_cancelled(session, row, result)
                return
            status = normalize_runtime_result_status(result.status)
            if status == RUN_STATUS_COMPLETED:
                self._mark_completed(session, row, result)
                return
            self._mark_failed(
                session,
                row,
                result=result,
                error_code="runtime_error",
                error_summary=result.error or "workflow runtime failed",
            )

    def _is_waiting_approval(self, result: WorkflowRunResult) -> bool:
        return bool(
            result.status == "blocked"
            and isinstance(result.error, str)
            and result.error.startswith("waiting_approval:")
        )

    def _is_cancelled_result(self, result: WorkflowRunResult) -> bool:
        return bool(
            result.status == "error"
            and isinstance(result.error, str)
            and _CANCEL_SENTINEL in result.error
        )

    def _is_waiting_event(self, result: WorkflowRunResult) -> bool:
        return bool(
            result.status == "blocked"
            and isinstance(result.error, str)
            and result.error.startswith("waiting_event:")
        )

    def _mark_waiting_approval(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        result: WorkflowRunResult,
        *,
        node_id: str,
        state_blob: dict[str, Any],
    ) -> None:
        assert_run_transition(run.status, RUN_STATUS_WAITING_APPROVAL)
        run.status = RUN_STATUS_WAITING_APPROVAL
        run.current_node_id = node_id
        run.error_code = "waiting_approval"
        run.error_summary = "waiting for runtime approval decision"
        now = datetime.now(timezone.utc)
        run.last_heartbeat_at = now
        run.lease_expires_at = None

        approval_id = new_runtime_approval_id()
        # Snapshot the node's ACTUAL policy. This was a hard-coded
        # ``{"timeout_behavior": "block"}`` literal, so the configured
        # ``required_role`` and ``approval_count`` never reached the decision path —
        # the UI promised controls the server did not honour. Snapshotting also pins
        # the policy at request time, so editing the manifest cannot retroactively
        # change what an already-pending approval requires.
        session.add(
            CaliberRuntimeApprovalRequest(
                runtime_approval_id=approval_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                node_id=node_id,
                status="pending",
                policy_snapshot=self._approval_policy_snapshot(run, node_id),
            )
        )

        checkpoint_seq = (
            session.execute(
                select(func.max(CaliberWorkflowRunCheckpoint.sequence)).where(
                    CaliberWorkflowRunCheckpoint.workflow_run_id == run.workflow_run_id
                )
            )
            .scalars()
            .first()
        )
        checkpoint_id = new_workflow_run_checkpoint_id()
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=(checkpoint_seq or 0) + 1,
                node_id=node_id,
                state_blob=state_blob,
            )
        )
        summary = dict(run.summary or {})
        summary["resume_checkpoint_id"] = checkpoint_id
        summary["resume_checkpoint_run_id"] = run.workflow_run_id
        summary["status"] = RUN_STATUS_WAITING_APPROVAL
        summary["steps"] = [_step_to_dict(step) for step in result.steps]
        summary["node_path"] = [step.node_id for step in result.steps]
        run.summary = summary

        self._append_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.waiting_approval",
            payload={"node_id": node_id, "runtime_approval_id": approval_id},
            node_id=node_id,
        )
        audit_record(
            session,
            actor="workflow_run_worker",
            action="workflow_run_waiting_approval",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={"node_id": node_id, "runtime_approval_id": approval_id},
        )
        session.commit()
        self._publish(
            {
                "type": "workflow.run.waiting_approval",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
                "node_id": node_id,
                "runtime_approval_id": approval_id,
            }
        )

    def _approval_policy_snapshot(self, run: CaliberWorkflowRun, node_id: str) -> dict[str, Any]:
        """The approval policy for ``node_id``, read from the run's own version.

        Falls back to the defaults when the node or manifest cannot be read: a run
        that reached an approval gate must remain decidable, and a default of
        "approver scope, quorum of one, no self-approval" is the safe reading.
        """
        from caliber.workflows.approval_policy import ApprovalPolicy  # noqa: PLC0415

        allow_self = bool(getattr(self._config, "approval_allow_self_approval", False))
        try:
            with self._session_factory() as session:
                version = (
                    session.get(CaliberWorkflowVersion, run.workflow_version_id)
                    if run.workflow_version_id
                    else None
                )
                manifest = version.manifest if version is not None else None
            nodes = (manifest or {}).get("nodes") or {}
            raw = nodes.get(node_id) if isinstance(nodes, dict) else None
            if isinstance(raw, dict):
                return ApprovalPolicy(
                    required_role=str(raw.get("required_role") or "caliber.approver"),
                    approval_count=max(1, int(raw.get("approval_count") or 1)),
                    timeout_behavior=str(raw.get("timeout_behavior") or "block"),
                    allow_self_approval=allow_self,
                ).to_snapshot()
        except Exception:
            logger.warning(
                "could not read approval policy for run %s node %s; using defaults",
                run.workflow_run_id,
                node_id,
                exc_info=True,
            )
        return ApprovalPolicy(allow_self_approval=allow_self).to_snapshot()

    def _mark_waiting_event(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        result: WorkflowRunResult,
        *,
        node_id: str,
        state_blob: dict[str, Any],
    ) -> None:
        assert_run_transition(run.status, RUN_STATUS_WAITING_EVENT)
        run.status = RUN_STATUS_WAITING_EVENT
        run.current_node_id = node_id
        run.error_code = "waiting_event"
        run.error_summary = "waiting for resume event"
        now = datetime.now(timezone.utc)
        run.last_heartbeat_at = now
        run.lease_expires_at = None

        checkpoint_seq = (
            session.execute(
                select(func.max(CaliberWorkflowRunCheckpoint.sequence)).where(
                    CaliberWorkflowRunCheckpoint.workflow_run_id == run.workflow_run_id
                )
            )
            .scalars()
            .first()
        )
        checkpoint_id = new_workflow_run_checkpoint_id()
        session.add(
            CaliberWorkflowRunCheckpoint(
                checkpoint_id=checkpoint_id,
                workflow_run_id=run.workflow_run_id,
                project_id=run.project_id,
                sequence=(checkpoint_seq or 0) + 1,
                node_id=node_id,
                state_blob=state_blob,
            )
        )
        summary = dict(run.summary or {})
        summary["resume_checkpoint_id"] = checkpoint_id
        summary["resume_checkpoint_run_id"] = run.workflow_run_id
        summary["status"] = RUN_STATUS_WAITING_EVENT
        summary["steps"] = [_step_to_dict(step) for step in result.steps]
        summary["node_path"] = [step.node_id for step in result.steps]
        run.summary = summary

        self._append_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.waiting_event",
            payload={"node_id": node_id},
            node_id=node_id,
        )
        audit_record(
            session,
            actor="workflow_run_worker",
            action="workflow_run_waiting_event",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={"node_id": node_id},
        )
        session.commit()
        self._publish(
            {
                "type": "workflow.run.waiting_event",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
                "node_id": node_id,
            }
        )

    def _mark_completed(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        result: WorkflowRunResult,
    ) -> None:
        assert_run_transition(run.status, RUN_STATUS_COMPLETED)
        run.status = RUN_STATUS_COMPLETED
        now = datetime.now(timezone.utc)
        run.completed_at = now
        run.current_node_id = None
        run.lease_expires_at = None
        run.last_heartbeat_at = now
        summary = workflow_run_summary(result, preview=False)
        summary.update(_manifest_summary_metadata(run.summary))
        summary["input"] = _input_from_summary(run.summary)
        run.summary = summary
        run.error_code = None
        run.error_summary = None
        self._append_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.completed",
            payload={"status": run.status},
        )
        audit_record(
            session,
            actor="workflow_run_worker",
            action="complete_workflow_run",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={"status": run.status},
        )
        session.commit()
        self._publish(
            {
                "type": "workflow.run.completed",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
            }
        )
        # After the run is durably completed, persist its file artifacts + a JSON
        # log to object storage (no-op unless a bucket is configured).
        self._persist_run_artifacts(run, result)

    def _mark_cancelled(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        result: WorkflowRunResult,
    ) -> None:
        assert_run_transition(run.status, RUN_STATUS_CANCELLED)
        run.status = RUN_STATUS_CANCELLED
        now = datetime.now(timezone.utc)
        run.completed_at = now
        run.current_node_id = None
        run.lease_expires_at = None
        run.last_heartbeat_at = now
        summary = workflow_run_summary(result, preview=False)
        summary.update(_manifest_summary_metadata(run.summary))
        summary["input"] = _input_from_summary(run.summary)
        run.summary = summary
        run.error_code = "cancelled"
        run.error_summary = "cancelled by operator"
        self._append_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.cancelled",
            payload={"status": run.status},
        )
        audit_record(
            session,
            actor="workflow_run_worker",
            action="cancel_workflow_run",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={"status": run.status},
        )
        session.commit()
        self._publish(
            {
                "type": "workflow.run.cancelled",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
            }
        )

    def _mark_failed(
        self,
        session: Session,
        run: CaliberWorkflowRun,
        *,
        result: WorkflowRunResult | None = None,
        error_code: str,
        error_summary: str,
    ) -> None:
        assert_run_transition(run.status, RUN_STATUS_FAILED)
        run.status = RUN_STATUS_FAILED
        now = datetime.now(timezone.utc)
        run.completed_at = now
        run.current_node_id = None
        run.lease_expires_at = None
        run.last_heartbeat_at = now
        if result is not None:
            summary = workflow_run_summary(result, preview=False)
        else:
            summary = dict(run.summary or {})
        summary.update(_manifest_summary_metadata(run.summary))
        summary["input"] = _input_from_summary(run.summary)
        summary["status"] = RUN_STATUS_FAILED
        summary["error"] = error_summary
        run.summary = summary
        run.error_code = error_code
        run.error_summary = error_summary
        self._append_event(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=run.project_id,
            event_type="workflow.run.failed",
            payload={"status": run.status, "error": error_summary},
        )
        audit_record(
            session,
            actor="workflow_run_worker",
            action="fail_workflow_run",
            entity_type="workflow_run",
            entity_id=run.workflow_run_id,
            details={"status": run.status, "error": error_summary},
        )
        session.commit()
        self._publish(
            {
                "type": "workflow.run.failed",
                "workflow_id": run.workflow_id,
                "workflow_version_id": run.workflow_version_id,
                "workflow_run_id": run.workflow_run_id,
                "status": run.status,
                "error": error_summary,
            }
        )


__all__ = ["WorkflowRunWorker"]
