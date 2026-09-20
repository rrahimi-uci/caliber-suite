"""The fourth real Workspace resource adapter -- a CALIBER MCP server
(`P4-B`/`P4-C`, slice 4 of the multi-slice managed-snapshot epic, and the last
named resource type in the original follow-up list -- see this module's
docstring for what remains after it).

``workspace_release_judge_adapter.py`` established the "single mutable row,
no version history at all" shape for a CALIBER-owned resource with no
external registry (a prompt) and no internal version table (a workflow) to
pin against. :class:`~caliber.db.models.CaliberMcpServer`
(``routes/mcp_servers.py``) is the same shape: a single row, unique on
``name`` (``uq_mcp_server_name``), carrying ``project_id``/``visibility``/
``owner`` directly on itself (exactly like ``CaliberJudge``, not through a
hidden target row the way a prompt's visibility is gated) -- and
``routes/mcp_servers.py::_UPDATABLE_FIELDS`` proves it is mutable in place
after registration (``update_mcp_server`` can rewrite ``transport``/``uri``/
``command``/``auth_config``/etc. on the exact same row), so, like a judge,
it has no numbered version a caller could pin an exact revision of. This
adapter therefore reuses the judge adapter's ``version_ref`` convention
verbatim: a declaration must supply the sentinel :data:`CURRENT_VERSION_REF`
("pin whatever this server's live row currently is" -- the only truthful
value there is to ask for), and the returned
:class:`~caliber.workspace_release_adapters.SnapshotPin` sets its *own*
``version_ref`` to the server's content digest, not the sentinel, so the
persisted pin is genuinely reproducible even though the declaration wasn't
asking for a specific version.

Content basis for ``content_sha256`` -- the "behavior/safety-determining
fields, not administrative metadata" split ``workspace_release_tool_adapter.py``
established, applied here because an MCP server is conceptually adjacent to a
tool: it exposes callable tools too, just through a remote transport instead
of a local ``module_path``/``callable_name`` pair. ``mcp_gateway.py::
McpServerConfig.from_row`` is the authoritative answer to "what does invoking
a tool through this server actually do" -- it is the *only* place a
``CaliberMcpServer`` row is turned into the config the gateway actually
dials out with, and it reads exactly: ``transport``, ``uri``, ``command``,
``args``, ``env``, ``headers``, ``auth_type``, ``auth_config``,
``discovered_tools``, and ``tool_policies``. This adapter hashes precisely
those fields:

* ``transport``/``uri``/``command``/``args``/``env``/``headers``/
  ``auth_type``/``auth_config`` -- the connection: which server is dialed,
  over what transport, and with what credentials. Changing any of these
  changes what a "call this MCP server" actually reaches, exactly the way
  the tool adapter's ``module_path``/``callable_name``/``execution_backend``/
  ``backend_config`` changes what a tool call reaches.
* ``discovered_tools`` -- the cached tool catalog (name/description/input
  schema per tool) a workflow's MCP node picks a tool from. This is this
  resource's analogue of a tool's own ``input_schema``/``output_schema``:
  it defines which calls are even possible and with what parameters.
* ``tool_policies`` -- the per-tool ``allowed``/``side_effect_level``/
  ``requires_approval`` governance map ``mcp_policy.py::effective_tool_policy``
  reads before any invocation is allowed to proceed. This is this resource's
  analogue of the tool adapter's own ``side_effect_level``/
  ``requires_approval``/``allow_in_preview`` inclusion: a safety policy is
  exactly as behavior-determining as the connection it gates.

Deliberately excluded as administrative/descriptive, mirroring both the tool
and judge adapters' own exclusions: ``name`` (immutable after creation --
absent from ``_UPDATABLE_FIELDS`` -- and, unlike a judge's ``name``, never
used as a runtime reference; a workflow's MCP node dependency binds to the
stable ``server_id`` primary key, per ``mcp_policy.py::extract_dependencies``,
not to ``name``), ``description``, ``icon``, ``owner``, ``status``,
``connection_error``, ``last_connected_at``, and the per-tool
``tool_test_cases``/``tool_calibrations`` artifacts (the same "saved test
cases and their scored calibration result are diagnostic, not behavioral"
reasoning the tool adapter already applied to its own ``test_cases``/
``last_calibration`` exclusion).

A note on secrets: ``env``/``headers``/``auth_config`` may still contain
*literal* credentials on legacy rows (``mcp_secrets.py``'s module docstring --
CALIBER's write-only sentinel containment is an API/audit-boundary contract,
not encryption at rest). Hashing those literal values is safe (a digest does
not reveal its input), but this adapter never places the raw resolved
config -- only the digest and small non-sensitive summary fields (transport,
tool count, status) -- into ``SnapshotPin.resolution``, which is persisted on
:class:`~caliber.db.models.CaliberWorkspaceRevisionResource` and returned
over the API. Reusing ``mcp_secrets.py::sanitize_mcp_config`` here would be
redundant *and* would defeat drift detection (a sanitized value is lossy, so
two different literal secrets could sanitize to the same placeholder and
hide real drift) -- the adapter reads the literal stored values for hashing
but never re-serializes them anywhere the API or audit trail can observe.

Security: ``resolve()`` checks the server's full 3-tier visibility
(``project``/``user``/``public``) via :func:`caliber.db.scoping.get_visible`
from the start -- the same lesson the prompt adapter's own follow-up fix
(PR #393) taught, and the same shape the judge adapter already reused
correctly: ``CaliberMcpServer`` carries ``project_id``/``visibility`` on the
row itself (like a judge, unlike a prompt's hidden target), so a bare
``server.project_id`` comparison could not distinguish another user's
unshared personal server (``project_id=None``, ``visibility="user"``) from a
public one (``project_id=None``, ``visibility="public"``) -- this is exactly
the same disclosure ``routes/mcp_servers.py::_visible_server`` already
guards every direct route against, reused rather than reimplemented.

``apply_release``/``rollback_release``/``observe_release``: like a judge (and
unlike a prompt/workflow alias), an MCP server has no per-environment alias
or external target to promote -- a workflow's MCP node binds directly to the
server's own stable ``server_id`` in its manifest, with no environment-scoped
indirection in between. The adapter's "provider" is therefore CALIBER's own
database (mirrors ``WorkflowWorkspaceResourceAdapter``'s and
``JudgeWorkspaceResourceAdapter``'s reasoning for sharing the caller's
session rather than risking a SQLite single-writer deadlock -- see
``workspace_release_adapters.py``'s module docstring), and a "release" is a
content-integrity check: it re-reads the live row, confirms it has not been
disabled since the pin was taken (mirroring the tool adapter's own
``tool_archived`` lifecycle refusal), and confirms its content digest still
matches what this revision pinned -- failing closed with
``mcp_server_content_drifted`` (mirroring ``tool_content_drifted``/
``judge_content_drifted``) rather than silently treating drifted connection
config or a since-changed tool policy as still-reviewed.

With this slice, every resource type named in `P4-C`'s original follow-up
list (`skill`, `eval_dataset`, `mcp_server`) except `skill`/`eval_dataset`
themselves -- tracked as separate concurrent slices, not claimed done here --
and `workflow`'s own still-placeholder ``snapshot()`` has a real adapter
registered. See ``docs/workspace-plan.md``'s `P4-B`/`P4-C` rows for the
up-to-date closure status across all slices.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberMcpServer,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.db.scoping import get_visible
from caliber.workflows.manifest import canonical_json
from caliber.workspace_release_adapters import (
    PreparedAction,
    ProviderOutcome,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)

logger = logging.getLogger(__name__)

_TARGET_REF_PREFIX = "mcp-server:"

#: The only value a caller may supply as ``version_ref`` when declaring an
#: MCP server pin -- see this module's docstring for why an MCP server has no
#: numbered version to be explicit about instead (mirrors
#: ``workspace_release_judge_adapter.py::CURRENT_VERSION_REF``).
CURRENT_VERSION_REF = "current"

#: Bumped whenever this adapter's resolution/snapshot strategy changes in a
#: way that would make an old pin's ``resolution.adapter_version`` stop
#: describing how it was produced -- the same convention every sibling
#: adapter in this epic uses.
MCP_SERVER_ADAPTER_VERSION = "workspace-release-mcp-server-adapter/1"


@dataclass(frozen=True)
class ResolvedMcpServer:
    """A CALIBER MCP server, loaded and visibility-checked."""

    server_id: str
    name: str
    transport: str
    uri: str
    command: str
    args: tuple[str, ...]
    env: Mapping[str, Any]
    headers: Mapping[str, Any]
    auth_type: str
    auth_config: Mapping[str, Any]
    discovered_tools: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)
    tool_policies: Mapping[str, Any] = field(default_factory=dict)
    status: str = "active"


def _target_ref(server_id: str) -> str:
    return f"{_TARGET_REF_PREFIX}{server_id}"


def _parse_target_ref(target_ref: str) -> str:
    if not target_ref.startswith(_TARGET_REF_PREFIX) or len(target_ref) <= len(_TARGET_REF_PREFIX):
        raise WorkspaceReleaseAdapterError(f"malformed mcp_server target_ref {target_ref!r}")
    return target_ref[len(_TARGET_REF_PREFIX) :]


def _content_payload(
    *,
    transport: str,
    uri: str,
    command: str,
    args: Sequence[str],
    env: Mapping[str, Any],
    headers: Mapping[str, Any],
    auth_type: str,
    auth_config: Mapping[str, Any],
    discovered_tools: Sequence[Mapping[str, Any]],
    tool_policies: Mapping[str, Any],
) -> dict[str, Any]:
    """The fields that determine what invoking a tool through this MCP
    server actually does, not its administrative metadata (see module
    docstring for the full rationale)."""
    return {
        "transport": transport,
        "uri": uri,
        "command": command,
        "args": list(args),
        "env": dict(env),
        "headers": dict(headers),
        "auth_type": auth_type,
        "auth_config": dict(auth_config),
        "discovered_tools": [dict(tool) for tool in discovered_tools],
        "tool_policies": dict(tool_policies),
    }


def _content_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def _row_content_sha256(row: CaliberMcpServer) -> str:
    return _content_sha256(
        _content_payload(
            transport=row.transport,
            uri=row.uri,
            command=row.command,
            args=row.args or [],
            env=row.env or {},
            headers=row.headers or {},
            auth_type=row.auth_type,
            auth_config=row.auth_config or {},
            discovered_tools=[t for t in (row.discovered_tools or []) if isinstance(t, dict)],
            tool_policies=row.tool_policies or {},
        )
    )


class McpServerWorkspaceResourceAdapter:
    """Resolves, snapshots, validates, and releases a CALIBER MCP server."""

    resource_type = "mcp_server"

    # -- resolve / snapshot --------------------------------------------------

    def resolve(
        self, session: object, _workspace: object, declaration: object, identity: object
    ) -> ResolvedMcpServer:
        """Load an MCP server and enforce its own 3-tier visibility model.

        ``declaration`` carries ``resource_id`` (the server's ``server_id``)
        and ``version_ref``, which must be exactly :data:`CURRENT_VERSION_REF`
        -- see this module's docstring for why an MCP server has no numbered
        version a caller could ask for instead.
        """
        assert isinstance(session, Session)
        assert isinstance(identity, CaliberIdentity)
        if not isinstance(declaration, Mapping):
            raise WorkspaceReleaseAdapterError("mcp_server declaration must be a mapping")
        server_id = declaration.get("resource_id") or declaration.get("server_id")
        version_ref = declaration.get("version_ref")
        if not server_id or not version_ref:
            raise WorkspaceReleaseAdapterError(
                "mcp_server declaration requires 'resource_id' and 'version_ref'"
            )
        server_id = str(server_id)
        if str(version_ref) != CURRENT_VERSION_REF:
            raise WorkspaceReleaseAdapterError(
                f"mcp_server version_ref must be {CURRENT_VERSION_REF!r} (an MCP server has "
                f"no numbered version history to pin an explicit version of); got {version_ref!r}"
            )

        server = get_visible(
            session, CaliberMcpServer, CaliberMcpServer.server_id, server_id, identity
        )
        if server is None:
            # Matches ``routes/mcp_servers.py``'s own "not found" 404 for both
            # a genuinely missing server and one that exists but is not
            # visible to this caller -- never distinguishing the two, so this
            # can't be used to probe for another user's server ids.
            raise WorkspaceReleaseAdapterError(f"mcp_server {server_id!r} not found or not visible")

        return ResolvedMcpServer(
            server_id=server.server_id,
            name=server.name,
            transport=server.transport,
            uri=server.uri,
            command=server.command,
            args=tuple(server.args or []),
            env=dict(server.env or {}),
            headers=dict(server.headers or {}),
            auth_type=server.auth_type,
            auth_config=dict(server.auth_config or {}),
            discovered_tools=tuple(
                dict(t) for t in (server.discovered_tools or []) if isinstance(t, dict)
            ),
            tool_policies=dict(server.tool_policies or {}),
            status=server.status,
        )

    def snapshot(self, _session: object, resolved_pin: object) -> SnapshotPin:
        """Compute a genuine content digest over the server's connection
        config, discovered tool catalog, and tool policy (see module
        docstring for exactly which fields and why). Never places the raw
        resolved config -- only the digest and small non-sensitive summary
        fields -- into the returned ``resolution``.
        """
        if not isinstance(resolved_pin, ResolvedMcpServer):
            raise WorkspaceReleaseAdapterError("mcp_server snapshot requires a resolved mcp server")
        content_sha256 = _content_sha256(
            _content_payload(
                transport=resolved_pin.transport,
                uri=resolved_pin.uri,
                command=resolved_pin.command,
                args=resolved_pin.args,
                env=resolved_pin.env,
                headers=resolved_pin.headers,
                auth_type=resolved_pin.auth_type,
                auth_config=resolved_pin.auth_config,
                discovered_tools=resolved_pin.discovered_tools,
                tool_policies=resolved_pin.tool_policies,
            )
        )
        return SnapshotPin(
            resource_id=resolved_pin.server_id,
            version_ref=content_sha256,
            content_sha256=content_sha256,
            provider_ref=f"mcp-server:{resolved_pin.server_id}",
            resolution={
                "strategy": "live_resource_adapter",
                "adapter_version": MCP_SERVER_ADAPTER_VERSION,
                "transport": resolved_pin.transport,
                "tool_count": len(resolved_pin.discovered_tools),
                "status": resolved_pin.status,
            },
        )

    # -- validate ------------------------------------------------------------

    def validate(
        self,
        session: object,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment | None = None,
    ) -> dict[str, Any]:
        """Not called by any live route or worker today (mirrors
        ``JudgeWorkspaceResourceAdapter.validate()``'s own note). The
        :class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
        Protocol's ``validate()`` does not carry a caller ``identity``, so
        this only compares ``project_id`` directly -- the same narrower check
        (and the same class of gap) ``resolve()`` above closes with a real
        identity -- unreachable in production while nothing calls this
        method.
        """
        assert isinstance(session, Session)
        server_id = pin.resource_id
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise WorkspaceReleaseAdapterError(f"mcp_server {server_id!r} not found")
        if (
            server.project_id is not None
            and environment is not None
            and server.project_id != environment.project_id
        ):
            raise WorkspaceReleaseAdapterError(
                f"mcp_server {server_id!r} belongs to project {server.project_id!r}, "
                f"not release environment project {environment.project_id!r}"
            )
        return {
            "valid": True,
            "resource_pin_id": pin.resource_pin_id,
            "server_id": server_id,
            "content_sha256": pin.content_sha256,
        }

    # -- prepare ---------------------------------------------------------

    def prepare_release(
        self,
        pin: CaliberWorkspaceRevisionResource,
        environment: CaliberWorkspaceEnvironment,
        *,
        before_ref: str | None,
    ) -> PreparedAction:
        after_ref = pin.version_ref
        return PreparedAction(
            resource_pin_id=pin.resource_pin_id,
            resource_type=self.resource_type,
            action="no_op" if before_ref == after_ref else "verify",
            target_ref=_target_ref(pin.resource_id),
            before_ref=before_ref,
            after_ref=after_ref,
            metadata={
                "server_id": pin.resource_id,
                "environment_id": environment.environment_id,
                "project_id": environment.project_id,
            },
        )

    # -- apply / observe / rollback ---------------------------------------
    # A genuinely same-database provider (mirrors
    # JudgeWorkspaceResourceAdapter's reasoning) -- all three share the
    # caller's session and only ever read, since there is no external alias
    # or target row to write for an MCP server. See this module's docstring
    # for why an MCP server "release" is a content-integrity check, not a
    # mutation.

    def apply_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def rollback_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def observe_release(self, session: object, prepared: PreparedAction) -> ProviderOutcome:
        assert isinstance(session, Session)
        return self._verify(session, prepared)

    def _verify(self, session: Session, prepared: PreparedAction) -> ProviderOutcome:
        if prepared.action == "no_op":
            return ProviderOutcome(
                "applied",
                f"mcp-server-no-op:{prepared.resource_pin_id}",
                provider_result={"content_sha256": prepared.after_ref},
            )
        server_id = _parse_target_ref(prepared.target_ref)
        after_ref = prepared.after_ref
        if not after_ref:
            raise WorkspaceReleaseAdapterError(
                f"prepared action for {prepared.target_ref!r} has no after_ref to verify"
            )
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            return ProviderOutcome(
                "failed",
                f"mcp-server-missing:{server_id}",
                error_code="mcp_server_not_found",
                error_summary=f"mcp_server {server_id!r} no longer exists",
            )
        if server.status == "disabled":
            return ProviderOutcome(
                "failed",
                f"mcp-server-disabled:{server_id}",
                error_code="mcp_server_disabled",
                error_summary=f"mcp_server {server_id!r} has been disabled",
            )
        project_id = prepared.metadata.get("project_id")
        if (
            project_id is not None
            and server.project_id is not None
            and server.project_id != project_id
        ):
            return ProviderOutcome(
                "failed",
                f"mcp-server-tenancy:{server_id}",
                error_code="resource_outside_project",
                error_summary=(
                    f"mcp_server {server_id!r} belongs to project {server.project_id!r}, "
                    f"not release project {project_id!r}"
                ),
            )
        current_sha256 = _row_content_sha256(server)
        if current_sha256 != after_ref:
            return ProviderOutcome(
                "failed",
                f"mcp-server-drift:{server_id}",
                error_code="mcp_server_content_drifted",
                error_summary=(
                    f"mcp_server {server_id!r} has changed since it was pinned "
                    f"(pinned {after_ref!r}, live {current_sha256!r})"
                ),
            )
        return ProviderOutcome(
            "applied",
            f"mcp-server:{server_id}:{current_sha256[:12]}",
            provider_result={
                "content_sha256": current_sha256,
                "tool_count": len(
                    [t for t in (server.discovered_tools or []) if isinstance(t, dict)]
                ),
            },
        )


__all__ = [
    "CURRENT_VERSION_REF",
    "MCP_SERVER_ADAPTER_VERSION",
    "McpServerWorkspaceResourceAdapter",
    "ResolvedMcpServer",
]
