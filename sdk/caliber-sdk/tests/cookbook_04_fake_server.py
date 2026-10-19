"""A stateful stand-in for the routes cookbook 04 calls: object store, install, runs, approvals.

``test_cookbook_04_example.py`` runs the real example script against this instead of a server, so
the script's call sequence, what it sends and what it reads back stay covered offline.

Unlike the thin stand-ins for other labs, this one runs the lab's own logic for real: a run executes
the cookbook's real extractor (``EXTRACT_FIELDS_CODE``, exec'd with the same pre-loaded ``re``,
``json`` and ``datetime`` the python_code sandbox provides), validates with the real
``json_schema`` data transform and the real invoice schema, and writes through an in-memory bucket
that refuses to overwrite where the real output_bucket node does. What it imitates rather than
runs is the platform around that: the queue, the checkpointing, the sandbox process and the
approval policy. Those are covered by ``caliber/tests`` and by running the script against a live
server (see the lab README).
"""

from __future__ import annotations

import datetime
import email
import email.policy
import json
import re
from typing import Any, ClassVar

import httpx
from _server_package import server_module

_PIPELINE = server_module("caliber.workflows.cookbook_document_pipeline")
EXTRACT_FIELDS_CODE: str = _PIPELINE.EXTRACT_FIELDS_CODE
INVOICE_SCHEMA: dict[str, Any] = _PIPELINE.INVOICE_SCHEMA
apply_data_transform = server_module("caliber.workflows.data_transform").apply_data_transform

