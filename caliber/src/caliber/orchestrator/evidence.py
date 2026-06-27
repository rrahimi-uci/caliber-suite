"""Evidence stage of the refinement pipeline.

Evidence collects the trace data, similar past feedback, and active artifact
references that the Diagnosis stage will reason over. When a ``trace_client``
is supplied and the item links a trace, the real execution trace summary is
fetched (best-effort — a missing/unreadable trace just omits that section);
similar past feedback for the same agent + category is always surfaced. The
deterministic state-machine + side-effects (counters, audit row) wrap that
collection.

The state-machine guard is the same shape as :func:`caliber.orchestrator.triage.run_triage`
so the worker can compose stages with uniform error handling.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberVerificationItem,
)

if TYPE_CHECKING:
    from caliber.trace_client import TraceClient

logger = logging.getLogger("caliber.orchestrator.evidence")

# How many similar past feedback items to surface to the diagnosis stage.
_SIMILAR_FEEDBACK_LIMIT = 5

_ELIGIBLE_STATUSES = frozenset({"running"})
_ELIGIBLE_STAGES = frozenset({"evidence"})


class EvidenceStateError(Exception):
    """Raised when :func:`run_evidence` is called on an ineligible job state."""


def run_evidence(
    session: Session,
    job_id: str,
    *,
    actor: str = "system",
    trace_client: TraceClient | None = None,
) -> CaliberRefinementJob:
    """Advance the named job through the evidence stage.

    Parameters
    ----------
    session:
        Open SQLAlchemy session. Committed inside this function so partial
        state is never observable to readers.
    job_id:
        ID of the refinement job to advance.
    actor:
        Who is triggering this. The worker passes ``"system"``.

    Returns
    -------
    The updated :class:`CaliberRefinementJob` row.

    Raises
    ------
    LookupError
        Job does not exist or its source verification item is missing.
    EvidenceStateError
        Job is not in a state where evidence collection can run.
    """
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise LookupError(f"refinement job {job_id!r} not found")

    if job.status not in _ELIGIBLE_STATUSES or job.current_stage not in _ELIGIBLE_STAGES:
        raise EvidenceStateError(
            f"job {job_id!r} not eligible for evidence: "
            f"status={job.status!r}, stage={job.current_stage!r}"
        )

    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None:
        raise LookupError(f"verification item {job.primary_item_id!r} not found for job {job_id!r}")

    evidence_summary = _collect(session, item, job, trace_client=trace_client)
    previous_stage = job.current_stage

    job.current_stage = "diagnosis"
    # Status stays ``running`` — diagnosis is the next stage in the same run.

    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": previous_stage,
            "to_stage": "diagnosis",
            "evidence": evidence_summary,
        },
    )

    session.commit()
    logger.info("evidence complete: job=%s summary=%s", job_id, evidence_summary)
    return job


def _collect(
    session: Session,
    item: CaliberVerificationItem,
    job: CaliberRefinementJob,
    *,
    trace_client: TraceClient | None = None,
) -> dict[str, Any]:
    """Collect evidence for the diagnosis stage.

    Assembles a summary from the verification-queue row, the actual execution
    trace (when ``trace_client`` is supplied and the item links a trace), and
    similar past feedback for the same agent + category. Trace fetching is
    best-effort — a missing/unreadable trace just omits that section.

    For skill-targeted jobs (``artifact_type == "skill"``), the evidence also
    includes the current skill content, version, affected agents, and
    dependency graph — giving the diagnosis stage the full context it needs
    to reason about a cross-agent skill issue.
    """
    base: dict[str, Any] = {
        "trace_id": item.trace_id,
        "experiment_id": item.experiment_id,
        "session_id": item.session_id,
        "has_trace_link": item.trace_id is not None,
        "has_session_link": item.session_id is not None,
        "free_text_preview": (item.free_text or "")[:120],
    }

    # Fetch the real execution trace so diagnosis reasons over what happened,
    # not just the verifier's note. Best-effort: never fails the stage.
    if item.trace_id and trace_client is not None:
        summary = trace_client.get_trace_summary(item.trace_id)
        if summary is not None:
            base["trace"] = {
                "status": summary.status,
                "request_preview": summary.request_preview,
                "response_preview": summary.response_preview,
                "span_count": summary.span_count,
                "tool_calls": summary.tool_calls,
                "error": summary.error,
            }

    # Similar past feedback for the same agent + category — lets diagnosis see
    # whether this is a one-off or a recurring failure mode.
    similar = (
        session.query(CaliberVerificationItem)
        .filter(
            CaliberVerificationItem.agent_id == item.agent_id,
            CaliberVerificationItem.category == item.category,
            CaliberVerificationItem.item_id != item.item_id,
        )
        .order_by(CaliberVerificationItem.created_at.desc())
        .limit(_SIMILAR_FEEDBACK_LIMIT)
        .all()
    )
    if similar:
        base["similar_feedback"] = {
            "count": len(similar),
            "examples": [
                {
                    "item_id": s.item_id,
                    "severity": s.severity,
                    "preview": (s.free_text or "")[:120],
                }
                for s in similar
            ],
        }

    if job.artifact_type == "skill" and job.skill_name:
        skill = (
            session.query(CaliberSkill)
            .filter(
                CaliberSkill.name == job.skill_name,
                CaliberSkill.status == "active",
            )
            .first()
        )
        if skill is not None:
            # Find all agents that reference this skill.
            agents = (
                session.query(CaliberAgentConfig)
                .filter(
                    CaliberAgentConfig.enabled.is_(True),
                )
                .all()
            )
            affected_agent_ids = [
                a.agent_id
                for a in agents
                if job.skill_name in (a.optimizer_config or {}).get("skills", [])
            ]
            base["skill"] = {
                "name": skill.name,
                "version": skill.version,
                "category": skill.category,
                "content_preview": (skill.content or "")[:500],
                "summary": skill.summary or "",
                "allowed_tools": skill.allowed_tools,
                "depends_on": list(skill.depends_on or []),
                "affected_agent_ids": affected_agent_ids,
                "affected_agent_count": len(affected_agent_ids),
            }

    return base
