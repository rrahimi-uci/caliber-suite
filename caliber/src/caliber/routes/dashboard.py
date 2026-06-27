"""``/caliber/dashboard/summary`` — aggregated counts for the Overview page.

The Overview page (the SPA's landing screen) renders six stat cards that
each map to a count CALIBER's tables already store. Rather than have the
frontend issue six round-trips, the dashboard endpoint computes them all
in a single read transaction — point-in-time consistent and one HTTP
call regardless of how many cards the design adds later.

The query shape is intentionally simple (SUM-of-CASE pattern) so it's
readable without ORM gymnastics. A future caching layer (60-second TTL,
say) would slot in here without changing the response shape.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import require_user
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAssistantRun,
    CaliberAssistantSession,
    CaliberAuditLog,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.routes._deps import envelope_response, get_session_factory
from caliber.schemas import AssistantSloSummarySchema, DashboardSummarySchema

SUMMARY_PATH = "/ajax-api/2.0/mlflow/caliber/dashboard/summary"


async def get_summary(request: Request) -> JSONResponse:
    """Compute and return the dashboard counts."""
    require_user(request)
    factory = get_session_factory(request)
    with factory() as session:
        summary = _compute_summary(session)
    return envelope_response(summary)


def _compute_summary(session: Session) -> DashboardSummarySchema:
    agents_total, agents_enabled = _count_agents(session)
    pending, pending_critical = _count_verification(session)
    job_counts = _count_jobs(session)
    approvals_pending = _count_pending_approvals(session)
    assistant_slo = _compute_assistant_slo(session)

    return DashboardSummarySchema(
        agents_total=agents_total,
        agents_enabled=agents_enabled,
        verification_pending=pending,
        verification_pending_critical=pending_critical,
        jobs_queued=job_counts["queued"],
        jobs_running=job_counts["running"],
        jobs_awaiting_approval=job_counts["awaiting_approval"],
        jobs_completed=job_counts["completed"],
        jobs_failed=job_counts["failed"],
        jobs_rejected=job_counts["rejected"],
        approvals_pending=approvals_pending,
        assistant_slo=assistant_slo,
        generated_at=datetime.now(timezone.utc),
    )


def _count_agents(session: Session) -> tuple[int, int]:
    total, enabled = session.execute(
        select(
            func.count(CaliberAgentConfig.agent_id),
            func.sum(case((CaliberAgentConfig.enabled.is_(True), 1), else_=0)),
        )
    ).one()
    return int(total or 0), int(enabled or 0)


def _count_verification(session: Session) -> tuple[int, int]:
    pending, critical = session.execute(
        select(
            func.sum(case((CaliberVerificationItem.status == "pending", 1), else_=0)),
            func.sum(
                case(
                    (
                        (CaliberVerificationItem.status == "pending")
                        & (CaliberVerificationItem.severity == "critical"),
                        1,
                    ),
                    else_=0,
                )
            ),
        )
    ).one()
    return int(pending or 0), int(critical or 0)


# Job statuses we count individually. Anything outside this set (legacy
# values, future additions) is silently ignored — the dashboard counts the
# states the UI renders, not the full universe of possible statuses.
_JOB_STATUSES = ("queued", "running", "awaiting_approval", "completed", "failed", "rejected")


def _count_jobs(session: Session) -> dict[str, int]:
    columns = [
        func.sum(case((CaliberRefinementJob.status == status, 1), else_=0)).label(status)
        for status in _JOB_STATUSES
    ]
    row = session.execute(select(*columns)).one()
    return {status: int(value or 0) for status, value in zip(_JOB_STATUSES, row, strict=True)}


def _count_pending_approvals(session: Session) -> int:
    value = session.execute(
        select(func.count())
        .select_from(CaliberApprovalRequest)
        .where(CaliberApprovalRequest.status == "pending")
    ).scalar_one()
    return int(value or 0)


def _rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _compute_assistant_slo(session: Session) -> AssistantSloSummarySchema:
    plans = _assistant_plans(session)
    plan_count = len(plans)
    plan_ready = sum(1 for plan in plans if plan.get("ready") is True)
    clarification_count = sum(
        1 for plan in plans if bool(plan.get("missing_slots")) or bool(plan.get("questions"))
    )
    confidences = [
        float(confidence)
        for plan in plans
        if isinstance((intent := plan.get("intent")), dict)
        and isinstance((confidence := intent.get("confidence")), int | float)
    ]

    run_statuses: list[tuple[str, str | None]] = [
        (row[0], row[1])
        for row in session.execute(
            select(CaliberAssistantRun.status, CaliberAssistantRun.error).where(
                CaliberAssistantRun.engine == "assistant-intent"
            )
        )
    ]
    execution_total = len(run_statuses)
    execution_completed = sum(1 for status, _error in run_statuses if status == "completed")
    execution_failed = sum(1 for status, _error in run_statuses if status == "failed")

    operations = _assistant_operations(session)
    execution_blocked = sum(
        1
        for operation in operations
        if isinstance((result := operation.get("result")), dict)
        and result.get("status") == "blocked"
    )
    adapter_error_classes = _assistant_error_classes(operations, run_statuses)
    publish_total, publish_success, publish_failed = _assistant_publish_counts(session)

    return AssistantSloSummarySchema(
        intent_confidence_avg=round(sum(confidences) / len(confidences), 4)
        if confidences
        else None,
        plans_total=plan_count,
        plans_ready=plan_ready,
        plan_readiness_rate=_rate(plan_ready, plan_count),
        clarification_rate=_rate(clarification_count, plan_count),
        executions_total=execution_total,
        executions_completed=execution_completed,
        executions_failed=execution_failed,
        executions_blocked=execution_blocked,
        execution_success_rate=_rate(execution_completed, execution_total),
        adapter_error_classes=adapter_error_classes,
        publish_total=publish_total,
        publish_success=publish_success,
        publish_failed=publish_failed,
        publish_success_rate=_rate(publish_success, publish_total),
    )


def _assistant_workbench_records(session: Session, key: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    metadata_rows = session.execute(select(CaliberAssistantSession.metadata_)).scalars().all()
    for metadata in metadata_rows:
        if not isinstance(metadata, dict):
            continue
        workbench = metadata.get("intent_workbench")
        if not isinstance(workbench, dict):
            continue
        grouped = workbench.get(key)
        if isinstance(grouped, dict):
            for record_id, raw in grouped.items():
                if isinstance(raw, dict):
                    clean_id = str(record_id)
                    records.append(raw)
                    seen_ids.add(clean_id)
        latest_key = "latest_plan" if key == "plans" else None
        if latest_key is None:
            continue
        latest = workbench.get(latest_key)
        if not isinstance(latest, dict):
            continue
        latest_id = latest.get("plan_id")
        if isinstance(latest_id, str) and latest_id in seen_ids:
            continue
        records.append(latest)
    return records


def _assistant_plans(session: Session) -> list[dict[str, object]]:
    return _assistant_workbench_records(session, "plans")


def _assistant_operations(session: Session) -> list[dict[str, object]]:
    return _assistant_workbench_records(session, "operations")


def _assistant_error_classes(
    operations: list[dict[str, object]],
    run_statuses: list[tuple[str, str | None]],
) -> dict[str, int]:
    classes: dict[str, int] = {}
    for operation in operations:
        result = operation.get("result")
        if not isinstance(result, dict):
            continue
        result_status = result.get("status")
        result_type = result.get("result_type")
        if result_status not in {"blocked", "failed"} and result_type != "error":
            continue
        raw_class = result.get("error_class") or result_type or result_status or "unknown"
        error_class = str(raw_class)
        classes[error_class] = classes.get(error_class, 0) + 1
    failed_without_operation = sum(1 for status, _error in run_statuses if status == "failed")
    if failed_without_operation and not classes:
        classes["failed"] = failed_without_operation
    return classes


def _assistant_publish_counts(session: Session) -> tuple[int, int, int]:
    rows = (
        session.execute(
            select(CaliberAuditLog.details).where(CaliberAuditLog.action == "publish_draft")
        )
        .scalars()
        .all()
    )
    total = len(rows)
    success = 0
    failed = 0
    for details in rows:
        if isinstance(details, dict) and details.get("success") is True:
            success += 1
        else:
            failed += 1
    return total, success, failed


def register(app: Starlette) -> None:
    """Add the dashboard route to the given Starlette application."""
    app.routes.append(Route(SUMMARY_PATH, get_summary, methods=["GET"]))