PROJECT = "PRJ-04"
MAX_BYTES = 100_000


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeDocumentServer:
    def __init__(self, *, store_ready: bool = True) -> None:
        self.store_ready = store_ready
        self.buckets: dict[str, dict[str, bytes]] = {"caliber-workspaces": {}}
        self.runs: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.workflow_status = "paused"
        self.members: list[dict[str, Any]] = []
        namespace: dict[str, Any] = {"re": re, "json": json, "datetime": datetime}
        exec(compile(EXTRACT_FIELDS_CODE, "<extract>", "exec"), namespace)
        self._extract = namespace["run_python_node"]

    # -- plumbing ------------------------------------------------------------------------
    def client(self, caliber_client_cls: Any, *, token: str = "calpat_admin") -> Any:
        return caliber_client_cls(
            "https://caliber.test",
            token=token,
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if path.startswith(("/workflow", "/cookbooks/04")) and (
            request.headers.get("x-caliber-project") != PROJECT
        ):
            return _error(400, "the lab's workflow calls must name the workspace")
        for pattern, method, fn in self.routes:
            found = re.fullmatch(pattern, path)
            if found and method == request.method:
                response: httpx.Response = getattr(self, fn)(request, token, *found.groups())
                return response
        raise AssertionError(f"example called an unstubbed route: {request.method} {path}")

    # -- object store --------------------------------------------------------------------
    def status(self, request: httpx.Request, token: str) -> httpx.Response:
        return _envelope({"connected": True, "endpoint": "http://s3.test", "bucket_count": 1})

    def list_buckets(self, request: httpx.Request, token: str) -> httpx.Response:
        return _envelope([{"name": name} for name in self.buckets])

    def create_bucket(self, request: httpx.Request, token: str) -> httpx.Response:
        self.buckets[json.loads(request.content)["name"]] = {}
        return _envelope({"name": json.loads(request.content)["name"]}, 201)

    def upload(self, request: httpx.Request, token: str, bucket: str) -> httpx.Response:
        message = email.message_from_bytes(
            b"Content-Type: "
            + request.headers["content-type"].encode()
            + b"\r\n\r\n"
            + request.content,
            policy=email.policy.HTTP,
        )
        parts = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
        key = parts["key"].get_content().strip()
        payload = parts["file"].get_payload(decode=True)
        assert isinstance(payload, bytes)
        self.buckets[bucket][key] = payload
        return _envelope({"key": key}, 201)

    def listing(self, request: httpx.Request, token: str, bucket: str) -> httpx.Response:
        prefix = request.url.params.get("prefix", "")
        keys = sorted(k for k in self.buckets[bucket] if k.startswith(prefix))
        objects = [{"key": k, "size": len(self.buckets[bucket][k])} for k in keys]
        return _envelope({"bucket": bucket, "prefix": prefix, "prefixes": [], "objects": objects})

    def delete_objects(self, request: httpx.Request, token: str, bucket: str) -> httpx.Response:
        keys = json.loads(request.content)["keys"]
        for key in keys:
            self.buckets[bucket].pop(key, None)
        return _envelope({"deleted": len(keys), "errors": []})

    def download(self, request: httpx.Request, token: str, bucket: str) -> httpx.Response:
        key = request.url.params["key"]
        if key not in self.buckets[bucket]:
            return _error(404, "object not found")
        return httpx.Response(200, content=self.buckets[bucket][key])

    # -- workspace, install, workflow ----------------------------------------------------
    def cookbooks(self, request: httpx.Request, token: str) -> httpx.Response:
        checks = [
            {
                "label": "An S3-compatible object store for the input and output buckets",
                "status": "ready" if self.store_ready else "configuration_required",
            },
            {
                "label": "Invoice text documents uploaded",
                "status": "operator_confirmation_required",
            },
        ]
        return _envelope(
            {
                "recipes": [
                    {
                        "id": "04",
                        "title": "Document-to-JSON Pipeline",
                        "summary": "s",
                        "prerequisites": ["Invoice text documents uploaded"],
                        "readiness": {"status": "configuration_required", "checks": checks},
                    }
                ]
            }
        )

    def create_project(self, request: httpx.Request, token: str) -> httpx.Response:
        return _envelope({"project_id": PROJECT, "name": json.loads(request.content)["name"]}, 201)

    def add_member(self, request: httpx.Request, token: str, project: str) -> httpx.Response:
        self.members.append(json.loads(request.content))
        return _envelope({"project_id": project, **json.loads(request.content)}, 201)

    def install(self, request: httpx.Request, token: str) -> httpx.Response:
        body = json.loads(request.content)
        assert body["acknowledge_prerequisites"] is True
        return _envelope(
            {
                "recipe": {"id": "04"},
                "workflow": {"workflow_id": "WF-04", "status": "paused"},
                "version": {"version_id": "WFV-04", "status": "draft"},
            },
            201,
        )

    def validate(self, request: httpx.Request, token: str) -> httpx.Response:
        return _envelope({"valid": True, "errors": [], "warnings": []})

    def update_workflow(self, request: httpx.Request, token: str) -> httpx.Response:
        self.workflow_status = json.loads(request.content)["status"]
        return _envelope({"workflow_id": "WF-04", "status": self.workflow_status})

    # -- runs ----------------------------------------------------------------------------
    def submit(self, request: httpx.Request, token: str) -> httpx.Response:
        if self.workflow_status != "active":
            return _error(409, "workflow is paused")
        body = json.loads(request.content)
        run_id = f"WR-{len(self.runs) + 1:02d}"
        run = {
            "workflow_run_id": run_id,
            "workflow_version_id": body["workflow_version_id"],
            "status": "queued",
            "initiator": token,
            "events": [],
        }
        self.runs[run_id] = run
        self._execute(run, body["input"])
        return _envelope(self._view(run), 202)

    def _view(self, run: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in run.items() if k not in {"events", "initiator", "packaged"}}

    def _fail(self, run: dict[str, Any], message: str, code: str = "runtime_error") -> None:
        run.update(status="failed", error_code=code, error_summary=message)

    def _execute(self, run: dict[str, Any], key: str) -> None:
        bucket = self.buckets.get("doc-intake", {})
        files = []
        for name in sorted(k for k in bucket if k.startswith(key)):
            raw = bucket[name]
            files.append(
                {
                    "key": name,
                    "bytes": min(len(raw), MAX_BYTES),
                    "truncated": len(raw) > MAX_BYTES,
                    "text": raw[:MAX_BYTES].decode("utf-8", errors="replace"),
                }
            )
        try:
            record = self._extract(inputs={"files": files}, run_input=key)
        except ValueError as exc:
            self._fail(
                run,
                f"ToolExecutionError: python_code node 'extract_fields' failed: ValueError: {exc}",
            )
            return
        checked = apply_data_transform("json_schema", record, {"schema": INVOICE_SCHEMA})
        errors = checked["metadata"]["errors"]
        run["events"].append(
            {
                "event_type": "workflow.run.step",
                "payload": {
                    "step": {
                        "node_id": "validate_schema",
                        "output_by_port": {
                            "valid": checked["valid"],
                            "metadata": {"errors": errors},
                        },
                    }
                },
            }
        )
        run["packaged"] = {"record": record, "schema_errors": errors}
        if checked["valid"]:
            self._write(run, f"invoices/{record['invoice_number']}.json", record, overwrite=False)
        else:
            run["status"] = "waiting_approval"

    def _write(
        self, run: dict[str, Any], key: str, record: dict[str, Any], *, overwrite: bool
    ) -> None:
        out = self.buckets["doc-extracted"]
        if key in out and not overwrite:
            self._fail(
                run,
                f"ToolExecutionError: output_bucket write failed for {key!r}: "
                f"object already exists: {key!r}",
            )
            return
        out[key] = json.dumps(record, indent=2, sort_keys=True).encode()
        run["status"] = "completed"

    def get_run(self, request: httpx.Request, token: str, run_id: str) -> httpx.Response:
        return _envelope(self._view(self.runs[run_id]))

    def events(self, request: httpx.Request, token: str, run_id: str) -> httpx.Response:
        return _envelope(self.runs[run_id]["events"])

    def decide(
        self, request: httpx.Request, token: str, run_id: str, decision: str
    ) -> httpx.Response:
        run = self.runs[run_id]
        if token == run["initiator"]:
            return _error(
                403,
                "the account that triggered this run cannot approve it; a second person decides",
            )
        if decision == "reject":
            self._fail(run, json.loads(request.content).get("reason", ""), "approval_rejected")
        else:
            run["approved"] = True
        return _envelope(self._view(run))

    def resume(self, request: httpx.Request, token: str, run_id: str) -> httpx.Response:
        run = self.runs[run_id]
        if not run.get("approved"):
            return _error(409, "has no approved runtime approval decision to resume from")
        packaged = run["packaged"]
        record = {
            **packaged["record"],
            "review": {"decision": "approved", "schema_errors": packaged["schema_errors"]},
        }
        stem = record["source_key"].split("/")[-1].rsplit(".", 1)[0]
        self._write(run, f"reviewed/{stem}.json", record, overwrite=True)
        return _envelope(self._view(run))

    routes: ClassVar[list[tuple[str, str, str]]] = [
        (r"/object-store/status", "GET", "status"),
        (r"/object-store/buckets", "GET", "list_buckets"),
        (r"/object-store/buckets", "POST", "create_bucket"),
        (r"/object-store/buckets/([^/]+)/objects", "POST", "upload"),
        (r"/object-store/buckets/([^/]+)/objects", "GET", "listing"),
        (r"/object-store/buckets/([^/]+)/objects/delete", "POST", "delete_objects"),
        (r"/object-store/buckets/([^/]+)/object", "GET", "download"),
        (r"/cookbooks", "GET", "cookbooks"),
        (r"/projects", "POST", "create_project"),
        (r"/projects/([^/]+)/members", "POST", "add_member"),
        (r"/cookbooks/04/install", "POST", "install"),
        (r"/workflow-versions/WFV-04/validate", "POST", "validate"),
        (r"/workflows/WF-04", "PATCH", "update_workflow"),
        (r"/workflow-runs", "POST", "submit"),
        (r"/workflow-runs/([^/]+)", "GET", "get_run"),
        (r"/workflow-runs/([^/]+)/events", "GET", "events"),
        (r"/workflow-runs/([^/]+)/approval/(approve|reject)", "POST", "decide"),
        (r"/workflow-runs/([^/]+)/resume", "POST", "resume"),
    ]
