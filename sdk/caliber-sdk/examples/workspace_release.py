"""Take a reviewed revision from Change Request to a live, applied release.

The property this demonstrates: a revision moves from "reviewed" to "live"
only through explicit, typed governance steps -- open a Change Request,
evaluate the release against pinned digests, record a quality decision and a
separate approval decision, then apply it. No step here writes to the live
target directly; every state transition is its own auditable call.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def promote_a_reviewed_revision(
    caliber: CaliberClient,
    *,
    project_id: str = "PRJ-1",
    revision_id: str = "WSR-1",
    environment_id: str = "development",
) -> dict[str, Any]:
    """Open a Change Request, evaluate a release, sign off, approve, and apply it."""
    change_request = caliber.workspaces.change_requests.create(
        project_id,
        title="Tighten the intake prompt",
        head_revision_id=revision_id,
        semantic_version="1.1.0",
    )
    caliber.workspaces.change_requests.submit(project_id, change_request.change_request_id)

    release = caliber.workspaces.releases.create(
        project_id,
        revision_id=revision_id,
        environment_id=environment_id,
        environment_config_sha256="a" * 64,
        runtime_dependencies_sha256="b" * 64,
        policy_sha256="c" * 64,
        request_idempotency_key=f"release-{revision_id}",
        change_request_id=change_request.change_request_id,
    )

    # Evaluation is a durable, worker-claimed attempt -- not something this
    # call runs inline -- so waiting for it is a separate, explicit step.
    evaluation = caliber.workspaces.releases.evaluate(
        project_id,
        release.release_id,
        idempotency_key=f"eval-{release.release_id}",
        evaluation_plan_sha256="d" * 64,
        input_sha256="e" * 64,
    )
    finished = caliber.workspaces.releases.wait_for_evaluation(
        project_id, release.release_id, evaluation.evaluation_id
    )

    # Quality and release approval are two distinct decisions -- a QA
    # reviewer's go/no-go, and a separate owner sign-off -- not one click.
    caliber.workspaces.releases.quality_signoff(
        project_id,
        release.release_id,
        decision="go",
        gate_evidence_sha256="f" * 64,
    )
    approval = caliber.workspaces.releases.approve(
        project_id,
        release.release_id,
        decision="go",
        gate_evidence_sha256="f" * 64,
    )

    operation = caliber.workspaces.release_operations.create(
        project_id,
        release.release_id,
        kind="apply",
        idempotency_key=f"apply-{release.release_id}",
        expected_environment_lock_version=1,
    )
    caliber.workspaces.release_operations.apply(
        project_id, release.release_id, operation.operation.operation_id
    )
    settled = caliber.workspaces.release_operations.wait(
        project_id, release.release_id, operation.operation.operation_id
    )

    return {
        "change_request_id": change_request.change_request_id,
        "release_id": release.release_id,
        "evaluation_status": finished.status,
        "approval_decision": approval.decision,
        "operation_status": settled.operation.status,
    }
