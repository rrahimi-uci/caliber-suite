"""``/caliber/mcp-servers`` endpoints — MCP server registry.

External MCP (Model Context Protocol) servers provide tools that workflow
agents can use alongside locally-registered tools. This module handles
CRUD for server connections and a test-connection endpoint that validates
the server is reachable and caches its tool list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes, require_user
from caliber.calibration import aggregate, evaluate_assertion
from caliber.db.models import (
    CaliberMcpServer,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.ids import new_mcp_server_id
from caliber.mcp_gateway import (
    McpGatewayError,
    McpServerConfig,
)
from caliber.mcp_gateway import (
    discover_tools as discover_tools_via_gateway,
)
from caliber.mcp_gateway import (
    invoke_tool as invoke_tool_via_gateway,
)
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
)
from caliber.schemas import (
    McpDiscoveredToolSchema,
    McpDiscoveredToolWithPolicySchema,
    McpDiscoverToolsResponse,
    McpServerCreateRequest,
    McpServerSchema,
    McpServerToolsResponse,
    McpServerUpdateRequest,
    McpToolCalibrationResponse,
    McpToolPolicySchema,
    McpToolPolicyUpdateRequest,
    McpToolPolicyUpdateResponse,
    McpToolTestCasesResponse,
    McpToolTestCasesUpdateRequest,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/mcp-servers"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/mcp-servers/{server_id}"
TEST_PATH = DETAIL_PATH + "/test-connection"
DISCOVER_PATH = DETAIL_PATH + "/discover-tools"
TOOLS_PATH = DETAIL_PATH + "/tools"
TOOL_POLICY_PATH = DETAIL_PATH + "/tools/{tool_name}/policy"
TOOL_TEST_CASES_PATH = DETAIL_PATH + "/tools/{tool_name}/test-cases"
TOOL_CALIBRATE_PATH = DETAIL_PATH + "/tools/{tool_name}/calibrate"
INVOKE_PATH = DETAIL_PATH + "/invoke-tool"
_PLAYGROUND_INVOKE_TIMEOUT_SECONDS = 10.0

_LIST_STATUS_VALUES = frozenset({"active", "error", "disabled", "all"})
_UPDATABLE_FIELDS = (
    "description",
    "transport",
    "uri",
    "command",
    "args",
    "env",
    "headers",
    "auth_type",
    "auth_config",
    "icon",
    "owner",
    "status",
)


def _gateway_config(server: CaliberMcpServer) -> McpServerConfig:
    return McpServerConfig.from_row(server)


def _effective_policy(server: CaliberMcpServer, tool_name: str) -> dict[str, Any]:
    base = {"allowed": True, "side_effect_level": "read", "requires_approval": False}
    raw = server.tool_policies or {}
    override = raw.get(tool_name)
    if isinstance(override, dict):
        base.update({k: v for k, v in override.items() if v is not None})
    return base


async def list_mcp_servers(request: Request) -> JSONResponse:
    require_user(request)
    requested_status = request.query_params.get("status", "all")
    if requested_status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid value for 'status': {requested_status!r}; "
                f"expected one of {sorted(_LIST_STATUS_VALUES)}"
            ),
        )
    factory = get_session_factory(request)
    limit, offset = list_limit(request)
    with factory() as session:
        stmt = select(CaliberMcpServer).order_by(CaliberMcpServer.name)
        if requested_status != "all":
            stmt = stmt.where(CaliberMcpServer.status == requested_status)
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
        items = [McpServerSchema.model_validate(r) for r in rows]
    return envelope_response(items)


async def get_mcp_server(request: Request) -> JSONResponse:
    require_user(request)
    server_id = request.path_params["server_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberMcpServer, server_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        data = McpServerSchema.model_validate(row)
    return envelope_response(data)


async def create_mcp_server(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = McpServerCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    factory = get_session_factory(request)
    with factory() as session:
        existing = (
            session.execute(select(CaliberMcpServer).where(CaliberMcpServer.name == payload.name))
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"MCP server {payload.name!r} already registered",
            )
        server = CaliberMcpServer(
            server_id=new_mcp_server_id(),
            name=payload.name,
            description=payload.description,
            transport=payload.transport,
            uri=payload.uri,
            command=payload.command,
            args=list(payload.args),
            env=dict(payload.env),
            headers=dict(payload.headers),
            auth_type=payload.auth_type,
            auth_config=dict(payload.auth_config),
            tool_policies={},
            icon=payload.icon,
            owner=payload.owner,
            discovered_tools=list(payload.discovered_tools),
            status="active",
        )
        session.add(server)
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="create_mcp_server",
            entity_type="mcp_server",
            entity_id=server.server_id,
            details={"name": server.name, "transport": server.transport},
        )
        session.commit()
        data = McpServerSchema.model_validate(server)
    return envelope_response(data, status_code=201)


async def update_mcp_server(request: Request) -> JSONResponse:
    server_id = request.path_params["server_id"]
    body = await parse_json_object(request)
    payload = McpServerUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(server, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(server, field, new_value)
        if not diff:
            return envelope_response(McpServerSchema.model_validate(server))
        audit_record(
            session,
            actor=actor,
            action="update_mcp_server",
            entity_type="mcp_server",
            entity_id=server.server_id,
            details={"changes": diff},
        )
        session.commit()
        data = McpServerSchema.model_validate(server)
    return envelope_response(data)


def _deployments_referencing_server(session: Session, server_id: str) -> list[str]:
    """``{alias}@{workflow_id}`` for every *active* deployment whose version
    manifest binds a tool on ``server_id`` — used to block a destructive delete
    that would silently orphan a live workflow's MCP binding."""
    deployments = (
        session.execute(
            select(CaliberWorkflowDeployment).where(CaliberWorkflowDeployment.status == "active")
        )
        .scalars()
        .all()
    )
    blocking: list[str] = []
    for deployment in deployments:
        version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version is None:
            continue
        tools = (version.manifest or {}).get("tools", {})
        if any(
            isinstance(binding, dict) and binding.get("server_id") == server_id
            for binding in tools.values()
        ):
            blocking.append(f"{deployment.alias}@{deployment.workflow_id}")
    return blocking


