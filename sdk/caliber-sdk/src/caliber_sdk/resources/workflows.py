"""Workflows, versions, runs, deployments, and services.

The workflow surface is five route groups on the server. It is one resource
tree here, because "workflow" is one concept to a caller and the split is an
implementation detail of the route layer.
"""

from __future__ import annotations

from typing import Any

from ..errors import CaliberError
from ..models._decode import decode, decode_list
from ..models.workflows import (
    FAILED_RUN_STATES,
    Workflow,
    WorkflowRun,
    WorkflowService,
    WorkflowVersion,
)
from ..waiters import wait_for
from ._base import Resource

_List = list


class WorkflowRunFailed(CaliberError):
    """A run reached a terminal state that is not success."""

    def __init__(self, run: WorkflowRun) -> None:
        super().__init__(f"workflow run {run.workflow_run_id} finished as {run.status!r}")
        self.run = run


class WorkflowVersionsAPI(Resource):
    """Immutable manifest snapshots of one workflow."""

    def list(self, workflow_id: str) -> _List[WorkflowVersion]:
        return decode_list(WorkflowVersion, self._get(f"/workflows/{workflow_id}/versions"))

    def get(self, version_id: str) -> WorkflowVersion:
        return decode(WorkflowVersion, self._get(f"/workflow-versions/{version_id}"))

    def create(self, workflow_id: str, manifest: dict[str, Any]) -> WorkflowVersion:
        """Register a draft version. Drafts are not runnable until published."""
        return decode(
            WorkflowVersion,
            self._post(f"/workflows/{workflow_id}/versions", json={"manifest": manifest}),
        )

    def validate(self, version_id: str) -> Any:
        """Validation report for a version's manifest.

        Returned untyped on purpose: the report is produced by the server's
        validator, and a schema here would be a second definition of a contract
        that lives there.
        """
        return self._post(f"/workflow-versions/{version_id}/validate")

    def compile(self, version_id: str) -> Any:
        return self._post(f"/workflow-versions/{version_id}/compile")

    def publish(self, version_id: str) -> WorkflowVersion:
        return decode(WorkflowVersion, self._post(f"/workflow-versions/{version_id}/publish"))

    def update(
        self, version_id: str, *, manifest: dict[str, Any], manifest_hash: str
    ) -> WorkflowVersion:
        """Edit a draft version's manifest. ``manifest_hash`` must match the
        version's current hash (optimistic concurrency) -- a mismatch is a
        409, meaning reload before editing. Published versions refuse
        (409): they are immutable."""
        return decode(
            WorkflowVersion,
            self._patch(
                f"/workflow-versions/{version_id}",
                json={"manifest": manifest, "manifest_hash": manifest_hash},
            ),
        )

    def restore(self, version_id: str) -> WorkflowVersion:
        """Clone any prior version's manifest into a new editable draft.
        History is preserved -- the source version is untouched."""
        return decode(WorkflowVersion, self._post(f"/workflow-versions/{version_id}/restore"))

    def diff(self, version_id: str, other_version_id: str) -> Any:
        """Structured, order-independent graph diff. ``version_id`` is the
        base (older/left); ``other_version_id`` is the candidate (newer/
        right) -- oriented so added/removed read naturally comparing
        v(n-1) -> v(n)."""
        return self._get(f"/workflow-versions/{version_id}/diff/{other_version_id}")

    def export_manifest(self, version_id: str) -> str:
        """The version's manifest as YAML text (not JSON -- the server
        returns ``application/x-yaml`` directly, so this downloads raw
        bytes rather than going through the envelope path)."""
        return self._transport.download(f"/workflow-versions/{version_id}/export/manifest").decode(
            "utf-8"
        )

    def export_python(self, version_id: str) -> str:
        """The version compiled to standalone Python source (text, not
        JSON). Prefers the immutable stored bundle for a published version,
        so the export is byte-identical to what was compiled/approved."""
        return self._transport.download(f"/workflow-versions/{version_id}/export/python").decode(
            "utf-8"
        )

    def preview_run(
        self, version_id: str, *, input: Any = None, session_id: str | None = None, **params: Any
    ) -> Any:
        """Run the version in preview mode: real tool bindings are not used.
        For a real, persisted run see :meth:`run` or ``client.workflows.runs``.
        ``params`` may carry ``manifest`` to preview an unsaved edit before
        it is written to a version."""
        body: dict[str, Any] = {**params}
        if input is not None:
            body["input"] = input
        if session_id is not None:
            body["session_id"] = session_id
        return self._post(f"/workflow-versions/{version_id}/preview-run", json=body)

    def run(
        self, version_id: str, *, input: Any = None, alias: str | None = None, **params: Any
    ) -> Any:
        """Execute the version as a real, persisted manual run -- real tool
        bindings and the configured executor, unlike :meth:`preview_run`.

        A ``manifest`` override in ``params`` is only accepted when
        ``alias`` is ``"manual"`` (the default): a deployed alias always
        executes its immutable saved version.
        """
        body: dict[str, Any] = {**params}
        if input is not None:
            body["input"] = input
        if alias is not None:
            body["alias"] = alias
        return self._post(f"/workflow-versions/{version_id}/run", json=body)

    def propose_patch(
        self, version_id: str, *, evidence: dict[str, Any], job_id: str | None = None
    ) -> Any:
        """Generate a patch candidate from failure evidence: localizes the
        failure, generates semantic patch ops, compiles the candidate, and
        persists it for the approval UI. Returns the diagnosis, patch,
        graph diff, and candidate validation report -- nothing is applied
        automatically."""
        body: dict[str, Any] = {"evidence": evidence}
        if job_id is not None:
            body["job_id"] = job_id
        return self._post(f"/workflow-versions/{version_id}/propose-patch", json=body)

    def copilot_edit(
        self, version_id: str, *, instruction: str, manifest: dict[str, Any] | None = None
    ) -> Any:
        """Propose a natural-language edit to the manifest. Nothing is
        persisted -- apply an accepted proposal through :meth:`update`.
        With the default ``fake`` LLM provider the manifest comes back
        unchanged (a safe no-op)."""
        body: dict[str, Any] = {"instruction": instruction}
        if manifest is not None:
            body["manifest"] = manifest
        return self._post(f"/workflow-versions/{version_id}/copilot-edit", json=body)

    def plan_build(
        self, version_id: str, *, goal: str, manifest: dict[str, Any] | None = None
    ) -> Any:
        """Author a manifest from a plain-language goal -- the blank-slate
        sibling of :meth:`copilot_edit`. Nothing is persisted."""
        body: dict[str, Any] = {"goal": goal}
        if manifest is not None:
            body["manifest"] = manifest
        return self._post(f"/workflow-versions/{version_id}/plan-build", json=body)


