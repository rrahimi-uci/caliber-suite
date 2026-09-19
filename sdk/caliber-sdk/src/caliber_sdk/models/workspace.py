"""Typed models for the Workspace revision/import lifecycle (`P6-B`).

Mirrors the server schemas in ``caliber.schemas`` (``WorkspaceImportJobSchema``,
``WorkspaceRevisionSchema``, ``WorkspaceRevisionResourceSchema``). Kept as
dataclasses rather than pydantic models for the same reason as
:mod:`.core` -- installing this SDK stays a two-dependency affair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: ``WorkspaceImportJobSchema.status`` values that will never advance on
#: their own -- a caller waiting past one of these would block until timeout.
IMPORT_JOB_TERMINAL_STATES = frozenset({"succeeded", "failed", "reconcile_required"})

#: ``WorkspaceReleaseEvaluationSchema.status`` values a worker will never
#: move past on its own.
RELEASE_EVALUATION_TERMINAL_STATES = frozenset({"succeeded", "failed"})

#: ``WorkspaceReleaseOperationSchema.status`` values that will never advance
#: on their own. ``reconcile_required`` is terminal for the same reason it is
#: for :class:`WorkspaceImportJob` -- it needs a caller to act
#: (:meth:`ProjectReleaseOperationsAPI.observe`), not more waiting.
RELEASE_OPERATION_TERMINAL_STATES = frozenset(
    {"applied", "failed", "reconcile_required", "cancelled"}
)


@dataclass
class WorkspaceImportJob:
    """Durable source-to-revision import intent."""

    import_job_id: str = ""
    project_id: str = ""
    source_id: str = ""
    repository: str = ""
    commit_sha: str = ""
    upload_sha256: str | None = None
    source_bundle_sha256: str | None = None
    source_snapshot_file_id: str | None = None
    manifest_sha256: str | None = None
    status: str = ""
    revision_id: str | None = None
    idempotency_key: str = ""
    attempt_count: int = 0
    max_attempts: int = 0
    claimed_by: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    last_heartbeat_at: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    created_by: str = ""
    updated_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    completed_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in IMPORT_JOB_TERMINAL_STATES


@dataclass
class WorkspaceSource:
    """A project's configured Git-backed source-control binding."""

    source_id: str = ""
    project_id: str = ""
    provider: str = ""
    provider_host: str = ""
    canonical_repository_id: str = ""
    display_path: str = ""
    default_branch: str = ""
    root_path: str = ""
    manifest_path: str = ""
    import_mode: str = ""
    status: str = ""
    has_connection: bool = False
    external_review_policy_version: str = ""
    provider_ruleset_sha256: str | None = None
    last_verified_at: str | None = None
    last_reconciled_at: str | None = None
    updated_at: str | None = None
    etag: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceSourceState:
    """Source mode plus its optional configured binding.

    ``source`` is ``None`` for a ``caliber_managed`` project -- one that has
    never called :meth:`~caliber_sdk.resources.projects.ProjectSourceAPI.configure`.
    """

    source_mode: str = "caliber_managed"
    source: WorkspaceSource | None = None


@dataclass
class WorkspaceSourceCapabilities:
    """Provider-neutral capability snapshot; never includes credentials."""

    source_id: str = ""
    provider: str = ""
    provider_host: str = ""
    available: bool = False
    capabilities: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    last_verified_at: str | None = None


@dataclass
class WorkspaceSourceConnection:
    """A project's configured GitHub App connection for its source binding
    (`P4-E`).

    Mirrors the server's own ``WorkspaceSourceConnectionSchema`` -- a
    secret-free projection by construction. ``private_key``/``webhook_secret``
    are accepted by :meth:`~caliber_sdk.resources.projects.ProjectSourceConnectionAPI.configure`
    but never echoed back by any read: there is no field for either here
    because the server response never contains one.
    """

    connection_id: str = ""
    source_id: str = ""
    project_id: str = ""
    provider: str = ""
    app_id: str = ""
    installation_id: str = ""
    status: str = ""
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class WorkspaceSourceReconciliationResult:
    """Result of one webhook-delivery reconciliation pass against the bound
    connection's GitHub App delivery log (`P4-E`).

    ``missed_delivery_ids``/``redelivery_requested_ids`` carry
    provider-assigned delivery guids (the same value GitHub sends as
    ``X-GitHub-Delivery``) -- identifiers, not secret material.
    """

    checked: int = 0
    missed_delivery_ids: list[str] = field(default_factory=list)
    redelivery_requested_ids: list[str] = field(default_factory=list)


