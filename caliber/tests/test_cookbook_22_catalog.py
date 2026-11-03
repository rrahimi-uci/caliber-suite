"""Runtime catalog contract for the governed evidence-review Cookbook."""

from __future__ import annotations

from caliber.workflows.cookbook_catalog import (
    build_cookbook_catalog,
    materialize_cookbook_manifest,
)
from caliber.workflows.manifest import parse_manifest


def test_lab22_is_a_versioned_catalog_recipe_with_guided_steps() -> None:
    recipe = next(item for item in build_cookbook_catalog()["recipes"] if item["id"] == "22")

    assert recipe["slug"] == "governed-evidence-review"
    assert recipe["activation_requires_review"] is True
    assert recipe["capabilities"] == [
        "governed effects",
        "runtime approvals",
        "evidence archives",
        "offline replay",
        "project isolation",
    ]
    assert [step["id"] for step in recipe["steps"]] == [f"22.{n}" for n in range(1, 9)]
    assert recipe["steps"][2]["route"] == "/workflow-runs/:runId"
    assert recipe["steps"][-1]["route"] == "/workflow-runs/:runId"


def test_lab22_manifest_binds_the_protected_document_request_tool() -> None:
    manifest = materialize_cookbook_manifest(
        "22",
        workflow_id="WF-cookbook-22",
        workflow_name="Governed Evidence Review",
    )
    parsed = parse_manifest(manifest)

    agent = parsed.nodes["agent"]
    assert agent.tools == ["request_missing_document"]
    binding = parsed.tools["request_missing_document"]
    assert binding.type == "registered_function"
    assert binding.registry_ref == "tool.request_missing_document.v1"
    assert binding.version_constraint == ">=1.0,<2.0"
    assert binding.requires_approval is True


def test_lab22_materialization_replaces_markers_without_dropping_tool_binding() -> None:
    manifest = materialize_cookbook_manifest(
        "22",
        workflow_id="WF-cookbook-22-materialized",
        workflow_name="Materialized governed review",
    )

    assert manifest["workflow_id"] == "WF-cookbook-22-materialized"
    assert manifest["name"] == "Materialized governed review"
    assert manifest["tools"]["request_missing_document"] == {
        "registry_ref": "tool.request_missing_document.v1",
        "version_constraint": ">=1.0,<2.0",
        "requires_approval": True,
    }
