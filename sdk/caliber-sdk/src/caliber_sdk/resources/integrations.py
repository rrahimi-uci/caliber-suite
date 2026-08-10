"""MCP servers, the LLM gateway, knowledge bases, and object storage.

All beta: real and supported, but their shapes are still moving. Check
``client.stability`` before depending on one in production code.
"""

from __future__ import annotations

from typing import Any

from ..models._decode import decode, decode_list
from ..models.integrations import Bucket, KnowledgeBase, McpServer, StoredObject
from ._base import Resource

_List = list


class McpServersAPI(Resource):
    """Managed MCP server definitions and governed tool use."""

    def list(self) -> _List[McpServer]:
        return decode_list(McpServer, self._get("/mcp-servers"))

    def get(self, server_id: str) -> McpServer:
        return decode(McpServer, self._get(f"/mcp-servers/{server_id}"))

    def create(self, name: str, **options: Any) -> McpServer:
        return decode(McpServer, self._post("/mcp-servers", json={"name": name, **options}))

    def update(self, server_id: str, **changes: Any) -> McpServer:
        return decode(McpServer, self._patch(f"/mcp-servers/{server_id}", json=changes))

    def delete(self, server_id: str) -> Any:
        return self._delete(f"/mcp-servers/{server_id}")

    def test_connection(self, server_id: str) -> Any:
        """Probe the server now, rather than trusting the last known state."""
        return self._post(f"/mcp-servers/{server_id}/test-connection")

    def discover_tools(self, server_id: str) -> Any:
        """Refresh the tool inventory from the remote server."""
        return self._post(f"/mcp-servers/{server_id}/discover-tools")

    def tools(self, server_id: str) -> Any:
        """The tool inventory as last discovered."""
        return self._get(f"/mcp-servers/{server_id}/tools")

    def invoke_tool(self, server_id: str, tool_name: str, arguments: Any = None) -> Any:
        """Call a remote tool through CALIBER's governed egress path.

        Routed through the server rather than called directly, which is what
        makes tool policy, secret resolution, and audit apply at all.
        """
        return self._post(
            f"/mcp-servers/{server_id}/invoke-tool",
            json={"tool_name": tool_name, "arguments": arguments or {}},
        )


class GatewayAPI(Resource):
    """External LLM gateway discovery, guardrails, and usage."""

    def get(self) -> Any:
        """Discovered endpoints and routing visibility."""
        return self._get("/gateway")

    def usage(self, **params: Any) -> Any:
        """Trace-derived usage. Derived, not metered: it reports what was
        traced, so untraced calls are absent rather than zero."""
        return self._get("/gateway/usage", params=params or None)

    def guardrails(self) -> Any:
        return self._get("/gateway/guardrails")

    def guardrail_catalog(self) -> Any:
        """What this deployment *can* enforce, versus what it does."""
        return self._get("/gateway/guardrails/catalog")

    def create_guardrail(self, **payload: Any) -> Any:
        return self._post("/gateway/guardrails", json=payload)

    def delete_guardrail(self, guardrail_id: str) -> Any:
        return self._delete(f"/gateway/guardrails/{guardrail_id}")

    def attach_guardrail(self, endpoint_id: str, **payload: Any) -> Any:
        return self._post(f"/gateway/endpoints/{endpoint_id}/guardrails", json=payload)


class KnowledgeBasesAPI(Resource):
    """Versioned RAG corpora, retrieval, and calibration."""

    def list(self) -> _List[KnowledgeBase]:
        return decode_list(KnowledgeBase, self._get("/knowledge-bases"))

    def get(self, knowledge_base_id: str) -> KnowledgeBase:
        return decode(KnowledgeBase, self._get(f"/knowledge-bases/{knowledge_base_id}"))

    def create(self, name: str, **options: Any) -> KnowledgeBase:
        return decode(KnowledgeBase, self._post("/knowledge-bases", json={"name": name, **options}))

    def update(self, knowledge_base_id: str, **changes: Any) -> KnowledgeBase:
        return decode(
            KnowledgeBase, self._patch(f"/knowledge-bases/{knowledge_base_id}", json=changes)
        )

    def delete(self, knowledge_base_id: str) -> Any:
        return self._delete(f"/knowledge-bases/{knowledge_base_id}")

    def options(self) -> Any:
        """Embedding models and chunking strategies this deployment offers."""
        return self._get("/knowledge-bases/options")

    def runs(self, knowledge_base_id: str) -> Any:
        return self._get(f"/knowledge-bases/{knowledge_base_id}/runs")

    def calibrate(self, knowledge_base_id: str, **options: Any) -> Any:
        return self._post(f"/knowledge-bases/{knowledge_base_id}/calibrate", json=options)

    def set_baseline(self, knowledge_base_id: str, **options: Any) -> Any:
        return self._post(f"/knowledge-bases/{knowledge_base_id}/baseline", json=options)

    def rollback(self, knowledge_base_id: str, **options: Any) -> Any:
        """Roll back to a prior version.

        Knowledge bases roll back by activation history, not by an alias
        restore — the semantics differ per asset family, and the server is the
        authority on what this one means.
        """
        return self._post(f"/knowledge-bases/{knowledge_base_id}/rollback", json=options)


class ObjectStoreAPI(Resource):
    """S3/MinIO console operations.

    Distinct from ``projects.files``: that is CALIBER's managed file registry
    with lineage and immutable refs, this is the raw bucket browser underneath.
    """

    def buckets(self) -> _List[Bucket]:
        return decode_list(Bucket, self._get("/object-store/buckets"))

    def create_bucket(self, bucket: str) -> Any:
        return self._post("/object-store/buckets", json={"bucket": bucket})

    def delete_bucket(self, bucket: str) -> Any:
        return self._delete(f"/object-store/buckets/{bucket}")

    def objects(self, bucket: str, *, prefix: str | None = None) -> _List[StoredObject]:
        params = {"prefix": prefix} if prefix else None
        payload = self._get(f"/object-store/buckets/{bucket}/objects", params=params)
        items = payload.get("items") if isinstance(payload, dict) else payload
        return decode_list(StoredObject, items)

    def preview(self, bucket: str, key: str) -> Any:
        return self._get(f"/object-store/buckets/{bucket}/object/preview", params={"key": key})

    def extract(self, bucket: str, key: str) -> Any:
        """Extract text/structure from a stored document."""
        return self._get(f"/object-store/buckets/{bucket}/object/extract", params={"key": key})

    def import_object(self, bucket: str, key: str, **options: Any) -> Any:
        """Register a stored object as a managed project file.

        The bridge from raw storage into the governed registry, where it gains
        a content hash and lineage.
        """
        return self._post(
            f"/object-store/buckets/{bucket}/object/import", json={"key": key, **options}
        )

    def delete_object(self, bucket: str, key: str) -> Any:
        return self._delete(f"/object-store/buckets/{bucket}/object", params={"key": key})


__all__ = ["GatewayAPI", "KnowledgeBasesAPI", "McpServersAPI", "ObjectStoreAPI"]
