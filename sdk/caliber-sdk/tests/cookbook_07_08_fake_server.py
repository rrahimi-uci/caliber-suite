"""A stateful stand-in for the routes cookbooks 07 and 08 call.

``test_cookbook_07_08_examples.py`` replays the real example scripts against this instead of a
server, so their call sequence and the shape of what they send and read stay covered offline. The
fake imitates the *contract* the scripts depend on (status codes, the fields they read, the
refusals). The mechanical parts are the real ones: the ``classify`` Python Code node runs in
CALIBER's own tool-sandbox namespace, and the decision tables run through
``caliber.workflows.data_transform``, so a rule change in the assets changes what the fake answers
exactly as it changes a real run. The real server, queue, approval rules and gate are exercised by
running the scripts against a live stack (see the lab READMEs) and by ``caliber/tests``.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
from _server_package import server_module

_namespace = server_module("caliber.tool_sandbox._runner")._namespace
apply_data_transform = server_module("caliber.workflows.data_transform").apply_data_transform

PREFIX = "/ajax-api/2.0/mlflow/caliber"
TOKENS = {
    "tok-admin": ("admin", ["caliber.admin", "caliber.approver", "caliber.operator"], True),
    "tok-lena": ("lena", ["caliber.approver", "caliber.operator"], False),
    "tok-sam": ("sam", ["caliber.operator"], False),
}


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def error(status: int, detail: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status, **extra})


class FakeCaliber:
    """One fake server. ``approval_class`` is the environment class whose promotions wait."""

    def __init__(self, *, approval_class: str = "staging") -> None:
        self.approval_class = approval_class
        self.counter = 0
        self.workflows: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, dict[str, dict[str, Any]]] = {}
        self.promotions: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []

    # -- plumbing ---------------------------------------------------------------------------
    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter:04d}"

    def client_for(self, user: str, project: str | None = None) -> Any:
        from caliber_sdk import CaliberClient

        token = f"tok-{user}"
        http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return CaliberClient("https://caliber.test", token=token, project=project, http_client=http)

    def who(self, request: httpx.Request) -> tuple[str, list[str]]:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        user, scopes, _ = TOKENS[token]
        return user, scopes

    def log(self, action: str, actor: str, entity_id: str, **details: Any) -> None:
        self.audit.append(
            {"action": action, "actor": actor, "entity_id": entity_id, "details": details}
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        self.requests.append((request.method, path))
        body = json.loads(request.content) if request.content else {}
        for method, pattern, fn in self.routes():
            match = re.fullmatch(pattern, path)
            if method == request.method and match:
                response: httpx.Response = fn(request, body, *match.groups())
                return response
        raise AssertionError(f"unstubbed route: {request.method} {path}")

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/me", self.me),
            ("GET", r"/cookbooks", self.cookbooks),
            ("POST", r"/projects", self.create_project),
            (
                "POST",
                r"/projects/([^/]+)/members",
                lambda r, b, p: envelope({"user_id": b["user_id"], "role": b["role"]}),
            ),
            ("POST", r"/cookbooks/(\d\d)/install", self.install),
            ("PATCH", r"/workflows/([^/]+)", self.update_workflow),
            ("POST", r"/workflows/import", self.import_workflow),
            ("POST", r"/workflows/import/preview", self.import_preview),
            (
                "GET",
                r"/workflows/([^/]+)/versions",
                lambda r, b, w: envelope(
                    [v for v in self.versions.values() if v["workflow_id"] == w]
                ),
            ),
            ("POST", r"/workflows/([^/]+)/versions", self.create_version),
            ("GET", r"/workflow-versions/([^/]+)", lambda r, b, v: envelope(self.versions[v])),
            (
                "POST",
                r"/workflow-versions/([^/]+)/validate",
                lambda r, b, v: envelope({"valid": True, "errors": [], "warnings": []}),
            ),
            ("POST", r"/workflow-versions/([^/]+)/publish", self.publish),
            ("POST", r"/workflow-versions/([^/]+)/preview-run", self.preview),
            ("POST", r"/workflow-versions/([^/]+)/run", self.sync_run),
            ("POST", r"/workflows/([^/]+)/deployments/([^/]+)/promote", self.promote),
            (
                "GET",
                r"/workflows/([^/]+)/deployments",
                lambda r, b, w: envelope(list(self.deployments.get(w, {}).values())),
            ),
            ("POST", r"/workflows/([^/]+)/deployments/([^/]+)/rollback", self.rollback),
            ("POST", r"/workflow-promotions/([^/]+)/approve", self.approve_promotion),
            ("POST", r"/workflow-runs", self.submit),
            ("POST", r"/workflow-runs/resume-by-event", self.resume_by_event),
            ("GET", r"/workflow-runs/([^/]+)", lambda r, b, run: envelope(self.runs[run])),
            (
                "GET",
                r"/workflow-runs/([^/]+)/checkpoints",
                lambda r, b, run: envelope(self.runs[run].get("checkpoints", [])),
            ),
            ("POST", r"/workflow-runs/([^/]+)/approval/(approve|reject)", self.decide),
            ("POST", r"/workflow-runs/([^/]+)/resume", self.resume),
            ("POST", r"/workflow-runs/([^/]+)/cancel", self.cancel),
            ("POST", r"/eval-datasets", self.create_dataset),
            ("POST", r"/eval-datasets/([^/]+)/examples", self.add_example),
            (
                "GET",
                r"/eval-datasets/([^/]+)/examples",
                lambda r, b, d: envelope(self.datasets[d]["examples"]),
            ),
            ("GET", r"/audit-log", self.audit_log),
            ("POST", r"/agents", lambda r, b: envelope({"agent_id": b["agent_id"]})),
            ("POST", r"/workflows/([^/]+)/calibration/runs", self.calibrate),
            ("GET", r"/jobs/([^/]+)", self.job),
            (
                "POST",
                r"/evaluations",
                lambda r, b: error(
                    400,
                    "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) and the matching API key, then retry.",  # noqa: E501
                ),
            ),
        ]

    # -- identity, projects, cookbooks --------------------------------------------------------
    def me(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        user, scopes, admin = TOKENS[token]
        return envelope({"user_id": user, "scopes": scopes, "is_admin": admin})

    def cookbooks(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return envelope(
            {"recipes": [{"id": "07", "title": "Support"}, {"id": "08", "title": "Incident"}]}
        )

    def create_project(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return envelope({"project_id": self.next_id("PRJ"), "name": body["name"]})

    def starter_manifest(self, recipe: str) -> dict[str, Any]:
        if recipe == "07":
            nodes = {
                "start": {"type": "start"},
                "agent": {"type": "agent"},
                "review": {"type": "human_approval"},
                "final": {"type": "output"},
                "incident_connector": {"type": "api_request"},
            }
        else:
            nodes = {
                "start": {"type": "start"},
                "deployment_connector": {"type": "api_request"},
                "service_health_connector": {"type": "api_request"},
                "final": {"type": "output"},
            }
        return {"name": f"starter-{recipe}", "nodes": nodes}

    def install(self, request: httpx.Request, body: dict[str, Any], recipe: str) -> httpx.Response:
        user, _ = self.who(request)
        workflow_id = self.next_id("WF")
        self.workflows[workflow_id] = {
            "workflow_id": workflow_id,
            "status": "paused",
            "owner": user,
        }
        version = self.add_version(workflow_id, self.starter_manifest(recipe))
        return envelope(
            {
                "recipe": {"id": recipe},
                "workflow": self.workflows[workflow_id],
                "version": version,
                "activation_requires_review": True,
            }
        )

    def update_workflow(
        self, request: httpx.Request, body: dict[str, Any], workflow_id: str
    ) -> httpx.Response:
        self.workflows[workflow_id].update(body)
        return envelope(self.workflows[workflow_id])

    # -- workflows and versions -----------------------------------------------------------------
    def add_version(self, workflow_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
        count = sum(1 for v in self.versions.values() if v["workflow_id"] == workflow_id)
        version: dict[str, Any] = {
            "version_id": self.next_id("WFV"),
            "workflow_id": workflow_id,
            "version_number": count + 1,
            "status": "draft",
            "manifest": {**manifest, "workflow_id": workflow_id},
        }
        self.versions[version["version_id"]] = version
        return version

    def unresolved(self, manifest: dict[str, Any]) -> list[dict[str, str]]:
        problems = []
        for ref in (manifest.get("artifacts") or {}).get("eval_datasets", {}).values():
            if not any(d["name"] == ref["dataset_name"] for d in self.datasets.values()):
                problems.append(
                    {"code": "unresolved_import_dependency", "path": ref["dataset_name"]}
                )
        for node in manifest["nodes"].values():
            if node.get("type") == "tool":
                problems.append({"code": "unresolved_import_dependency", "path": node["tool_name"]})
        return problems

    def import_preview(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        errors = self.unresolved(body["manifest"])
        return envelope(
            {"ready_to_import": not errors, "validation": {"valid": not errors, "errors": errors}}
        )

    def import_workflow(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        if self.unresolved(body["manifest"]):
            return error(
                400,
                "workflow import preflight failed; resolve validation and dependency errors first",
            )
        user, _ = self.who(request)
        workflow_id = self.next_id("WF")
        self.workflows[workflow_id] = {
            "workflow_id": workflow_id,
            "status": "active",
            "owner": user,
        }
        manifest = {**body["manifest"], "name": body.get("name") or body["manifest"]["name"]}
        version = self.add_version(workflow_id, manifest)
        return envelope({"workflow": self.workflows[workflow_id], "version": version})

    def create_version(
        self, request: httpx.Request, body: dict[str, Any], workflow_id: str
    ) -> httpx.Response:
        return envelope(self.add_version(workflow_id, body["manifest"]))

    def publish(
        self, request: httpx.Request, body: dict[str, Any], version_id: str
    ) -> httpx.Response:
        self.versions[version_id]["status"] = "published"
        return envelope(self.versions[version_id])

    def blocked_types(self, manifest: dict[str, Any]) -> list[str]:
        return [
            f"{nid} ({n['type']})"
            for nid, n in manifest["nodes"].items()
            if n.get("type") in {"python_code", "api_request"}
        ]

    def preview(
        self, request: httpx.Request, body: dict[str, Any], version_id: str
    ) -> httpx.Response:
        manifest = self.versions[version_id]["manifest"]
        blocked = self.blocked_types(manifest)
        if blocked:
            return envelope(
                {
                    "status": "error",
                    "output": "",
                    "error": "preview blocked before execution because ordinary Preview cannot isolate these capability nodes: "  # noqa: E501
                    + ", ".join(blocked),
                }
            )
        if any(n.get("type") == "wait_for_event" for n in manifest["nodes"].values()):
            return envelope({"status": "blocked", "output": "", "error": "waiting_event:wait"})
        outcome = self.execute(manifest, body.get("input", ""), "preview")
        return envelope(
            {"status": "completed", "output": outcome["output"], "steps": outcome["steps"]}
        )

    def sync_run(
        self, request: httpx.Request, body: dict[str, Any], version_id: str
    ) -> httpx.Response:
        if any(
            n.get("type") == "human_approval"
            for n in self.versions[version_id]["manifest"]["nodes"].values()
        ):
            return error(
                400,
                "cannot run workflow: synchronous real runs cannot checkpoint runtime approvals required by human approval node 'review'; submit through the queued /workflow-runs endpoint",  # noqa: E501
            )
        return envelope({"status": "completed"})

    # -- deployments ------------------------------------------------------------------------------
    def env_class(self, alias: str) -> str:
        return {"prod": "production", "staging": "staging"}.get(alias, "development")

    def gate(self, version: dict[str, Any]) -> dict[str, Any] | None:
        gates = version["manifest"].get("deploy_gates") or {}
        manifest = version["manifest"]
        for gate in gates.values():
            ref = manifest["artifacts"]["eval_datasets"][gate["dataset_ref"]]["dataset_name"]
            dataset = next(d for d in self.datasets.values() if d["name"] == ref)
            rows = dataset["examples"]
            passed = 0
            for row in rows:
                out = self.execute(manifest, json.dumps(row["input"]), "gate")["output"]
                passed += int(row["expected"]["answer"] in out)
            rate = round(passed / len(rows), 4)
            return {
                "has_gate": True,
                "passed": rate >= gate["thresholds"]["min_pass_rate"],
                "executor": {
                    "provider": "fake",
                    "executor": "FakeWorkflowExecutor",
                    "model": None,
                    "deterministic": True,
                },
                "runs": [
                    {
                        "name": "verdict-matches-dataset",
                        "passed": rate >= 1.0,
                        "pass_rate": rate,
                        "n_examples": len(rows),
                    }
                ],
            }
        return None

    def promote(
        self, request: httpx.Request, body: dict[str, Any], workflow_id: str, alias: str
    ) -> httpx.Response:
        user, _ = self.who(request)
        version = self.versions[body["version_id"]]
        gate = self.gate(version) if alias == "prod" else None
        if alias == "prod" and gate is None:
            return error(400, "promoting to 'prod' (production) requires a deploy gate")
        if gate is not None and not gate["passed"]:
            return httpx.Response(
                400,
                json={
                    "detail": "deploy gate failed for alias 'prod'",
                    "status_code": 400,
                    "gate": gate,
                },
            )
        gate = gate or {"has_gate": False, "passed": True, "executor": None, "runs": []}
        if self.env_class(alias) == self.approval_class:
            promotion = {
                "promotion_id": self.next_id("WPR"),
                "workflow_id": workflow_id,
                "alias": alias,
                "version_id": version["version_id"],
                "status": "pending",
                "requested_by": user,
            }
            self.promotions[promotion["promotion_id"]] = promotion
            return envelope(
                {"rotated": False, "deployment": None, "promotion": promotion, "gate": gate}, 202
            )
        self.rotate(workflow_id, alias, version["version_id"], user)
        return envelope(
            {
                "rotated": True,
                "deployment": self.deployments[workflow_id][alias],
                "promotion": None,
                "gate": gate,
            }
        )

    def rotate(self, workflow_id: str, alias: str, version_id: str, user: str) -> None:
        current = self.deployments.setdefault(workflow_id, {}).get(alias)
        history = (current or {}).get("rollback_checkpoint", [])
        if current:
            history = [*history, {"version_id": current["version_id"]}]
        self.deployments[workflow_id][alias] = {
            "alias": alias,
            "version_id": version_id,
            "rollback_checkpoint": history,
            "deployed_by": user,
        }

    def approve_promotion(
        self, request: httpx.Request, body: dict[str, Any], promotion_id: str
    ) -> httpx.Response:
        user, scopes = self.who(request)
        if "caliber.approver" not in scopes:
            return error(403, "missing required scope; expected one of ['caliber.approver']")
        promotion = self.promotions[promotion_id]
        promotion["status"] = "approved"
        promotion["decided_by"] = user
        self.rotate(promotion["workflow_id"], promotion["alias"], promotion["version_id"], user)
        return envelope({"rotated": True, "promotion": promotion})

    def rollback(
        self, request: httpx.Request, body: dict[str, Any], workflow_id: str, alias: str
    ) -> httpx.Response:
        deployment = self.deployments[workflow_id][alias]
        if not deployment["rollback_checkpoint"]:
            return error(404, "no rollback checkpoint")
        previous = deployment["rollback_checkpoint"][-1]["version_id"]
        deployment["rollback_checkpoint"] = deployment["rollback_checkpoint"][:-1]
        deployment["version_id"] = previous
        return envelope(deployment)

    # -- running ----------------------------------------------------------------------------------
    def execute(self, manifest: dict[str, Any], text: str, mode: str) -> dict[str, Any]:
        nodes = manifest["nodes"]
        if "classify" in nodes:
            return self.execute_triage(nodes, text)
        if "severity_table" in nodes:
            return self.execute_incident(nodes, text, mode)
        if "deployment_connector" in nodes:
            return {
                "status": "completed",
                "path": ["start", "final"],
                "output": "starter",
                "steps": [],
            }
        if "boundary" in nodes:
            return self.execute_boundary(nodes, text)
        if "call" in nodes:
            return {
                "status": "failed",
                "path": ["start"],
                "output": "",
                "steps": [],
                "error": "ToolExecutionError: outbound request to 'status.example.invalid' is blocked: this process could not resolve it",  # noqa: E501
            }
        if "wait" in nodes:
            return {"status": "waiting_event", "path": ["start", "wait"], "output": "", "steps": []}
        return {
            "status": "waiting_approval",
            "path": ["start", "agent", "review"],
            "output": "",
            "steps": [],
        }

    def execute_triage(self, nodes: dict[str, Any], text: str) -> dict[str, Any]:
        _, fn = _namespace(nodes["classify"]["code"], "run_python_node")
        returned = fn(input=text)
        triage = returned["result"]
        steps = [{"node_id": "classify", "output_by_port": {"result": returned}}]
        if returned["approval"] == "true":
            return {
                "status": "waiting_approval",
                "path": ["start", "classify", "triage_router", "review"],
                "output": "",
                "steps": steps,
            }
        branch = {"reply": "draft_reply", "escalate_support": "support_note"}.get(
            triage["decision"], "clarify_question"
        )
        return {
            "status": "completed",
            "path": ["start", "classify", "triage_router", branch, "final"],
            "output": f"[{branch}] {returned['text']}",
            "steps": steps,
        }

    def execute_incident(self, nodes: dict[str, Any], text: str, mode: str) -> dict[str, Any]:
        value = json.loads(text)
        severity = apply_data_transform("decision_table", value, nodes["severity_table"]["config"])[
            "result"
        ]["decision"]
        action = apply_data_transform("decision_table", value, nodes["action_table"]["config"])[
            "result"
        ]["decision"]
        verdict = {"verdict": f"{severity} {action}", "severity": severity, "action": action}
        steps = [{"node_id": "summarize", "output_by_port": {"result": verdict}}]
        if action == "rollback" and mode in {"preview", "gate"}:
            # Preview and the gate replay run through the approval node as if approved.
            path = ["start", "summarize", "triage_router", "review", "rollback_request", "final"]
            return {
                "status": "completed",
                "path": path,
                "output": f"ROLLBACK_APPROVED. Verdict: {json.dumps(verdict)}",
                "steps": steps,
            }
        if action == "rollback":
            return {
                "status": "waiting_approval",
                "path": ["start", "summarize", "triage_router", "review"],
                "output": "",
                "steps": steps,
            }
        branch = (
            "handoff_note"
            if action in {"escalate_human", "gather_more_evidence"}
            else "recommendation"
        )
        return {
            "status": "completed",
            "path": ["start", "summarize", "triage_router", branch, "final"],
            "output": f"{branch.upper()}. Verdict: {json.dumps(verdict)}",
            "steps": steps,
        }

    def execute_boundary(self, nodes: dict[str, Any], text: str) -> dict[str, Any]:
        _, fn = _namespace(nodes["lookup"]["code"], "run_python_node")
        try:
            output = fn(input=text)["text"]
        except ValueError:
            output = nodes["boundary"]["fallback_text"]
        return {
            "status": "completed",
            "path": ["start", "boundary", "final"],
            "output": output,
            "steps": [],
        }

    def submit(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        user, _ = self.who(request)
        if body.get("alias"):
            version_id = self.deployments[body["workflow_id"]][body["alias"]]["version_id"]
        else:
            version_id = body["workflow_version_id"]
        version = self.versions[version_id]
        outcome = self.execute(version["manifest"], body.get("input", ""), "queue")
        run_id = self.next_id("WR")
        run = {
            "workflow_run_id": run_id,
            "workflow_id": version["workflow_id"],
            "workflow_version_id": version_id,
            "status": outcome["status"],
            "summary": {
                "node_path": outcome["path"],
                "steps": outcome["steps"],
                "output": outcome["output"],
            },
            "owner": user,
            "error_summary": outcome.get("error"),
            "error_code": "runtime_error" if outcome["status"] == "failed" else None,
        }
        if outcome["status"] == "waiting_event":
            run["checkpoints"] = [
                {
                    "state_blob": {
                        "expected_event_name": version["manifest"]["nodes"]["wait"]["event_name"]
                    }
                }
            ]
        self.runs[run_id] = run
        self.log("enqueue_workflow_run", user, run_id)
        return envelope({"workflow_run_id": run_id, "status": "queued"}, 202)

    def decide(
        self, request: httpx.Request, body: dict[str, Any], run_id: str, verb: str
    ) -> httpx.Response:
        user, scopes = self.who(request)
        run = self.runs[run_id]
        if "caliber.approver" not in scopes:
            return error(403, "missing required scope; expected one of ['caliber.approver']")
        if verb == "approve" and user == run["owner"]:
            return error(
                403,
                "the account that triggered this run cannot approve it; a second person must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a single-operator install)",  # noqa: E501
            )
        if verb == "reject":
            run.update(status="failed", error_code="approval_rejected")
        else:
            run["approved"] = True
            self.log("approve_workflow_run_runtime_approval", user, "RAP-1", workflow_run_id=run_id)
        return envelope(run)

    def resume(self, request: httpx.Request, body: dict[str, Any], run_id: str) -> httpx.Response:
        user, _ = self.who(request)
        run = self.runs[run_id]
        released = (
            "ROLLBACK_APPROVED"
            if "triage_router" in run["summary"]["node_path"]
            and "summarize" in str(run["summary"]["steps"])
            else "APPROVED_ACTION"
        )
        run.update(status="completed")
        run["summary"] = {
            "node_path": ["review", "action_request"],
            "steps": [],
            "output": f"{released}. released",
        }
        self.log("resume_workflow_run", user, run_id)
        return envelope(run)

    def cancel(self, request: httpx.Request, body: dict[str, Any], run_id: str) -> httpx.Response:
        self.runs[run_id]["status"] = "cancelled"
        return envelope(self.runs[run_id])

    def resume_by_event(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        for run in self.runs.values():
            if run["status"] == "waiting_event":
                run.update(status="completed")
        return envelope({"resumed": True})

    # -- datasets, audit, calibration -------------------------------------------------------------
    def create_dataset(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        dataset_id = self.next_id("ED")
        self.datasets[dataset_id] = {"dataset_id": dataset_id, "name": body["name"], "examples": []}
        return envelope({"dataset_id": dataset_id, "name": body["name"]})

    def add_example(
        self, request: httpx.Request, body: dict[str, Any], dataset_id: str
    ) -> httpx.Response:
        row = {
            "example_id": self.next_id("EX"),
            "input": body["input"],
            "expected": body["expected"],
        }
        self.datasets[dataset_id]["examples"].append(row)
        return envelope(row)

    def audit_log(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        params = request.url.params
        rows = [
            e
            for e in self.audit
            if (not params.get("entity_id") or e["entity_id"] == params["entity_id"])
            and (not params.get("action") or e["action"] == params["action"])
        ]
        return envelope({"entries": rows})

    def calibrate(
        self, request: httpx.Request, body: dict[str, Any], workflow_id: str
    ) -> httpx.Response:
        return envelope({"job": {"job_id": "RFN-1"}})

    def job(self, request: httpx.Request, body: dict[str, Any], job_id: str) -> httpx.Response:
        return envelope(
            {
                "job_id": job_id,
                "status": "rejected",
                "eval_results": {
                    "workflow": True,
                    "calibration": True,
                    "n_examples": 20,
                    "baseline": {"overall": 0.95},
                    "gate": {"passed": False, "reasons": ["no candidate met calibration gates"]},
                },
            }
        )
