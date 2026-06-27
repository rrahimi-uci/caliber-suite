"""Approval impact preview / blast-radius resolver."""

from __future__ import annotations

import difflib
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberEvalDataset,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
    CaliberSkill,
)
from caliber.regression import candidate_hash
from caliber.schemas import (
    ImpactAgentSchema,
    ImpactDiffSchema,
    ImpactPreviewResponse,
    ImpactReferenceSchema,
    ImpactRollbackSchema,
)


class ActiveArtifactStore(Protocol):
    """Subset of ``ArtifactStore`` needed by impact preview."""

    def get_active_prompt(self, agent_id: str) -> str | None: ...


def build_impact_preview(
    session: Session,
    approval_id: str,
    *,
    artifact_store: ActiveArtifactStore | None = None,
) -> ImpactPreviewResponse:
    """Compute the current blast radius for an approval candidate."""
    approval = session.get(CaliberApprovalRequest, approval_id)
    if approval is None:
        raise LookupError(f"approval request {approval_id!r} not found")
    job = session.get(CaliberRefinementJob, approval.job_id)
    if job is None:
        raise LookupError(f"approval {approval_id!r} references missing job {approval.job_id!r}")
    agent = session.get(CaliberAgentConfig, approval.agent_id)
    if agent is None:
        raise LookupError(
            f"approval {approval_id!r} references missing agent {approval.agent_id!r}"
        )

    candidate = approval.candidate_snapshot or job.candidate or {}
    raw_content = candidate.get("content")
    candidate_content = raw_content if isinstance(raw_content, str) else ""
    current_content = _current_content(artifact_store, agent.agent_id, candidate, job)
    diff = _build_diff(
        current_content=current_content,
        candidate_content=candidate_content,
        diff_summary=_optional_str(candidate.get("diff_summary")),
    )
    impacted_agents = _impacted_agents(session, job, agent, candidate)
    skill_refs = _resolve_skills(session, job, agent, candidate)
    dataset_refs = _resolve_datasets(session, agent, approval)
    rollback = _rollback_preview(session, approval)
    risk_flags = _risk_flags(
        current_content=current_content,
        candidate_content=candidate_content,
        impacted_agents=impacted_agents,
        skills=skill_refs,
        datasets=dataset_refs,
    )

    return ImpactPreviewResponse(
        approval_id=approval.approval_id,
        job_id=job.job_id,
        agent_id=agent.agent_id,
        artifact_type=job.artifact_type,
        impacted_agents=impacted_agents,
        impacted_skills=skill_refs,
        eval_datasets=dataset_refs,
        diff=diff,
        rollback=rollback,
        risk_flags=risk_flags,
    )


def _current_content(
    artifact_store: ActiveArtifactStore | None,
    agent_id: str,
    candidate: dict[str, object],
    job: CaliberRefinementJob,
) -> str | None:
    if artifact_store is not None:
        current = artifact_store.get_active_prompt(agent_id)
        if current is not None:
            return current
    for key in ("baseline_content", "current_content", "previous_content"):
        raw = candidate.get(key)
        if isinstance(raw, str):
            return raw
    if isinstance(job.candidate, dict):
        for key in ("baseline_content", "current_content", "previous_content"):
            raw = job.candidate.get(key)
            if isinstance(raw, str):
                return raw
    return None


def _build_diff(
    *,
    current_content: str | None,
    candidate_content: str,
    diff_summary: str | None,
) -> ImpactDiffSchema:
    before = current_content or ""
    diff_lines = difflib.unified_diff(
        before.splitlines(),
        candidate_content.splitlines(),
        fromfile="current",
        tofile="candidate",
        lineterm="",
    )
    return ImpactDiffSchema(
        current_available=current_content is not None,
        candidate_hash=candidate_hash(candidate_content),
        diff_summary=diff_summary,
        unified_diff="\n".join(diff_lines),
    )


def _impacted_agents(
    session: Session,
    job: CaliberRefinementJob,
    agent: CaliberAgentConfig,
    candidate: dict[str, object],
) -> list[ImpactAgentSchema]:
    agent_roles: dict[str, str | None] = {agent.agent_id: "primary"}
    for entry in job.bundle_targets or []:
        if not isinstance(entry, dict):
            continue
        target_id = entry.get("agent_id")
        if isinstance(target_id, str) and target_id:
            agent_roles[target_id] = _optional_str(entry.get("role")) or "bundle"

    if job.artifact_type == "skill":
        skill_name = _candidate_artifact_name(candidate)
        if skill_name:
            for row in session.execute(select(CaliberAgentConfig)).scalars().all():
                if skill_name in _skill_names(row.optimizer_config):
                    agent_roles.setdefault(row.agent_id, "shared_skill")

    rows = (
        session.execute(
            select(CaliberAgentConfig).where(CaliberAgentConfig.agent_id.in_(list(agent_roles)))
        )
        .scalars()
        .all()
    )
    names = {row.agent_id: row.name for row in rows}
    return [
        ImpactAgentSchema(agent_id=agent_id, name=names.get(agent_id), role=role)
        for agent_id, role in agent_roles.items()
    ]


