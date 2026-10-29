"""Resolve and validate the immutable inputs for a Workspace release evaluation.

The release queue stores digests, not a second copy of a workflow run
manifest.  This module turns the revision's own resource pins into one
provider request and verifies every pin again immediately before dispatch.
That is the important boundary for B10.2: a release must never evaluate a
current alias, an unrelated run manifest, or a mutable resource that drifted
after the revision was created.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.eval.provider import EvalRequest, describe_provider
from caliber.workflows.manifest import canonical_json
from caliber.workspace_release_adapters import (
    SnapshotPin,
    WorkspaceReleaseAdapterError,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_service import WorkspaceReleaseConflictError

_PLAN_SCHEMA = "workspace-release-evaluation/v1"
_ROLE_KEY = "release_evaluation"
_VALID_CANDIDATE_TYPES = frozenset({"prompt", "workflow", "skill"})


class WorkspaceReleaseEvaluationPlanError(WorkspaceReleaseConflictError):
    """A release cannot be evaluated from its immutable revision pins."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkspaceReleaseEvaluationPlan:
    """A fully resolved, digest-bound request for one provider invocation."""

    request: EvalRequest
    provider_info: dict[str, object]
    plan_digest: str
    input_digest: str
    min_aggregate_score: float
    min_aggregate_delta: float | None
    candidate_pin_id: str
    dataset_pin_id: str
    judge_pin_id: str


def _sha256_json(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_plan_invalid", f"{label} must be an object"
        )
    return value


def _role(pin: CaliberWorkspaceRevisionResource) -> str | None:
    metadata = pin.resolution.get(_ROLE_KEY) if isinstance(pin.resolution, Mapping) else None
    if isinstance(metadata, Mapping):
        value = metadata.get("role")
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    if pin.purpose == "candidate":
        return "candidate"
    if pin.purpose == "baseline":
        return "baseline"
    if pin.purpose == "evaluation":
        if pin.resource_type == "eval_dataset":
            return "dataset"
        if pin.resource_type == "judge":
            return "judge"
    return None


def _one(
    pins: list[CaliberWorkspaceRevisionResource],
    *,
    role: str,
    required: bool = True,
) -> CaliberWorkspaceRevisionResource | None:
    matches = [pin for pin in pins if _role(pin) == role]
    if len(matches) > 1:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_plan_ambiguous", f"revision contains multiple {role} pins"
        )
    if not matches and required:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_plan_missing_pin", f"revision is missing its {role} pin"
        )
    return matches[0] if matches else None


def _content(resolved: object, pin: CaliberWorkspaceRevisionResource) -> str:
    """Extract only adapter-owned immutable content, never arbitrary DB metadata."""

    template = getattr(resolved, "template", None)
    if isinstance(template, str) and template:
        return template
    manifest = getattr(resolved, "manifest", None)
    if isinstance(manifest, Mapping):
        return canonical_json(cast(dict[str, Any], dict(manifest)))
    content = getattr(resolved, "content", None)
    if isinstance(content, str) and content:
        return content
    raise WorkspaceReleaseEvaluationPlanError(
        "evaluation_candidate_content_unavailable",
        f"adapter for {pin.resource_type!r} did not return evaluation content",
    )


def _config(
    revision: CaliberWorkspaceRevision, dataset_pin: CaliberWorkspaceRevisionResource
) -> Mapping[str, Any]:
    raw = revision.manifest.get("evaluation") if isinstance(revision.manifest, Mapping) else None
    if raw is None and isinstance(dataset_pin.resolution, Mapping):
        raw = dataset_pin.resolution.get(_ROLE_KEY)
    if raw is None:
        return {}
    return _mapping(raw, label="evaluation configuration")


def _number(config: Mapping[str, Any], key: str, default: float) -> float:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_plan_invalid", f"evaluation configuration {key!r} must be numeric"
        )
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_plan_invalid", f"evaluation configuration {key!r} must be between 0 and 1"
        )
    return result


def _optional_number(config: Mapping[str, Any], key: str) -> float | None:
    if key not in config or config[key] is None:
        return None
    return _number(config, key, 0.0)


