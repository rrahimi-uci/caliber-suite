"""MCP servers, the LLM gateway, knowledge bases, and object storage.

All beta: real and supported, but their shapes are still moving. Check
``client.stability`` before depending on one in production code.
"""

from __future__ import annotations

from typing import Any

from ..models._decode import decode, decode_list
from ..models.integrations import (
    Bucket,
    KnowledgeBase,
    McpServer,
    OpenApiIntegration,
    OpenApiIntegrationVersion,
    OpenApiOperation,
    OpenApiOperationDependency,
    OpenApiToolDraft,
    StoredObject,
)
from ._base import Resource

_List = list


class McpServersAPI(Resource):
    """Managed MCP server definitions and governed tool use."""

    def list(self) -> _List[McpServer]:
        return decode_list(McpServer, self._get("/mcp-servers"))

    def get(self, server_id: str) -> McpServer:
        return decode(McpServer, self._get(f"/mcp-servers/{server_id}"))

    def history(self, server_id: str) -> Any:
        return self._get(f"/mcp-servers/{server_id}/history")

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

    def update_tool_policy(self, server_id: str, tool_name: str, **policy: Any) -> Any:
        return self._patch(f"/mcp-servers/{server_id}/tools/{tool_name}/policy", json=policy)

    def save_test_cases(
        self, server_id: str, tool_name: str, test_cases: _List[dict[str, Any]]
    ) -> Any:
        return self._put(
            f"/mcp-servers/{server_id}/tools/{tool_name}/test-cases",
            json={"test_cases": test_cases},
        )

    def calibrate_tool(self, server_id: str, tool_name: str) -> Any:
        return self._post(f"/mcp-servers/{server_id}/tools/{tool_name}/calibrate")

    def invoke_tool(self, server_id: str, tool_name: str, arguments: Any = None) -> Any:
        """Call a remote tool through CALIBER's governed egress path.

        Routed through the server rather than called directly, which is what
        makes tool policy, secret resolution, and audit apply at all.
        """
        return self._post(
            f"/mcp-servers/{server_id}/invoke-tool",
            json={"tool_name": tool_name, "arguments": arguments or {}},
        )


