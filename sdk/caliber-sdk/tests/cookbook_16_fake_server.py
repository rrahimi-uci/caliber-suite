"""A small stateful stand-in for the routes cookbook 16 calls.

``test_cookbook_examples.py`` replays the real example script against this instead of a server, so
the script's call sequence and the shape of what it sends and reads stay covered offline. What the
fake reproduces is the contract the script depends on: the trace and run shapes, the ``ERROR`` root
span of a failed run, the 404 for an unknown trace, the example and queue bookkeeping, and the
write-back of review answers as assessments. It does not run the workflow: a ticket's outcome is
derived from its tags in ``prod-regression.sample.jsonl`` (the way the real workflow treats them,
which ``caliber/tests/test_cookbook_16_observability.py`` checks against the real runtime). The
script is verified against a live stack as the lab README describes.

``trace_status`` lets a test make every trace ``OK``, which the script must refuse.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

ASSETS = Path(__file__).resolve().parents[3] / "docs-site/cookbooks/16-observability-triage/assets"
NODE_FOR = {"validate": "validate", "poison": "check_reference", "upstream": "decide"}
STEPS = ("start", "validate", "check_reference", "lookup", "decide", "final")


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeObservabilityServer:
    def __init__(self, rows: list[dict[str, Any]], *, ignore_failures: bool = False) -> None:
        self.rows = rows
        self.by_payload = {row["input"]["payload"]: row for row in rows}
        self.ignore_failures = ignore_failures
        self.calls: list[str] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.traces: dict[str, dict[str, Any]] = {}
        self.run_for_trace: dict[str, str] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.examples: dict[str, list[dict[str, Any]]] = {}
        self.queues: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.assessments: dict[str, list[dict[str, Any]]] = {}
        self.archived: list[str] = []
        self._counter = 0
        self._clock = int(time.time() * 1000)

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
        query = dict(request.url.params)
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                response: httpx.Response = handler(body, query, *match.groups())
                return response
        raise AssertionError(  # pragma: no cover - guards against an unstubbed call
            f"example called an unstubbed route: {request.method} {path}"
        )

    def _routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/me", lambda *_: _envelope({"user_id": "admin"})),
            ("POST", r"/workflows/import", self.import_workflow),
            ("GET", r"/workflows/([^/]+)/versions", self.list_versions),
            ("POST", r"/workflow-versions/([^/]+)/publish", self.publish),
            ("POST", r"/workflow-runs", self.submit_run),
            ("GET", r"/workflow-runs/by-trace/([^/]+)", self.run_by_trace),
            ("GET", r"/workflow-runs/([^/]+)", self.get_run),
            ("GET", r"/observability/traces", self.list_traces),
            ("GET", r"/observability/traces/([^/]+)", self.get_trace),
            ("POST", r"/eval-datasets", self.create_dataset),
            ("POST", r"/eval-datasets/([^/]+)/examples/from-trace", self.from_trace),
            ("GET", r"/eval-datasets/([^/]+)/examples", self.list_examples),
            ("PATCH", r"/eval-datasets/([^/]+)", self.archive),
            ("POST", r"/review-queues", self.create_queue),
            ("POST", r"/review-queues/([^/]+)/items", self.enqueue),
            ("POST", r"/review-queues/([^/]+)/items/([^/]+)/submit", self.submit),
            ("PATCH", r"/review-queues/([^/]+)", self.archive),
        ]

    # -- workflow and runs -----------------------------------------------------------------
    def import_workflow(self, body: Any, query: Any) -> httpx.Response:
        workflow_id = self._new("WF")
        version = {"version_id": self._new("WFV"), "workflow_id": workflow_id, "status": "draft"}
        self.versions[version["version_id"]] = version
        workflow = {"workflow_id": workflow_id, "name": body["name"], "owner": "admin"}
        return _envelope({"workflow": workflow, "version": version}, 201)

    def list_versions(self, body: Any, query: Any, workflow_id: str) -> httpx.Response:
        return _envelope(list(self.versions.values()))

    def publish(self, body: Any, query: Any, version_id: str) -> httpx.Response:
        self.versions[version_id]["status"] = "published"
        return _envelope(self.versions[version_id])

    @staticmethod
    def _kind(row: dict[str, Any]) -> str | None:
        return next((tag for tag in row["tags"] if tag in NODE_FOR), None)

    def submit_run(self, body: Any, query: Any) -> httpx.Response:
        row = self.by_payload[body["input"]]
        failing = None if self.ignore_failures else self._kind(row)
        node = NODE_FOR.get(failing) if failing else None
        run_id, trace_id = self._new("WR"), self._new("tr")
        self._clock += 1000
        run: dict[str, Any] = {
            "workflow_run_id": run_id,
            "workflow_version_id": body["workflow_version_id"],
            "trace_id": trace_id,
            "status": "failed" if node else "completed",
            "summary": {"output": "", "steps": []},
            "error_summary": None,
        }
        if node:
            run["error_summary"] = f"ToolExecutionError: python_code node '{node}' failed"
        elif "silent_wrong" in row["tags"]:
            run["summary"]["output"] = json.dumps(
                {"decision": "manual_review", "reason_code": "ORDER_REF_UNVERIFIED"}
            )
        else:
            expected = row["expected"]
            run["summary"]["output"] = json.dumps(
                {"decision": expected["decision"], "reason_code": expected["reason_code"]}
            )
        self.runs[run_id] = run
        self.run_for_trace[trace_id] = run_id
        spans = [{"name": "workflow.run", "status": "ERROR" if node else "OK"}] + [
            {"name": f"node.{n}", "status": "ERROR" if n == node else "OK"} for n in STEPS
        ]
        self.traces[trace_id] = {
            "trace_id": trace_id,
            "name": "workflow.run",
            "status": "ERROR" if node else "OK",
            "timestamp_ms": self._clock,
            "request": body["input"],
            "response": run["error_summary"] or run["summary"]["output"],
            "spans": spans,
            "assessments": self.assessments.setdefault(trace_id, []),
        }
        return _envelope(run, 202)

    def get_run(self, body: Any, query: Any, run_id: str) -> httpx.Response:
        return _envelope(self.runs[run_id])

    def run_by_trace(self, body: Any, query: Any, trace_id: str) -> httpx.Response:
        return _envelope(self.runs[self.run_for_trace[trace_id]])

    # -- observability ---------------------------------------------------------------------
    def list_traces(self, body: Any, query: Any) -> httpx.Response:
        since = int(query.get("since_ms", 0))
        wanted = query.get("status", "").upper()
        found = [
            {k: t[k] for k in ("trace_id", "name", "status", "timestamp_ms")}
            for t in self.traces.values()
            if t["timestamp_ms"] >= since and (not wanted or t["status"] == wanted)
        ]
        return _envelope({"traces": sorted(found, key=lambda t: -t["timestamp_ms"])})

    def get_trace(self, body: Any, query: Any, trace_id: str) -> httpx.Response:
        if trace_id not in self.traces:
            return _error(404, f"trace {trace_id!r} not found")
        return _envelope(self.traces[trace_id])

    # -- test set --------------------------------------------------------------------------
    def create_dataset(self, body: Any, query: Any) -> httpx.Response:
        dataset_id = self._new("ED")
        self.datasets[dataset_id] = {"dataset_id": dataset_id, "name": body["name"], "version": 1}
        self.examples[dataset_id] = []
        return _envelope(self.datasets[dataset_id], 201)

    def from_trace(self, body: Any, query: Any, dataset_id: str) -> httpx.Response:
        trace = self.traces[body["trace_id"]]
        example = {
            "example_id": self._new("EX"),
            "input": {"input": trace["request"]},
            "expected": body.get("expected") or {"expected": trace["response"]},
            "tags": ["from-trace", f"trace:{body['trace_id']}", *body.get("tags", [])],
        }
        self.examples[dataset_id].append(example)
        return _envelope(example, 201)

    def list_examples(self, body: Any, query: Any, dataset_id: str) -> httpx.Response:
        return _envelope(self.examples[dataset_id])

    def archive(self, body: Any, query: Any, entity_id: str) -> httpx.Response:
        self.archived.append(entity_id)
        return _envelope({"status": body["status"]})

    # -- review queue ----------------------------------------------------------------------
    def create_queue(self, body: Any, query: Any) -> httpx.Response:
        queue_id = self._new("RVQ")
        self.queues[queue_id] = {"queue_id": queue_id, "name": body["name"]}
        return _envelope(self.queues[queue_id], 201)

    def enqueue(self, body: Any, query: Any, queue_id: str) -> httpx.Response:
        created = []
        for trace_id in body["trace_ids"]:
            item = {"item_id": self._new("RVI"), "queue_id": queue_id, "trace_id": trace_id}
            self.items[item["item_id"]] = item
            created.append(item)
        return _envelope(created, 201)

    def submit(self, body: Any, query: Any, queue_id: str, item_id: str) -> httpx.Response:
        trace_id = self.items[item_id]["trace_id"]
        for name, value in body["answers"].items():
            self.assessments[trace_id].append({"name": name, "value": value})
        return _envelope({"item_id": item_id, "status": "answered"})
