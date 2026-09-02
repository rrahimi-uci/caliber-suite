"""End-to-end contracts for sealed workflow deployment bundles."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberSkill,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.workflows import deployment_bundle as deployment_bundle_module
from caliber.workflows.deployment_bundle import seal_bundle, verify_bundle
from caliber.workflows.promoter import (
    AliasPreflightError,
    compile_version,
    require_alias_target_ready,
)
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    fake_resolver,
    make_manifest,
)


def test_compile_seals_exportable_bundle_and_import_verifies_it(client: TestClient) -> None:
    workflow_id = create_workflow(client, "Portable source")
    version_id, _ = create_draft(client, workflow_id, make_manifest(workflow_id))

    compiled = client.post(f"{PREFIX}/workflow-versions/{version_id}/compile")
    assert compiled.status_code == 200, compiled.text

    status = client.get(f"{PREFIX}/workflow-versions/{version_id}/deployment-bundle/status")
    assert status.status_code == 200, status.text
    assert status.json()["data"] == {
        "sealed": True,
        "valid": True,
        "errors": [],
        "digest": status.json()["data"]["digest"],
        "dependency_count": 0,
        "ready_to_deploy": True,
        "dependencies": [],
    }

    exported = client.get(f"{PREFIX}/workflow-versions/{version_id}/export/deployment-bundle")
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("application/json")
    assert ".bundle.json" in exported.headers["content-disposition"]
    bundle = exported.json()
    assert verify_bundle(bundle).ready_to_deploy is True

    preview = client.post(
        f"{PREFIX}/workflows/import/preview",
        json={"deployment_bundle": bundle, "name": "Portable copy"},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["bundle_verification"]["valid"] is True
    assert preview.json()["data"]["ready_to_import"] is True

    imported = client.post(
        f"{PREFIX}/workflows/import",
        json={"deployment_bundle": bundle, "name": "Portable copy"},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["data"]["version"]["status"] == "draft"


def test_import_rejects_tampered_bundle(client: TestClient) -> None:
    workflow_id = create_workflow(client, "Tamper source")
    version_id, _ = create_draft(client, workflow_id, make_manifest(workflow_id))
    assert client.post(f"{PREFIX}/workflow-versions/{version_id}/compile").status_code == 200
    bundle = client.get(f"{PREFIX}/workflow-versions/{version_id}/export/deployment-bundle").json()
    bundle["manifest"]["description"] = "modified after sealing"

    response = client.post(f"{PREFIX}/workflows/import/preview", json={"deployment_bundle": bundle})

    assert response.status_code == 400
    assert "digest does not match" in response.json()["detail"]

    bundle["dependencies"] = ["not-a-dependency-object"]
    malformed = seal_bundle(bundle)
    response = client.post(
        f"{PREFIX}/workflows/import/preview", json={"deployment_bundle": malformed}
    )
    assert response.status_code == 400
    assert "dependencies entries must be objects" in response.json()["detail"]


def test_bundle_pins_tool_version_and_embeds_skill_without_secret_values(
    db_session: Session,
) -> None:
    manifest = make_manifest("wf-bundle")
    manifest["nodes"]["agent"]["skills"] = ["finance-analysis"]
    manifest["nodes"]["agent"]["tools"] = ["lookup"]
    manifest["tools"] = {
        "lookup": {
            "type": "registered_function",
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1,<2",
            "secret_refs": ["finance-api"],
        }
    }
    workflow = CaliberWorkflow(workflow_id="wf-bundle", name="Bundle", owner="@test")
    version = CaliberWorkflowVersion(
        version_id="wfv-bundle",
        workflow_id=workflow.workflow_id,
        version_number=1,
        status="draft",
        manifest=manifest,
        manifest_hash="",
        created_by="@test",
    )
    skill = CaliberSkill(
        skill_id="skill-finance",
        name="finance-analysis",
        summary="Analyze monthly financials",
        content="Compute mean, median, and percentiles.",
        owner="@test",
        version=7,
    )
    db_session.add_all([workflow, version, skill])
    db_session.commit()

    compile_version(db_session, version, resolver=fake_resolver(), persist=True)
    bundle = version.compiled_bundle["deployment_bundle"]

    assert verify_bundle(bundle).ready_to_deploy is True
    assert bundle["resolved_manifest"]["tools"]["lookup"]["version_constraint"] == "==1.0"
    assert bundle["skill_snapshots"]["finance-analysis"]["version"] == 7
    assert bundle["skill_snapshots"]["finance-analysis"]["content"].startswith("Compute")
    serialized = json.dumps(bundle)
    assert "finance-api" in serialized
    assert "secret_value" not in serialized

    imported_snapshot = deepcopy(bundle["skill_snapshots"])
    skill.version = 8
    skill.content = "New workspace content that must not replace the sealed snapshot."
    imported_version = CaliberWorkflowVersion(
        version_id="wfv-bundle-imported",
        workflow_id=workflow.workflow_id,
        version_number=2,
        status="draft",
        manifest=bundle["resolved_manifest"],
        manifest_hash="",
        compiled_bundle={"imported_skill_snapshots": imported_snapshot},
        created_by="@test",
    )
    db_session.add(imported_version)
    db_session.commit()

    compile_version(db_session, imported_version, resolver=fake_resolver(), persist=True)
    imported_bundle = imported_version.compiled_bundle["deployment_bundle"]

    assert imported_bundle["skill_snapshots"]["finance-analysis"]["version"] == 7
    assert imported_bundle["skill_snapshots"]["finance-analysis"]["content"].startswith("Compute")

    # Publishing recompiles a draft, so the imported snapshot must survive
    # repeated compilation instead of silently switching to workspace content.
    compile_version(db_session, imported_version, resolver=fake_resolver(), persist=True)
    recompiled_bundle = imported_version.compiled_bundle["deployment_bundle"]
    assert recompiled_bundle["skill_snapshots"]["finance-analysis"]["version"] == 7


def test_bundle_embeds_prompt_text_in_resolved_execution_manifest(
    db_session: Session,
    monkeypatch,
) -> None:
    manifest = make_manifest("wf-prompt-bundle")
    manifest["artifacts"] = {
        "prompts": {
            "financial_prompt": {
                "registry_name": "monthly-financial-analysis",
                "alias": "prod",
            }
        }
    }
    manifest["nodes"]["agent"]["instructions"] = {
        "type": "mlflow_prompt",
        "ref": "financial_prompt",
    }
    workflow = CaliberWorkflow(workflow_id="wf-prompt-bundle", name="Prompt bundle", owner="@test")
    version = CaliberWorkflowVersion(
        version_id="wfv-prompt-bundle",
        workflow_id=workflow.workflow_id,
        version_number=1,
        status="draft",
        manifest=manifest,
        manifest_hash="",
        created_by="@test",
    )
    db_session.add_all([workflow, version])
    db_session.commit()
    monkeypatch.setattr(
        deployment_bundle_module,
        "_load_prompt_snapshot",
        lambda _name, _alias: (
            {
                "name": "monthly-financial-analysis",
                "alias": "prod",
                "version": 12,
                "template": "Calculate mean, median, P25, P50, P75, and P90.",
            },
            "",
        ),
    )

    compile_version(db_session, version, resolver=fake_resolver(), persist=True)
    bundle = version.compiled_bundle["deployment_bundle"]

    assert bundle["manifest"]["nodes"]["agent"]["instructions"]["type"] == "mlflow_prompt"
    assert bundle["resolved_manifest"]["nodes"]["agent"]["instructions"] == {
        "type": "inline",
        "text": "Calculate mean, median, P25, P50, P75, and P90.",
    }
    assert bundle["resolved_manifest"]["artifacts"]["prompts"] == {}
    assert "monthly-financial-analysis@prod" not in bundle["compiled"]["generated_python"]


def test_bundle_preserves_an_explicit_historical_dataset_version(
    db_session: Session,
) -> None:
    manifest = make_manifest("wf-dataset-bundle")
    manifest["artifacts"] = {
        "eval_datasets": {
            "quality": {"dataset_name": "monthly-quality", "version": 2},
        }
    }
    workflow = CaliberWorkflow(
        workflow_id="wf-dataset-bundle",
        name="Dataset bundle",
        owner="@test",
    )
    version = CaliberWorkflowVersion(
        version_id="wfv-dataset-bundle",
        workflow_id=workflow.workflow_id,
        version_number=1,
        status="draft",
        manifest=manifest,
        manifest_hash="",
        created_by="@test",
    )
    dataset = CaliberEvalDataset(
        dataset_id="ED-bundle-history",
        name="monthly-quality",
        owner="@test",
        version=3,
    )
    db_session.add_all(
        [
            workflow,
            version,
            dataset,
            CaliberEvalDatasetExample(
                example_id="EX-v1",
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input={"month": "2026-01"},
                expected={"revenue": 100},
            ),
            CaliberEvalDatasetExample(
                example_id="EX-v3",
                dataset_id=dataset.dataset_id,
                dataset_version=3,
                input={"month": "2026-03"},
                expected={"revenue": 130},
            ),
        ]
    )
    db_session.commit()

    compile_version(db_session, version, resolver=fake_resolver(), persist=True)
    bundle = version.compiled_bundle["deployment_bundle"]
    dependency = next(item for item in bundle["dependencies"] if item["kind"] == "eval_dataset")

    assert dependency["version"] == "2"
    assert dependency["snapshot"]["version"] == 2
    assert dependency["snapshot"]["example_count"] == 1
    assert bundle["resolved_manifest"]["artifacts"]["eval_datasets"]["quality"]["version"] == 2


def test_alias_rotation_rejects_a_tampered_stored_bundle(
    db_session: Session,
) -> None:
    workflow = CaliberWorkflow(workflow_id="wf-unready-bundle", name="Unready", owner="@test")
    version = CaliberWorkflowVersion(
        version_id="wfv-unready-bundle",
        workflow_id=workflow.workflow_id,
        version_number=1,
        status="published",
        manifest=make_manifest(workflow.workflow_id),
        manifest_hash="",
        created_by="@test",
    )
    db_session.add_all([workflow, version])
    db_session.commit()
    compile_version(db_session, version, resolver=fake_resolver(), persist=True)
    bundle = deepcopy(version.compiled_bundle["deployment_bundle"])
    bundle["manifest"]["description"] = "tampered after compilation"
    version.compiled_bundle = {**version.compiled_bundle, "deployment_bundle": bundle}
    db_session.commit()

    with pytest.raises(AliasPreflightError, match="integrity check failed"):
        require_alias_target_ready(db_session, "dev", version.version_id)
