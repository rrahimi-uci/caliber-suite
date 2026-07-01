"""Shared candidate-promotion logic for the operator Apply flow.

After the eval gate clears, a refinement job lands at the terminal
``candidate_ready`` state — no human-feedback approval is created. An operator
then promotes the job's candidate by hitting the Apply endpoint
(:func:`caliber.routes.jobs.apply_job`), which mints a born-``approved``
:class:`CaliberApprovalRequest` as a provenance/rollback anchor and calls
:func:`apply_candidate` here to do the actual promotion.

This module is self-contained: it owns the promotion/checkpoint helpers that
used to live in the (now-removed) approvals route, so the promotion path
survives the removal of the approval-governance subsystem. The three promotion
shapes are dispatched on the candidate payload:

* ``candidate["promotion_type"] == "prompt_alias"`` → rotate a prompt alias to
  an existing version (assistant proposals).
* ``job.artifact_type == "workflow_manifest"`` → publish a new workflow version
  and rotate its alias via :func:`caliber.workflows.promoter.promote_workflow_candidate`.
* otherwise → prompt/skill bundle promotion via :func:`caliber.bundle.promote_bundle`
  with the injected :class:`caliber.promoter.Promoter`.

Each path records one or more :class:`CaliberRollbackCheckpoint` rows anchored
to the passed ``approval`` and writes a ``promote`` audit row. The caller owns
the surrounding transaction (and the commit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.audit import record as audit_record
from caliber.bundle import (
    BundleTarget,
    promote_bundle,
    resolve_bundle_targets,
)
from caliber.db.models import (
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberRollbackCheckpoint,
)
from caliber.ids import new_checkpoint_id
from caliber.promoter import Promoter, PromoterError, PromotionResult
from caliber.workflows.promoter import (
    DeployError,
    DeployGateFailedError,
    PublishError,
    promote_workflow_candidate,
)


@dataclass(frozen=True)
class ApplyOutcome:
    """Result of promoting a job's candidate.

    ``promotion`` is the JSON-serializable payload returned to the Apply
    caller (legacy ``artifact_ref`` / ``rotated_at`` / ``details`` fields plus
    bundle-aware extras). ``checkpoint_ids`` are the rollback-checkpoint rows
    written in the same transaction.
    """

    promotion: dict[str, object]
    checkpoint_ids: list[str] = field(default_factory=list)


def _as_str(value: object, default: str = "") -> str:
    """Return ``value`` when it is a string, else ``default`` (candidate dicts
    are loosely typed ``dict[str, object]``, so call-site values need narrowing)."""
    return value if isinstance(value, str) else default


def _build_checkpoint(
    approval: CaliberApprovalRequest,
    candidate: dict[str, object],
    result: PromotionResult,
) -> CaliberRollbackCheckpoint:
    """Translate a successful :class:`PromotionResult` into a checkpoint row.

    We rely on MLflow's monotonically-increasing prompt version numbers
    to compute the prior version (``version_after - 1``). For v1 promotions
    (no prior version) we return ``None`` so the rollback endpoint can
    surface a clean 502 ("nothing to roll back to").
    """
    details = getattr(result, "details", None) or {}
    version_after_raw = details.get("version") if isinstance(details, dict) else None
    version_after: int | None
    if isinstance(version_after_raw, int):
        version_after = version_after_raw
    elif isinstance(version_after_raw, str) and version_after_raw.isdigit():
        version_after = int(version_after_raw)
    else:
        version_after = None
    # Prefer the exact outgoing live version captured by the promoter; fall back
    # to ``version_after - 1`` only for promoters that don't report it (e.g.
    # FakePromoter), which is wrong when intermediate versions didn't rotate.
    version_before_raw = details.get("version_before") if isinstance(details, dict) else None
    version_before: int | None
    if isinstance(version_before_raw, int):
        version_before = version_before_raw
    elif isinstance(version_before_raw, str) and version_before_raw.isdigit():
        version_before = int(version_before_raw)
    else:
        version_before = (
            version_after - 1 if version_after is not None and version_after > 1 else None
        )
    artifact_type = _as_str(candidate.get("artifact_type"), "prompt")
    artifact_ref_before = (
        f"prompts:/{approval.agent_id}/{version_before}" if version_before is not None else None
    )
    # Skills have no alias indirection (the active skill *is* the DB row), so the
    # checkpoint must carry the prior content for SkillPromoter.rollback to
    # restore it; prompts roll back via alias rotation and need no snapshot.
    snapshot_payload: dict[str, object] | None = None
    if artifact_type == "skill":
        content_before = details.get("content_before") if isinstance(details, dict) else None
        snapshot_payload = {"content_before": content_before, "version_before": version_before}
        artifact_ref_before = (
            f"skill://{approval.agent_id}/v{version_before}" if version_before is not None else None
        )
    return CaliberRollbackCheckpoint(
        checkpoint_id=new_checkpoint_id(),
        approval_id=approval.approval_id,
        agent_id=approval.agent_id,
        artifact_type=artifact_type,
        artifact_name=approval.agent_id,
        artifact_ref_before=artifact_ref_before,
        artifact_ref_after=str(getattr(result, "artifact_ref", ""))
        if getattr(result, "artifact_ref", None) is not None
        else "",
        version_before=version_before,
        version_after=version_after,
        snapshot_payload=snapshot_payload,
    )


def _build_bundle_checkpoint(
    approval: CaliberApprovalRequest,
    target: BundleTarget,
    result: PromotionResult,
) -> CaliberRollbackCheckpoint:
    """Per-target checkpoint for a multi-artifact bundle promotion.

    Differs from :func:`_build_checkpoint` in that the agent and
    artifact_type come from the bundle target, not the approval's
    canonical agent — the approval may span multiple agents.
    """
    details = getattr(result, "details", None) or {}
    version_after_raw = details.get("version") if isinstance(details, dict) else None
    version_after: int | None
    if isinstance(version_after_raw, int):
        version_after = version_after_raw
    elif isinstance(version_after_raw, str) and version_after_raw.isdigit():
        version_after = int(version_after_raw)
    else:
        version_after = None
    # Prefer the exact outgoing live version captured by the promoter; fall back
    # to ``version_after - 1`` only for promoters that don't report it (e.g.
    # FakePromoter), which is wrong when intermediate versions didn't rotate.
    version_before_raw = details.get("version_before") if isinstance(details, dict) else None
    version_before: int | None
    if isinstance(version_before_raw, int):
        version_before = version_before_raw
    elif isinstance(version_before_raw, str) and version_before_raw.isdigit():
        version_before = int(version_before_raw)
    else:
        version_before = (
            version_after - 1 if version_after is not None and version_after > 1 else None
        )
    artifact_ref_before = (
        f"prompts:/{target.agent_id}/{version_before}" if version_before is not None else None
    )
    return CaliberRollbackCheckpoint(
        checkpoint_id=new_checkpoint_id(),
        approval_id=approval.approval_id,
        agent_id=target.agent_id,
        artifact_type=target.artifact_type,
        artifact_name=target.agent_id,
        artifact_ref_before=artifact_ref_before,
        artifact_ref_after=str(getattr(result, "artifact_ref", ""))
        if getattr(result, "artifact_ref", None) is not None
        else "",
        version_before=version_before,
        version_after=version_after,
        snapshot_payload={"bundle_target": True},
    )


def _record_checkpoints(
    session: Session,
    *,
    approval: CaliberApprovalRequest,
    candidate: dict[str, object],
    targets: list[BundleTarget],
    results: list[PromotionResult],
) -> list[str]:
    """Insert one or more rollback checkpoints for a completed bundle.

    Single-target jobs get the legacy checkpoint shape so the
    existing rollback endpoint keeps working unchanged. Multi-target
    bundles get one checkpoint per target, all tagged with the same
    ``approval_id``.
    """
    if len(targets) == 1:
        checkpoint = _build_checkpoint(approval, candidate, results[0])
        session.add(checkpoint)
        return [checkpoint.checkpoint_id]
    checkpoints = [
        _build_bundle_checkpoint(approval, target, target_result)
        for target, target_result in zip(targets, results, strict=True)
    ]
    for cp in checkpoints:
        session.add(cp)
    return [cp.checkpoint_id for cp in checkpoints]


def _build_promotion_payload(
    targets: list[BundleTarget],
    results: list[PromotionResult],
) -> dict[str, object]:
    """Build the ``promotion`` field returned to the Apply caller.

    Legacy fields (``artifact_ref``, ``rotated_at``, ``details``)
    reference the primary (first) target so single-target callers see
    the same shape they always did. Bundle-aware callers consume
    ``bundle_size`` + ``bundle_results``.
    """
    result = results[0]
    return {
        "artifact_ref": result.artifact_ref,
        "rotated_at": result.rotated_at.isoformat(),
        "details": dict(result.details),
        "bundle_size": len(targets),
        "bundle_results": [
            {
                "agent_id": target.agent_id,
                "artifact_type": target.artifact_type,
                "artifact_ref": target_result.artifact_ref,
            }
            for target, target_result in zip(targets, results, strict=True)
        ],
    }


def apply_candidate(
    session: Session,
    *,
    job: CaliberRefinementJob,
    approval: CaliberApprovalRequest,
    promoter: Promoter,
    actor: str,
    terminal_status: str,
    extra_audit: dict[str, object] | None = None,
) -> ApplyOutcome:
    """Promote a job's candidate and mark the job terminal.

    Dispatches on the candidate/job shape (prompt-alias rotation, workflow
    manifest, or prompt/skill bundle), records rollback checkpoint(s) anchored
    to ``approval``, writes a ``promote`` audit row, and sets
    ``job.status = terminal_status`` / ``job.current_stage = "done"``.

    The caller owns the transaction and is responsible for the commit. Promoter
    failures are raised (:class:`PromoterError` / :class:`HTTPException` 502) so
    the caller can roll back and surface the error, exactly as the legacy
    approve route did.
    """
    candidate = approval.candidate_snapshot or job.candidate or {}
    if not isinstance(candidate, dict):
        candidate = {}

    if candidate.get("promotion_type") == "prompt_alias":
        return _apply_prompt_alias(
            session,
            job=job,
            approval=approval,
            candidate=candidate,
            actor=actor,
            terminal_status=terminal_status,
            extra_audit=extra_audit,
        )

    if job.artifact_type == "workflow_manifest":
        return _apply_workflow_manifest(
            session,
            job=job,
            approval=approval,
            candidate=candidate,
            actor=actor,
            terminal_status=terminal_status,
            extra_audit=extra_audit,
        )

    return _apply_bundle(
        session,
        job=job,
        approval=approval,
        candidate=candidate,
        promoter=promoter,
        actor=actor,
        terminal_status=terminal_status,
        extra_audit=extra_audit,
    )


def _apply_bundle(
    session: Session,
    *,
    job: CaliberRefinementJob,
    approval: CaliberApprovalRequest,
    candidate: dict[str, object],
    promoter: Promoter,
    actor: str,
    terminal_status: str,
    extra_audit: dict[str, object] | None,
) -> ApplyOutcome:
    """Promote a prompt/skill candidate (single artifact or multi-target bundle)."""
    candidate_content = candidate.get("content")
    if not isinstance(candidate_content, str) or not candidate_content:
        raise HTTPException(
            status_code=409,
            detail=f"job {job.job_id!r} has no candidate content to promote",
        )

    targets = resolve_bundle_targets(
        job,
        candidate_content=candidate_content,
        candidate_rationale=_as_str(candidate.get("rationale")),
    )
    try:
        results = promote_bundle(promoter, targets, approval_id=approval.approval_id)
    except PromoterError as exc:
        raise HTTPException(status_code=502, detail=f"promotion failed: {exc}") from exc

    result = results[0]
    job.status = terminal_status
    job.current_stage = "done"

    checkpoint_ids = _record_checkpoints(
        session,
        approval=approval,
        candidate=candidate,
        targets=targets,
        results=results,
    )
    promotion_payload = _build_promotion_payload(targets, results)

    audit_record(
        session,
        actor=actor,
        action="promote",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "approval_id": approval.approval_id,
            "artifact_ref": result.artifact_ref,
            "rotated_at": result.rotated_at.isoformat(),
            "checkpoint_ids": checkpoint_ids,
            "bundle_size": len(targets),
            **(extra_audit or {}),
        },
    )
    return ApplyOutcome(promotion=promotion_payload, checkpoint_ids=checkpoint_ids)


def _apply_workflow_manifest(
    session: Session,
    *,
    job: CaliberRefinementJob,
    approval: CaliberApprovalRequest,
    candidate: dict[str, object],
    actor: str,
    terminal_status: str,
    extra_audit: dict[str, object] | None,
) -> ApplyOutcome:
    """Publish a new workflow version from the candidate manifest and rotate its alias."""
    candidate_manifest = candidate.get("candidate_manifest")
    if not isinstance(candidate_manifest, dict) or not candidate_manifest:
        raise HTTPException(
            status_code=409,
            detail=f"job {job.job_id!r} has no candidate manifest to promote",
        )
    workflow_id = str(candidate.get("workflow_id") or job.workflow_id or "")
    alias = str(candidate.get("target_alias") or "dev")
    try:
        version, deployment = promote_workflow_candidate(
            session,
            workflow_id=workflow_id,
            candidate_manifest=candidate_manifest,
            alias=alias,
            actor=actor,
        )
    except (PublishError, DeployError, DeployGateFailedError) as exc:
        raise HTTPException(status_code=502, detail=f"workflow promotion failed: {exc}") from exc

    job.status = terminal_status
    job.current_stage = "done"
    promotion_payload: dict[str, object] = {
        "artifact_ref": version.version_id,
        "workflow_id": workflow_id,
        "alias": deployment.alias,
        "version_number": version.version_number,
        "rotated_at": deployment.deployed_at.isoformat(),
        "kind": "workflow_manifest",
    }
    audit_record(
        session,
        actor=actor,
        action="promote",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "approval_id": approval.approval_id,
            "artifact_ref": version.version_id,
            "workflow_id": workflow_id,
            "alias": deployment.alias,
            "kind": "workflow_manifest",
            **(extra_audit or {}),
        },
    )
    return ApplyOutcome(promotion=promotion_payload, checkpoint_ids=[])


def _apply_prompt_alias(
    session: Session,
    *,
    job: CaliberRefinementJob,
    approval: CaliberApprovalRequest,
    candidate: dict[str, object],
    actor: str,
    terminal_status: str,
    extra_audit: dict[str, object] | None,
) -> ApplyOutcome:
    """Rotate a prompt alias to an existing version (assistant proposal)."""
    source_version = candidate.get("source_version")
    target_alias = candidate.get("target_alias")
    if not isinstance(source_version, int) or source_version < 1:
        raise HTTPException(
            status_code=409,
            detail=f"job {job.job_id!r} candidate has no valid source_version",
        )
    if not isinstance(target_alias, str) or not target_alias:
        raise HTTPException(
            status_code=409,
            detail=f"job {job.job_id!r} candidate has no valid target_alias",
        )
    assistant_context = {
        key: value
        for key in (
            "assistant_session_id",
            "assistant_plan_id",
            "assistant_operation_id",
            "assistant_trace_id",
            "assistant_correlation_id",
        )
        if isinstance((value := candidate.get(key)), str) and value
    }

    from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

    # Read the version live on the alias BEFORE rotating so both the rollback
    # checkpoint and the ``promote_prompt`` audit row below record an exact
    # ``from_version`` (``None`` on a cold-start alias).
    previous_info = prompt_routes._load_prompt_info(approval.agent_id, target_alias)
    previous_version = previous_info.get("version") if previous_info else None

    try:
        alias_result = prompt_routes.set_prompt_alias_version(
            name=approval.agent_id,
            alias=target_alias,
            version=source_version,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"promotion failed: {exc}") from exc

    artifact_ref = f"prompts:/{approval.agent_id}@{target_alias}"
    job.status = terminal_status
    job.current_stage = "done"

    checkpoint = CaliberRollbackCheckpoint(
        checkpoint_id=new_checkpoint_id(),
        approval_id=approval.approval_id,
        agent_id=approval.agent_id,
        artifact_type="prompt",
        artifact_name=approval.agent_id,
        artifact_ref_before=(
            f"prompts:/{approval.agent_id}@{target_alias}#{previous_version}"
            if previous_version is not None
            else None
        ),
        artifact_ref_after=artifact_ref,
        version_before=previous_version,
        version_after=source_version,
        snapshot_payload={
            "promotion_type": "prompt_alias",
            "target_alias": target_alias,
            "source_version": source_version,
            **assistant_context,
        },
    )
    session.add(checkpoint)

    rotated_at = datetime.now(timezone.utc)
    promotion_payload: dict[str, object] = {
        "artifact_ref": artifact_ref,
        "rotated_at": rotated_at.isoformat(),
        "details": {
            **alias_result,
            "approval_id": approval.approval_id,
            "checkpoint_ids": [checkpoint.checkpoint_id],
            "promotion_type": "prompt_alias",
            **assistant_context,
        },
        "bundle_size": 1,
        "bundle_results": [
            {
                "agent_id": approval.agent_id,
                "artifact_type": "prompt",
                "artifact_ref": artifact_ref,
            }
        ],
    }

    audit_record(
        session,
        actor=actor,
        action="promote",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "approval_id": approval.approval_id,
            "artifact_ref": artifact_ref,
            "rotated_at": rotated_at.isoformat(),
            "checkpoint_ids": [checkpoint.checkpoint_id],
            "promotion_type": "prompt_alias",
            "source_version": source_version,
            "target_alias": target_alias,
            **assistant_context,
            **(extra_audit or {}),
        },
    )
    # Also record the promotion on the *prompt* entity so it shows up in the
    # same ``promote_prompt`` audit trail that ``rollback_prompt`` walks. Without
    # this, an alias rotation applied through the assistant path is invisible to
    # prompt rollback and the operator can't undo it via the Version panel.
    audit_record(
        session,
        actor=actor,
        action="promote_prompt",
        entity_type="prompt",
        entity_id=approval.agent_id,
        details={
            "alias": target_alias,
            "from_version": previous_version,
            "to_version": source_version,
            "promotion_type": "prompt_alias",
            "approval_id": approval.approval_id,
            **assistant_context,
        },
    )
    return ApplyOutcome(promotion=promotion_payload, checkpoint_ids=[checkpoint.checkpoint_id])