def _resolve_pin(
    session: Session,
    *,
    project: CaliberProject,
    environment: CaliberWorkspaceEnvironment,
    pin: CaliberWorkspaceRevisionResource,
    identity: CaliberIdentity,
    registry: WorkspaceResourceAdapterRegistry,
) -> object:
    try:
        adapter = registry.require(pin.resource_type)
        adapter.validate(session, pin, environment)
        resolved = adapter.resolve(
            session,
            project,
            {"resource_id": pin.resource_id, "version_ref": pin.version_ref},
            identity,
        )
        snapshot = adapter.snapshot(session, resolved)
    except WorkspaceReleaseAdapterError as exc:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_pin_unavailable",
            f"could not resolve pinned {pin.resource_type!r} resource",
        ) from exc
    except Exception as exc:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_pin_unavailable",
            f"could not resolve pinned {pin.resource_type!r} resource",
        ) from exc
    if not isinstance(snapshot, SnapshotPin):
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_pin_invalid",
            f"adapter for {pin.resource_type!r} did not return a snapshot pin",
        )
    if snapshot.resource_id != pin.resource_id or snapshot.version_ref != pin.version_ref:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_pin_changed",
            f"pinned {pin.resource_type!r} identity changed during resolution",
        )
    if snapshot.content_sha256 != pin.content_sha256:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_pin_changed",
            f"pinned {pin.resource_type!r} content no longer matches its digest",
        )
    return resolved