async def delete_mcp_server(request: Request) -> JSONResponse:
    server_id = request.path_params["server_id"]
    actor = require_scopes(request, [SCOPE_ADMIN])
    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        # Block deletion that would orphan a live workflow's MCP binding, rather
        # than silently breaking the next run (the console used to delete blind).
        referencing = _deployments_referencing_server(session, server_id)
        if referencing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"MCP server {server_id!r} is referenced by active deployment(s): "
                    f"{', '.join(referencing)}; undeploy those workflows first"
                ),
            )
        # Snapshot the FULL definition (not just the name) into the audit row so
        # the delete is recoverable / recreatable from history.
        snapshot = McpServerSchema.model_validate(server).model_dump(mode="json")
        audit_record(
            session,
            actor=actor,
            action="delete_mcp_server",
            entity_type="mcp_server",
            entity_id=server.server_id,
            details={"name": server.name, "snapshot": snapshot},
        )
        session.delete(server)
        session.commit()
    return JSONResponse(content=None, status_code=204)


async def test_connection(request: Request) -> JSONResponse:
    """``POST /caliber/mcp-servers/{server_id}/test-connection``

    Connects to the configured MCP server, calls ``tools/list``, and caches
    discovered tools.
    """
    server_id = request.path_params["server_id"]
    require_scopes(request, [SCOPE_ADMIN])

    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")

        # Validate minimum configuration.
        config_errors: list[str] = []
        if server.transport == "stdio" and not server.command:
            config_errors.append("stdio transport requires a 'command'")
        if server.transport in ("sse", "streamable-http") and not server.uri:
            config_errors.append(f"{server.transport} transport requires a 'uri'")

        if config_errors:
            server.status = "error"
            server.connection_error = "; ".join(config_errors)
            session.commit()
            return JSONResponse(
                {
                    "data": {
                        "server_id": server_id,
                        "success": False,
                        "error": server.connection_error,
                        "tools": [],
                    }
                }
            )

        try:
            tools = await discover_tools_via_gateway(_gateway_config(server))
        except McpGatewayError as exc:
            server.status = "error"
            server.connection_error = str(exc)
            session.commit()
            return JSONResponse(
                {
                    "data": {
                        "server_id": server_id,
                        "success": False,
                        "error": server.connection_error,
                        "tools": [],
                    }
                }
            )

        server.discovered_tools = tools
        server.status = "active"
        server.connection_error = None
        server.last_connected_at = datetime.now(timezone.utc)
        session.commit()

        return JSONResponse(
            {
                "data": {
                    "server_id": server_id,
                    "success": True,
                    "error": None,
                    "tools": tools,
                }
            }
        )


async def discover_tools(request: Request) -> JSONResponse:
    """``POST /caliber/mcp-servers/{server_id}/discover-tools``.

    Performs a discover refresh and persists discovered tools.
    """
    server_id = request.path_params["server_id"]
    require_scopes(request, [SCOPE_ADMIN])

    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")

        try:
            tools = await discover_tools_via_gateway(_gateway_config(server))
        except McpGatewayError as exc:
            server.status = "error"
            server.connection_error = str(exc)
            session.commit()
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        typed_tools = [McpDiscoveredToolSchema.model_validate(tool) for tool in tools]
        server.discovered_tools = tools
        server.status = "active"
        server.connection_error = None
        server.last_connected_at = datetime.now(timezone.utc)
        session.commit()
        return envelope_response(
            McpDiscoverToolsResponse(
                server_id=server_id,
                tools=typed_tools,
                tool_count=len(typed_tools),
                discovered_at=server.last_connected_at,
            )
        )