class WorkflowRunsAPI(Resource):
    """Executions, and waiting on them."""

    def list(self, workflow_id: str, *, status: str | None = None) -> _List[WorkflowRun]:
        """Runs of one workflow.

        Scoped to a workflow because the server has no unscoped run listing:
        ``/workflow-runs`` is POST-only (submission). An SDK method implying
        otherwise returned 405 at runtime, which is how this was found.
        """
        params = {"status": status} if status else None
        return decode_list(WorkflowRun, self._get(f"/workflows/{workflow_id}/runs", params=params))

    def get(self, run_id: str) -> WorkflowRun:
        return decode(WorkflowRun, self._get(f"/workflow-runs/{run_id}"))

    def submit(
        self,
        *,
        workflow_version_id: str | None = None,
        workflow_id: str | None = None,
        alias: str | None = None,
        input: Any = None,
        idempotency_key: str | None = None,
        **options: Any,
    ) -> WorkflowRun:
        """Queue a run. Returns immediately with a run to poll.

        A run targets either a specific version or a workflow plus a deployment
        alias — the server accepts both, and forcing one here would make the
        alias path unreachable, which is how a deployed workflow is invoked.

        ``idempotency_key`` is passed through because submission is the one
        mutating call the SDK cannot safely retry on its own.
        """
        body: dict[str, Any] = {**options}
        for key, value in (
            ("workflow_version_id", workflow_version_id),
            ("workflow_id", workflow_id),
            ("alias", alias),
            ("input", input),
            ("idempotency_key", idempotency_key),
        ):
            if value is not None:
                body[key] = value
        return decode(WorkflowRun, self._post("/workflow-runs", json=body))

    def cancel(self, run_id: str) -> WorkflowRun:
        return decode(WorkflowRun, self._post(f"/workflow-runs/{run_id}/cancel"))

    def wait(
        self, run_id: str, *, timeout: float = 900.0, raise_on_failure: bool = True, **options: Any
    ) -> WorkflowRun:
        """Poll until the run stops.

        Raises by default, unlike calibration: a script that submitted work and
        got a failure almost always wants to stop, whereas a calibration score
        is the thing being measured. Pass ``raise_on_failure=False`` to inspect
        instead.
        """
        run = wait_for(
            lambda: self.get(run_id),
            is_done=lambda item: item.is_terminal,
            timeout=timeout,
            **options,
        )
        if raise_on_failure and run.status in FAILED_RUN_STATES:
            raise WorkflowRunFailed(run)
        return run


