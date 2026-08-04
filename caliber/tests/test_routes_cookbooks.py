"""Catalog and atomic installation tests for built-in Cookbook examples."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.cookbooks as cookbook_routes
from caliber.db.models import CaliberAuditLog, CaliberWorkflow, CaliberWorkflowVersion
from caliber.workflows.cookbook_catalog import (
    build_cookbook_catalog,
    materialize_cookbook_manifest,
)
from caliber.workflows.manifest import parse_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def test_catalog_contains_exactly_16_versioned_parseable_examples(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/cookbooks")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == 1
    assert data["catalog_version"]
    assert [recipe["id"] for recipe in data["recipes"]] == [
        f"{number:02d}" for number in range(1, 17)
    ]

    for recipe in build_cookbook_catalog()["recipes"]:
        assert recipe["activation_requires_review"] is True
        manifest = materialize_cookbook_manifest(
            recipe["id"],
            workflow_id=f"WF-cookbook-{recipe['id']}",
            workflow_name=recipe["title"],
        )
        parsed = parse_manifest(manifest)
        assert parsed.workflow_id == f"WF-cookbook-{recipe['id']}"
        assert parsed.nodes["cookbook_guide"].type == "note"


def test_capability_cookbooks_use_real_data_transform_nodes() -> None:
    expected = {
        "03": {"decision_table"},
        "04": {"json_schema"},
        "06": {"confidence"},
        "08": {"fixture"},
    }
    for cookbook_id, operations in expected.items():
        manifest = materialize_cookbook_manifest(
            cookbook_id,
            workflow_id=f"WF-transform-{cookbook_id}",
            workflow_name=f"Transform {cookbook_id}",
        )
        actual = {
            node.operation
            for node in parse_manifest(manifest).nodes.values()
            if node.type == "data_transform"
        }
        assert actual == operations


def test_operational_cookbooks_include_safe_fixture_and_live_connector_starters() -> None:
    expected = {
        "07": {"incident_connector"},
        "08": {"deployment_connector", "service_health_connector"},
    }
    for cookbook_id, connector_ids in expected.items():
        manifest = materialize_cookbook_manifest(
            cookbook_id,
            workflow_id=f"WF-connectors-{cookbook_id}",
            workflow_name=f"Connectors {cookbook_id}",
        )
        parsed = parse_manifest(manifest)
        assert {
            node_id for node_id, node in parsed.nodes.items() if node.type == "api_request"
        } == (connector_ids)
        for connector_id in connector_ids:
            connector = parsed.nodes[connector_id]
            assert connector.method == "GET"
            assert "Authorization" not in connector.headers
            if "example.invalid" in connector.url:
                assert connector.url.startswith("https://")


def test_catalog_reports_concrete_runtime_approval_readiness(client: TestClient) -> None:
    recipes = {
        item["id"]: item for item in client.get(f"{PREFIX}/cookbooks").json()["data"]["recipes"]
    }
    approval_check = recipes["03"]["readiness"]["checks"][0]
    assert approval_check == {
        "label": "Workflow queue, runtime approvals, and checkpoints",
        "status": "configuration_required",
        "settings_path": "/settings/runtime",
    }

    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
        }
    )
    recipes = {
        item["id"]: item for item in client.get(f"{PREFIX}/cookbooks").json()["data"]["recipes"]
    }
    assert recipes["03"]["readiness"]["checks"][0]["status"] == "ready"


def test_install_requires_prerequisite_acknowledgement(client: TestClient) -> None:
    response = client.post(f"{PREFIX}/cookbooks/03/install", json={})
    assert response.status_code == 400
    assert "acknowledge" in response.json()["detail"]


def test_install_is_a_paused_workflow_with_one_draft_and_audit(
    client: TestClient,
    db_session: Session,
) -> None:
    response = client.post(
        f"{PREFIX}/cookbooks/03/install",
        json={
            "name": "Refund policy example",
            "acknowledge_prerequisites": True,
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    workflow = data["workflow"]
    version = data["version"]
    assert workflow["name"] == "Refund policy example"
    assert workflow["status"] == "paused"
    assert version["workflow_id"] == workflow["workflow_id"]
    assert version["version_number"] == 1
    assert version["status"] == "draft"
    assert data["activation_requires_review"] is True

    stored_versions = (
        db_session.execute(
            select(CaliberWorkflowVersion).where(
                CaliberWorkflowVersion.workflow_id == workflow["workflow_id"]
            )
        )
        .scalars()
        .all()
    )
    assert len(stored_versions) == 1
    audit = db_session.execute(
        select(CaliberAuditLog).where(
            CaliberAuditLog.action == "install_cookbook_draft",
            CaliberAuditLog.entity_id == workflow["workflow_id"],
        )
    ).scalar_one()
    assert audit.details["cookbook_id"] == "03"
    assert audit.details["activation_requires_review"] is True


def test_install_without_prerequisites_needs_no_acknowledgement(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/cookbooks/02/install",
        json={"name": "Precision skill example"},
    )
    assert response.status_code == 201, response.text


def test_duplicate_install_name_is_rejected_without_extra_rows(
    client: TestClient,
    db_session: Session,
) -> None:
    body = {"name": "Duplicate example"}
    assert client.post(f"{PREFIX}/cookbooks/02/install", json=body).status_code == 201
    response = client.post(f"{PREFIX}/cookbooks/02/install", json=body)
    assert response.status_code == 409
    rows = (
        db_session.execute(
            select(CaliberWorkflow).where(CaliberWorkflow.name == "Duplicate example")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_install_rolls_back_if_version_insert_fails(
    client: TestClient,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_manifest(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ValueError("invalid generated example")

    monkeypatch.setattr(cookbook_routes, "materialize_cookbook_manifest", invalid_manifest)
    with pytest.raises(ValueError, match="invalid generated example"):
        client.post(
            f"{PREFIX}/cookbooks/02/install",
            json={"name": "Must not persist"},
        )
    assert (
        db_session.execute(
            select(CaliberWorkflow).where(CaliberWorkflow.name == "Must not persist")
        ).scalar_one_or_none()
        is None
    )


def test_unknown_cookbook_is_404(client: TestClient) -> None:
    assert client.post(f"{PREFIX}/cookbooks/99/install", json={}).status_code == 404


def test_viewer_can_read_catalog_but_cannot_install(client: TestClient) -> None:
    headers = {"X-CALIBER-User": "viewer-only"}
    assert client.get(f"{PREFIX}/cookbooks", headers=headers).status_code == 200
    assert (
        client.post(
            f"{PREFIX}/cookbooks/02/install",
            json={"name": "Denied"},
            headers=headers,
        ).status_code
        == 403
    )
