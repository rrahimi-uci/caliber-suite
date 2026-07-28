"""Fail-closed MCP execution policy and deployment preflight.

This module deliberately calls local stdio controls *containment*, not a
sandbox.  Exact command/host allowlists, a sanitized environment, a private
working directory, and process timeouts reduce ambient authority; only an
an explicitly attested managed sidecar or a non-loopback HTTPS MCP endpoint
counts as an external execution boundary for production deployment preflight.
Local wrappers, including the shipped bubblewrap profile, remain containment.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from caliber.config import CaliberConfig


class McpPolicyError(RuntimeError):
    """An MCP target violates configured execution policy."""


@dataclass(frozen=True)
class McpExecutionReadiness:
    ready: bool
    transport_ready: bool
    status_ready: bool
    boundary: str
    production_isolated: bool
    command_allowed: bool | None
    executable_available: bool | None
    remote_host_allowed: bool | None
    controls: tuple[str, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "transport_ready": self.transport_ready,
            "status_ready": self.status_ready,
            "boundary": self.boundary,
            "production_isolated": self.production_isolated,
            "command_allowed": self.command_allowed,
            "executable_available": self.executable_available,
            "remote_host_allowed": self.remote_host_allowed,
            "controls": list(self.controls),
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class McpDependency:
    label: str
    server_id: str
    tool_name: str
    requires_approval: bool
    declared_side_effect: str | None


@dataclass
class _ExecutionParts:
    boundary: str = "none"
    production_isolated: bool = False
    command_allowed: bool | None = None
    executable_available: bool | None = None
    remote_host_allowed: bool | None = None
    controls: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
_MAX_RATE_KEYS = 10_000
_BUBBLEWRAP_TMP = "/tmp"  # noqa: S108 - private tmpfs inside the new namespace.
_BUBBLEWRAP_PROFILE_ARGS = (
    "--unshare-all",
    "--die-with-parent",
    "--new-session",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "--dev",
    "/dev",
    "--tmpfs",
    _BUBBLEWRAP_TMP,
    "--chdir",
    _BUBBLEWRAP_TMP,
)
_FORBIDDEN_STDIO_ENV = frozenset(
    {
        "BASH_ENV",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "ENV",
        "HOME",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PATH",
        "PERL5OPT",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "RUBYOPT",
        "SHELL",
    }
)


def execution_readiness(
    server: Any,
    *,
    config: CaliberConfig | None = None,
) -> McpExecutionReadiness:
    config = config or CaliberConfig.load()
    transport = str(getattr(server, "transport", "")).strip().lower()
    status = str(getattr(server, "status", "active")).strip().lower()
    status_ready = status == "active"
    if transport == "stdio":
        parts = _stdio_execution_parts(server, config)
    elif transport in {"sse", "streamable-http"}:
        parts = _remote_execution_parts(server, config, transport)
    else:
        parts = _ExecutionParts(
            blockers=[
                f"unsupported MCP transport {transport!r}; expected stdio, sse, or streamable-http"
            ]
        )

    auth_type = str(getattr(server, "auth_type", "none")).strip().lower()
    if auth_type == "oauth":
        parts.blockers.append(
            "OAuth MCP authentication is not implemented; use token auth or an operator-managed proxy"
        )
    elif auth_type not in {"none", "token", "basic", "custom"}:
        parts.blockers.append(f"unsupported MCP authentication type {auth_type!r}")

    if not status_ready:
        parts.blockers.append(f"MCP server status is {status!r}, not 'active'")
    transport_blockers = [
        item for item in parts.blockers if not item.startswith("MCP server status is")
    ]
    return McpExecutionReadiness(
        ready=status_ready and not transport_blockers,
        transport_ready=not transport_blockers,
        status_ready=status_ready,
        boundary=parts.boundary,
        production_isolated=parts.production_isolated,
        command_allowed=parts.command_allowed,
        executable_available=parts.executable_available,
        remote_host_allowed=parts.remote_host_allowed,
        controls=tuple(parts.controls),
        blockers=tuple(parts.blockers),
        warnings=tuple(parts.warnings),
    )


def _stdio_execution_parts(server: Any, config: CaliberConfig) -> _ExecutionParts:
    parts = _ExecutionParts(
        boundary="local_containment",
        controls=["no_shell", "sanitized_environment", "command_allowlist"],
    )
    if config.mcp_stdio_isolated_workdir:
        parts.controls.append("private_working_directory")
    else:
        parts.warnings.append("stdio MCP inherits the CALIBER working directory")
    _check_stdio_command(server, config, parts)
    _check_stdio_environment(server, parts)
    try:
        prefix = isolation_prefix(config)
    except McpPolicyError as exc:
        prefix = ()
        parts.blockers.append(str(exc))
    if prefix:
        parts.boundary = "external_wrapper"
        parts.controls.append("operator_isolation_wrapper")
        if _executable_path(prefix[0]) is None:
            parts.blockers.append(
                f"MCP isolation wrapper executable {prefix[0]!r} is not available"
            )
        elif _is_verified_isolation_prefix(prefix, config):
            parts.boundary = "bubblewrap"
            parts.controls.extend(
                ["verified_namespace_profile", "network_namespace", "read_only_host_root"]
            )
            parts.warnings.append(
                "bubblewrap containment exposes host files read-only and is not accepted as a production boundary"
            )
        else:
            parts.warnings.append(
                "configured wrapper is not a verified isolation profile and is not accepted for production"
            )
    else:
        parts.warnings.append("local stdio containment does not block filesystem or network access")
    return parts


def _check_stdio_command(
    server: Any,
    config: CaliberConfig,
    parts: _ExecutionParts,
) -> None:
    command = str(getattr(server, "command", "")).strip()
    if not command:
        parts.blockers.append("stdio transport requires a non-empty command")
        parts.command_allowed = False
        parts.executable_available = False
        return
    resolved = resolve_stdio_command(command)
    parts.command_allowed = _command_is_allowed(command, resolved, config)
    if not parts.command_allowed:
        parts.blockers.append(
            f"stdio command {command!r} is not in CALIBER_MCP_STDIO_COMMAND_ALLOWLIST"
        )
    parts.executable_available = _executable_path(resolved) is not None
    if not parts.executable_available:
        parts.blockers.append(f"stdio executable {resolved!r} is not available")
    if _same_executable(resolved, sys.executable) and not _python_target_allowed(server, config):
        parts.blockers.append(
            "CALIBER Python MCP commands must use an allowlisted '-m' module or "
            "an absolute path in CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST"
        )


def _python_target_allowed(server: Any, config: CaliberConfig) -> bool:
    args = [str(arg) for arg in (getattr(server, "args", ()) or ())]
    allowed_modules = set(_csv_items(config.mcp_stdio_python_module_allowlist))
    allowed_scripts = {
        os.path.realpath(item)
        for item in _csv_items(config.mcp_stdio_python_script_allowlist)
        if Path(item).is_absolute()
    }
    module_allowed = len(args) > 1 and args[0] == "-m" and args[1] in allowed_modules
    script_allowed = bool(
        args and Path(args[0]).is_absolute() and os.path.realpath(args[0]) in allowed_scripts
    )
    return module_allowed or script_allowed


def _check_stdio_environment(server: Any, parts: _ExecutionParts) -> None:
    env = getattr(server, "env", {}) or {}
    if not isinstance(env, dict):
        return
    unsafe_env = sorted(str(key) for key in env if str(key).upper() in _FORBIDDEN_STDIO_ENV)
    if unsafe_env:
        parts.blockers.append(
            "stdio environment overrides protected process variables: " + ", ".join(unsafe_env)
        )


def _is_verified_isolation_prefix(
    prefix: tuple[str, ...],
    config: CaliberConfig,
) -> bool:
    if config.mcp_stdio_isolation_profile != "bubblewrap":
        return False
    if Path(prefix[0]).name not in {"bwrap", "bubblewrap"}:
        return False
    # Exact argv is intentional: accepting a required subset would allow a
    # later ``--share-net``, writable bind, or injected command to silently
    # weaken/bypass the boundary while still passing deployment preflight.
    return prefix[1:] == _BUBBLEWRAP_PROFILE_ARGS


def _remote_execution_parts(
    server: Any,
    config: CaliberConfig,
    transport: str,
) -> _ExecutionParts:
    parts = _ExecutionParts(boundary="remote_transport", controls=["remote_host_allowlist"])
    parsed = urlsplit(str(getattr(server, "uri", "")).strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        parts.blockers.append(f"{transport} transport requires an absolute HTTP(S) URI")
        parts.remote_host_allowed = False
        return parts
    if parsed.username is not None or parsed.password is not None:
        parts.blockers.append("MCP URI must not contain embedded credentials")
        parts.remote_host_allowed = False
        return parts
    parts.remote_host_allowed = host in _csv_values(config.mcp_remote_host_allowlist)
    if not parts.remote_host_allowed:
        parts.blockers.append(
            f"remote MCP host {host!r} is not in CALIBER_MCP_REMOTE_HOST_ALLOWLIST"
        )
    managed_sidecar = host in _csv_values(config.mcp_managed_sidecar_hosts)
    if (
        parsed.scheme == "http"
        and not _is_loopback(host)
        and not config.mcp_allow_insecure_http
        and not managed_sidecar
    ):
        parts.blockers.append(
            "plain HTTP is disabled for non-loopback MCP hosts; use HTTPS or explicitly opt in"
        )
    if managed_sidecar:
        parts.boundary = "managed_sidecar"
        parts.production_isolated = True
        parts.controls.append("operator_attested_sidecar_boundary")
        parts.warnings.append(
            "managed-sidecar isolation is supplied and attested by the deployment operator"
        )
    elif parsed.scheme == "https" and not _is_loopback(host):
        parts.boundary = "remote_https"
        parts.production_isolated = True
    else:
        parts.warnings.append(
            "local or insecure remote transport is not treated as a production isolation boundary"
        )
    return parts


def resolve_stdio_command(command: str) -> str:
    return sys.executable if command.strip() == "${PYTHON}" else command.strip()


def isolation_prefix(config: CaliberConfig) -> tuple[str, ...]:
    raw = config.mcp_stdio_isolation_prefix.strip()
    if not raw:
        return ()
    try:
        parsed = tuple(shlex.split(raw))
    except ValueError as exc:
        raise McpPolicyError(f"invalid CALIBER_MCP_STDIO_ISOLATION_PREFIX: {exc}") from exc
    if not parsed:
        raise McpPolicyError("CALIBER_MCP_STDIO_ISOLATION_PREFIX must contain an executable")
    return parsed


def stdio_launch(
    server: Any,
    *,
    config: CaliberConfig | None = None,
) -> tuple[str, list[str]]:
    config = config or CaliberConfig.load()
    readiness = execution_readiness(server, config=config)
    if not readiness.transport_ready:
        raise McpPolicyError("; ".join(readiness.blockers))
    command = resolve_stdio_command(str(getattr(server, "command", "")))
    args = [str(arg) for arg in (getattr(server, "args", ()) or ()) if str(arg) != ""]
    command_path = _executable_path(command)
    if command_path is None:  # Defensive: readiness above already checks this.
        raise McpPolicyError(f"stdio executable {command!r} is not available")
    prefix = isolation_prefix(config)
    if prefix:
        prefix_path = _executable_path(prefix[0])
        if prefix_path is None:  # Defensive: readiness above already checks this.
            raise McpPolicyError(f"MCP isolation wrapper executable {prefix[0]!r} is not available")
        return prefix_path, [*prefix[1:], command_path, *args]
    return command_path, args


def validate_transport(server: Any, *, config: CaliberConfig | None = None) -> None:
    readiness = execution_readiness(server, config=config)
    if not readiness.transport_ready:
        raise McpPolicyError("; ".join(readiness.blockers))


def effective_tool_policy(server: Any, tool_name: str) -> dict[str, Any]:
    policy: dict[str, Any] = {
        # Missing policy is deliberately denied.  Discovery establishes tool
        # identity, not operator intent or an effect classification.
        "allowed": False,
        "side_effect_level": "read",
        "requires_approval": False,
        "rate_limit_per_minute": None,
    }
    policies = getattr(server, "tool_policies", {}) or {}
    override = policies.get(tool_name) if isinstance(policies, dict) else None
    if isinstance(override, dict):
        policy.update({key: value for key, value in override.items() if value is not None})
    return policy


def validate_tool_access(
    server: Any,
    tool_name: str,
    *,
    consume_rate: bool = True,
    config: CaliberConfig | None = None,
) -> dict[str, Any]:
    readiness = execution_readiness(server, config=config)
    if not readiness.ready:
        raise McpPolicyError("; ".join(readiness.blockers))
    known = {
        str(item.get("name", "")).strip()
        for item in (getattr(server, "discovered_tools", ()) or ())
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    if not known:
        raise McpPolicyError("MCP tools have not been discovered; test the connection first")
    if tool_name not in known:
        raise McpPolicyError(f"MCP tool {tool_name!r} is not in the discovered tool allowlist")
    policies = getattr(server, "tool_policies", {}) or {}
    explicit = policies.get(tool_name) if isinstance(policies, dict) else None
    required_fields = {"allowed", "side_effect_level", "requires_approval"}
    if not isinstance(explicit, dict) or not required_fields.issubset(explicit):
        raise McpPolicyError(
            f"MCP tool {tool_name!r} has no explicit allow, side-effect, and approval classification"
        )
    if not isinstance(explicit.get("allowed"), bool):
        raise McpPolicyError(f"MCP tool {tool_name!r} has an invalid allowed classification")
    if explicit.get("side_effect_level") not in {"read", "write", "external_action"}:
        raise McpPolicyError(f"MCP tool {tool_name!r} has an invalid side-effect classification")
    if not isinstance(explicit.get("requires_approval"), bool):
        raise McpPolicyError(f"MCP tool {tool_name!r} has an invalid approval classification")
    policy = effective_tool_policy(server, tool_name)
    if not bool(policy.get("allowed", True)):
        raise McpPolicyError(f"MCP tool {tool_name!r} is blocked by policy")
    limit = policy.get("rate_limit_per_minute")
    if consume_rate and isinstance(limit, int):
        _consume_rate(str(getattr(server, "server_id", "")), tool_name, limit)
    return policy


def deployment_blockers(
    session: Any,
    manifest: dict[str, Any],
    *,
    alias: str,
    config: CaliberConfig | None = None,
) -> list[str]:
    """Return deterministic MCP dependency blockers for a deployment."""

    from caliber.db.models import CaliberMcpServer  # noqa: PLC0415

    config = config or CaliberConfig.load()
    require_external = alias in _csv_values(config.mcp_require_external_isolation_for_aliases)
    blockers: list[str] = []
    for dependency in extract_dependencies(manifest):
        server = session.get(CaliberMcpServer, dependency.server_id)
        prefix = dependency.label
        if server is None:
            blockers.append(f"{prefix}: MCP server {dependency.server_id!r} does not exist")
            continue
        readiness = execution_readiness(server, config=config)
        blockers.extend(f"{prefix}: {item}" for item in readiness.blockers)
        if require_external and not readiness.production_isolated:
            blockers.append(
                f"{prefix}: alias {alias!r} requires an external MCP isolation boundary"
            )
        if server.last_connected_at is None:
            blockers.append(f"{prefix}: MCP connection has never completed a live discovery")
        raw_policies = getattr(server, "tool_policies", {}) or {}
        explicit_policy = (
            raw_policies.get(dependency.tool_name) if isinstance(raw_policies, dict) else None
        )
        required_fields = {"allowed", "side_effect_level", "requires_approval"}
        if not isinstance(explicit_policy, dict) or not required_fields.issubset(explicit_policy):
            blockers.append(
                f"{prefix}: MCP tool {dependency.tool_name!r} has no explicit allow, side-effect, and approval classification"
            )
            continue
        try:
            policy = validate_tool_access(
                server,
                dependency.tool_name,
                consume_rate=False,
                config=config,
            )
        except McpPolicyError as exc:
            blockers.append(f"{prefix}: {exc}")
            continue
        if bool(policy.get("requires_approval")) and not dependency.requires_approval:
            blockers.append(
                f"{prefix}: tool policy requires approval but the workflow binding does not"
            )
        side_effect = str(policy.get("side_effect_level", "read"))
        if dependency.declared_side_effect is None and side_effect != "read":
            blockers.append(
                f"{prefix}: direct MCP resource cannot deploy a {side_effect!r} tool without an approval-capable binding"
            )
        elif dependency.declared_side_effect is not None and _side_effect_rank(
            dependency.declared_side_effect
        ) < _side_effect_rank(side_effect):
            blockers.append(
                f"{prefix}: workflow declares {dependency.declared_side_effect!r} but policy classifies the tool as {side_effect!r}"
            )
    return list(dict.fromkeys(blockers))


def extract_dependencies(manifest: dict[str, Any]) -> list[McpDependency]:
    dependencies: list[McpDependency] = []
    tools = manifest.get("tools", {})
    if isinstance(tools, dict):
        for local_name, raw in sorted(tools.items(), key=lambda item: str(item[0])):
            if not isinstance(raw, dict):
                continue
            if raw.get("type") != "mcp_tool" and not {"server_id", "tool_name"}.issubset(raw):
                continue
            dependencies.append(
                McpDependency(
                    label=f"tool binding {str(local_name)!r}",
                    server_id=str(raw.get("server_id", "")),
                    tool_name=str(raw.get("tool_name", "")),
                    requires_approval=bool(raw.get("requires_approval", False)),
                    declared_side_effect=str(raw.get("side_effect_level", "read")),
                )
            )
    nodes = manifest.get("nodes", [])
    node_items: list[tuple[str, object]] = []
    if isinstance(nodes, dict):
        node_items = [(str(node_id), raw) for node_id, raw in nodes.items()]
    elif isinstance(nodes, list):
        node_items = [
            (str(raw.get("id", "")) if isinstance(raw, dict) else "", raw) for raw in nodes
        ]
    for node_id, raw in node_items:
        if not isinstance(raw, dict) or raw.get("type") != "mcp_resource":
            continue
        dependencies.append(
            McpDependency(
                label=f"MCP resource node {node_id!r}",
                server_id=str(raw.get("server_id", "")),
                tool_name=str(raw.get("tool_name", "")),
                requires_approval=False,
                declared_side_effect=None,
            )
        )
    return dependencies


def _consume_rate(server_id: str, tool_name: str, limit: int) -> None:
    now = time.monotonic()
    cutoff = now - 60.0
    key = (server_id, tool_name)
    with _RATE_LOCK:
        window = _RATE_WINDOWS.setdefault(key, deque())
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= limit:
            raise McpPolicyError(
                f"MCP tool {tool_name!r} exceeded its process-local limit of {limit}/minute"
            )
        window.append(now)
        _RATE_WINDOWS.move_to_end(key)
        while len(_RATE_WINDOWS) > _MAX_RATE_KEYS:
            _RATE_WINDOWS.popitem(last=False)


def _reset_rate_limits_for_tests() -> None:
    with _RATE_LOCK:
        _RATE_WINDOWS.clear()


def _csv_values(raw: str) -> set[str]:
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _csv_items(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _command_is_allowed(command: str, resolved: str, config: CaliberConfig) -> bool:
    command_path = _canonical_executable(command)
    resolved_path = _canonical_executable(resolved)
    for allowed in _csv_items(config.mcp_stdio_command_allowlist):
        if allowed == "${PYTHON}":
            if command.strip() == "${PYTHON}":
                return True
            continue
        allowed_resolved = resolve_stdio_command(allowed)
        if command == allowed or resolved == allowed_resolved:
            return True
        allowed_path = _canonical_executable(allowed_resolved)
        if allowed_path is not None and allowed_path in {command_path, resolved_path}:
            return True
    return False


def _canonical_executable(command: str) -> str | None:
    path = _executable_path(command)
    return os.path.realpath(path) if path else None


def _same_executable(left: str, right: str) -> bool:
    left_path = _canonical_executable(left)
    right_path = _canonical_executable(right)
    return left_path is not None and left_path == right_path


def _executable_path(command: str) -> str | None:
    if not command:
        return None
    path = Path(command)
    if path.is_absolute() or os.sep in command:
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(command)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _side_effect_rank(value: str) -> int:
    return {"read": 0, "write": 1, "external_action": 2}.get(value, 99)


__all__ = [
    "McpExecutionReadiness",
    "McpPolicyError",
    "deployment_blockers",
    "effective_tool_policy",
    "execution_readiness",
    "extract_dependencies",
    "isolation_prefix",
    "resolve_stdio_command",
    "stdio_launch",
    "validate_tool_access",
    "validate_transport",
]