def resolve_workspace_release_evaluation_plan(  # noqa: PLR0915 - ordered resolution contract is intentionally explicit
    session: Session,
    *,
    release: CaliberWorkspaceRelease,
    evaluation: CaliberWorkspaceReleaseEvaluation,
    identity: CaliberIdentity,
    adapter_registry: WorkspaceResourceAdapterRegistry,
    provider: object,
    config: Any,
    verify_digests: bool = True,
) -> WorkspaceReleaseEvaluationPlan:
    """Resolve exactly the release revision's candidate, dataset and judge pins."""

    if release.project_id != evaluation.project_id:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_project_mismatch", "evaluation and release belong to different projects"
        )
    revision = session.get(CaliberWorkspaceRevision, release.revision_id)
    project = session.get(CaliberProject, release.project_id)
    environment = session.get(CaliberWorkspaceEnvironment, release.environment_id)
    if revision is None or revision.project_id != release.project_id or revision.status != "ready":
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_revision_unavailable", "release revision is missing, foreign, or not ready"
        )
    if project is None or environment is None or environment.project_id != release.project_id:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_coordinates_unavailable", "release project or environment is unavailable"
        )
    pins = list(
        session.execute(
            select(CaliberWorkspaceRevisionResource)
            .where(CaliberWorkspaceRevisionResource.revision_id == revision.revision_id)
            .order_by(
                CaliberWorkspaceRevisionResource.resource_type,
                CaliberWorkspaceRevisionResource.logical_name,
            )
        ).scalars()
    )
    candidate_pin = _one(pins, role="candidate")
    assert candidate_pin is not None
    if candidate_pin.resource_type not in _VALID_CANDIDATE_TYPES:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_candidate_unsupported",
            f"resource type {candidate_pin.resource_type!r} cannot be evaluated in the reference profile",
        )
    baseline_pin = _one(pins, role="baseline", required=False)
    dataset_pin = _one(pins, role="dataset")
    judge_pin = _one(pins, role="judge")
    assert dataset_pin is not None and judge_pin is not None

    candidate = _resolve_pin(
        session,
        project=project,
        environment=environment,
        pin=candidate_pin,
        identity=identity,
        registry=adapter_registry,
    )
    baseline = (
        _resolve_pin(
            session,
            project=project,
            environment=environment,
            pin=baseline_pin,
            identity=identity,
            registry=adapter_registry,
        )
        if baseline_pin is not None
        else None
    )
    dataset = _resolve_pin(
        session,
        project=project,
        environment=environment,
        pin=dataset_pin,
        identity=identity,
        registry=adapter_registry,
    )
    judge = _resolve_pin(
        session,
        project=project,
        environment=environment,
        pin=judge_pin,
        identity=identity,
        registry=adapter_registry,
    )

    dataset_id = str(getattr(dataset, "dataset_id", ""))
    dataset_version = getattr(dataset, "version", None)
    judge_name = str(getattr(judge, "name", ""))
    if not dataset_id or not isinstance(dataset_version, int) or not judge_name:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_pin_invalid", "dataset or judge adapter returned incomplete identity"
        )
    scorer_name = f"Judge.{judge_name}"
    scorer_config = {
        "instructions": getattr(judge, "instructions", ""),
        "model": getattr(judge, "model", None),
        "feedback_value_type": getattr(judge, "feedback_value_type", None),
    }
    eval_config = _config(revision, dataset_pin)
    provider_info = describe_provider(provider)
    provider_name = str(provider_info["provider"])
    requested_provider = eval_config.get("provider")
    if requested_provider is not None and str(requested_provider) != provider_name:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_provider_mismatch",
            "release evaluation provider does not match the worker provider",
        )
    configured_provider = getattr(config, "eval_provider", None)
    if configured_provider and str(configured_provider).lower() != provider_name.lower():
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_provider_mismatch",
            "worker configuration does not match the evaluation provider",
        )
    candidate_content = _content(candidate, candidate_pin)
    baseline_content = (
        _content(baseline, baseline_pin) if baseline is not None and baseline_pin else None
    )
    request = EvalRequest(
        agent_id=candidate_pin.resource_id,
        job_id=evaluation.evaluation_id,
        artifact_type=candidate_pin.resource_type,
        candidate_content=candidate_content,
        baseline_content=baseline_content,
        eval_dataset_id=dataset_id,
        eval_dataset_version=dataset_version,
        scorer_names=[scorer_name],
        scorer_configs={scorer_name: scorer_config},
        scorer_weights=dict(eval_config.get("scorer_weights", {}))
        if isinstance(eval_config.get("scorer_weights", {}), Mapping)
        else {},
    )

    def pin_descriptor(pin: CaliberWorkspaceRevisionResource) -> dict[str, str]:
        return {
            "resource_pin_id": pin.resource_pin_id,
            "resource_type": pin.resource_type,
            "resource_id": pin.resource_id,
            "version_ref": pin.version_ref,
            "content_sha256": pin.content_sha256,
        }

    plan_descriptor = {
        "schema": _PLAN_SCHEMA,
        "provider": provider_info,
        "candidate": pin_descriptor(candidate_pin),
        "baseline": pin_descriptor(baseline_pin) if baseline_pin is not None else None,
        "dataset": pin_descriptor(dataset_pin),
        "judge": pin_descriptor(judge_pin),
        "scorer_names": request.scorer_names,
        "scorer_configs": request.scorer_configs,
        "scorer_weights": request.scorer_weights,
        "min_aggregate_score": _number(eval_config, "min_aggregate_score", 0.0),
        "min_aggregate_delta": _optional_number(eval_config, "min_aggregate_delta"),
    }
    input_descriptor = {
        "schema": _PLAN_SCHEMA,
        "revision_id": revision.revision_id,
        "environment_id": environment.environment_id,
        "environment_config_sha256": release.environment_config_sha256,
        "runtime_dependencies_sha256": release.runtime_dependencies_sha256,
        "policy_sha256": release.policy_sha256,
        "candidate_content_sha256": hashlib.sha256(candidate_content.encode("utf-8")).hexdigest(),
        "baseline_content_sha256": hashlib.sha256(baseline_content.encode("utf-8")).hexdigest()
        if baseline_content is not None
        else None,
        "dataset_content_sha256": dataset_pin.content_sha256,
        "judge_content_sha256": judge_pin.content_sha256,
    }
    plan_digest = _sha256_json(plan_descriptor)
    input_digest = _sha256_json(input_descriptor)
    if verify_digests and evaluation.evaluation_plan_sha256 != plan_digest:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_plan_digest_mismatch",
            "queued evaluation plan digest does not match release pins",
        )
    if verify_digests and evaluation.input_sha256 != input_digest:
        raise WorkspaceReleaseEvaluationPlanError(
            "evaluation_input_digest_mismatch",
            "queued evaluation input digest does not match release pins",
        )
    return WorkspaceReleaseEvaluationPlan(
        request=request,
        provider_info=provider_info,
        plan_digest=plan_digest,
        input_digest=input_digest,
        min_aggregate_score=_number(eval_config, "min_aggregate_score", 0.0),
        min_aggregate_delta=_optional_number(eval_config, "min_aggregate_delta"),
        candidate_pin_id=candidate_pin.resource_pin_id,
        dataset_pin_id=dataset_pin.resource_pin_id,
        judge_pin_id=judge_pin.resource_pin_id,
    )


__all__ = [
    "WorkspaceReleaseEvaluationPlan",
    "WorkspaceReleaseEvaluationPlanError",
    "resolve_workspace_release_evaluation_plan",
]
