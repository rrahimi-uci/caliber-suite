"""Typed models for the integration and data surfaces (beta tier).

These mirror route payloads whose shapes are less settled than the GA
families — which is what ``beta`` means here. Every model keeps ``extra``, so a
field added while these stabilise is reachable without an SDK release.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class McpServer:
    """A managed MCP server definition.

    ``discovered_tools`` is what the server reported at last connection, not a
    contract: an MCP server can change its tool list, and a stale entry here
    means "we saw this once", which is why ``last_connected_at`` sits beside it.
    """

    server_id: str = ""
    name: str = ""
    description: str | None = None
    transport: str | None = None
    uri: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, Any] = field(default_factory=dict)
    auth_type: str | None = None
    auth_config: dict[str, Any] = field(default_factory=dict)
    tool_policies: dict[str, Any] = field(default_factory=dict)
    icon: str | None = None
    owner: str | None = None
    status: str = ""
    connection_error: str | None = None
    discovered_tools: list[dict[str, Any]] = field(default_factory=list)
    last_connected_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_connected(self) -> bool:
        """Whether the last connection attempt succeeded.

        Not whether it is reachable *now* — that would require a probe, and
        :meth:`McpServersAPI.test_connection` is how you ask.
        """
        return self.connection_error is None and self.last_connected_at is not None


@dataclass
class KnowledgeBase:
    """A versioned RAG corpus."""

    knowledge_base_id: str = ""
    name: str = ""
    description: str | None = None
    owner: str | None = None
    status: str = ""
    active_version_id: str | None = None
    embedding_model: str | None = None
    chunking_strategy: str | None = None
    document_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Bucket:
    """One object-store bucket."""

    name: str = ""
    creation_date: str | None = None
    object_count: int | None = None
    size_bytes: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StoredObject:
    """One object inside a bucket."""

    key: str = ""
    size: int | None = None
    last_modified: str | None = None
    etag: str | None = None
    content_type: str | None = None
    is_directory: bool = False


@dataclass
class OpenApiIntegration:
    """A governed external OpenAPI surface CALIBER has imported for curation.

    This is the control-plane identity — see :class:`OpenApiIntegrationVersion`
    for one pinned imported contract snapshot, and :class:`OpenApiToolDraft` for
    a curated callable derived from it.
    """

    integration_id: str = ""
    name: str = ""
    description: str = ""
    owner: str = ""
    status: str = ""
    project_id: str | None = None
    visibility: str = ""
    last_imported_version_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenApiIntegrationVersion:
    """One pinned imported OpenAPI document."""

    version_id: str = ""
    integration_id: str = ""
    source_kind: str = ""
    source_ref: str = ""
    spec_sha256: str = ""
    openapi_version: str = ""
    title: str = ""
    spec_version: str = ""
    spec_description: str = ""
    server_urls: list[str] = field(default_factory=list)
    auth_schemes: list[str] = field(default_factory=list)
    import_warnings: list[str] = field(default_factory=list)
    operation_count: int = 0
    normalized_summary: dict[str, Any] = field(default_factory=dict)
    dependency_detected_at: str | None = None
    created_by: str = ""
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenApiOperation:
    """One normalized operation extracted from an imported OpenAPI snapshot."""

    operation_id: str = ""
    integration_version_id: str = ""
    operation_key: str = ""
    method: str = ""
    path: str = ""
    spec_operation_id: str | None = None
    summary: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    deprecated: bool = False
    side_effect_level: str = ""
    auth_schemes: list[str] = field(default_factory=list)
    request_body_required: bool = False
    request_content_types: list[str] = field(default_factory=list)
    response_statuses: list[str] = field(default_factory=list)
    normalized_operation: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenApiToolDraft:
    """A curated OpenAPI-derived tool before publication to the tool registry."""

    draft_id: str = ""
    integration_id: str = ""
    integration_version_id: str = ""
    operation_id: str = ""
    additional_operation_ids: list[str] = field(default_factory=list)
    name: str = ""
    description: str = ""
    owner: str = ""
    status: str = ""
    server_url: str = ""
    auth_binding: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    execution_config: dict[str, Any] | None = None
    side_effect_level: str = ""
    requires_approval: bool = False
    allow_in_preview: bool = False
    secret_refs: list[str] = field(default_factory=list)
    published_tool_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenApiOperationDependency:
    """One canonical, typed dependency record between two imported operations.

    The authoritative dependency object: the API graph a deployment can also
    fetch is a derived projection of rows like this one, not the other way
    around.
    """

    dependency_id: str = ""
    integration_version_id: str = ""
    from_operation_id: str = ""
    to_operation_id: str = ""
    dependency_type: str = ""
    confidence: str = ""
    source: str = ""
    required: bool = False
    binding_field_map: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    status: str = ""
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["Bucket", "KnowledgeBase", "McpServer", "StoredObject"]
