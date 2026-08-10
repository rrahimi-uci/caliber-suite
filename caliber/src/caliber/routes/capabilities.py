"""``/caliber/capabilities`` — runtime feature capability contract.

The frontend uses this endpoint to hide or disable controls that depend on
rollout flags (queue-based runs, runtime approvals, checkpoints, etc.).
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.artifact_capabilities import ARTIFACT_FAMILY_CAPABILITIES
from caliber.auth import require_user
from caliber.extensibility import optimizer_registry
from caliber.extensibility.entrypoints import (
    ALLOWLIST_ENV_VAR,
    available_optimizer_plugins,
)
from caliber.routes._deps import envelope_response
from caliber.routes.openapi import stability_summary
from caliber.schemas import (
    ExtensibilityCapabilitySchema,
    OptimizerPluginSchema,
    PlatformCapabilitiesSchema,
    RegisteredOptimizerSchema,
    WorkflowRunCapabilitySchema,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CAPABILITIES_PATH = PREFIX + "/capabilities"


def _extensibility() -> ExtensibilityCapabilitySchema:
    """Report what can run here and what has been permitted to run.

    Two lists rather than one. ``optimizers`` is what the deployment can
    actually dispatch; ``plugins`` is what is *installed*, including entries
    that are inert because nobody allowlisted them. Collapsing them would hide
    the case an operator most needs to see -- a wheel that was installed and
    then did nothing -- and reading ``plugins`` never imports the code it
    describes, which is what makes it safe to render before deciding to trust
    anything in it.

    Offloaded to a thread by the caller, not because listing is expensive but
    because the *first* call also loads allowlisted plugins -- reading dist-info
    and importing modules from disk. That is blocking I/O, and one slow plugin
    import must not stall the event loop for every other request.
    """
    registry = optimizer_registry()
    optimizers = [
        RegisteredOptimizerSchema(
            name=spec.name,
            summary=spec.summary,
            artifact_types=sorted(spec.artifact_types),
            source=spec.source,
            requires=spec.requires,
            distribution=spec.distribution,
            explicit_only=spec.explicit_only,
            experimental=spec.experimental,
        )
        for spec in registry
    ]
    plugins = [
        OptimizerPluginSchema(
            name=str(entry["name"]),
            distribution=(str(entry["distribution"]) if entry["distribution"] else None),
            value=str(entry["value"]),
            allowlisted=bool(entry["allowlisted"]),
            # An allowlisted plugin that failed to load is the case worth
            # surfacing: the deployment asked for it, so silence would read as
            # success.
            error=registry.load_errors.get(str(entry["distribution"] or entry["name"])),
        )
        for entry in available_optimizer_plugins()
    ]
    return ExtensibilityCapabilitySchema(
        optimizers=optimizers,
        plugins=plugins,
        allowlist_env_var=ALLOWLIST_ENV_VAR,
    )


async def get_capabilities(request: Request) -> JSONResponse:
    require_user(request)
    config = request.app.state.config
    queue_enabled = bool(config.workflow_run_queue_enabled)
    runtime_approvals = bool(config.workflow_run_runtime_approvals_enabled)
    checkpointing = bool(config.workflow_run_checkpointing_enabled)
    approval_blockers = []
    if not queue_enabled:
        approval_blockers.append("workflow run queue is disabled")
    if not runtime_approvals:
        approval_blockers.append("runtime approvals are disabled")
    if not checkpointing:
        approval_blockers.append("checkpoint persistence is disabled")

    workflow_capabilities = WorkflowRunCapabilitySchema(
        queue_enabled=queue_enabled,
        supports_async_submit=queue_enabled,
        supports_cancel=queue_enabled,
        supports_retry=queue_enabled,
        supports_resume=queue_enabled and checkpointing,
        runtime_approvals_enabled=runtime_approvals,
        checkpointing_enabled=checkpointing,
        event_backend=str(config.workflow_run_event_backend),
        approval_readiness={
            "status": "ready" if not approval_blockers else "configuration_required",
            "blockers": approval_blockers,
            "decision_scope": "Each Human Approval node's required_role is enforced.",
            "allow_self_approval": bool(config.approval_allow_self_approval),
            "audit_actions": [
                "workflow_run_waiting_approval",
                "workflow_run_approval_approved",
                "workflow_run_approval_rejected",
            ],
            # A SPA route, not an API path. The SPA registers "/settings" only;
            # "/settings/runtime" is the *API* path (routes/settings.py), and
            # naming it here produced a link that resolves nowhere. Latent so
            # far because the type declares the field and no component renders
            # it yet -- the same shape as the Cookbook readiness dead link.
            "settings_path": "/settings",
        },
    )
    payload = PlatformCapabilitiesSchema(
        workflow_runs=workflow_capabilities,
        sync_workflow_version_run=True,
        artifact_families=ARTIFACT_FAMILY_CAPABILITIES,
        # Served alongside the OpenAPI document's per-operation
        # ``x-caliber-stability`` so an SDK can feature-detect without parsing
        # a 189 KB specification. A test asserts the two never disagree.
        sdk_stability=stability_summary(),
        extensibility=await run_in_threadpool(_extensibility),
    )
    return envelope_response(payload)


def register(app: Starlette) -> None:
    app.routes.append(Route(CAPABILITIES_PATH, get_capabilities, methods=["GET"]))
