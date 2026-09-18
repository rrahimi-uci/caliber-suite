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
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_sha256: str = ""
    source_bundle_sha256: str = ""
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


__all__ = [
    "IMPORT_JOB_TERMINAL_STATES",
    "WorkspaceChangeRequest",
    "WorkspaceChangeRequestCheck",
    "WorkspaceChangeRequestComment",
    "WorkspaceChangeRequestHead",
    "WorkspaceChangeRequestReview",
    "WorkspaceChangeRequestReviewer",
    "WorkspaceExternalReviewAttestation",
    "WorkspaceImportJob",
    "WorkspaceImportReconciliation",
    "WorkspaceRevision",
    "WorkspaceRevisionDiff",
    "WorkspaceRevisionResource",
    "WorkspaceSource",
    "WorkspaceSourceCapabilities",
    "WorkspaceSourceState",
    "WorkspaceVersionTag",
]
