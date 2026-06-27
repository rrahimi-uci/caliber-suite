"""Contract tests for ``GET /caliber/capabilities``."""

from __future__ import annotations

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
