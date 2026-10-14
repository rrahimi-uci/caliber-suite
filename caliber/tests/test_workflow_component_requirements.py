"""Every workflow node says what it needs, and the palette can tell whether this server has it
(alpha gap P1-22).

All thirty-one node types are listed without a prerequisite or maturity field. An ``agent``
node on the default fake provider only echoes, ``input_bucket`` needs an object store,
``mcp_resource`` needs a reachable MCP server, ``external_app`` is refused until an
entrypoint allowlist names it, and a wait or an approval cannot pause without the run queue.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.workflows import component_catalog as catalog
from caliber.workflows.component_catalog import (
    _COMPONENT_ORDER,
    _COMPONENT_REQUIRES,
    REQUIREMENT_LABELS,
    build_workflow_component_catalog,
    component_requirements,
    evaluate_requirements,
)

COMPONENTS = "/ajax-api/2.0/mlflow/caliber/workflow-components"


def _config(**environ: str) -> CaliberConfig:
    return CaliberConfig.load(environ={"CALIBER_DATABASE_URL": "sqlite+pysqlite://", **environ})


def test_every_node_type_declares_what_it_requires() -> None:
    types = {node_type for node_type, _model in _COMPONENT_ORDER}
    assert set(_COMPONENT_REQUIRES) == types, (
        "a node type was added or removed without deciding its prerequisites: "
        f"{sorted(set(_COMPONENT_REQUIRES) ^ types)}"
    )


def test_every_requirement_id_has_a_label_and_a_resolver() -> None:
    used = {item for needs in _COMPONENT_REQUIRES.values() for item in needs}
    assert used <= set(REQUIREMENT_LABELS), used - set(REQUIREMENT_LABELS)
    assert used <= set(evaluate_requirements(_config())), used - set(
        evaluate_requirements(_config())
    )
    assert all(label.strip() for label in REQUIREMENT_LABELS.values())


def test_the_catalog_carries_the_declared_requirements() -> None:
    by_type = {item["type"]: item for item in build_workflow_component_catalog()["components"]}
    assert by_type["agent"]["requires"] == ["real_llm"]
    assert by_type["input_bucket"]["requires"] == ["object_store"]
    assert by_type["mcp_resource"]["requires"] == ["mcp_server"]
    assert by_type["external_app"]["requires"] == ["entrypoint_allowlist"]
    assert by_type["human_approval"]["requires"] == ["run_queue"]
    assert by_type["wait_for_event"]["requires"] == ["run_queue"]
    assert by_type["start"]["requires"] == []
    assert by_type["python_code"]["requires"] == []


def test_a_default_install_meets_none_of_the_optional_prerequisites() -> None:
    status = evaluate_requirements(_config())
    assert status["real_llm"] is False  # the fake provider
    assert status["object_store"] is False  # local workflow storage
    assert status["entrypoint_allowlist"] is False  # fail-closed until set
    assert status["mcp_server"] is None  # registered data, not configuration


def test_the_run_queue_needs_checkpointing_as_well() -> None:
    both = evaluate_requirements(
        _config(
            CALIBER_WORKFLOW_RUN_QUEUE_ENABLED="true",
            CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED="true",
        )
    )
    assert both["run_queue"] is True
    queue_only = evaluate_requirements(
        _config(
            CALIBER_WORKFLOW_RUN_QUEUE_ENABLED="true",
            CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED="false",
        )
    )
    assert queue_only["run_queue"] is False


def test_configured_prerequisites_are_reported_as_met() -> None:
    status = evaluate_requirements(
        _config(
            CALIBER_LLM_PROVIDER="openai",
            CALIBER_EXTERNAL_APP_ENTRYPOINT_ALLOWLIST="mycompany.apps.*",
            CALIBER_WORKFLOW_STORAGE_BACKEND="s3",
            CALIBER_WORKFLOW_STORAGE_BUCKET="b",
        )
    )
    assert status["real_llm"] is True
    assert status["entrypoint_allowlist"] is True
    assert status["object_store"] is True


def test_component_requirements_pairs_each_id_with_its_label_and_status() -> None:
    rows = component_requirements("agent", _config())
    assert rows == [{"id": "real_llm", "label": REQUIREMENT_LABELS["real_llm"], "met": False}]
    assert component_requirements("start", _config()) == []


def test_the_route_reports_each_requirement_against_the_running_config(client: TestClient) -> None:
    response = client.get(COMPONENTS)
    assert response.status_code == 200, response.text
    components = {c["type"]: c for c in response.json()["data"]["components"]}

    agent = components["agent"]
    assert agent["requires"] == ["real_llm"]
    assert agent["requirements"][0]["id"] == "real_llm"
    assert agent["requirements"][0]["met"] is False  # the suite runs on the fake provider
    assert "echoes" in agent["requirements"][0]["label"]
    assert components["start"]["requirements"] == []
    assert components["mcp_resource"]["requirements"][0]["met"] is None

    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True, "workflow_run_checkpointing_enabled": True}
    )
    queue = client.get(COMPONENTS).json()["data"]["components"]
    wait = next(c for c in queue if c["type"] == "wait_for_event")
    assert wait["requirements"][0]["met"] is True


def test_the_cached_catalog_is_not_mutated_by_a_request(client: TestClient) -> None:
    client.get(COMPONENTS)
    assert all(
        "requirements" not in item for item in build_workflow_component_catalog()["components"]
    )


@pytest.mark.parametrize("node_type", sorted(_COMPONENT_REQUIRES))
def test_requirements_resolve_for_every_node_type(node_type: str) -> None:
    for row in component_requirements(node_type, _config()):
        assert row["id"] in REQUIREMENT_LABELS
        assert row["met"] in (True, False, None)


def test_the_module_exposes_no_requirement_without_a_resolver() -> None:
    assert set(REQUIREMENT_LABELS) == set(evaluate_requirements(_config())), (
        "a requirement id has a label but no resolver, or the reverse"
    )
    assert catalog.REQUIREMENT_LABELS is REQUIREMENT_LABELS
