"""A stateful stand-in for the routes cookbook 05 calls.

``test_cookbook_05_example.py`` replays the real example script against this. The MCP side is real
in one respect: a call runs the lab's own server (``assets/server/repo_tracker_mcp.py``) in
process and writes its ledger file, so the data, the tool errors and the "did it reach the server"
proof are the real ones. What the fake imitates is CALIBER: it copies the gateway's admission
rules (unclassified, blocked, unknown, rate limited), the policy rules, the stdio allowlist and
the egress refusal for loopback HTTP, and it answers in the server's shapes. That the real CALIBER
behaves this way is shown by the script's run against a live stack (see the lab README) and by
``caliber/tests``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from caliber_sdk import CaliberClient

LAB = Path(__file__).resolve().parents[3] / "docs-site/cookbooks/05-mcp-connectivity-governance-lab"
ADMIN = {"calpat_admin": "admin"}
OPERATOR = {"calpat_admin": "admin", "calpat_operator": "lab05-operator"}
SECOND_PERSON = "the account that triggered this run cannot approve it; a second person must decide"
PLAYGROUND_TIMEOUT_SECONDS = 10


def _tracker() -> Any:
    pytest.importorskip("mcp", reason="the lab's MCP server needs the mcp package")
    spec = importlib.util.spec_from_file_location(
        "cookbook05_repo_tracker", LAB / "assets/server/repo_tracker_mcp.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # No bytecode next to the lab's files: a __pycache__ in the lab folder is not a lab asset.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeMcpServer:
    def __init__(self) -> None:
        self.tracker = _tracker()
        self.mcp = self.tracker.build_server()
        self.servers: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[str]] = {}
        self.workflows: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.traces: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self.rate_windows: dict[tuple[str, str], int] = {}
        self.accounts: set[str] = set()
        self.calls: list[str] = []
        self._n = 0

    def client(self, token: str) -> CaliberClient:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: self.handle(r, token)))
        return CaliberClient("https://caliber.test", token=token, http_client=http)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"

    def _who(self, token: str) -> str:
        return {
            "calpat_admin": "admin",
            "calpat_operator": "lab05-operator",
            "calpat_approver": "lab05-approver",
            "calpat_viewer": "lab05-viewer",
        }[token]

    def _log(self, token: str, action: str, entity_id: str) -> None:
        self.audit.append(
            {
                "actor": self._who(token),
                "action": action,
                "entity_type": "mcp_server",
                "entity_id": entity_id,
            }
        )
        self.history.setdefault(entity_id, []).insert(0, action)

    def handle(self, request: httpx.Request, token: str) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else {}
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                return handler(body, token, *match.groups())  # type: ignore[no-any-return]
        raise AssertionError(f"example called an unstubbed route: {request.method} {path}")

    def _routes(self) -> list[tuple[str, str, Any]]:
        s = r"/mcp-servers/([^/]+)"
        return [
            (
                "POST",
                r"/projects",
                lambda b, t: _envelope({"project_id": "PRJ-1", "name": b["name"]}, 201),
            ),
            (
                "POST",
                r"/projects/([^/]+)/members",
                lambda b, t, p: _envelope({"user_id": b["user_id"]}, 201),
            ),
            ("POST", r"/mcp-servers", self.create_server),
            (
                "GET",
                r"/mcp-servers",
                lambda b, t: _envelope([self._public(x) for x in self.servers.values()]),
            ),
            ("GET", s + r"/history", self.server_history),
            ("POST", s + r"/test-connection", self.test_connection),
            ("POST", s + r"/discover-tools", self.discover),
            ("GET", s + r"/tools", self.list_tools),
            ("PATCH", s + r"/tools/([^/]+)/policy", self.set_policy),
            ("PUT", s + r"/tools/([^/]+)/test-cases", self.save_cases),
            ("POST", s + r"/tools/([^/]+)/calibrate", self.calibrate),
            ("POST", s + r"/invoke-tool", self.invoke),
            ("POST", r"/workflows/import/preview", self.preview),
            ("POST", r"/workflows/import", self.import_workflow),
            ("GET", r"/workflows/([^/]+)/versions", self.versions),
            ("POST", r"/workflow-runs", self.submit),
            ("GET", r"/workflow-runs/([^/]+)", self.get_run),
            ("POST", r"/workflow-runs/([^/]+)/approval/approve", self.approve),
            ("POST", r"/workflow-runs/([^/]+)/resume", self.resume),
            ("GET", r"/workflow-runs/([^/]+)/trace", self.run_trace),
            (
                "GET",
                r"/observability/traces",
                lambda b, t: _envelope({"traces": list(reversed(self.traces))}),
            ),
            ("GET", r"/observability/traces/([^/]+)", self.trace_detail),
            ("GET", r"/audit-log", lambda b, t: _envelope({"entries": list(reversed(self.audit))})),
            ("GET", r"/cookbooks", self.cookbooks),
            ("POST", r"/cookbooks/05/install", self.install),
        ]

    # -- servers ------------------------------------------------------------------------------
    def _execution(self, body: dict[str, Any]) -> dict[str, Any]:
        if body["transport"] == "stdio":
            args = body.get("args", [])
            command = body.get("command", "")
            allowed = command == "${PYTHON}" and bool(args) and args[0] == str(self.script)
            blockers = []
            if command != "${PYTHON}":
                blockers.append(
                    f"stdio command {command!r} is not in CALIBER_MCP_STDIO_COMMAND_ALLOWLIST"
                )
            elif not allowed:
                blockers.append(
                    "CALIBER Python MCP commands must use an allowlisted '-m' module or an "
                    "absolute path in CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST"
                )
            return {
                "ready": not blockers,
                "boundary": "local_containment",
                "command_allowed": command == "${PYTHON}",
                "blockers": blockers,
                "warnings": ["local stdio containment does not block filesystem or network access"],
            }
        host = re.sub(r"^https?://([^/:]+).*", r"\1", body.get("uri", ""))
        blockers = (
            [f"remote MCP host {host!r} resolves into the blocked loopback range; allowlisting a "
             "name does not authorize the address it points at"]
            if host in {"127.0.0.1", "localhost"}
            else []
        )  # fmt: skip
        return {
            "ready": not blockers,
            "boundary": "remote_transport",
            "blockers": blockers,
            "warnings": [],
        }

    @property
    def script(self) -> Path:
        return LAB / "assets/server/repo_tracker_mcp.py"

    def _public(self, server: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in server.items() if not k.startswith("_")}

    def create_server(self, body: Any, token: str) -> httpx.Response:
        if token not in ADMIN:
            return _error(403, "missing required scope; expected one of ['caliber.admin']")
        if any(s["name"] == body["name"] for s in self.servers.values()):
            return _error(409, f"MCP server {body['name']!r} already registered")
        server_id = self._id("MCP")
        self.servers[server_id] = {
            "server_id": server_id,
            "status": "active",
            "discovered_tools": [],
            "tool_policies": {},
            "tool_test_cases": {},
            "execution": self._execution(body),
            **body,
        }
        self._log(token, "create_mcp_server", server_id)
        return _envelope(self._public(self.servers[server_id]), 201)

    def server_history(self, body: Any, token: str, server_id: str) -> httpx.Response:
        return _envelope([{"action": a, "actor": "admin"} for a in self.history.get(server_id, [])])

    def _ledger_env(self, server: dict[str, Any]) -> str:
        args = server.get("args", [])
        return args[args.index("--ledger") + 1] if "--ledger" in args else ""

    def _discover(self, server: dict[str, Any]) -> dict[str, Any]:
        execution = server["execution"]
        if not execution["ready"]:
            return {"success": False, "error": "; ".join(execution["blockers"]), "tools": []}

        async def listing() -> list[Any]:
            listed: list[Any] = await self.mcp.list_tools()
            return listed

        tools = [
            {
                "name": t.name,
                "description": t.description or "",
                "input_schema": dict(t.input_schema),
                "output_schema": t.output_schema,
            }
            for t in anyio.run(listing)
        ]
        server["discovered_tools"] = tools
        return {"success": True, "error": None, "tools": tools}

    def test_connection(self, body: Any, token: str, server_id: str) -> httpx.Response:
        if token not in ADMIN:
            return _error(403, "missing required scope; expected one of ['caliber.admin']")
        found = self._discover(self.servers[server_id])
        return _envelope({"server_id": server_id, **found})

    def discover(self, body: Any, token: str, server_id: str) -> httpx.Response:
        found = self._discover(self.servers[server_id])
        return _envelope(
            {"server_id": server_id, "tools": found["tools"], "tool_count": len(found["tools"])}
        )

    @staticmethod
    def _effective(server: dict[str, Any], tool: str) -> dict[str, Any]:
        policy = {"allowed": False, "side_effect_level": "read", "requires_approval": False}
        policy.update(server["tool_policies"].get(tool, {}))
        policy.setdefault("rate_limit_per_minute", None)
        return policy

    def list_tools(self, body: Any, token: str, server_id: str) -> httpx.Response:
        server = self.servers[server_id]
        return _envelope(
            {
                "server_id": server_id,
                "tools": [
                    {
                        **t,
                        "policy": self._effective(server, t["name"]),
                        "classified": t["name"] in server["tool_policies"],
                    }
                    for t in server["discovered_tools"]
                ],
            }
        )

    def set_policy(self, body: Any, token: str, server_id: str, tool: str) -> httpx.Response:
        if token not in ADMIN:
            return _error(403, "missing required scope; expected one of ['caliber.admin']")
        server = self.servers[server_id]
        merged = {**self._effective(server, tool), **body}
        if (
            merged["allowed"]
            and merged["side_effect_level"] != "read"
            and not merged["requires_approval"]
        ):
            return _error(
                400,
                f"set requires_approval=true for side_effect_level "
                f"{merged['side_effect_level']!r}, or lower the side-effect level to 'read'",
            )
        server["tool_policies"][tool] = merged
        self._log(token, "update_mcp_tool_policy", server_id)
        return _envelope({"server_id": server_id, "tool_name": tool, "policy": merged})

    # -- invoking ----------------------------------------------------------------------------
    def _gateway(
        self, server: dict[str, Any], tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        base = {
            "server_id": server["server_id"],
            "tool_name": tool,
            "result": None,
            "duration_ms": 0,
        }
        refusal = None
        if tool not in {t["name"] for t in server["discovered_tools"]}:
            refusal = f"Tool {tool!r} not found on this server"
        elif tool not in server["tool_policies"]:
            refusal = (
                f"MCP tool {tool!r} has no explicit allow, side-effect, and approval classification"
            )
        elif not server["tool_policies"][tool]["allowed"]:
            refusal = f"MCP tool {tool!r} is blocked by policy"
        if refusal:
            return {**base, "success": False, "error": refusal}
        limit = server["tool_policies"][tool].get("rate_limit_per_minute")
        if limit:
            key = (server["server_id"], tool)
            if self.rate_windows.get(key, 0) >= limit:
                return {
                    **base,
                    "success": False,
                    "error": f"MCP tool {tool!r} exceeded its process-local limit of "
                    f"{limit}/minute",
                }
            self.rate_windows[key] = self.rate_windows.get(key, 0) + 1
        trace: dict[str, Any] = {
            "trace_id": self._id("tr"),
            "name": f"mcp.{server['name']}.{tool}",
            "status": "OK",
        }
        self.traces.append(trace)
        if arguments.get("delay_seconds", 0) > PLAYGROUND_TIMEOUT_SECONDS:
            trace["status"] = "ERROR"
            return {
                **base,
                "success": False,
                "error": "MCP tools/call failed: Request 'tools/call' timed out",
            }
        previous = os.environ.get("REPO_TRACKER_LEDGER")
        os.environ["REPO_TRACKER_LEDGER"] = self._ledger_env(server)

        async def call() -> Any:
            return await self.mcp.call_tool(tool, arguments)

        try:
            result = anyio.run(call)
        except Exception as exc:  # the real gateway turns an error result into this message
            trace["status"] = "ERROR"
            return {**base, "success": False, "error": str(exc), "duration_ms": 1}
        finally:
            if previous is None:
                os.environ.pop("REPO_TRACKER_LEDGER", None)
            else:
                os.environ["REPO_TRACKER_LEDGER"] = previous
        trace["span"] = {"input": arguments, "output": result.structured_content}
        return {
            **base,
            "success": True,
            "error": None,
            "result": result.structured_content,
            "duration_ms": 1,
        }

    def invoke(self, body: Any, token: str, server_id: str) -> httpx.Response:
        if token not in ADMIN:
            return _error(403, "missing required scope; expected one of ['caliber.admin']")
        return _envelope(
            self._gateway(self.servers[server_id], body["tool_name"], body["arguments"])
        )

    def save_cases(self, body: Any, token: str, server_id: str, tool: str) -> httpx.Response:
        if token not in OPERATOR:
            return _error(403, "missing required scope; expected one of ['caliber.operator']")
        server = self.servers[server_id]
        server["tool_test_cases"][tool] = body["test_cases"]
        self._log(token, "update_mcp_tool_test_cases", server_id)
        return _envelope(
            {"server_id": server_id, "tool_name": tool, "test_cases": body["test_cases"]}
        )

    def calibrate(self, body: Any, token: str, server_id: str, tool: str) -> httpx.Response:
        if token not in OPERATOR:
            return _error(403, "missing required scope; expected one of ['caliber.operator']")
        server = self.servers[server_id]
        if tool not in {t["name"] for t in server["discovered_tools"]}:
            return _error(404, f"tool {tool!r} not found on MCP server {server_id!r}")
        if self._effective(server, tool)["requires_approval"]:
            return _error(
                409,
                f"tool {tool!r} requires approval, but MCP calibration has no approval checkpoint",
            )
        cases = server["tool_test_cases"].get(tool)
        if not cases:
            return _error(
                400,
                f"tool {tool!r} on MCP server {server_id!r} has no saved test cases to "
                "calibrate against",
            )
        scored = []
        for case in cases:
            got = self._gateway(server, tool, case["input"])
            rendered = json.dumps(got["result"], sort_keys=True)
            kind = case["assertion"]["type"]
            passed = got["success"] and (
                kind == "no_error"
                or (kind == "output_contains" and case["assertion"]["value"] in rendered)
                or (kind == "equals" and case["assertion"]["value"] == rendered)
            )
            scored.append(
                {
                    "name": case["name"],
                    "passed": bool(passed),
                    "output": got["result"],
                    "error": got["error"],
                    "duration_ms": 1,
                }
            )
        passed_n = sum(c["passed"] for c in scored)
        self._log(token, "calibrate_mcp_tool", server_id)
        return _envelope(
            {
                "server_id": server_id,
                "tool_name": tool,
                "pass_rate": round(passed_n / len(scored), 4),
                "total": len(scored),
                "passed": passed_n,
                "cases": scored,
            }
        )

    # -- workflows --------------------------------------------------------------------------
    def _binding(self, manifest_yaml: str) -> dict[str, str]:
        def grab(key: str) -> str:
            return re.search(rf"^    {key}: (.+)$", manifest_yaml, re.MULTILINE).group(1)  # type: ignore[union-attr]

        return {
            "server_id": grab("server_id"),
            "tool_name": grab("tool_name"),
            "requires_approval": grab("requires_approval"),
        }

    def preview(self, body: Any, token: str) -> httpx.Response:
        binding = self._binding(body["manifest_yaml"])
        return _envelope(
            {
                "ready_to_import": True,
                "validation": {"valid": True, "errors": [], "warnings": []},
                "dependencies": [
                    {
                        "kind": "mcp_tool",
                        "reference": f"{binding['server_id']}/{binding['tool_name']}",
                        "status": "resolved",
                    }
                ],
            }
        )

    def import_workflow(self, body: Any, token: str) -> httpx.Response:
        workflow_id = self._id("WF")
        self.workflows[workflow_id] = {
            "binding": self._binding(body["manifest_yaml"]),
            "version": self._id("WFV"),
        }
        return _envelope(
            {
                "workflow": {"workflow_id": workflow_id, "name": body["name"]},
                "version": {"version_id": self.workflows[workflow_id]["version"]},
            },
            201,
        )

    def versions(self, body: Any, token: str, workflow_id: str) -> httpx.Response:
        return _envelope(
            [
                {
                    "version_id": self.workflows[workflow_id]["version"],
                    "version_number": 1,
                    "status": "draft",
                }
            ]
        )

    def submit(self, body: Any, token: str) -> httpx.Response:
        workflow = next(
            w for w in self.workflows.values() if w["version"] == body["workflow_version_id"]
        )
        binding = workflow["binding"]
        server = self.servers[binding["server_id"]]
        policy = server["tool_policies"].get(binding["tool_name"], {})
        if not policy.get("allowed"):
            return _error(
                400,
                f"MCP runtime preflight failed: tool binding '{binding['tool_name']}': "
                f"MCP tool '{binding['tool_name']}' is blocked by policy",
            )
        run_id = self._id("WR")
        self.runs[run_id] = {
            "workflow_run_id": run_id,
            "status": "waiting_approval" if binding["requires_approval"] == "true" else "completed",
            "current_node_id": "call_tool",
            "input": json.loads(body["input"]),
            "binding": binding,
            "initiator": self._who(token),
            "approved": False,
        }
        return _envelope({"workflow_run_id": run_id, "status": "queued"}, 202)

    def get_run(self, body: Any, token: str, run_id: str) -> httpx.Response:
        run = self.runs[run_id]
        return _envelope(
            {k: v for k, v in run.items() if k in {"workflow_run_id", "status", "current_node_id"}}
        )

    def approve(self, body: Any, token: str, run_id: str) -> httpx.Response:
        run = self.runs[run_id]
        if self._who(token) == run["initiator"]:
            return _error(403, SECOND_PERSON)
        if token == "calpat_viewer":
            return _error(403, "project role 'viewer' cannot perform resource.approve")
        run["approved"] = True
        return _envelope({"status": "approved"})

    def resume(self, body: Any, token: str, run_id: str) -> httpx.Response:
        run = self.runs[run_id]
        server = self.servers[run["binding"]["server_id"]]
        assert run["approved"], "resumed before a second person decided"
        before = len(self.traces)
        self._gateway(server, run["binding"]["tool_name"], run["input"])
        del self.traces[
            before:
        ]  # inside a run the span nests in the run's trace, not a trace of its own
        run["status"] = "completed"
        run["spans"] = [
            "workflow.run",
            "node.call_tool",
            "tool.issue_write",
            f"mcp.{server['name']}.issue_write",
            "node.done",
        ]
        return _envelope({"status": "resumed"})

    def run_trace(self, body: Any, token: str, run_id: str) -> httpx.Response:
        return _envelope({"spans": [{"name": n} for n in self.runs[run_id].get("spans", [])]})

    # -- traces, cookbooks --------------------------------------------------------------------
    def trace_detail(self, body: Any, token: str, trace_id: str) -> httpx.Response:
        trace = next(t for t in self.traces if t["trace_id"] == trace_id)
        server = next(
            s for s in self.servers.values() if trace["name"].startswith(f"mcp.{s['name']}.")
        )
        return _envelope(
            {
                "trace_id": trace_id,
                "spans": [
                    {
                        "span_type": "TOOL",
                        "attributes": {
                            "caliber.mcp.transport": "stdio",
                            "caliber.mcp.server_id": server["server_id"],
                            "caliber.tool.input": json.dumps(trace.get("span", {}).get("input")),
                            "caliber.tool.output": json.dumps(trace.get("span", {}).get("output")),
                        },
                    }
                ],
            }
        )

    def cookbooks(self, body: Any, token: str) -> httpx.Response:
        steps = [
            {"id": f"05.{n}", "title": f"step {n}", "route": "/mcp-servers"} for n in range(1, 11)
        ]
        return _envelope(
            {
                "recipes": [
                    {
                        "id": "05",
                        "title": "Governed Tool Connectivity",
                        "steps": steps,
                        "prerequisites": ["x"],
                    }
                ]
            }
        )

    def install(self, body: Any, token: str) -> httpx.Response:
        nodes = {
            "start": {"type": "start"},
            "agent": {"type": "agent"},
            "final": {"type": "output"},
            "cookbook_guide": {"type": "note"},
        }
        return _envelope(
            {
                "workflow": {"workflow_id": self._id("WF")},
                "version": {"version_id": "WFV-S", "manifest": {"nodes": nodes, "tools": {}}},
            },
            201,
        )
