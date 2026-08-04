"""Built-in Cookbook catalog and atomic draft installation routes."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user, resolve_identity
from caliber.db.models import CaliberWorkflow, CaliberWorkflowVersion
from caliber.ids import new_workflow_id, new_workflow_version_id
from caliber.routes._deps import envelope_response_dict, get_session_factory, parse_json_object
from caliber.schemas import WorkflowSchema, WorkflowVersionSchema
from caliber.workflows.cookbook_catalog import (
    build_cookbook_catalog,
    materialize_cookbook_manifest,
)
from caliber.workflows.manifest import compute_manifest_hash

PREFIX = "/ajax-api/2.0/mlflow/caliber"
CATALOG_PATH = PREFIX + "/cookbooks"
INSTALL_PATH = CATALOG_PATH + "/{cookbook_id}/install"


class CookbookInstallRequest(BaseModel):
    """Explicit operator choices for installing a built-in example."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=256)
    acknowledge_prerequisites: bool = False


def _recipe_or_404(cookbook_id: str) -> dict[str, Any]:
    recipe = next(
        (item for item in build_cookbook_catalog()["recipes"] if item["id"] == cookbook_id),
        None,
    )
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"cookbook {cookbook_id!r} not found")
    return deepcopy(recipe)


def _catalog_payload(request: Request) -> dict[str, Any]:
    catalog = deepcopy(build_cookbook_catalog())
    config = request.app.state.config
    approval_ready = bool(
        config.workflow_run_queue_enabled
        and config.workflow_run_runtime_approvals_enabled
        and config.workflow_run_checkpointing_enabled
    )
    for recipe in catalog["recipes"]:
        prerequisites = recipe.get("prerequisites") or []
        checks = [
            {
                "label": prerequisite,
                "status": "operator_confirmation_required",
            }
            for prerequisite in prerequisites
        ]
        if recipe["id"] in {"03", "07", "09"}:
            checks.insert(
                0,
                {
                    "label": "Workflow queue, runtime approvals, and checkpoints",
                    "status": "ready" if approval_ready else "configuration_required",
                    "settings_path": "/settings/runtime",
                },
            )
        recipe["readiness"] = {
            "status": (
                "ready"
                if not prerequisites and all(check["status"] == "ready" for check in checks)
                else "configuration_required"
            ),
            "checks": checks,
        }
    return catalog


async def list_cookbooks(request: Request) -> JSONResponse:
    require_user(request)
    return envelope_response_dict(_catalog_payload(request))


def _default_install_name(recipe: dict[str, Any]) -> str:
    return f"Cookbook {recipe['id']} — {recipe['title']}"


def _install_cookbook_draft(
    factory: Any,
    *,
    actor: str,
    identity: Any,
    recipe: dict[str, Any],
    cookbook_id: str,
    workflow_id: str,
    version_id: str,
    name: str,
    manifest: dict[str, Any],
    prerequisites_acknowledged: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist the atomic example bundle off the ASGI event loop."""

    with factory() as session:
        existing = (
            session.execute(select(CaliberWorkflow).where(CaliberWorkflow.name == name))
            .scalars()
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"workflow name {name!r} is already in use by {existing.workflow_id!r}",
            )
        workflow = CaliberWorkflow(
            workflow_id=workflow_id,
            name=name,
            description=(
                f"Built-in Cookbook {cookbook_id} example ({recipe['catalog_version']}): "
                f"{recipe['summary']}"
            ),
            owner=actor,
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            status="paused",
        )
        version = CaliberWorkflowVersion(
            version_id=version_id,
            workflow_id=workflow_id,
            version_number=1,
            status="draft",
            manifest=manifest,
            manifest_hash=compute_manifest_hash(manifest),
            validation_report={
                "valid": True,
                "errors": [],
                "warnings": [
                    {
                        "code": "cookbook_review_required",
                        "path": "",
                        "message": "Review prerequisites and bindings before publishing this example.",
                        "severity": "warning",
                    }
                ],
            },
            created_by=actor,
        )
        session.add_all([workflow, version])
        session.flush()
        audit_record(
            session,
            actor=actor,
            action="install_cookbook_draft",
            entity_type="workflow",
            entity_id=workflow_id,
            details={
                "cookbook_id": cookbook_id,
                "catalog_version": recipe["catalog_version"],
                "version_id": version_id,
                "prerequisites_acknowledged": prerequisites_acknowledged,
                "activation_requires_review": True,
            },
        )
        session.commit()
        return (
            WorkflowSchema.model_validate(workflow).model_dump(mode="json"),
            WorkflowVersionSchema.model_validate(version).model_dump(mode="json"),
        )


async def install_cookbook(request: Request) -> JSONResponse:
    """Install a Cookbook as one paused workflow and one editable draft.

    Workflow + version creation share a transaction.  A parse/constraint error
    therefore cannot leave the orphan workflow produced by the ordinary
    two-request template flow.  The workflow is paused intentionally: example
    manifests can contain model, connector, approval, or side-effect bindings
    that an operator must review before publication or activation.
    """

    cookbook_id = request.path_params["cookbook_id"]
    recipe = _recipe_or_404(cookbook_id)
    body = await parse_json_object(request)
    payload = CookbookInstallRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    prerequisites = list(recipe.get("prerequisites") or [])
    if prerequisites and not payload.acknowledge_prerequisites:
        raise HTTPException(
            status_code=400,
            detail=(
                "review and acknowledge the Cookbook prerequisites before installation: "
                + "; ".join(prerequisites)
            ),
        )

    workflow_id = new_workflow_id()
    version_id = new_workflow_version_id()
    name = payload.name.strip() if payload.name else _default_install_name(recipe)
    manifest = materialize_cookbook_manifest(
        cookbook_id,
        workflow_id=workflow_id,
        workflow_name=name,
    )

    workflow_data, version_data = await run_in_threadpool(
        _install_cookbook_draft,
        get_session_factory(request),
        actor=actor,
        identity=identity,
        recipe=recipe,
        cookbook_id=cookbook_id,
        workflow_id=workflow_id,
        version_id=version_id,
        name=name,
        manifest=manifest,
        prerequisites_acknowledged=bool(prerequisites),
    )

    return envelope_response_dict(
        {
            "recipe": {key: value for key, value in recipe.items() if key != "manifest_template"},
            "workflow": workflow_data,
            "version": version_data,
            "activation_requires_review": True,
        },
        status_code=201,
    )


def register(app: Starlette) -> None:
    app.routes.append(Route(CATALOG_PATH, list_cookbooks, methods=["GET"]))
    app.routes.append(Route(INSTALL_PATH, install_cookbook, methods=["POST"]))


__all__ = ["register"]
