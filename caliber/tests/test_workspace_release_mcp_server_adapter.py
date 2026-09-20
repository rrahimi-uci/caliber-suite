"""`P4-B`/`P4-C`: the fourth real Workspace resource adapter -- a CALIBER MCP
server.

Mirrors ``test_workspace_release_judge_adapter.py``'s pattern (proving the
:class:`~caliber.workspace_release_adapters.WorkspaceResourceAdapter`
Protocol against a genuine resource type with no version history), but for a
resource whose content basis is a connection config plus a discovered tool
catalog and tool policy rather than a grading prompt -- see
``workspace_release_mcp_server_adapter.py``'s module docstring for the full
design rationale.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.auth import SCOPE_ADMIN, CaliberIdentity
from caliber.db.models import (
    CaliberMcpServer,
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    PreparedAction,
    SnapshotPin,
    WorkspaceReleaseAdapterError,
)
from caliber.workspace_release_mcp_server_adapter import (
    CURRENT_VERSION_REF,
    MCP_SERVER_ADAPTER_VERSION,
    McpServerWorkspaceResourceAdapter,
    ResolvedMcpServer,
    _content_payload,
    _content_sha256,
)

PROJECT_ID = "PRJ-mcp-server-adapter"
OTHER_PROJECT_ID = "PRJ-mcp-server-adapter-other"
ADAPTER = McpServerWorkspaceResourceAdapter()


def _identity(
    user_id: str = "@caller", *, active_project_id: str | None = None, admin: bool = False
) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user_id,
        scopes=frozenset({SCOPE_ADMIN}) if admin else frozenset(),
        active_project_id=active_project_id,
    )


def _seed_project(session: Session, *, project_id: str = PROJECT_ID) -> None:
    session.add(
        CaliberProject(project_id=project_id, name=f"MCP adapter {project_id}", owner="admin")
    )
    session.flush()


def _seed_server(
    session: Session,
    *,
    server_id: str,
    project_id: str | None,
    owner: str = "@test",
    visibility: str | None = None,
    name: str | None = None,
    transport: str = "stdio",
    uri: str = "",
    command: str = "npx test-server",
    args: list[str] | None = None,
    env: dict[str, object] | None = None,
    headers: dict[str, object] | None = None,
    auth_type: str = "none",
    auth_config: dict[str, object] | None = None,
    discovered_tools: list[dict[str, object]] | None = None,
    tool_policies: dict[str, object] | None = None,
    status: str = "active",
) -> CaliberMcpServer:
    server = CaliberMcpServer(
        server_id=server_id,
        name=name or server_id,
        description="",
        transport=transport,
        uri=uri,
        command=command,
        args=args if args is not None else [],
        env=env if env is not None else {},
        headers=headers if headers is not None else {},
        auth_type=auth_type,
        auth_config=auth_config if auth_config is not None else {},
        discovered_tools=discovered_tools
        if discovered_tools is not None
        else [{"name": "search", "description": "Search things"}],
        tool_policies=tool_policies
        if tool_policies is not None
        else {"search": {"allowed": True, "side_effect_level": "read", "requires_approval": False}},
        owner=owner,
        project_id=project_id,
        visibility=visibility or ("project" if project_id else "user"),
        status=status,
    )
    session.add(server)
    session.flush()
    return server


def _server_content_sha(server: CaliberMcpServer) -> str:
    return _content_sha256(
        _content_payload(
            transport=server.transport,
            uri=server.uri,
            command=server.command,
            args=server.args or [],
            env=server.env or {},
            headers=server.headers or {},
            auth_type=server.auth_type,
            auth_config=server.auth_config or {},
            discovered_tools=[t for t in (server.discovered_tools or []) if isinstance(t, dict)],
            tool_policies=server.tool_policies or {},
        )
    )


# -- resolve --------------------------------------------------------------


def test_resolve_loads_the_server_and_returns_its_connection_fields(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID, owner="@caller")
    project = db_session.get(CaliberProject, PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        project,
        {"resource_id": "MCP-1", "version_ref": CURRENT_VERSION_REF},
        _identity("@caller", active_project_id=PROJECT_ID),
    )
    assert isinstance(resolved, ResolvedMcpServer)
    assert resolved.server_id == "MCP-1"
    assert resolved.name == "MCP-1"
    assert resolved.transport == "stdio"
    assert resolved.command == "npx test-server"
    assert resolved.discovered_tools == ({"name": "search", "description": "Search things"},)
    assert resolved.tool_policies == {
        "search": {"allowed": True, "side_effect_level": "read", "requires_approval": False}
    }


def test_resolve_requires_resource_id_and_version_ref(db_session: Session) -> None:
    identity = _identity()
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {"resource_id": "x"}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError, match="requires"):
        ADAPTER.resolve(db_session, None, {}, identity)
    with pytest.raises(WorkspaceReleaseAdapterError):
        ADAPTER.resolve(db_session, None, "not-a-mapping", identity)  # type: ignore[arg-type]


def test_resolve_rejects_a_version_ref_other_than_current(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID, owner="@caller")
    with pytest.raises(WorkspaceReleaseAdapterError, match="must be 'current'"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "MCP-1", "version_ref": "3"},
            _identity("@caller", active_project_id=PROJECT_ID),
        )


def test_resolve_raises_when_the_server_does_not_exist(db_session: Session) -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "MCP-ghost", "version_ref": CURRENT_VERSION_REF},
            _identity(),
        )


def test_resolve_refuses_a_server_bound_to_a_different_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_server(db_session, server_id="MCP-other", project_id=OTHER_PROJECT_ID, owner="@owner")
    caller = _identity("@caller", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "MCP-other", "version_ref": CURRENT_VERSION_REF},
            caller,
        )


def test_resolve_allows_a_project_scoped_server_for_an_active_member(db_session: Session) -> None:
    """The positive side of the project-tier check: a caller who is an
    active member of the target's own project (not its owner) can still
    resolve it -- proving this reuses the *real* visibility model
    (owner-or-member), not a stricter "owner only" shortcut."""
    _seed_project(db_session)
    _seed_server(db_session, server_id="MCP-team", project_id=PROJECT_ID, owner="@owner")
    db_session.add(
        CaliberProjectMember(
            member_id="PM-mcp-adapter-1",
            project_id=PROJECT_ID,
            user_id="@teammate",
            role="editor",
            status="active",
            created_by="@owner",
        )
    )
    db_session.flush()
    member = _identity("@teammate", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "MCP-team", "version_ref": CURRENT_VERSION_REF},
        member,
    )
    assert resolved.server_id == "MCP-team"


def test_resolve_refuses_another_users_unshared_personal_server(db_session: Session) -> None:
    """The actual disclosure this design closes from the start: a personal
    server (``project_id=None``, ``visibility="user"``) has the *same*
    ``None`` project id as a public server, so a bare ``server.project_id !=
    caller's project`` comparison could not tell them apart and would let
    any operator on any project snapshot another user's unshared personal
    server by id. Must be refused, the same way ``routes/mcp_servers.py``'s
    lookup routes already refuse it."""
    _seed_project(db_session)
    _seed_server(
        db_session,
        server_id="MCP-personal",
        project_id=None,
        owner="@someone-else",
        auth_config={"token": "secret-value"},
    )
    other_user = _identity("@a-different-user", active_project_id=PROJECT_ID)
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found or not visible"):
        ADAPTER.resolve(
            db_session,
            None,
            {"resource_id": "MCP-personal", "version_ref": CURRENT_VERSION_REF},
            other_user,
        )


def test_resolve_allows_the_owners_own_personal_server(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_server(db_session, server_id="MCP-mine", project_id=None, owner="@owner-self")
    owner = _identity("@owner-self", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "MCP-mine", "version_ref": CURRENT_VERSION_REF},
        owner,
    )
    assert resolved.server_id == "MCP-mine"


def test_resolve_allows_a_public_server_for_any_caller(db_session: Session) -> None:
    """A genuinely public server also has ``project_id=None`` -- proving this
    allows that tier too, not just refusing everything with a ``None``
    project id."""
    _seed_project(db_session)
    _seed_server(
        db_session,
        server_id="MCP-public",
        project_id=None,
        owner="@publisher",
        visibility="public",
    )
    stranger = _identity("@total-stranger", active_project_id=PROJECT_ID)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "MCP-public", "version_ref": CURRENT_VERSION_REF},
        stranger,
    )
    assert resolved.server_id == "MCP-public"


def test_resolve_admin_bypasses_visibility(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_server(db_session, server_id="MCP-personal", project_id=None, owner="@someone-else")
    admin = _identity("@platform-admin", active_project_id=PROJECT_ID, admin=True)
    resolved = ADAPTER.resolve(
        db_session,
        None,
        {"resource_id": "MCP-personal", "version_ref": CURRENT_VERSION_REF},
        admin,
    )
    assert resolved.server_id == "MCP-personal"


# -- snapshot ---------------------------------------------------------------


def _resolved(
    *,
    server_id: str = "MCP-1",
    name: str = "srv",
    transport: str = "stdio",
    uri: str = "",
    command: str = "npx test-server",
    args: tuple[str, ...] = (),
    env: dict[str, object] | None = None,
    headers: dict[str, object] | None = None,
    auth_type: str = "none",
    auth_config: dict[str, object] | None = None,
    discovered_tools: tuple[dict[str, object], ...] = (),
    tool_policies: dict[str, object] | None = None,
    status: str = "active",
) -> ResolvedMcpServer:
    return ResolvedMcpServer(
        server_id=server_id,
        name=name,
        transport=transport,
        uri=uri,
        command=command,
        args=args,
        env=env or {},
        headers=headers or {},
        auth_type=auth_type,
        auth_config=auth_config or {},
        discovered_tools=discovered_tools,
        tool_policies=tool_policies or {},
        status=status,
    )


def test_snapshot_computes_a_genuine_content_digest() -> None:
    resolved = _resolved(
        discovered_tools=({"name": "search", "description": "Search"},),
        tool_policies={"search": {"allowed": True}},
    )
    pin = ADAPTER.snapshot(None, resolved)
    assert isinstance(pin, SnapshotPin)
    assert pin.resource_id == "MCP-1"
    expected_sha = _content_sha256(
        _content_payload(
            transport="stdio",
            uri="",
            command="npx test-server",
            args=(),
            env={},
            headers={},
            auth_type="none",
            auth_config={},
            discovered_tools=({"name": "search", "description": "Search"},),
            tool_policies={"search": {"allowed": True}},
        )
    )
    assert pin.version_ref == expected_sha
    assert pin.content_sha256 == expected_sha
    assert pin.provider_ref == "mcp-server:MCP-1"
    assert pin.resolution["strategy"] == "live_resource_adapter"
    assert pin.resolution["adapter_version"] == MCP_SERVER_ADAPTER_VERSION
    assert pin.resolution["transport"] == "stdio"
    assert pin.resolution["tool_count"] == 1


def test_snapshot_resolution_never_contains_raw_secret_values() -> None:
    """The security-relevant guarantee this module's docstring promises:
    ``resolution`` may carry small summary fields but never the literal
    ``env``/``headers``/``auth_config`` values that could contain a legacy
    plaintext credential."""
    resolved = _resolved(auth_config={"token": "super-secret-value"})
    pin = ADAPTER.snapshot(None, resolved)
    assert "super-secret-value" not in repr(pin.resolution)


def test_snapshot_distinguishes_different_transport_config() -> None:
    a = ADAPTER.snapshot(None, _resolved(transport="stdio", command="cmd-a"))
    b = ADAPTER.snapshot(None, _resolved(transport="stdio", command="cmd-b"))
    assert a.content_sha256 != b.content_sha256
    assert a.version_ref != b.version_ref


def test_snapshot_distinguishes_different_auth_config() -> None:
    a = ADAPTER.snapshot(None, _resolved(auth_type="bearer", auth_config={"token": "one"}))
    b = ADAPTER.snapshot(None, _resolved(auth_type="bearer", auth_config={"token": "two"}))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_distinguishes_different_discovered_tools() -> None:
    a = ADAPTER.snapshot(None, _resolved(discovered_tools=({"name": "search"},)))
    b = ADAPTER.snapshot(None, _resolved(discovered_tools=({"name": "search"}, {"name": "fetch"})))
    assert a.content_sha256 != b.content_sha256


def test_snapshot_distinguishes_different_tool_policies() -> None:
    a = ADAPTER.snapshot(
        None,
        _resolved(tool_policies={"search": {"allowed": True, "requires_approval": False}}),
    )
    b = ADAPTER.snapshot(
        None,
        _resolved(tool_policies={"search": {"allowed": True, "requires_approval": True}}),
    )
    assert a.content_sha256 != b.content_sha256


def test_snapshot_ignores_administrative_fields() -> None:
    """``name``/``status`` are excluded from the content basis (see module
    docstring) -- changing only those must not change the digest."""
    a = ADAPTER.snapshot(None, _resolved(name="alpha", status="active"))
    b = ADAPTER.snapshot(None, _resolved(name="beta", status="error"))
    assert a.content_sha256 == b.content_sha256


def test_snapshot_rejects_a_non_resolved_input() -> None:
    with pytest.raises(WorkspaceReleaseAdapterError, match="resolved mcp server"):
        ADAPTER.snapshot(None, {"not": "resolved"})


# -- validate -----------------------------------------------------------


def test_validate_accepts_an_unscoped_pin(db_session: Session) -> None:
    _seed_project(db_session)
    server = _seed_server(db_session, server_id="MCP-validate-1", project_id=PROJECT_ID)
    content_sha = _server_content_sha(server)
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-1",
        revision_id="WSR-validate-1",
        resource_type="mcp_server",
        logical_name="MCP-validate-1",
        resource_id="MCP-validate-1",
        version_ref=content_sha,
        content_sha256=content_sha,
        provider_ref="mcp-server:MCP-validate-1",
        purpose="runtime",
        resolution={},
    )
    result = ADAPTER.validate(db_session, pin)
    assert result == {
        "valid": True,
        "resource_pin_id": "WSRR-validate-1",
        "server_id": "MCP-validate-1",
        "content_sha256": content_sha,
    }


def test_validate_refuses_a_missing_server(db_session: Session) -> None:
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-ghost",
        revision_id="WSR-validate-ghost",
        resource_type="mcp_server",
        logical_name="MCP-ghost",
        resource_id="MCP-ghost",
        version_ref="a" * 64,
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="not found"):
        ADAPTER.validate(db_session, pin)


def test_validate_refuses_a_server_outside_the_environments_project(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_server(db_session, server_id="MCP-other", project_id=OTHER_PROJECT_ID)
    environment = CaliberWorkspaceEnvironment(
        environment_id="WSE-validate",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )
    pin = CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-validate-2",
        revision_id="WSR-validate-2",
        resource_type="mcp_server",
        logical_name="MCP-other",
        resource_id="MCP-other",
        version_ref="a" * 64,
        content_sha256="a" * 64,
        purpose="runtime",
        resolution={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="project"):
        ADAPTER.validate(db_session, pin, environment)


# -- prepare_release ------------------------------------------------------


def _pin(*, version_ref: str = "a" * 64) -> CaliberWorkspaceRevisionResource:
    return CaliberWorkspaceRevisionResource(
        resource_pin_id="WSRR-prep-1",
        revision_id="WSR-prep-1",
        resource_type="mcp_server",
        logical_name="MCP-1",
        resource_id="MCP-1",
        version_ref=version_ref,
        content_sha256=version_ref,
        purpose="runtime",
        resolution={},
    )


def _environment() -> CaliberWorkspaceEnvironment:
    return CaliberWorkspaceEnvironment(
        environment_id="WSE-prep-1",
        project_id=PROJECT_ID,
        name="prod",
        environment_class="production",
        promotion_order=40,
        status="active",
        created_by="admin",
    )


def test_prepare_release_verifies_when_before_differs_from_after() -> None:
    prepared = ADAPTER.prepare_release(
        _pin(version_ref="b" * 64), _environment(), before_ref="a" * 64
    )
    assert prepared.action == "verify"
    assert prepared.target_ref == "mcp-server:MCP-1"
    assert prepared.before_ref == "a" * 64
    assert prepared.after_ref == "b" * 64
    assert prepared.metadata["server_id"] == "MCP-1"


def test_prepare_release_is_a_no_op_when_before_equals_after() -> None:
    prepared = ADAPTER.prepare_release(
        _pin(version_ref="a" * 64), _environment(), before_ref="a" * 64
    )
    assert prepared.action == "no_op"


# -- apply / observe / rollback -------------------------------------------


def _prepared_action(
    *, action: str = "verify", after_ref: str = "a" * 64, server_id: str = "MCP-1"
) -> PreparedAction:
    return PreparedAction(
        resource_pin_id="WSRR-apply-1",
        resource_type="mcp_server",
        action=action,
        target_ref=f"mcp-server:{server_id}",
        before_ref="b" * 64,
        after_ref=after_ref,
        metadata={"server_id": server_id, "environment_id": "WSE-1", "project_id": PROJECT_ID},
    )


def test_apply_release_confirms_matching_content(db_session: Session) -> None:
    _seed_project(db_session)
    server = _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID)
    content_sha = _server_content_sha(server)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "applied"
    assert outcome.provider_result["content_sha256"] == content_sha
    assert outcome.provider_result["tool_count"] == 1


def test_apply_release_no_op_does_not_touch_the_database(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(action="no_op"))
    assert outcome.status == "applied"
    assert outcome.provider_result["content_sha256"] == "a" * 64


def test_apply_release_reports_a_missing_server(db_session: Session) -> None:
    outcome = ADAPTER.apply_release(db_session, _prepared_action(server_id="MCP-ghost"))
    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_server_not_found"


def test_apply_release_reports_content_drift(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref="stale" + "a" * 59))
    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_server_content_drifted"


def test_apply_release_reports_a_disabled_server(db_session: Session) -> None:
    _seed_project(db_session)
    server = _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID, status="disabled")
    content_sha = _server_content_sha(server)
    outcome = ADAPTER.apply_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "failed"
    assert outcome.error_code == "mcp_server_disabled"


def test_apply_release_reports_a_project_mismatch(db_session: Session) -> None:
    _seed_project(db_session)
    _seed_project(db_session, project_id=OTHER_PROJECT_ID)
    _seed_server(db_session, server_id="MCP-1", project_id=OTHER_PROJECT_ID)
    outcome = ADAPTER.apply_release(db_session, _prepared_action())
    assert outcome.status == "failed"
    assert outcome.error_code == "resource_outside_project"


def test_rollback_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    server = _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID)
    content_sha = _server_content_sha(server)
    outcome = ADAPTER.rollback_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "applied"


def test_observe_release_runs_the_same_verification(db_session: Session) -> None:
    _seed_project(db_session)
    server = _seed_server(db_session, server_id="MCP-1", project_id=PROJECT_ID)
    content_sha = _server_content_sha(server)
    outcome = ADAPTER.observe_release(db_session, _prepared_action(after_ref=content_sha))
    assert outcome.status == "applied"


def test_malformed_target_ref_is_rejected(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad",
        resource_type="mcp_server",
        action="verify",
        target_ref="not-an-mcp-server-ref",
        before_ref=None,
        after_ref="a" * 64,
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="malformed"):
        ADAPTER._verify(db_session, bad)


def test_apply_release_requires_an_after_ref(db_session: Session) -> None:
    bad = PreparedAction(
        resource_pin_id="WSRR-bad-2",
        resource_type="mcp_server",
        action="verify",
        target_ref="mcp-server:MCP-1",
        before_ref=None,
        after_ref=None,
        metadata={},
    )
    with pytest.raises(WorkspaceReleaseAdapterError, match="no after_ref"):
        ADAPTER._verify(db_session, bad)
