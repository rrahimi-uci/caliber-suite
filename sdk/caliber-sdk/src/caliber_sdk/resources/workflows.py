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


class WorkflowsAPI(Resource):
    """Workflows, plus versions, runs, services, and promotions as
    sub-resources."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.versions = WorkflowVersionsAPI(transport)
        self.runs = WorkflowRunsAPI(transport)
        self.services = WorkflowServicesAPI(transport)
        self.promotions = WorkflowPromotionsAPI(transport)

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


__all__ = [
    "WorkflowPromotionsAPI",
    "WorkflowRunFailed",
    "WorkflowRunsAPI",
    "WorkflowServicesAPI",
    "WorkflowVersionsAPI",
    "WorkflowsAPI",
]
