"""``/caliber/capabilities`` — runtime feature capability contract.

The frontend uses this endpoint to hide or disable controls that depend on
rollout flags (queue-based runs, runtime approvals, checkpoints, etc.).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.artifact_capabilities import ARTIFACT_FAMILY_CAPABILITIES
from caliber.auth import require_user
from caliber.routes._deps import envelope_response
from caliber.schemas import PlatformCapabilitiesSchema, WorkflowRunCapabilitySchema

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CAPABILITIES_PATH = PREFIX + "/capabilities"


async def get_capabilities(request: Request) -> JSONResponse:
    require_user(request)
    config = request.app.state.config
    queue_enabled = bool(config.workflow_run_queue_enabled)
    runtime_approvals = bool(config.workflow_run_runtime_approvals_enabled)
    checkpointing = bool(config.workflow_run_checkpointing_enabled)

    workflow_capabilities = WorkflowRunCapabilitySchema(
        queue_enabled=queue_enabled,
        supports_async_submit=queue_enabled,
        supports_cancel=queue_enabled,
        supports_retry=queue_enabled,
        supports_resume=queue_enabled and checkpointing,
        runtime_approvals_enabled=runtime_approvals,
        checkpointing_enabled=checkpointing,
        event_backend=str(config.workflow_run_event_backend),
    )
    payload = PlatformCapabilitiesSchema(
        workflow_runs=workflow_capabilities,
        sync_workflow_version_run=True,
        artifact_families=ARTIFACT_FAMILY_CAPABILITIES,
    )
    return envelope_response(payload)


def register(app: Starlette) -> None:
    app.routes.append(Route(CAPABILITIES_PATH, get_capabilities, methods=["GET"]))