@dataclass
class WorkspaceImportReconciliation:
    """An explicit observation of an ambiguous local import snapshot."""

    job: WorkspaceImportJob = field(default_factory=WorkspaceImportJob)
    observed: bool = False
    observation: str = ""


@dataclass
class WorkspaceRevisionResource:
    """One exact resource pin in an immutable revision."""

    resource_pin_id: str = ""
    revision_id: str = ""
    resource_type: str = ""
    logical_name: str = ""
    resource_id: str = ""
    version_ref: str = ""
    content_sha256: str = ""
    source_path: str | None = None
    source_sha256: str | None = None
    provider_ref: str | None = None
    snapshot_file_id: str | None = None
    snapshot_sha256: str | None = None
    purpose: str = ""
    resolution: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceRevision:
    """Revision metadata and its exact resource pins."""

    revision_id: str = ""
    project_id: str = ""
    revision_number: int = 0
    source_id: str | None = None
    source_commit_sha: str | None = None
    # 'git' (the only kind before this field existed) or 'managed' (created
    # via `.snapshot()` -- no Git commit/bundle, see manifest_sha256/
    # source_bundle_sha256 below).
    source_kind: str = "git"
    manifest: dict[str, Any] = field(default_factory=dict)
    # Git-import-only digests; ``None`` for a 'managed' revision, which has
    # neither a committed manifest file nor an uploaded source bundle.
    # ``revision_sha256`` remains the integrity anchor for both kinds.
    manifest_sha256: str | None = None
    source_bundle_sha256: str | None = None
    source_snapshot_file_id: str | None = None
    source_attestation: str = ""
    revision_sha256: str = ""
    status: str = ""
    validation_report: dict[str, Any] | None = None
    created_by: str = ""
    validated_by: str | None = None
    validated_at: str | None = None
    created_at: str | None = None
    resources: list[WorkspaceRevisionResource] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceRevisionDiff:
    """Deterministic base-to-candidate revision difference."""

    base_revision_id: str = ""
    revision_id: str = ""
    manifest_changed: bool = False
    source_bundle_changed: bool = False
    source_commit_changed: bool = False
    added: list[WorkspaceRevisionResource] = field(default_factory=list)
    removed: list[WorkspaceRevisionResource] = field(default_factory=list)
    changed: list[WorkspaceRevisionResource] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceChangeRequestHead:
    """One generation of a Change Request's reviewed revision pointer."""

    head_id: str = ""
    change_request_id: str = ""
    generation: int = 0
    revision_id: str = ""
    revision_sha256: str = ""
    review_policy_version: str = ""
    review_policy_sha256: str = ""
    changed_by: str = ""
    change_summary: str = ""
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceChangeRequest:
    """A revision proposed for review, promotion through a fixed status
    machine (``draft`` -> ``open`` -> ... -> ``accepted``/``closed``)."""

    change_request_id: str = ""
    project_id: str = ""
    base_revision_id: str | None = None
    current_head_revision_id: str = ""
    created_by: str = ""
    title: str = ""
    description: str = ""
    head_generation: int = 0
    status: str = ""
    review_backend: str = ""
    accepted_at: str | None = None
    accepted_by: str | None = None
    closed_reason: str | None = None
    lock_version: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    current_head: WorkspaceChangeRequestHead = field(default_factory=WorkspaceChangeRequestHead)
    # A version claim is server-side bookkeeping (``caliber.schemas`` itself
    # types it as a plain dict, not a nested schema), so it stays a raw dict
    # here too rather than gaining a dataclass this SDK would have to keep in
    # lockstep with an internal shape.
    version_claim: dict[str, Any] | None = None
    active_reviewer_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceChangeRequestReviewer:
    """One user assigned to review a Change Request."""

    reviewer_id: str = ""
    change_request_id: str = ""
    user_id: str = ""
    assigned_by: str = ""
    assigned_at: str | None = None
    removed_by: str | None = None
    removed_at: str | None = None
    active: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceChangeRequestComment:
    """One comment on a Change Request, optionally anchored to a resource."""

    comment_id: str = ""
    change_request_id: str = ""
    head_id: str | None = None
    resource_type: str | None = None
    resource_name: str | None = None
    source_path: str | None = None
    body: str = ""
    author: str = ""
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceChangeRequestCheck:
    """One automated check run against a Change Request head."""

    check_id: str = ""
    head_id: str = ""
    check_name: str = ""
    attempt_number: int = 0
    implementation_version: str = ""
    input_digest: str = ""
    evidence_ref: str | None = None
    evidence_digest: str | None = None
    status: str = ""
    claimed_by: str | None = None
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    completed_at: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceChangeRequestReview:
    """One reviewer's approve/request-changes decision on a specific head."""

    review_id: str = ""
    change_request_id: str = ""
    head_id: str = ""
    reviewer_id: str = ""
    decision: str = ""
    rationale: str = ""
    actor_role: str = ""
    actor_scopes: list[str] = field(default_factory=list)
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceExternalReviewAttestation:
    """A verified provider-side (e.g. GitHub PR) review, mapped onto a head."""

    attestation_id: str = ""
    change_request_id: str = ""
    head_id: str = ""
    source_id: str = ""
    provider_change_request_id: str = ""
    provider_url: str | None = None
    provider_head_commit: str = ""
    provider_resulting_commit: str = ""
    source_tree_sha256: str = ""
    workspace_revision_sha256: str = ""
    policy_version: str = ""
    policy_sha256: str = ""
    provider_ruleset_sha256: str | None = None
    required_checks: list[str] = field(default_factory=list)
    trusted_check_sources: list[str] = field(default_factory=list)
    check_conclusions: dict[str, Any] = field(default_factory=dict)
    review_actors: list[dict[str, Any]] = field(default_factory=list)
    merge_method: str | None = None
    merge_actor: str | None = None
    merged_at: str | None = None
    provider_event_ids: list[str] = field(default_factory=list)
    adapter_version: str = ""
    verified_at: str | None = None
    status: str = ""
    reason: str = ""
    verification_input_digest: str = ""
    coverage_digest: str | None = None
    uncovered_commits: list[str] = field(default_factory=list)
    uncovered_paths: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceVersionTag:
    """An immutable semantic-version claim recorded against a revision."""

    tag_id: str = ""
    project_id: str = ""
    revision_id: str = ""
    change_request_id: str = ""
    tag: str = ""
    kind: str = ""
    created_by: str = ""
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceRelease:
    """A Workspace release's evaluation/decision state machine record
    (`P5-A` through `P5-F`) -- ``draft`` -> ``evaluating`` ->
    ``{blocked, rejected, approved, awaiting_quality_signoff}`` ->
    ``awaiting_approval`` -> ``{approved, rejected}``."""

    release_id: str = ""
    project_id: str = ""
    revision_id: str = ""
    environment_id: str = ""
    change_request_id: str | None = None
    change_request_head_id: str | None = None
    version_tag_id: str | None = None
    predecessor_release_id: str | None = None
    environment_config_sha256: str = ""
    runtime_dependencies_sha256: str = ""
    policy_sha256: str = ""
    request_idempotency_key: str = ""
    evaluation_evidence_sha256: str | None = None
    decision_set_sha256: str | None = None
    status: str = ""
    requested_by: str = ""
    requested_at: str | None = None
    evaluated_by: str | None = None
    evaluated_at: str | None = None
    lock_version: int = 0
    error_code: str | None = None
    error_summary: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceReleaseEvaluation:
    """One durable evaluation attempt against a release's pinned digests."""

    evaluation_id: str = ""
    runtime_lineage_id: str | None = None
    project_id: str = ""
    workspace_release_id: str = ""
    idempotency_key: str = ""
    evaluation_plan_sha256: str = ""
    input_sha256: str = ""
    status: str = ""
    attempt_number: int = 0
    claimed_by: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    linked_evaluation_run_ids: list[str] = field(default_factory=list)
    gate_verdict_id: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    requested_by: str = ""
    requested_at: str | None = None
    started_by: str | None = None
    started_at: str | None = None
    completed_by: str | None = None
    completed_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in RELEASE_EVALUATION_TERMINAL_STATES


