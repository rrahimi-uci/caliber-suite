"""A small stateful stand-in for the routes cookbook 09 calls.

``test_cookbook_examples.py`` replays the real example script against this instead of a server, so
the script's call sequence and the shape of what it sends and reads stay covered offline. What the
fake reproduces is the contract the script depends on: the response shapes, the version and alias
bookkeeping, and the refusals. It does not run the workflow. A ticket's outcome comes from the
expectations stored with it in ``failing-inputs.jsonl``, which are themselves checked against the
real runtime in ``caliber/tests/test_cookbook_09_self_healing.py``, and the script is verified
against a live stack as the lab README describes.

``broken_patch=True`` imitates a patch that does not fix the failures (version 2 behaves like
version 1), so the script must stop with a ``VerificationError``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

yaml = pytest.importorskip("yaml", reason="the lab's assets are YAML")

ASSETS = (
    Path(__file__).resolve().parents[3]
    / "docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets"
)
STEP_NODES = ("start", "validate", "check_reference", "lookup", "decide", "final")


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeWorkflowServer:
    def __init__(self, rows: list[dict[str, Any]], *, broken_patch: bool = False) -> None:
        self.by_payload = {row["input"]["payload"]: row["expected"] for row in rows}
        self.broken_patch = broken_patch
        self.calls: list[str] = []
        self.deleted: list[str] = []
        self.workflow: dict[str, Any] | None = None
        self.versions: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.deployments: dict[str, list[str]] = {}
        self._counter = 0

    def client(self, caliber_client_cls: Any) -> Any:
        return caliber_client_cls(
            "https://caliber.test",
            token="t",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
            max_retries=0,
        )

    def _new(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else None
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                response: httpx.Response = handler(body, *match.groups())
                return response
        raise AssertionError(  # pragma: no cover - guards against an unstubbed call
            f"example called an unstubbed route: {request.method} {path}"
        )

    def _routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("POST", r"/workflows/import", self.import_workflow),
            ("DELETE", r"/workflows/([^/]+)", self.delete_workflow),
            ("GET", r"/workflows/([^/]+)/versions", self.list_versions),
            ("POST", r"/workflows/([^/]+)/versions", self.create_version),
            ("GET", r"/workflow-versions/([^/]+)", self.get_version),
            ("POST", r"/workflow-versions/([^/]+)/validate", self.validate_version),
            ("POST", r"/workflow-versions/([^/]+)/publish", self.publish_version),
            ("POST", r"/workflow-versions/([^/]+)/propose-patch", self.propose_patch),
            ("POST", r"/workflow-runs", self.submit_run),
            ("GET", r"/workflow-runs/([^/]+)", self.get_run),
            ("GET", r"/workflow-runs/([^/]+)/trace", self.trace),
            ("GET", r"/workflow-runs/([^/]+)/events", self.events),
            ("GET", r"/workflow-runs/([^/]+)/checkpoints", lambda *_: _envelope([])),
            ("POST", r"/workflow-runs/([^/]+)/retry", self.retry),
            ("GET", r"/workflow-runs/([^/]+)/lineage", self.lineage),
            ("POST", r"/workflows/([^/]+)/deployments/([^/]+)/promote", self.promote),
            ("POST", r"/workflows/([^/]+)/deployments/([^/]+)/rollback", self.rollback),
            ("GET", r"/workflows/([^/]+)/deployments", self.list_deployments),
        ]

    # -- workflow and versions -----------------------------------------------------------
    def import_workflow(self, body: Any) -> httpx.Response:
        manifest = yaml.safe_load(body["manifest_yaml"])
        workflow_id = self._new("WF")
        self.workflow = {"workflow_id": workflow_id, "name": body["name"], "owner": "admin"}
        manifest["workflow_id"], manifest["name"] = workflow_id, body["name"]
        version = self._add_version(manifest, published=False)
        return _envelope({"workflow": self.workflow, "version": version}, 201)

    def _add_version(self, manifest: dict[str, Any], *, published: bool) -> dict[str, Any]:
        assert self.workflow is not None
        version_id = self._new("WFV")
        number = len(self.versions) + 1
        version = {
            "version_id": version_id,
            "workflow_id": self.workflow["workflow_id"],
            "version_number": number,
            "status": "published" if published else "draft",
            "manifest": manifest,
        }
        self.versions[version_id] = version
        return version

    def delete_workflow(self, body: Any, workflow_id: str) -> httpx.Response:
        self.deleted.append(workflow_id)
        return _envelope({"deleted": True})

    def list_versions(self, body: Any, workflow_id: str) -> httpx.Response:
        return _envelope(list(self.versions.values()))

    def create_version(self, body: Any, workflow_id: str) -> httpx.Response:
        return _envelope(self._add_version(body["manifest"], published=False), 201)

    def get_version(self, body: Any, version_id: str) -> httpx.Response:
        return _envelope(self.versions[version_id])

    def validate_version(self, body: Any, version_id: str) -> httpx.Response:
        return _envelope({"valid": True, "errors": [], "warnings": []})

    def publish_version(self, body: Any, version_id: str) -> httpx.Response:
        self.versions[version_id]["status"] = "published"
        return _envelope(self.versions[version_id])

    def propose_patch(self, body: Any, version_id: str) -> httpx.Response:
        evidence = body["evidence"]
        if evidence.get("category") == "guardrail":
            return _error(400, "cannot generate patch: patch produced an invalid manifest")
        return _envelope(
            {"patch_id": "WP-1", "patch_kind": "prompt", "semantic_ops": [], "summary": "Tighten"}
        )

    # -- runs ----------------------------------------------------------------------------
    def _is_patched(self, version_id: str) -> bool:
        if self.broken_patch:
            return False
        code = self.versions[version_id]["manifest"]["nodes"]["validate"]["code"]
        return "_amount" in code

    def submit_run(self, body: Any) -> httpx.Response:
        version_id = body.get("workflow_version_id") or self.deployments[body["alias"]][-1]
        expected = self.by_payload[body["input"]]
        run_id = self._new("WR")
        patched = self._is_patched(version_id)
        failing = None if patched else expected["v1_failing_node"]
        run: dict[str, Any] = {
            "workflow_run_id": run_id,
            "workflow_id": self.workflow["workflow_id"] if self.workflow else None,
            "workflow_version_id": version_id,
            "status": "failed" if failing else "completed",
            "attempt_number": 1,
            "parent_run_id": None,
            "error_summary": None,
            "summary": {"steps": [], "output": ""},
        }
        if failing:
            run["error_summary"] = (
                f"ToolExecutionError: python_code node '{failing}' failed: "
                f"{expected['v1_error_contains']}"
            )
            done = STEP_NODES[: STEP_NODES.index(failing)]
            run["summary"]["steps"] = [{"node_id": n, "status": "ok"} for n in done]
        else:
            out = {"decision": expected["decision"], "reason_code": expected["reason_code"]}
            if "upstream_status" in expected:
                out["upstream_status"] = expected["upstream_status"]
            if "v2_errors_contain" in expected:
                out["errors"] = [f"... {expected['v2_errors_contain']} ..."]
            run["summary"]["output"] = json.dumps(out, sort_keys=True)
        run["_failing"] = failing
        self.runs[run_id] = run
        return _envelope({k: v for k, v in run.items() if k != "_failing"}, 202)

    def get_run(self, body: Any, run_id: str) -> httpx.Response:
        return _envelope({k: v for k, v in self.runs[run_id].items() if k != "_failing"})

    def trace(self, body: Any, run_id: str) -> httpx.Response:
        failing = self.runs[run_id]["_failing"]
        spans = [{"name": "workflow.run", "status": "ERROR" if failing else "OK"}]
        spans += [
            {"name": f"node.{n}", "status": "ERROR" if n == failing else "OK"} for n in STEP_NODES
        ]
        return _envelope({"trace_id": f"tr-{run_id}", "spans": spans})

    def events(self, body: Any, run_id: str) -> httpx.Response:
        kind = "workflow.run.failed" if self.runs[run_id]["_failing"] else "workflow.run.completed"
        return _envelope([{"event_type": "workflow.run.queued"}, {"event_type": kind}])

    def retry(self, body: Any, run_id: str) -> httpx.Response:
        original = self.runs[run_id]
        retried = self._new("WR")
        self.runs[retried] = {**original, "workflow_run_id": retried, "attempt_number": 2}
        self.runs[retried]["parent_run_id"] = run_id
        return _envelope({"workflow_run_id": retried, "status": "queued"}, 202)

    def lineage(self, body: Any, run_id: str) -> httpx.Response:
        attempts = self.runs[run_id]["attempt_number"]
        return _envelope({"workflow_run_id": run_id, "total_attempts": attempts})

    # -- aliases -------------------------------------------------------------------------
    def promote(self, body: Any, workflow_id: str, alias: str) -> httpx.Response:
        if alias == "prod":
            return _error(
                400,
                "promoting to 'prod' (production) requires a deploy gate; attach one whose "
                "required_for_aliases includes this alias",
            )
        self.deployments.setdefault(alias, []).append(body["version_id"])
        return _envelope({"rotated": True})

    def rollback(self, body: Any, workflow_id: str, alias: str) -> httpx.Response:
        self.deployments[alias].pop()
        return _envelope({"alias": alias, "version_id": self.deployments[alias][-1]})

    def list_deployments(self, body: Any, workflow_id: str) -> httpx.Response:
        return _envelope(
            [{"alias": a, "version_id": v[-1]} for a, v in self.deployments.items() if v]
        )