class WorkflowServicesAPI(Resource):
    """Workflows published as external HTTP services.

    The server splits this in two, and so does this class. *Management* lives
    under ``/workflows/{id}/service`` — configuring and publishing is a
    property of the workflow. *Invocation* lives under ``/services/{id}`` —
    that is the external surface, authenticated by per-service tokens rather
    than a user credential.

    There is no unscoped service listing; a service is reached through its
    workflow. An earlier version of this class invented ``GET /services`` and
    returned 404 at runtime.
    """

    def get(self, workflow_id: str) -> WorkflowService:
        return decode(WorkflowService, self._get(f"/workflows/{workflow_id}/service"))

    def publish(self, workflow_id: str, **options: Any) -> WorkflowService:
        return decode(
            WorkflowService, self._post(f"/workflows/{workflow_id}/service", json=options)
        )

    def unpublish(self, workflow_id: str) -> bool:
        payload = self._delete(f"/workflows/{workflow_id}/service")
        return isinstance(payload, dict) and payload.get("status") == "unpublished"

    def openapi(self, workflow_id: str) -> Any:
        """The per-workflow OpenAPI document the service surface publishes."""
        return self._get(f"/services/{workflow_id}/openapi.json")

    def invoke(self, workflow_id: str, payload: Any = None, **options: Any) -> Any:
        """Call a published service.

        The external surface: in production this is authenticated by a
        per-service token rather than the user credential the rest of this
        client carries.
        """
        return self._post(f"/services/{workflow_id}/invoke", json=payload or {}, **options)


class WorkflowPromotionsAPI(Resource):
    """Approve or reject a pending deployment-alias promotion.

    A promotion is created by ``deployments.promote`` on a gated alias (see
    ``ARCHITECTURE.md`` §4) and sits pending until an approver scope acts on
    it. There is no listing method here yet -- ``GET
    /workflows/{id}/promotions`` is covered by a follow-up wave.
    """

    def approve(self, promotion_id: str, *, reason: str | None = None, **params: Any) -> Any:
        body: dict[str, Any] = {**params}
        if reason is not None:
            body["reason"] = reason
        return self._post(f"/workflow-promotions/{promotion_id}/approve", json=body)

    def reject(self, promotion_id: str, *, reason: str | None = None, **params: Any) -> Any:
        body: dict[str, Any] = {**params}
        if reason is not None:
            body["reason"] = reason
        return self._post(f"/workflow-promotions/{promotion_id}/reject", json=body)