@dataclass
class WorkspaceReleaseEvidence:
    """One piece of evidence (an evaluation run, a gate verdict, ...) recorded
    against a release, some of which a decision must reference to be valid."""

    evidence_id: str = ""
    workspace_release_id: str = ""
    runtime_lineage_id: str | None = None
    kind: str = ""
    evidence_ref: str = ""
    evidence_sha256: str = ""
    required: bool = False
    recorded_by: str = ""
    recorded_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceReleaseDecision:
    """A digest-bound quality or release go/no-go decision, snapshotting the
    deciding actor's role and scopes at decision time."""

    decision_id: str = ""
    workspace_release_id: str = ""
    kind: str = ""
    decision: str = ""
    change_request_head_id: str | None = None
    rationale: str = ""
    decided_by: str = ""
    actor_role_snapshot: dict[str, Any] = field(default_factory=dict)
    effective_scope_snapshot: dict[str, Any] = field(default_factory=dict)
    revision_sha256: str = ""
    environment_config_sha256: str = ""
    runtime_dependencies_sha256: str = ""
    gate_evidence_sha256: str = ""
    policy_sha256: str = ""
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceBreakGlassApplyResult:
    """What a break-glass apply call actually returns.

    Deliberately thin: the server hands back only enough to look up the
    resulting authorization and operation (``authorization_id``,
    ``operation_id``), not the full authorization record -- fetch that
    separately if needed rather than expecting it to ride along here.
    """

    authorization_id: str = ""
    operation_id: str = ""


