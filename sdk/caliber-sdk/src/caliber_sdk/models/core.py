"""Typed models for the core admin surfaces: auth, identity, capabilities, settings.

Mirrors the server schemas formalized in M0-PR2 (``caliber.schemas``). Kept as
dataclasses rather than pydantic models so installing this SDK stays a two-
dependency affair — the isolation the separate distribution exists to provide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Identity:
    """Who the caller is, from ``GET /me``.

    Note the server reports identity rather than requiring it: an invalid or
    revoked credential yields ``user_id == "anonymous"`` with no scopes rather
    than an error. :meth:`is_anonymous` is the check to make.
    """

    user_id: str = ""
    scopes: list[str] = field(default_factory=list)
    is_admin: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_anonymous(self) -> bool:
        return self.user_id == "anonymous" or not self.user_id


@dataclass
class SessionInfo:
    """How the caller's identity was established, from ``GET /auth/session``."""

    user_id: str = ""
    scopes: list[str] = field(default_factory=list)
    is_admin: bool = False
    auth_mode: str = ""
    authenticated_by: str = ""
    login_required: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Account:
    """A user account. Never carries a password hash."""

    user_id: str = ""
    disabled: bool = False
    created_at: str | None = None
    password_updated_at: str | None = None
    last_login_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PersonalAccessToken:
    """Token metadata. Never carries the secret.

    The plaintext lives on :class:`IssuedToken`, which only the issue and
    rotate calls return — mirroring the server, where a listed token has no
    ``token`` key at all rather than a null one.
    """

    token_id: str = ""
    user_id: str = ""
    name: str = ""
    scopes: list[str] = field(default_factory=list)
    created_at: str | None = None
    created_by: str | None = None
    expires_at: str | None = None
    last_used_at: str | None = None
    revoked_at: str | None = None
    revoked_reason: str | None = None
    rotated_from: str | None = None
    active: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class IssuedToken(PersonalAccessToken):
    """A freshly issued token. ``token`` is returned exactly once, ever."""

    token: str = ""


@dataclass
class WorkflowRunCapabilities:
    """Which workflow-run features the deployment has switched on."""

    queue_enabled: bool = False
    supports_async_submit: bool = False
    supports_cancel: bool = False
    supports_retry: bool = False
    supports_resume: bool = False
    runtime_approvals_enabled: bool = False
    checkpointing_enabled: bool = False
    event_backend: str = ""
    approval_readiness: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Capabilities:
    """Runtime feature flags plus the SDK stability tiers.

    ``artifact_families`` is deliberately left as a mapping: the server
    documents that each family means something different by the same key
    (``rollback`` in particular), so flattening it here would imply a
    uniformity the platform does not have.
    """

    workflow_runs: WorkflowRunCapabilities = field(default_factory=WorkflowRunCapabilities)
    sync_workflow_version_run: bool = True
    artifact_families: dict[str, Any] = field(default_factory=dict)
    sdk_stability: dict[str, list[str]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def tier_of(self, tag: str) -> str | None:
        """Which stability tier an API tag falls in, or ``None`` if unknown."""
        for tier, tags in self.sdk_stability.items():
            if tag in tags:
                return tier
        return None

    def is_ga(self, tag: str) -> bool:
        return self.tier_of(tag) == "ga"


@dataclass
class LlmSetupStatus:
    """Which LLM credentials are configured — presence, never values.

    The server returns masked fingerprints only. A field here that looked like
    a key would misrepresent what the endpoint is willing to disclose.
    """

    llm_provider: str = ""
    gateway_url: str = ""
    openai_key_env: str | None = None
    openai_key_present: bool = False
    anthropic_key_present: bool = False
    assistant_engine: str = ""
    openai_key_fingerprint: str | None = None
    anthropic_key_fingerprint: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeSettingsSummary:
    total: int = 0
    live_editable: int = 0
    environment_managed: int = 0
    configured: int = 0
    defaults: int = 0
    secret_sources: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuntimeSettings:
    """Grouped inventory of runtime configuration knobs."""

    summary: RuntimeSettingsSummary = field(default_factory=RuntimeSettingsSummary)
    groups: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Project:
    """A project/workspace.

    ``file_count`` is present on list responses and absent on detail ones —
    the server computes it with one grouped query for the list only. ``None``
    means "not reported here", which is why it is not defaulted to 0.
    """

    project_id: str = ""
    name: str = ""
    description: str | None = None
    owner: str = ""
    status: str = ""
    storage_backend: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    file_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectFile:
    """One stored file."""

    file_id: str = ""
    file_ref: str | None = None
    name: str = ""
    kind: str | None = None
    relative_path: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    etag: str | None = None
    object_version_id: str | None = None
    version: int | None = None
    status: str | None = None
    storage_backend: str | None = None
    producer_node_id: str | None = None
    project_id: str | None = None
    workflow_run_id: str | None = None
    playground_run_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    immutable_ref: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProjectFolder:
    path: str = ""
    name: str | None = None
    file_ref: str | None = None
    storage_backend: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "Account",
    "Capabilities",
    "Identity",
    "IssuedToken",
    "LlmSetupStatus",
    "PersonalAccessToken",
    "Project",
    "ProjectFile",
    "ProjectFolder",
    "RuntimeSettings",
    "RuntimeSettingsSummary",
    "SessionInfo",
    "WorkflowRunCapabilities",
]
