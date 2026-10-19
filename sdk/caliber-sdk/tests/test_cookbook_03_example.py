"""Cookbook 03's script, run against a stateful fake of the server's refund lab.

The fake answers the way the real server did when the script was run for real (tool
registration, sandbox, calibration, runs, approvals and the audit log), so the test pins the
script's call sequence and its own assertions. It is not evidence that the server behaves
this way: that evidence is the script's run against a live stack (see the lab README).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict

import httpx
import pytest

from caliber_sdk import CaliberClient
from examples.cookbooks import cookbook_03_policy_safe_decision_tool as lab

BASE = "https://caliber.test"
Reply = Callable[..., httpx.Response]
"""The ``ok(data, status=200)`` envelope helper ``handle`` passes to the route methods."""

SECOND_PERSON = "the account that triggered this run cannot approve it; a second person must decide"


def error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeRefundServer:
    def __init__(self, *, calibration_passes: bool = True) -> None:
        self.expected = {f"{r['id']} {r['name']}": r["expectations"] for r in lab.load_dataset()}
        self.calibration_passes = calibration_passes
        self.tools: dict[str, dict[str, Any]] = {}
        self.test_cases: dict[str, list[dict[str, Any]]] = {}
        self.test_runs: dict[str, dict[str, Any]] = {}
        self.baseline: str | None = None
        self.runs: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def client(self, token: str) -> CaliberClient:
        http = httpx.Client(transport=httpx.MockTransport(lambda r: self.handle(r, token)))
        return CaliberClient(BASE, token=token, http_client=http)

    def log(self, actor: str, action: str) -> None:
        who = {"tok-admin": "admin", "tok-approver": "approver1", "tok-viewer": "viewer1"}.get(
            actor, actor
        )
        self.audit.append({"actor": who, "action": action, "entity_type": "x", "entity_id": "y"})

    def handle(self, request: httpx.Request, actor: str) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        method = request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append(f"{method} {path}")
        ok: Reply = lambda data, status=200: httpx.Response(status, json={"data": data})  # noqa: E731
        parts = path.strip("/").split("/")
        if (method, path) == ("POST", "/projects"):
            return ok({"project_id": "PRJ-1", "name": body["name"]}, 201)
        if parts[0] == "projects" and parts[-1] == "members":
            return ok({"user_id": body["user_id"], "role": body["role"]}, 201)
        if parts[0] == "tools":
            return self.tool_route(method, parts, body, actor, ok)
        if parts[0] == "workflows":
            return self.workflow_route(method, path, parts, body, actor, ok)
        if parts[0] == "workflow-runs":
            return self.run_route(method, parts, body, actor, ok)
        if (method, path) == ("GET", "/audit-log"):
            return ok({"entries": self.audit})
        if (method, path) == ("GET", "/system/effects"):
            return ok({"effects": [], "status": "any", "resolutions": ["skip", "retry"]})
        if (method, path) == ("POST", "/cookbooks/03/install"):
            return ok(
                {"workflow": {"workflow_id": "WF-S"}, "version": {"version_id": "WFV-S"}}, 201
            )
        if (method, path) == ("GET", "/workflow-versions/WFV-S"):
            nodes = {
                "policy_decision": {"type": "data_transform"},
                "review": {"type": "human_approval"},
            }
            return ok({"version_id": "WFV-S", "manifest": {"nodes": nodes}})
        raise AssertionError(f"unstubbed route {method} {path}")

    # -- tools ---------------------------------------------------------------
    def tool_route(
        self, method: str, parts: list[str], body: dict[str, Any], actor: str, ok: Reply
    ) -> httpx.Response:
        if (method, parts) == ("POST", ["tools"]):
            if body["side_effect_level"] == "write" and not body.get("requires_approval"):
                return error(400, "set requires_approval=true for side_effect_level 'write'")
            tool_id = f"TL-{len(self.tools) + 1}"
            self.tools[tool_id] = {
                "tool_id": tool_id,
                "builtin": False,
                "availability": "available",
                "requires_approval": body.get("requires_approval", False),
                "allow_in_preview": body.get("allow_in_preview", False),
                "side_effect_level": body["side_effect_level"],
                "version": body["version"],
                "name": body["name"],
            }
            self.log(actor, "register_tool")
            return ok(self.tools[tool_id], 201)
        if parts[1] == "test-runs":
            if method == "POST":
                results = body["results"]
                run_id = f"TTR-{len(self.test_runs) + 1}"
                self.test_runs[run_id] = {
                    "test_run_id": run_id,
                    "kind": body["kind"],
                    "results": results,
                    "passed_count": sum(r["verdict"] == "pass" for r in results),
                }
                self.log(actor, "create_tool_test_run")
                return ok({k: v for k, v in self.test_runs[run_id].items() if k != "results"}, 201)
            return ok(self.test_runs[parts[2]])
        tool = self.tools[parts[1]]
        action = parts[2]
        if action == "test-run":
            if actor != "tok-admin":
                return error(403, "missing required scope; expected one of ['caliber.operator']")
            return ok(self.invoke(tool, body["input"]))
        if action == "test-cases":
            self.test_cases[tool["tool_id"]] = body["test_cases"]
            return ok({"tool_id": tool["tool_id"], "test_cases": body["test_cases"]})
        if action == "calibrate":
            if actor != "tok-admin":
                return error(403, "missing required scope; expected one of ['caliber.operator']")
            cases = []
            for case in self.test_cases[tool["tool_id"]]:
                output = dict(self.expected[case["name"]])
                if not self.calibration_passes:
                    output["decision"] = "approve"
                passed = json.dumps(output, sort_keys=True) == case["assertion"]["value"]
                cases.append(
                    {
                        "name": case["name"],
                        "passed": passed,
                        "output": output,
                        "error": None,
                        "duration_ms": 1,
                    }
                )
            passed_n = sum(c["passed"] for c in cases)
            self.log(actor, "calibrate_tool")
            return ok(
                {
                    "tool_id": tool["tool_id"],
                    "pass_rate": round(passed_n / len(cases), 4),
                    "total": len(cases),
                    "passed": passed_n,
                    "cases": cases,
                }
            )
        if action == "baseline":
            self.baseline = body["test_run_id"]
            self.log(actor, "set_tool_baseline")
            return ok({"baseline_run_id": self.baseline})
        if action == "workspace":
            return ok({"baseline_run_id": self.baseline, "lifecycle": "Hardened"})
        raise AssertionError(f"unstubbed tool route {method} {parts}")

    def invoke(self, tool: dict[str, Any], tool_input: dict[str, Any]) -> dict[str, Any]:
        if tool["side_effect_level"] == "write":
            return {
                "output": {"_preview_mock": True},
                "mocked": True,
                "isolation": "mocked",
                "error": None,
            }
        base = {"mocked": False, "isolation": "subprocess"}
        if "code" in tool_input:
            code = tool_input["code"]
            if "sleep" in code:
                return {**base, "output": None, "error": "tool sandbox timed out after 5.00s"}
            out = {"returncode": 0, "stderr": "", "stdout": "", "timed_out": False}
            if "big.bin" in code:
                out.update(returncode=1, stderr="OSError: [Errno 27] File too large")
            elif "environ" in code:
                out["stdout"] = "['LC_CTYPE']\n"
            else:
                out["stdout"] = "200\n"
            return {**base, "output": out, "error": None}
        if "bogus" in tool_input:
            return {
                **base,
                "output": None,
                "error": "TypeError: decide_refund() got an unexpected keyword argument 'bogus'",
            }
        return {**base, "output": self.decide(tool_input), "error": None}

    @staticmethod
    def decide(case: dict[str, Any]) -> dict[str, Any]:
        rows: dict[str, dict[str, Any]] = {
            json.dumps(r["inputs"], sort_keys=True): r["expectations"] for r in lab.load_dataset()
        }
        return rows[json.dumps(case, sort_keys=True)]

    # -- workflows and runs ---------------------------------------------------
    def workflow_route(
        self, method: str, path: str, parts: list[str], body: dict[str, Any], actor: str, ok: Reply
    ) -> httpx.Response:
        if path == "/workflows/import/preview":
            return ok(
                {
                    "ready_to_import": True,
                    "validation": {
                        "valid": True,
                        "errors": [],
                        "warnings": [{"code": "write_tool_without_approval"}],
                    },
                }
            )
        if path == "/workflows/import":
            self.log(actor, "import_workflow")
            return ok(
                {
                    "workflow": {"workflow_id": "WF-1", "name": body["name"]},
                    "version": {"version_id": "WFV-1"},
                },
                201,
            )
        if (method, path) == ("GET", "/workflows/WF-1/versions"):
            return ok([{"version_id": "WFV-1", "version_number": 1, "status": "draft"}])
        raise AssertionError(f"unstubbed workflow route {method} {path}")

    def run_route(
        self, method: str, parts: list[str], body: dict[str, Any], actor: str, ok: Reply
    ) -> httpx.Response:
        if method == "POST" and parts == ["workflow-runs"]:
            run_id = f"WR-{len(self.runs) + 1}"
            case = json.loads(body["input"])
            self.log(actor, "enqueue_workflow_run")
            decision = self.decide(case)
            self.runs[run_id] = {
                "workflow_run_id": run_id,
                "case": case,
                "decision": decision,
                "state": "queued",
            }
            return ok({"workflow_run_id": run_id, "status": "queued"}, 202)
        run = self.runs[parts[1]]
        if method == "GET" and len(parts) == 2:
            return ok(self.view(run))
        if parts[2] == "approvals":
            if run["decision"]["decision"] == "deny":
                return ok([])
            status = "approved" if run["state"] in {"approved", "completed"} else "pending"
            return ok(
                [
                    {
                        "status": status,
                        "decided_by": run.get("decided_by"),
                        "policy_snapshot": {
                            "allow_self_approval": False,
                            "required_role": "caliber.approver",
                        },
                    }
                ]
            )
        if parts[-1] == "approve":
            if actor == "tok-admin":
                return error(403, SECOND_PERSON)
            if actor == "tok-viewer":
                return error(403, "project role 'viewer' cannot perform resource.approve")
            run.update(state="approved", decided_by="approver1")
            self.log("approver1", "approve_workflow_run_runtime_approval")
            return ok(self.view(run))
        if parts[-1] == "reject":
            run.update(state="rejected")
            self.log("approver1", "reject_workflow_run_runtime_approval")
            return ok(self.view(run))
        if parts[-1] == "resume":
            if actor != "tok-admin":
                return error(403, "missing required scope; expected one of ['caliber.operator']")
            run["state"] = "completed"
            self.log(actor, "resume_workflow_run")
            return ok(self.view(run))
        raise AssertionError(f"unstubbed run route {method} {parts}")

    def view(self, run: dict[str, Any]) -> dict[str, Any]:
        case, decision, state = run["case"], run["decision"], run["state"]
        summary: dict[str, Any] = {"steps": [{"node_id": "policy", "tool_calls": []}]}
        view: dict[str, Any] = {"workflow_run_id": run["workflow_run_id"], "summary": summary}
        if decision["decision"] == "deny":
            view["status"] = "completed"
            summary["output"] = json.dumps(decision)
        elif state in {"queued", "approved"}:
            view.update(status="waiting_approval", current_node_id="refund")
            summary["steps"].append({"node_id": "refund", "tool_calls": []})
        elif state == "rejected":
            view.update(status="failed", error_code="approval_rejected")
            summary["steps"].append({"node_id": "refund", "tool_calls": []})
        else:
            result = {
                "amount_usd": case["amount"],
                "order_id": case["order_id"],
                "refund_status": "initiated",
            }
            view["status"] = "completed"
            summary["steps"].append(
                {"node_id": "refund", "tool_calls": [{"tool": "initiate_refund", "result": result}]}
            )
        return view


class Clients(TypedDict):
    admin: CaliberClient
    approver: CaliberClient
    viewer: CaliberClient


def clients(server: FakeRefundServer) -> Clients:
    return {
        "admin": server.client("tok-admin"),
        "approver": server.client("tok-approver"),
        "viewer": server.client("tok-viewer"),
    }


def test_script_verifies_every_documented_outcome() -> None:
    server = FakeRefundServer()
    result = lab.run(**clients(server), suffix="_x")
    assert result["calibration"] == {"pass_rate": 1.0, "passed": 49, "total": 49}
    assert result["regression"]["regressed"] == ["R09 amount boundary: exactly 200.00"]
    assert result["approved_run"]["approved_by"] == "approver1"
    assert result["rejected_run"]["error_code"] == "approval_rejected"
    assert result["manual_review_run"]["amount_usd"] == 250.0
    assert result["effect_ledger_rows"] == 0
    assert server.baseline == "TTR-1"
    names = {t["name"] for t in server.tools.values()}
    assert names == {"decide_refund_x", "initiate_refund_x", "probe_python_x"}


def test_script_fails_when_calibration_does_not_pass() -> None:
    server = FakeRefundServer(calibration_passes=False)
    with pytest.raises(AssertionError, match="calibration"):
        lab.run(**clients(server))


def test_fixture_cases_compare_exact_sorted_json() -> None:
    rows = lab.load_dataset()
    cases = lab.fixture_cases(rows)
    assert len(cases) == len(rows) >= 40
    first = cases[0]["assertion"]
    assert first["type"] == "equals"
    assert json.loads(first["value"]) == rows[0]["expectations"]
    assert first["value"] == json.dumps(rows[0]["expectations"], sort_keys=True)


def test_workflow_yaml_suffixes_both_tool_names() -> None:
    text = lab.workflow_yaml("_s")
    assert "tool.decide_refund_s.v1" in text and "tool.initiate_refund_s.v1" in text
    assert "decide_refund\n" not in text.replace("decide_refund_s", "")


def test_cleanup_archives_what_a_run_created() -> None:
    server = FakeRefundServer()
    seen: list[str] = []

    class Admin:
        class tools:
            @staticmethod
            def list() -> list[Any]:
                return [
                    type("T", (), {"name": "decide_refund_c", "tool_id": "TL-9"})(),
                    type("T", (), {"name": "unrelated", "tool_id": "TL-0"})(),
                ]

            @staticmethod
            def archive(tool_id: str) -> None:
                seen.append(f"archive {tool_id}")

        class workflows:
            @staticmethod
            def list() -> list[Any]:
                return [type("W", (), {"name": "refund-decision_c", "workflow_id": "WF-9"})()]

            @staticmethod
            def delete(workflow_id: str) -> None:
                seen.append(f"delete {workflow_id}")

        class projects:
            @staticmethod
            def list() -> list[Any]:
                return [type("P", (), {"name": "refund-ops_c", "project_id": "PRJ-9"})()]

            @staticmethod
            def archive(project_id: str) -> None:
                seen.append(f"archive {project_id}")

    assert server is not None
    lab.cleanup(Admin(), "_c")  # type: ignore[arg-type]
    assert seen == ["archive TL-9", "delete WF-9", "archive PRJ-9"]
