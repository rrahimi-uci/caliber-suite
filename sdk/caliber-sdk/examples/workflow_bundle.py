"""Export, verify, and import a workflow release through the CALIBER SDK."""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def clone_sealed_release(
    caliber: CaliberClient,
    *,
    version_id: str,
    new_name: str,
) -> dict[str, Any]:
    """Clone a dependency-complete release into a fresh editable workflow."""
    status = caliber.workflows.versions.deployment_bundle_status(version_id)
    if not status.get("valid"):
        raise RuntimeError("workflow deployment bundle failed integrity verification")
    if not status.get("ready_to_deploy"):
        raise RuntimeError("workflow deployment bundle has unresolved dependencies")

    bundle = caliber.workflows.versions.export_deployment_bundle(version_id)
    preview = caliber.workflows.preview_import(
        deployment_bundle=bundle,
        name=new_name,
    )
    if not preview.get("ready_to_import"):
        raise RuntimeError("destination dependency preflight failed")
    imported = caliber.workflows.import_workflow(
        deployment_bundle=bundle,
        name=new_name,
    )
    return {
        "source_digest": status["digest"],
        "workflow_id": imported.workflow_id,
        "name": imported.name,
    }
