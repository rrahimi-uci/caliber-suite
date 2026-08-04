"""Contract tests for ``GET /caliber/capabilities``."""

from __future__ import annotations

from starlette.testclient import TestClient

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def test_capabilities_default_contract(client) -> None:
    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200
    data = response.json()["data"]
    runs = data["workflow_runs"]

    assert runs["queue_enabled"] is False
    assert runs["supports_async_submit"] is False
    assert runs["supports_cancel"] is False
    assert runs["supports_retry"] is False
    assert runs["supports_resume"] is False
    assert runs["runtime_approvals_enabled"] is False
    assert runs["checkpointing_enabled"] is False
    assert runs["event_backend"] == "in_process"
    assert data["sync_workflow_version_run"] is True
    assert set(data["artifact_families"]) == {
        "prompt",
        "workflow",
        "knowledge_base",
        "skill",
        "tool",
        "test_set",
        "mcp_server",
        "judge",
        "agent",
    }
    assert data["artifact_families"]["prompt"]["rollbackable"] is True
    assert data["artifact_families"]["tool"]["rollbackable"] is False


def test_each_artifact_family_declares_the_complete_contract(client) -> None:
    families = client.get(f"{PREFIX}/capabilities").json()["data"]["artifact_families"]
    expected_fields = {
        "history",
        "live_target",
        "promotable",
        "rollbackable",
        "evidence_bearing",
        "gate_mode",
        "calibration",
    }
    for family, contract in families.items():
        assert set(contract) == expected_fields, family


def test_capabilities_reflect_flag_overrides(client) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": True,
            "workflow_run_event_backend": "database",
        }
    )
    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200
    runs = response.json()["data"]["workflow_runs"]

    assert runs["queue_enabled"] is True
    assert runs["supports_async_submit"] is True
    assert runs["supports_cancel"] is True
    assert runs["supports_retry"] is True
    assert runs["supports_resume"] is True
    assert runs["runtime_approvals_enabled"] is True
    assert runs["checkpointing_enabled"] is True
    assert runs["event_backend"] == "database"


def test_capabilities_keep_resume_disabled_without_checkpointing(client) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_run_queue_enabled": True,
            "workflow_run_runtime_approvals_enabled": True,
            "workflow_run_checkpointing_enabled": False,
            "workflow_run_event_backend": "database",
        }
    )
    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200
    runs = response.json()["data"]["workflow_runs"]

    assert runs["queue_enabled"] is True
    assert runs["supports_async_submit"] is True
    assert runs["supports_cancel"] is True
    assert runs["supports_retry"] is True
    assert runs["supports_resume"] is False
    assert runs["runtime_approvals_enabled"] is True
    assert runs["checkpointing_enabled"] is False
    assert runs["event_backend"] == "database"


# ---------------------------------------------------------------------------
# Honest disclosure of the executable envelope
# ---------------------------------------------------------------------------


def test_capabilities_endpoint_reports_what_aria_can_actually_execute(
    client: TestClient,
) -> None:
    """The drafting UI is broad; the executable registry is seven built-ins.

    Nothing surfaced that number, so the envelope a user infers from the panel
    was wider than the one the product implements.
    """
    from caliber.assistant.capabilities import registered_capabilities
    from caliber.routes.aria_plans import CAPABILITIES_PATH

    resp = client.get(CAPABILITIES_PATH)

    assert resp.status_code == 200
    body = resp.json()["data"] if "data" in resp.json() else resp.json()
    assert body["count"] == len(registered_capabilities())
    keys = {item["key"] for item in body["capabilities"]}
    assert keys == {capability.key for capability in registered_capabilities()}


def test_each_capability_carries_the_scopes_needed_to_invoke_it(
    client: TestClient,
) -> None:
    """Disclosure without the authorization detail would invite the same wrong inference."""
    from caliber.routes.aria_plans import CAPABILITIES_PATH

    body = client.get(CAPABILITIES_PATH).json()
    body = body["data"] if "data" in body else body

    for item in body["capabilities"]:
        assert "required_scopes" in item
        assert "tier" in item
        assert "input_schema" in item


def test_capabilities_requires_authentication(client: TestClient) -> None:
    from caliber.routes.aria_plans import CAPABILITIES_PATH

    resp = client.get(CAPABILITIES_PATH, headers={"X-CALIBER-User": ""})
    assert resp.status_code in {401, 403}