class WorkflowBenchmarkReportsAPI(Resource):
    """Saved bakeoff/benchmark scorecards -- an evidence record, not a
    per-workflow child; reports are not scoped to one workflow's own id."""

    def list(self, *, status: str = "all") -> Any:
        return self._get("/workflow-benchmark-reports", params={"status": status})

    def create(self, *, name: str, worksheet: dict[str, Any], **options: Any) -> Any:
        return self._post(
            "/workflow-benchmark-reports",
            json={"name": name, "worksheet": worksheet, **options},
        )

    def update(self, report_id: str, **changes: Any) -> Any:
        return self._patch(f"/workflow-benchmark-reports/{report_id}", json=changes)

    def delete(self, report_id: str) -> Any:
        return self._delete(f"/workflow-benchmark-reports/{report_id}")


class WorkflowsAPI(Resource):
    """Workflows, plus versions, runs, services, promotions, and benchmark
    reports as sub-resources."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.versions = WorkflowVersionsAPI(transport)
        self.runs = WorkflowRunsAPI(transport)
        self.services = WorkflowServicesAPI(transport)
        self.promotions = WorkflowPromotionsAPI(transport)
        self.benchmark_reports = WorkflowBenchmarkReportsAPI(transport)

    def list(self, *, status: str | None = None) -> _List[Workflow]:
        params = {"status": status} if status else None
        return decode_list(Workflow, self._get("/workflows", params=params))

    def get(self, workflow_id: str) -> Workflow:
        return decode(Workflow, self._get(f"/workflows/{workflow_id}"))

    def create(self, name: str, *, description: str | None = None, **options: Any) -> Workflow:
        body: dict[str, Any] = {"name": name, **options}
        if description is not None:
            body["description"] = description
        return decode(Workflow, self._post("/workflows", json=body))

    def update(self, workflow_id: str, **changes: Any) -> Workflow:
        return decode(Workflow, self._patch(f"/workflows/{workflow_id}", json=changes))

    def delete(self, workflow_id: str) -> Any:
        return self._delete(f"/workflows/{workflow_id}")

    def patches(self, workflow_id: str) -> Any:
        """Proposed patch candidates (from ``versions.propose_patch``) for
        this workflow's approval UI."""
        return self._get(f"/workflows/{workflow_id}/patches")

    def components(self) -> Any:
        """The Studio node-palette catalog: every built-in component type
        and its typed input/output ports."""
        return self._get("/workflow-components")

    def templates(self) -> Any:
        """The starter-manifest catalog used by "New workflow from
        template"."""
        return self._get("/workflow-templates")

    def cron_preview(self, *, expr: str, tz: str = "UTC", count: int = 5) -> Any:
        """Next fire times for a Start-trigger cron expression. Read-only,
        no workflow required -- powers the Studio trigger panel's preview."""
        return self._get("/workflow-cron-preview", params={"expr": expr, "tz": tz, "count": count})

    def upload_staging_file(
        self,
        filename: str,
        content: bytes,
        *,
        kind: str = "input",
        media_type: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Upload a file before any run exists -- a manual-run input staged
        ahead of :meth:`WorkflowVersionsAPI.run`, not yet bound to a
        ``workflow_run_id``. Multipart, so it does not go through the JSON
        path.
        """
        files = {"file": (filename, content, media_type or "application/octet-stream")}
        data: dict[str, str] = {"kind": kind}
        params = {"session_id": session_id} if session_id else None
        response = self._transport.request(
            "POST", "/workflow-files", files=files, data=data, params=params
        )
        return response.data


__all__ = [
    "WorkflowBenchmarkReportsAPI",
    "WorkflowPromotionsAPI",
    "WorkflowRunFailed",
    "WorkflowRunsAPI",
    "WorkflowServicesAPI",
    "WorkflowVersionsAPI",
    "WorkflowsAPI",
]