@dataclass
class WorkspaceReleaseOperation:
    """One durable apply-or-rollback intent against a release
    (`P5-C`), executed and reconciled through :class:`ProjectReleaseOperationsAPI`."""

    operation_id: str = ""
    project_id: str = ""
    workspace_release_id: str = ""
    environment_id: str = ""
    runtime_lineage_id: str | None = None
    kind: str = ""
    target_release_id: str | None = None
    idempotency_key: str = ""
    expected_current_release_id: str | None = None
    expected_environment_lock_version: int = 0
    status: str = ""
    lock_version: int = 0
    requested_by: str = ""
    requested_at: str | None = None
    applied_by: str | None = None
    applied_at: str | None = None
    completed_by: str | None = None
    completed_at: str | None = None
    observation_count: int = 0
    last_observed_at: str | None = None
    break_glass_authorization_id: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in RELEASE_OPERATION_TERMINAL_STATES


@dataclass
class WorkspaceReleaseOperationItem:
    """One resource-level step (bind/promote/activate/publish/verify) within
    a release operation, tracked separately since a partial failure leaves
    some items applied and others not."""

    operation_item_id: str = ""
    workspace_release_operation_id: str = ""
    revision_resource_id: str = ""
    action: str = ""
    target_ref: str = ""
    before_ref: str | None = None
    after_ref: str | None = None
    status: str = ""
    provider_operation_ref: str | None = None
    provider_result: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceReleaseOperationResult:
    """An operation plus its per-resource items, the shape every
    create/get/apply/observe/cancel-expired call on
    :class:`ProjectReleaseOperationsAPI` returns."""

    operation: WorkspaceReleaseOperation = field(default_factory=WorkspaceReleaseOperation)
    items: list[WorkspaceReleaseOperationItem] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        return self.operation.is_terminal


__all__ = [
    "IMPORT_JOB_TERMINAL_STATES",
    "RELEASE_EVALUATION_TERMINAL_STATES",
    "RELEASE_OPERATION_TERMINAL_STATES",
    "WorkspaceBreakGlassApplyResult",
    "WorkspaceChangeRequest",
    "WorkspaceChangeRequestCheck",
    "WorkspaceChangeRequestComment",
    "WorkspaceChangeRequestHead",
    "WorkspaceChangeRequestReview",
    "WorkspaceChangeRequestReviewer",
    "WorkspaceExternalReviewAttestation",
    "WorkspaceImportJob",
    "WorkspaceImportReconciliation",
    "WorkspaceRelease",
    "WorkspaceReleaseDecision",
    "WorkspaceReleaseEvaluation",
    "WorkspaceReleaseEvidence",
    "WorkspaceReleaseOperation",
    "WorkspaceReleaseOperationItem",
    "WorkspaceReleaseOperationResult",
    "WorkspaceRevision",
    "WorkspaceRevisionDiff",
    "WorkspaceRevisionResource",
    "WorkspaceSource",
    "WorkspaceSourceCapabilities",
    "WorkspaceSourceState",
    "WorkspaceVersionTag",
]