async def list_tools(request: Request) -> JSONResponse:
    """``GET /caliber/mcp-servers/{server_id}/tools``.

    Returns discovered tools plus effective policy for each tool.
    """
    require_user(request)
    server_id = request.path_params["server_id"]
    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        tools: list[McpDiscoveredToolWithPolicySchema] = []
        for raw in server.discovered_tools or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            tools.append(
                McpDiscoveredToolWithPolicySchema.model_validate(
                    {
                        **raw,
                        "policy": _effective_policy(server, name),
                    }
                )
            )
        return envelope_response(McpServerToolsResponse(server_id=server_id, tools=tools))


async def update_tool_policy(request: Request) -> JSONResponse:
    """``PATCH /caliber/mcp-servers/{server_id}/tools/{tool_name}/policy``."""
    server_id = request.path_params["server_id"]
    tool_name = request.path_params["tool_name"]
    actor = require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request)
    payload = McpToolPolicyUpdateRequest.model_validate(body)
    patch = payload.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        known_names = {
            str(tool.get("name", "")).strip()
            for tool in (server.discovered_tools or [])
            if isinstance(tool, dict)
        }
        if tool_name not in known_names:
            raise HTTPException(
                status_code=404,
                detail=f"tool {tool_name!r} not found on MCP server {server_id!r}",
            )

        existing = _effective_policy(server, tool_name)
        next_policy = dict(existing)
        next_policy.update(patch)
        validated = McpToolPolicySchema.model_validate(next_policy).model_dump(exclude_none=True)
        policies = dict(server.tool_policies or {})
        policies[tool_name] = validated
        server.tool_policies = policies
        audit_record(
            session,
            actor=actor,
            action="update_mcp_tool_policy",
            entity_type="mcp_server",
            entity_id=server.server_id,
            details={"tool_name": tool_name, "policy": validated},
        )
        session.commit()
        return envelope_response(
            McpToolPolicyUpdateResponse(
                server_id=server_id,
                tool_name=tool_name,
                policy=McpToolPolicySchema.model_validate(validated),
            )
        )