def _resolve_skills(
    session: Session,
    job: CaliberRefinementJob,
    agent: CaliberAgentConfig,
    candidate: dict[str, object],
) -> list[ImpactReferenceSchema]:
    names = set(_skill_names(agent.optimizer_config))
    if job.artifact_type == "skill":
        skill_name = _candidate_artifact_name(candidate)
        if skill_name:
            names.add(skill_name)
    if not names:
        return []
    rows = (
        session.execute(select(CaliberSkill).where(CaliberSkill.name.in_(sorted(names))))
        .scalars()
        .all()
    )
    return [
        ImpactReferenceSchema(
            id=row.skill_id,
            name=row.name,
            status=row.status,
            version=row.version,
        )
        for row in rows
    ]


def _resolve_datasets(
    session: Session,
    agent: CaliberAgentConfig,
    approval: CaliberApprovalRequest,
) -> list[ImpactReferenceSchema]:
    names = _dataset_names(agent.eval_thresholds)
    eval_results = approval.eval_results or {}
    raw_eval_dataset = (
        eval_results.get("eval_dataset_id") if isinstance(eval_results, dict) else None
    )
    if isinstance(raw_eval_dataset, str) and raw_eval_dataset:
        names.add(raw_eval_dataset)
    if not names:
        return []

    rows = (
        session.execute(
            select(CaliberEvalDataset).where(
                (CaliberEvalDataset.dataset_id.in_(names)) | (CaliberEvalDataset.name.in_(names))
            )
        )
        .scalars()
        .all()
    )
    found = {ref for row in rows for ref in (row.dataset_id, row.name)}
    resolved = [
        ImpactReferenceSchema(
            id=row.dataset_id,
            name=row.name,
            status=row.status,
            version=row.version,
        )
        for row in rows
    ]
    unresolved = sorted(names - found)
    resolved.extend(ImpactReferenceSchema(id=name, name=name) for name in unresolved)
    return resolved


def _rollback_preview(
    session: Session,
    approval: CaliberApprovalRequest,
) -> ImpactRollbackSchema:
    latest = (
        session.execute(
            select(CaliberRollbackCheckpoint)
            .where(CaliberRollbackCheckpoint.agent_id == approval.agent_id)
            .order_by(CaliberRollbackCheckpoint.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return ImpactRollbackSchema(
        will_create_checkpoint=True,
        latest_checkpoint_id=latest.checkpoint_id if latest is not None else None,
        latest_checkpoint_created_at=latest.created_at if latest is not None else None,
        rollback_available=latest is not None,
    )


def _risk_flags(
    *,
    current_content: str | None,
    candidate_content: str,
    impacted_agents: list[ImpactAgentSchema],
    skills: list[ImpactReferenceSchema],
    datasets: list[ImpactReferenceSchema],
) -> list[str]:
    flags: list[str] = []
    if current_content is None:
        flags.append("current_artifact_unavailable")
    if not candidate_content:
        flags.append("candidate_content_empty")
    if len(impacted_agents) > 1:
        flags.append("multi_agent_blast_radius")
    if skills:
        flags.append("skill_dependency_present")
    if not datasets:
        flags.append("no_eval_dataset_resolved")
    return flags


def _candidate_artifact_name(candidate: dict[str, object]) -> str | None:
    for key in ("artifact_name", "name", "skill_name"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _skill_names(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    raw = payload.get("skills")
    if isinstance(raw, str) and raw:
        return {raw}
    if isinstance(raw, list):
        return {item for item in raw if isinstance(item, str) and item}
    return set()


def _dataset_names(payload: object) -> set[str]:
    if not isinstance(payload, dict):
        return set()
    names: set[str] = set()
    for key in ("eval_dataset_id", "eval_dataset", "golden_dataset_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            names.add(value)
    for key in ("eval_dataset_ids", "golden_dataset_ids", "regression_dataset_ids"):
        value = payload.get(key)
        if isinstance(value, list):
            names.update(item for item in value if isinstance(item, str) and item)
    return names


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
