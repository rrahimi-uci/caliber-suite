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


__all__ = [
    "IMPORT_JOB_TERMINAL_STATES",
    "WorkspaceImportJob",
    "WorkspaceImportReconciliation",
    "WorkspaceRevision",
    "WorkspaceRevisionDiff",
    "WorkspaceRevisionResource",
    "WorkspaceSource",
    "WorkspaceSourceCapabilities",
    "WorkspaceSourceState",
]
