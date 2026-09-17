"""Provider-neutral Workspace source capability boundary (`P4-C`).

This module intentionally contains no GitHub/GitLab/Bitbucket client. The API
routes depend on this small protocol and a registry supplied by the host. A
provider adapter can therefore be added in `P4-E` without leaking provider
response objects or provider-specific policy into Workspace persistence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from caliber.db.models import CaliberWorkspaceSource


class WorkspaceSourceProviderError(RuntimeError):
    """Base error raised by a provider-neutral source adapter."""

    code = "workspace_source_provider_error"


class WorkspaceSourceProviderUnavailableError(WorkspaceSourceProviderError):
    """No adapter is installed for the configured provider."""

    code = "source_provider_unavailable"


class WorkspaceSourceVerificationError(WorkspaceSourceProviderError):
    """An adapter could not verify the configured source binding."""

    code = "source_verification_failed"


@dataclass(frozen=True)
class WorkspaceSourceVerification:
    """Normalized result returned by any source provider adapter."""

    canonical_repository_id: str
    capabilities: Mapping[str, object]


class WorkspaceSourceProvider(Protocol):
    """The provider-neutral interface consumed by Workspace services."""

    name: str

    def capabilities(self) -> Mapping[str, object]: ...

    def verify(self, source: CaliberWorkspaceSource) -> WorkspaceSourceVerification: ...


class WorkspaceSourceProviderRegistry:
    """Small explicit registry; no implicit network clients are constructed."""

    def __init__(self, providers: Mapping[str, WorkspaceSourceProvider] | None = None) -> None:
        self._providers: dict[str, WorkspaceSourceProvider] = dict(providers or {})

    def register(self, provider: WorkspaceSourceProvider) -> None:
        name = str(provider.name).strip().lower()
        if not name:
            raise ValueError("source provider name must not be empty")
        self._providers[name] = provider

    def get(self, provider_name: str) -> WorkspaceSourceProvider | None:
        return self._providers.get(provider_name.strip().lower())

    def capabilities(self, provider_name: str) -> Mapping[str, object] | None:
        provider = self.get(provider_name)
        if provider is None:
            return None
        try:
            return dict(provider.capabilities())
        except WorkspaceSourceProviderError:
            return None

    def verify(self, source: CaliberWorkspaceSource) -> WorkspaceSourceVerification:
        provider = self.get(source.provider)
        if provider is None:
            raise WorkspaceSourceProviderUnavailableError(
                f"{WorkspaceSourceProviderUnavailableError.code}: no adapter is installed for "
                f"provider {source.provider!r}"
            )
        result = provider.verify(source)
        if result.canonical_repository_id != source.canonical_repository_id:
            raise WorkspaceSourceVerificationError(
                f"{WorkspaceSourceVerificationError.code}: provider identity does not match "
                "the configured canonical repository"
            )
        return WorkspaceSourceVerification(
            canonical_repository_id=result.canonical_repository_id,
            capabilities=dict(result.capabilities),
        )


def source_etag(source: CaliberWorkspaceSource) -> str:
    """Return a stable entity tag over public mutable binding coordinates.

    There is no lock-version column until the later release machinery. Hashing
    the canonical source configuration still gives `If-Match` useful CAS
    semantics for the source lifecycle without exposing connection material.
    """
    payload: dict[str, Any] = {
        "project_id": source.project_id,
        "provider": source.provider,
        "provider_host": source.provider_host,
        "canonical_repository_id": source.canonical_repository_id,
        "display_path": source.display_path,
        "default_branch": source.default_branch,
        "root_path": source.root_path,
        "manifest_path": source.manifest_path,
        "import_mode": source.import_mode,
        "status": source.status,
        "provider_capabilities": source.provider_capabilities or {},
        "external_review_policy_version": source.external_review_policy_version,
        "provider_ruleset_sha256": source.provider_ruleset_sha256,
        # The opaque reference must participate in CAS without ever appearing
        # in the tag itself. Otherwise replacing one connection with another
        # would be an undetectable lost update whenever both references exist.
        "connection_ref_sha256": (
            hashlib.sha256(source.connection_ref.encode("utf-8")).hexdigest()
            if source.connection_ref
            else None
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "WorkspaceSourceProvider",
    "WorkspaceSourceProviderError",
    "WorkspaceSourceProviderRegistry",
    "WorkspaceSourceProviderUnavailableError",
    "WorkspaceSourceVerification",
    "WorkspaceSourceVerificationError",
    "source_etag",
]