class OpenApiIntegrationsAPI(Resource):
    """Governed OpenAPI import, curation, and publication.

    The control-plane pipeline is: create an integration shell, import a pinned
    spec version into it, review the normalized operations and detected
    dependencies, generate tool drafts from selected operations, then publish
    an approved draft into CALIBER's tool registry. Importing a spec never
    creates a runtime tool by itself — ``generate_tool_drafts`` and
    ``publish_tool_draft`` are the two explicit steps that do.
    """

    def list(self, *, status: str | None = None) -> _List[OpenApiIntegration]:
        params = {"status": status} if status else None
        return decode_list(
            OpenApiIntegration, self._get("/openapi-integrations", params=params)
        )

    def get(self, integration_id: str) -> OpenApiIntegration:
        return decode(OpenApiIntegration, self._get(f"/openapi-integrations/{integration_id}"))

    def create(self, name: str, **options: Any) -> OpenApiIntegration:
        return decode(
            OpenApiIntegration,
            self._post("/openapi-integrations", json={"name": name, **options}),
        )

    def update(self, integration_id: str, **changes: Any) -> OpenApiIntegration:
        return decode(
            OpenApiIntegration,
            self._patch(f"/openapi-integrations/{integration_id}", json=changes),
        )

    def archive(self, integration_id: str) -> OpenApiIntegration:
        return decode(
            OpenApiIntegration, self._post(f"/openapi-integrations/{integration_id}/archive")
        )

    def import_spec(
        self,
        integration_id: str,
        *,
        spec_text: str | None = None,
        spec_base64: str | None = None,
        spec_url: str | None = None,
        source_ref: str | None = None,
    ) -> OpenApiIntegrationVersion:
        """Import one OpenAPI 3.x document, pinning it as a new version.

        Exactly one of ``spec_text`` (pasted JSON/YAML), ``spec_base64`` (an
        uploaded file), or ``spec_url`` (fetched over CALIBER's guarded egress
        path) must be given.
        """

        if spec_url is not None:
            body: dict[str, Any] = {"source_kind": "url", "spec_url": spec_url}
        elif spec_base64 is not None:
            body = {"source_kind": "upload", "spec_base64": spec_base64}
        else:
            body = {"source_kind": "inline_text", "spec_text": spec_text}
        if source_ref is not None:
            body["source_ref"] = source_ref
        return decode(
            OpenApiIntegrationVersion,
            self._post(f"/openapi-integrations/{integration_id}/import", json=body),
        )

    def reimport(self, integration_id: str) -> Any:
        """Re-fetch the last imported version's ``url`` source and diff it.

        Only meaningful when the last imported version came from ``spec_url``;
        an inline or uploaded spec has nothing live to re-fetch.
        """
        return self._post(f"/openapi-integrations/{integration_id}/reimport")

    def validate_spec_source(
        self, integration_id: str, *, spec_url: str, source_kind: str = "url"
    ) -> Any:
        """Check whether a spec source is reachable and permitted, without importing it."""
        return self._post(
            f"/openapi-integrations/{integration_id}/validate-spec-source",
            json={"source_kind": source_kind, "spec_url": spec_url},
        )

    def versions(self, integration_id: str) -> _List[OpenApiIntegrationVersion]:
        return decode_list(
            OpenApiIntegrationVersion,
            self._get(f"/openapi-integrations/{integration_id}/versions"),
        )

    def version(self, integration_id: str, version_id: str) -> OpenApiIntegrationVersion:
        return decode(
            OpenApiIntegrationVersion,
            self._get(f"/openapi-integrations/{integration_id}/versions/{version_id}"),
        )

    def diff_version(
        self, integration_id: str, version_id: str, *, compare_to_version_id: str | None = None
    ) -> Any:
        """Diff one pinned version against another, defaulting to its predecessor."""
        body = {"compare_to_version_id": compare_to_version_id} if compare_to_version_id else {}
        return self._post(
            f"/openapi-integrations/{integration_id}/versions/{version_id}/diff", json=body
        )

    def list_operations(
        self, integration_id: str, *, version_id: str | None = None
    ) -> _List[OpenApiOperation]:
        params = {"version_id": version_id} if version_id else None
        return decode_list(
            OpenApiOperation,
            self._get(f"/openapi-integrations/{integration_id}/operations", params=params),
        )

    def get_operation(self, integration_id: str, operation_id: str) -> OpenApiOperation:
        return decode(
            OpenApiOperation,
            self._get(f"/openapi-integrations/{integration_id}/operations/{operation_id}"),
        )

    def list_dependencies(
        self,
        integration_id: str,
        *,
        version_id: str | None = None,
        status: str | None = None,
    ) -> _List[OpenApiOperationDependency]:
        """Canonical dependency rows — the source of truth the API graph derives from."""
        params: dict[str, Any] = {}
        if version_id is not None:
            params["version_id"] = version_id
        if status is not None:
            params["status"] = status
        return decode_list(
            OpenApiOperationDependency,
            self._get(
                f"/openapi-integrations/{integration_id}/dependencies", params=params or None
            ),
        )

    def review_dependency(
        self,
        integration_id: str,
        dependency_id: str,
        *,
        status: str,
        notes: str | None = None,
    ) -> OpenApiOperationDependency:
        """Confirm or reject one suggested/advisory dependency (``status`` is
        ``"confirmed"`` or ``"rejected"``). A high-confidence, already
        auto-wired row cannot be reviewed."""
        body: dict[str, Any] = {"status": status}
        if notes is not None:
            body["notes"] = notes
        return decode(
            OpenApiOperationDependency,
            self._patch(
                f"/openapi-integrations/{integration_id}/dependencies/{dependency_id}", json=body
            ),
        )

    def graph(self, integration_id: str, *, version_id: str | None = None) -> Any:
        """The derived API dependency graph (nodes/edges) for planning and display."""
        params = {"version_id": version_id} if version_id else None
        return self._get(f"/openapi-integrations/{integration_id}/graph", params=params)

    def generate_tool_drafts(
        self,
        integration_id: str,
        *,
        operation_ids: _List[str] | None = None,
        tags: _List[str] | None = None,
        methods: _List[str] | None = None,
        path_prefix: str | None = None,
        group_as_pack: bool = False,
        version_id: str | None = None,
        server_url: str | None = None,
        auth_binding: dict[str, Any] | None = None,
        requires_approval: bool = False,
        allow_in_preview: bool = False,
    ) -> _List[OpenApiToolDraft]:
        """Generate one or more curated tool drafts from selected operations.

        Select operations by id, or by filter (``tags``/``methods``/``path_prefix``)
        — useful for a large spec without enumerating every id by hand. With
        ``group_as_pack=True`` and more than one selected operation, all of them
        are bound into a single tool-pack draft instead of one draft each.
        """
        body: dict[str, Any] = {
            "group_as_pack": group_as_pack,
            "requires_approval": requires_approval,
            "allow_in_preview": allow_in_preview,
        }
        if operation_ids is not None:
            body["operation_ids"] = operation_ids
        if tags is not None:
            body["tags"] = tags
        if methods is not None:
            body["methods"] = methods
        if path_prefix is not None:
            body["path_prefix"] = path_prefix
        if version_id is not None:
            body["version_id"] = version_id
        if server_url is not None:
            body["server_url"] = server_url
        if auth_binding is not None:
            body["auth_binding"] = auth_binding
        return decode_list(
            OpenApiToolDraft,
            self._post(f"/openapi-integrations/{integration_id}/tool-drafts/generate", json=body),
        )

    def list_tool_drafts(self, integration_id: str) -> _List[OpenApiToolDraft]:
        return decode_list(
            OpenApiToolDraft, self._get(f"/openapi-integrations/{integration_id}/tool-drafts")
        )

    def get_tool_draft(self, integration_id: str, draft_id: str) -> OpenApiToolDraft:
        return decode(
            OpenApiToolDraft,
            self._get(f"/openapi-integrations/{integration_id}/tool-drafts/{draft_id}"),
        )

    def update_tool_draft(
        self, integration_id: str, draft_id: str, **changes: Any
    ) -> OpenApiToolDraft:
        return decode(
            OpenApiToolDraft,
            self._patch(
                f"/openapi-integrations/{integration_id}/tool-drafts/{draft_id}", json=changes
            ),
        )

    def preview_tool_draft(
        self, integration_id: str, draft_id: str, *, input: dict[str, Any] | None = None
    ) -> Any:
        """Run one real upstream call for an unpublished draft.

        This is a live effect, not a simulation — refused unless the draft has
        ``allow_in_preview`` set, so an approval-gated write cannot be fired
        through preview before anyone approves it.
        """
        return self._post(
            f"/openapi-integrations/{integration_id}/tool-drafts/{draft_id}/preview",
            json={"input": input or {}},
        )

    def publish_tool_draft(
        self,
        integration_id: str,
        draft_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        version: str = "1.0",
    ) -> Any:
        """Publish an approved draft into CALIBER's governed tool registry.

        Returns ``{"draft": ..., "tool": ...}`` — the tool is now reachable
        through the standard tool, workflow, and SDK ``tools`` surfaces.
        """
        body: dict[str, Any] = {"version": version}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        return self._post(
            f"/openapi-integrations/{integration_id}/tool-drafts/{draft_id}/publish", json=body
        )

    def validate_credential_binding(
        self, integration_id: str, *, auth_binding: dict[str, Any]
    ) -> Any:
        """Check whether an auth binding's secret references resolve, without publishing."""
        return self._post(
            f"/openapi-integrations/{integration_id}/validate-credential-binding",
            json={"auth_binding": auth_binding},
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

    def list(self, *, status: str | None = None) -> _List[KnowledgeBase]:
        params = {"status": status} if status else None
        return decode_list(KnowledgeBase, self._get("/knowledge-bases", params=params))

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

    def versions(self, knowledge_base_id: str) -> Any:
        return self._get(f"/knowledge-bases/{knowledge_base_id}/versions")

    def create_version(self, knowledge_base_id: str, **payload: Any) -> Any:
        return self._post(f"/knowledge-bases/{knowledge_base_id}/versions", json=payload)

    def activate_version(self, knowledge_base_id: str, version_id: str) -> Any:
        return self._post(f"/knowledge-bases/{knowledge_base_id}/versions/{version_id}/activate")

    def runs(self, knowledge_base_id: str) -> Any:
        return self._get(f"/knowledge-bases/{knowledge_base_id}/runs")

    def run_events(self, run_id: str) -> Any:
        return self._get(f"/knowledge-runs/{run_id}/events")

    def version(self, version_id: str) -> Any:
        return self._get(f"/knowledge-base-versions/{version_id}")

    def sync_version_to_age(self, version_id: str) -> Any:
        return self._post(f"/knowledge-base-versions/{version_id}/age-sync")

    def sources(self, version_id: str) -> Any:
        return self._get(f"/knowledge-base-versions/{version_id}/sources")

    def chunks(
        self,
        version_id: str,
        *,
        q: str | None = None,
        source_key: str | None = None,
        limit: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        for key, value in (("q", q), ("source_key", source_key), ("limit", limit)):
            if value is not None:
                params[key] = value
        return self._get(f"/knowledge-base-versions/{version_id}/chunks", params=params or None)

    def entities(self, version_id: str) -> Any:
        return self._get(f"/knowledge-base-versions/{version_id}/entities")

    def relationships(self, version_id: str) -> Any:
        return self._get(f"/knowledge-base-versions/{version_id}/relationships")

    def graph(self, version_id: str, **params: Any) -> Any:
        return self._get(f"/knowledge-base-versions/{version_id}/graph", params=params or None)

    def calibrate(self, knowledge_base_id: str, **options: Any) -> Any:
        return self._post(f"/knowledge-bases/{knowledge_base_id}/calibrate", json=options)

    def test_runs(self, knowledge_base_id: str, *, limit: int | None = None) -> Any:
        params = {"limit": limit} if limit is not None else None
        return self._get(f"/knowledge-bases/{knowledge_base_id}/test-runs", params=params)

    def test_run(self, test_run_id: str) -> Any:
        return self._get(f"/knowledge/test-runs/{test_run_id}")

    def set_baseline(self, knowledge_base_id: str, **options: Any) -> Any:
        return self._post(f"/knowledge-bases/{knowledge_base_id}/baseline", json=options)

    def rollback(self, knowledge_base_id: str, **options: Any) -> Any:
        """Roll back to a prior version.

        Knowledge bases roll back by activation history, not by an alias
        restore — the semantics differ per asset family, and the server is the
        authority on what this one means.
        """
        return self._post(f"/knowledge-bases/{knowledge_base_id}/rollback", json=options)

    def query(self, **payload: Any) -> Any:
        return self._post("/knowledge/query", json=payload)


class ObjectStoreAPI(Resource):
    """S3/MinIO console operations.

    Distinct from ``projects.files``: that is CALIBER's managed file registry
    with lineage and immutable refs, this is the raw bucket browser underneath.
    """

    def status(self) -> Any:
        return self._get("/object-store/status")

    def buckets(self) -> _List[Bucket]:
        return decode_list(Bucket, self._get("/object-store/buckets"))

    def create_bucket(self, bucket: str) -> Any:
        return self._post("/object-store/buckets", json={"name": bucket})

    def delete_bucket(self, bucket: str) -> Any:
        return self._delete(f"/object-store/buckets/{bucket}")

    def listing(
        self,
        bucket: str,
        *,
        prefix: str | None = None,
        token: str | None = None,
        recursive: bool = False,
    ) -> Any:
        params: dict[str, Any] = {}
        if prefix is not None:
            params["prefix"] = prefix
        if token is not None:
            params["token"] = token
        if recursive:
            params["recursive"] = "true"
        return self._get(f"/object-store/buckets/{bucket}/objects", params=params or None)

    def objects(
        self,
        bucket: str,
        *,
        prefix: str | None = None,
        token: str | None = None,
        recursive: bool = False,
    ) -> _List[StoredObject]:
        payload = self.listing(bucket, prefix=prefix, token=token, recursive=recursive)
        items = payload.get("objects") if isinstance(payload, dict) else payload
        return decode_list(StoredObject, items)

    def folders(self, bucket: str, *, prefix: str | None = None) -> _List[str]:
        payload = self.listing(bucket, prefix=prefix)
        items = payload.get("prefixes") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [str(item) for item in items]

    def upload(
        self,
        bucket: str,
        *,
        filename: str,
        content: bytes,
        prefix: str | None = None,
        key: str | None = None,
        media_type: str | None = None,
    ) -> Any:
        files = {"file": (filename, content, media_type or "application/octet-stream")}
        data: dict[str, str] = {}
        if prefix is not None:
            data["prefix"] = prefix
        if key is not None:
            data["key"] = key
        response = self._transport.request(
            "POST", f"/object-store/buckets/{bucket}/objects", files=files, data=data
        )
        return response.data

    def create_folder(self, bucket: str, name: str, *, prefix: str | None = None) -> Any:
        body: dict[str, Any] = {"name": name}
        if prefix is not None:
            body["prefix"] = prefix
        return self._post(f"/object-store/buckets/{bucket}/folders", json=body)

    def delete_objects(
        self, bucket: str, *, keys: _List[str] | None = None, prefix: str | None = None
    ) -> Any:
        body: dict[str, Any] = {}
        if keys is not None:
            body["keys"] = keys
        if prefix is not None:
            body["prefix"] = prefix
        return self._post(f"/object-store/buckets/{bucket}/objects/delete", json=body)

    def download(self, bucket: str, key: str, *, disposition: str | None = None) -> bytes:
        params = {"key": key}
        if disposition is not None:
            params["disposition"] = disposition
        return self._transport.download(f"/object-store/buckets/{bucket}/object", params=params)

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


__all__ = [
    "GatewayAPI",
    "KnowledgeBasesAPI",
    "McpServersAPI",
    "ObjectStoreAPI",
    "OpenApiIntegrationsAPI",
]
