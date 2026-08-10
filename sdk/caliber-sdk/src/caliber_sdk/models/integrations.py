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
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["Bucket", "KnowledgeBase", "McpServer", "StoredObject"]