async def _invoke_mcp_tool(
    server: CaliberMcpServer, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Invoke one discovered MCP tool through the gateway, honoring policy.

    Shared by the one-off ``invoke-tool`` endpoint and the scored ``calibrate``
    endpoint so both go through the identical known-tool + policy + gateway
    path. Returns ``{server_id, tool_name, success, error, result, duration_ms}``.
    """
    server_id = server.server_id
    known_names = {t["name"] for t in (server.discovered_tools or [])}
    if known_names and tool_name not in known_names:
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "success": False,
            "error": f"Tool {tool_name!r} not found on this server",
            "result": None,
            "duration_ms": 0,
        }
    policy = _effective_policy(server, tool_name)
    if not bool(policy.get("allowed", True)):
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "success": False,
            "error": f"Tool {tool_name!r} is blocked by policy",
            "result": None,
            "duration_ms": 0,
        }

    started = perf_counter()
    try:
        result = await invoke_tool_via_gateway(
            _gateway_config(server),
            tool_name=tool_name,
            arguments=arguments,
            timeout_seconds=_PLAYGROUND_INVOKE_TIMEOUT_SECONDS,
        )
    except McpGatewayError as exc:
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "success": False,
            "error": str(exc),
            "result": None,
            "duration_ms": round((perf_counter() - started) * 1000),
        }
    return {
        "server_id": server_id,
        "tool_name": tool_name,
        "success": True,
        "error": None,
        "result": result,
        "duration_ms": round((perf_counter() - started) * 1000),
    }


async def invoke_tool(request: Request) -> JSONResponse:
    """``POST /caliber/mcp-servers/{server_id}/invoke-tool``

    Invokes a discovered MCP tool via the configured transport.
    """
    server_id = request.path_params["server_id"]
    require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request)
    tool_name = body.get("tool_name", "")
    arguments = body.get("arguments", {})

    if not tool_name:
        raise HTTPException(status_code=400, detail="'tool_name' is required")

    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        data = await _invoke_mcp_tool(server, tool_name, arguments)
    return JSONResponse({"data": data})


def _known_tool_names(server: CaliberMcpServer) -> set[str]:
    return {
        str(tool.get("name", "")).strip()
        for tool in (server.discovered_tools or [])
        if isinstance(tool, dict)
    }


async def save_mcp_tool_test_cases(request: Request) -> JSONResponse:
    """``PUT /caliber/mcp-servers/{id}/tools/{tool}/test-cases`` — persist cases."""
    server_id = request.path_params["server_id"]
    tool_name = request.path_params["tool_name"]
    body = await parse_json_object(request)
    payload = McpToolTestCasesUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])

    cases = [case.model_dump() for case in payload.test_cases]
    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        if tool_name not in _known_tool_names(server):
            raise HTTPException(
                status_code=404,
                detail=f"tool {tool_name!r} not found on MCP server {server_id!r}",
            )
        all_cases = dict(server.tool_test_cases or {})
        all_cases[tool_name] = cases
        server.tool_test_cases = all_cases
        audit_record(
            session,
            actor=actor,
            action="update_mcp_tool_test_cases",
            entity_type="mcp_server",
            entity_id=server.server_id,
            details={"tool_name": tool_name, "count": len(cases)},
        )
        session.commit()
    return envelope_response(
        McpToolTestCasesResponse(
            server_id=server_id, tool_name=tool_name, test_cases=payload.test_cases
        )
    )


async def calibrate_mcp_tool(request: Request) -> JSONResponse:
    """``POST /caliber/mcp-servers/{id}/tools/{tool}/calibrate`` — score cases.

    Runs every saved test case through the same gateway path ``invoke_tool``
    uses, scores each against its assertion, and stores the aggregate result in
    ``tool_calibrations[tool_name]``.
    """
    server_id = request.path_params["server_id"]
    tool_name = request.path_params["tool_name"]
    actor = require_scopes(request, [SCOPE_OPERATOR])

    factory = get_session_factory(request)
    with factory() as session:
        server = session.get(CaliberMcpServer, server_id)
        if server is None:
            raise HTTPException(status_code=404, detail=f"MCP server {server_id!r} not found")
        if tool_name not in _known_tool_names(server):
            raise HTTPException(
                status_code=404,
                detail=f"tool {tool_name!r} not found on MCP server {server_id!r}",
            )
        saved_cases = list((server.tool_test_cases or {}).get(tool_name, []))
        if not saved_cases:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"tool {tool_name!r} on MCP server {server_id!r} has no saved "
                    "test cases to calibrate against"
                ),
            )

        scored: list[dict[str, Any]] = []
        for case in saved_cases:
            name = str(case.get("name", "")) or "case"
            raw_input = case.get("input")
            case_input: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}
            invocation = await _invoke_mcp_tool(server, tool_name, case_input)
            error = invocation["error"] if not invocation["success"] else None
            passed = evaluate_assertion(case.get("assertion"), invocation["result"], error)
            scored.append(
                {
                    "name": name,
                    "passed": passed,
                    "output": invocation["result"],
                    "error": error,
                    "duration_ms": invocation["duration_ms"],
                }
            )

        result = aggregate(scored)
        result["ran_at"] = datetime.now(timezone.utc).isoformat()
        calibrations = dict(server.tool_calibrations or {})
        calibrations[tool_name] = result
        server.tool_calibrations = calibrations
        audit_record(
            session,
            actor=actor,
            action="calibrate_mcp_tool",
            entity_type="mcp_server",
            entity_id=server.server_id,
            details={
                "tool_name": tool_name,
                "pass_rate": result["pass_rate"],
                "total": result["total"],
                "passed": result["passed"],
            },
        )
        session.commit()

    return envelope_response(
        McpToolCalibrationResponse(server_id=server_id, tool_name=tool_name, **result)
    )


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_mcp_servers, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_mcp_server, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_mcp_server, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_mcp_server, methods=["PATCH"]))
    app.routes.append(Route(DETAIL_PATH, delete_mcp_server, methods=["DELETE"]))
    app.routes.append(Route(TEST_PATH, test_connection, methods=["POST"]))
    app.routes.append(Route(DISCOVER_PATH, discover_tools, methods=["POST"]))
    app.routes.append(Route(TOOLS_PATH, list_tools, methods=["GET"]))
    app.routes.append(Route(TOOL_POLICY_PATH, update_tool_policy, methods=["PATCH"]))
    app.routes.append(Route(TOOL_TEST_CASES_PATH, save_mcp_tool_test_cases, methods=["PUT"]))
    app.routes.append(Route(TOOL_CALIBRATE_PATH, calibrate_mcp_tool, methods=["POST"]))
    app.routes.append(Route(INVOKE_PATH, invoke_tool, methods=["POST"]))
