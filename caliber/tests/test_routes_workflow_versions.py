"""Integration tests for workflow version routes (plan §19.9)."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.testclient import TestClient

import caliber.routes.workflow_versions as workflow_versions_routes
from caliber.db.models import CaliberWorkflowRun, CaliberWorkflowVersion
from caliber.workflows.manifest import compute_manifest_hash
from caliber.workflows.runtime import NodeStep, WorkflowRunResult
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_manifest,
    make_support_manifest,
    register_demo_tools,
)


def _support_workflow(client: TestClient) -> tuple[str, str, str]:
    register_demo_tools(client)
    wid = create_workflow(client, "Support")
    vid, h = create_draft(client, wid, make_support_manifest(wid))
    return wid, vid, h


def _assert_validate_and_publish_rejects(
    client: TestClient,
    *,
    workflow_id: str,
    manifest: dict[str, object],
    error_code: str,
    error_path: str,
) -> None:
    vid, _ = create_draft(client, workflow_id, manifest)

    validate = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert validate.status_code == 200, validate.text
    body = validate.json()["data"]
    assert body["valid"] is False
    assert any(
        error["code"] == error_code and error["path"] == error_path for error in body["errors"]
    )

    publish = client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    assert publish.status_code == 400, publish.text
    assert "version does not compile" in publish.json()["detail"]


def test_create_draft(client: TestClient) -> None:
    wid = create_workflow(client)
    r = client.post(f"{PREFIX}/workflows/{wid}/versions", json={"manifest": make_manifest(wid)})
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["status"] == "draft"
    assert data["version_number"] == 1
    assert data["manifest_hash"]


def test_create_draft_invalid_manifest_400(client: TestClient) -> None:
    wid = create_workflow(client)
    bad = make_manifest(wid)
    del bad["nodes"]["agent"]["model"]
    r = client.post(f"{PREFIX}/workflows/{wid}/versions", json={"manifest": bad})
    assert r.status_code == 400


def test_create_draft_rejects_inline_secret(client: TestClient) -> None:
    wid = create_workflow(client)
    bad = make_manifest(wid)
    bad["nodes"]["agent"]["output_type"] = {"password": "hunter2"}
    r = client.post(f"{PREFIX}/workflows/{wid}/versions", json={"manifest": bad})
    assert r.status_code == 400
    assert "secret" in r.json()["detail"].lower()


def test_list_versions(client: TestClient) -> None:
    wid = create_workflow(client)
    create_draft(client, wid, make_manifest(wid))
    create_draft(client, wid, make_manifest(wid))
    r = client.get(f"{PREFIX}/workflows/{wid}/versions")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 2


def test_update_draft_with_matching_hash(client: TestClient) -> None:
    wid = create_workflow(client)
    vid, h = create_draft(client, wid, make_manifest(wid))
    updated = make_manifest(wid, owner="@new")
    r = client.patch(
        f"{PREFIX}/workflow-versions/{vid}", json={"manifest": updated, "manifest_hash": h}
    )
    assert r.status_code == 200


def test_update_draft_stale_hash_409(client: TestClient) -> None:
    wid = create_workflow(client)
    vid, _ = create_draft(client, wid, make_manifest(wid))
    r = client.patch(
        f"{PREFIX}/workflow-versions/{vid}",
        json={"manifest": make_manifest(wid), "manifest_hash": "stale"},
    )
    assert r.status_code == 409


def test_validate_valid(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert r.status_code == 200
    assert r.json()["data"]["valid"] is True


def test_validate_invalid_reports_errors(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_support_manifest(wid)  # no tools registered -> missing_tool
    vid, _ = create_draft(client, wid, manifest)
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["valid"] is False
    assert any(e["code"] == "missing_tool" for e in body["errors"])


def test_validate_invalid_manifest_surfaces_field_paths(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": "",
    }
    manifest["nodes"]["mcp"] = {
        "id": "mcp",
        "type": "mcp_resource",
        "server_id": "",
        "tool_name": "",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
        },
    }
    vid, _ = create_draft(client, wid, make_manifest(wid))
    with client.app.state.session_factory() as session:
        version = session.get(CaliberWorkflowVersion, vid)
        assert version is not None
        version.manifest = manifest
        session.commit()

    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")

    assert r.status_code == 200
    body = r.json()["data"]
    assert body["valid"] is False
    paths = {error["path"] for error in body["errors"] if error["code"] == "parse_error"}
    assert "nodes.subflow.workflow_id" in paths
    assert "nodes.mcp.server_id" in paths
    assert "nodes.mcp.tool_name" in paths


def test_validate_subworkflow_reports_unknown_child_workflow(client: TestClient) -> None:
    parent_wid = create_workflow(client, "Parent")
    manifest = make_manifest(parent_wid)
    del manifest["nodes"]["agent"]
    manifest["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": "WF-missing",
        "alias": "prod",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "subflow", "map": {"msg": "input"}},
        {"id": "e2", "from": "subflow", "to": "final", "map": {"output": "response"}},
    ]
    vid, _ = create_draft(client, parent_wid, manifest)

    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["valid"] is False
    assert any(error["code"] == "subworkflow_unknown_workflow" for error in body["errors"])


def test_validate_subworkflow_reports_missing_active_alias(client: TestClient) -> None:
    child_wid = create_workflow(client, "Child")
    child_vid, _ = create_draft(client, child_wid, make_manifest(child_wid))
    publish = client.post(f"{PREFIX}/workflow-versions/{child_vid}/publish")
    assert publish.status_code == 200, publish.text
    promote_dev = client.post(
        f"{PREFIX}/workflows/{child_wid}/deployments/dev/promote",
        json={"version_id": child_vid},
    )
    assert promote_dev.status_code == 200, promote_dev.text

    parent_wid = create_workflow(client, "Parent")
    manifest = make_manifest(parent_wid)
    del manifest["nodes"]["agent"]
    manifest["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": child_wid,
        "alias": "staging",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "subflow", "map": {"msg": "input"}},
        {"id": "e2", "from": "subflow", "to": "final", "map": {"output": "response"}},
    ]
    vid, _ = create_draft(client, parent_wid, manifest)

    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["valid"] is False
    assert any(error["code"] == "subworkflow_missing_active_alias" for error in body["errors"])


def test_validate_subworkflow_warns_when_manual_alias_tracks_latest_draft(
    client: TestClient,
) -> None:
    child_wid = create_workflow(client, "Child")
    create_draft(client, child_wid, make_manifest(child_wid))

    parent_wid = create_workflow(client, "Parent")
    manifest = make_manifest(parent_wid)
    del manifest["nodes"]["agent"]
    manifest["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": child_wid,
        "alias": "manual",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "subflow", "map": {"msg": "input"}},
        {"id": "e2", "from": "subflow", "to": "final", "map": {"output": "response"}},
    ]
    vid, _ = create_draft(client, parent_wid, manifest)

    r = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")

    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["valid"] is True
    assert any(
        warning["code"] == "subworkflow_manual_uses_unpublished_version"
        for warning in body["warnings"]
    )


def test_compile_valid(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/compile")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["compiled_artifact_uri"] is not None
    assert data["report"]["export_mode"] == "agents_sdk_direct"
    assert "def run" in data["generated_python"]
    assert "openai-agents>=0.1.0" in data["requirements"]
    assert isinstance(data["compile_ms"], (int, float))
    assert isinstance(data["cached"], bool)


def test_compile_invalid_400(client: TestClient) -> None:
    wid = create_workflow(client)
    vid, _ = create_draft(client, wid, make_support_manifest(wid))  # tools not registered
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/compile")
    assert r.status_code == 400


def test_publish_draft(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "published"


def test_publish_rejects_missing_file_input_path(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "file_input": {
            "id": "file_input",
            "type": "file_input",
            "path": "",
            "inputs": {"path": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "path": {"type": "string"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}},
    ]

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_file_path",
        error_path="nodes.file_input.path",
    )


def test_publish_rejects_missing_folder_input_path(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "folder_input": {
            "id": "folder_input",
            "type": "folder_input",
            "path": "",
            "pattern": "*.txt",
            "recursive": False,
            "max_files": 5,
            "inputs": {"path": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "files": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}},
    ]

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_folder_path",
        error_path="nodes.folder_input.path",
    )


def test_publish_rejects_missing_input_bucket_name(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["bucket_in"] = {
        "id": "bucket_in",
        "type": "input_bucket",
        "bucket": "",
        "prefix": "",
        "recursive": True,
        "max_files": 10,
        "inputs": {"prefix": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_bucket", "from": "start", "to": "bucket_in", "map": {"msg": "prefix"}},
        {"id": "e_bucket_final", "from": "bucket_in", "to": "final", "map": {"text": "response"}},
    ]
    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_input_bucket",
        error_path="nodes.bucket_in.bucket",
    )


def test_publish_rejects_missing_output_bucket_name(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["bucket_out"] = {
        "id": "bucket_out",
        "type": "output_bucket",
        "bucket": "",
        "prefix": "run1/",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "keys": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
        {
            "id": "e_agent_bucket",
            "from": "agent",
            "to": "bucket_out",
            "map": {"final_output": "input"},
        },
    ]
    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_output_bucket",
        error_path="nodes.bucket_out.bucket",
    )


def test_publish_rejects_missing_output_folder_path(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["output_folder"] = {
        "id": "output_folder",
        "type": "output_folder",
        "path": "",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    manifest["edges"] = [
        {"id": "e_start_agent", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
        {
            "id": "e_agent_folder",
            "from": "agent",
            "to": "output_folder",
            "map": {"final_output": "input"},
        },
    ]

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_output_folder_path",
        error_path="nodes.output_folder.path",
    )


def test_publish_rejects_guardrail_without_checks(client: TestClient) -> None:
    register_demo_tools(client)
    wid = create_workflow(client)
    manifest = make_support_manifest(wid)
    guardrail = manifest["nodes"]["policy_guardrail"]
    assert isinstance(guardrail, dict)
    guardrail["checks"] = []

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_guardrail_checks",
        error_path="nodes.policy_guardrail.checks",
    )


def test_publish_rejects_knowledge_build_missing_required_setup(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "",
        "chunking_strategy": "",
        "embedding_model": "",
        "inputs": {
            "sources": {"type": "structured"},
            "chunking_strategy": {"type": "string"},
            "embedding_model": {"type": "string"},
        },
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    vid, _ = create_draft(client, wid, manifest)

    validate = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert validate.status_code == 200, validate.text
    body = validate.json()["data"]
    assert body["valid"] is False
    assert any(
        error["code"] == "missing_knowledge_build_target"
        and error["path"] == "nodes.knowledge_build.knowledge_base_id"
        for error in body["errors"]
    )
    assert any(
        error["code"] == "missing_knowledge_build_chunking_strategy"
        and error["path"] == "nodes.knowledge_build.chunking_strategy"
        for error in body["errors"]
    )
    assert any(
        error["code"] == "missing_knowledge_build_embedding_model"
        and error["path"] == "nodes.knowledge_build.embedding_model"
        for error in body["errors"]
    )

    publish = client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    assert publish.status_code == 400, publish.text
    assert "version does not compile" in publish.json()["detail"]


def test_publish_rejects_router_without_branches(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["router_empty"] = {
        "id": "router_empty",
        "type": "router",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"route": {"type": "string"}},
        "branches": [],
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_router_branches",
        error_path="nodes.router_empty.branches",
    )


def test_publish_rejects_note_with_incoming_edge(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["note"] = {
        "id": "note",
        "type": "note",
        "text": "Operator-only context",
    }
    manifest["edges"].append(
        {"id": "e_start_note", "from": "start", "to": "note", "map": {"msg": "ignored"}}
    )

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="map_bad_target_port",
        error_path="edges.e_start_note.map",
    )


def test_publish_rejects_note_with_outgoing_edge(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["note"] = {
        "id": "note",
        "type": "note",
        "text": "Operator-only context",
    }
    manifest["edges"].append(
        {"id": "e_note_final", "from": "note", "to": "final", "map": {"missing": "response"}}
    )

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="map_bad_source_port",
        error_path="edges.e_note_final.map",
    )


def test_publish_rejects_for_each_with_missing_target_node(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "ghost",
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="foreach_bad_target",
        error_path="nodes.for_each.target_node_id",
    )


def test_publish_rejects_loop_without_target_node(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "max_iterations": 3,
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="loop_missing_target",
        error_path="nodes.loop.target_node_id",
    )


def test_publish_rejects_error_boundary_with_missing_compensation_target(
    client: TestClient,
) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "agent",
        "compensate_with": "ghost",
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="error_boundary_bad_compensation",
        error_path="nodes.boundary.compensate_with",
    )


def test_publish_rejects_subworkflow_self_reference(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": wid,
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="subworkflow_self_reference",
        error_path="nodes.subflow.workflow_id",
    )


def test_publish_rejects_for_each_with_unsupported_target_type(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume.ticket",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    manifest["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "wait_event",
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="foreach_unsupported_target_type",
        error_path="nodes.for_each.target_node_id",
    )


def test_publish_rejects_loop_with_unsupported_target_type(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume.ticket",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    manifest["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "wait_event",
        "max_iterations": 3,
    }

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="loop_unsupported_target_type",
        error_path="nodes.loop.target_node_id",
    )


def test_publish_rejects_error_boundary_with_unsupported_target_types(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume.ticket",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    manifest["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "wait_event",
        "compensate_with": "final",
    }
    vid, _ = create_draft(client, wid, manifest)

    validate = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert validate.status_code == 200, validate.text
    body = validate.json()["data"]
    assert body["valid"] is False
    assert any(
        error["code"] == "error_boundary_unsupported_target_type"
        and error["path"] == "nodes.boundary.target_node_id"
        for error in body["errors"]
    )
    assert any(
        error["code"] == "error_boundary_unsupported_compensation_type"
        and error["path"] == "nodes.boundary.compensate_with"
        for error in body["errors"]
    )

    publish = client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    assert publish.status_code == 400, publish.text
    assert "version does not compile" in publish.json()["detail"]


def test_publish_rejects_missing_prompt_ref(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["agent"]["instructions"] = {"type": "mlflow_prompt", "ref": "nope"}

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_prompt_ref",
        error_path="nodes.agent.instructions.ref",
    )


def test_publish_rejects_missing_agent_eval_dataset_ref(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid, artifacts={"eval_datasets": {}})
    manifest["nodes"]["agent"]["eval_dataset"] = "ED-support"

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_eval_dataset",
        error_path="nodes.agent.eval_dataset",
    )


def test_publish_rejects_unbound_agent_tool_reference(client: TestClient) -> None:
    wid = create_workflow(client)
    manifest = make_manifest(wid)
    manifest["nodes"]["agent"]["tools"] = ["lookup_policy"]

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="unbound_tool",
        error_path="nodes.agent.tools",
    )


def test_publish_rejects_missing_registered_tool_binding(client: TestClient) -> None:
    register_demo_tools(client)
    wid = create_workflow(client)
    manifest = make_support_manifest(wid)
    manifest["tools"]["lookup_policy"]["registry_ref"] = "tool.missing.v1"

    _assert_validate_and_publish_rejects(
        client,
        workflow_id=wid,
        manifest=manifest,
        error_code="missing_tool",
        error_path="nodes.support_agent.tools",
    )


def test_publish_idempotent(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    r = client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "published"


def test_update_published_409(client: TestClient) -> None:
    wid, vid, h = _support_workflow(client)
    client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    r = client.patch(
        f"{PREFIX}/workflow-versions/{vid}",
        json={"manifest": make_support_manifest(wid), "manifest_hash": h},
    )
    assert r.status_code == 409


def test_preview_run(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/preview-run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["preview"] is True
    assert body["status"] == "completed"
    assert "output" in body


def test_preview_run_persists_saved_manifest_snapshot_and_survives_version_deletion(
    client: TestClient,
) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/preview-run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert run is not None
        assert version is not None
        assert run.manifest_snapshot == version.manifest
        assert run.summary is not None
        assert run.summary["preview"] is True
        assert run.summary["manifest_mode"] == "saved_version"
        assert run.summary["manifest_hash"] == version.manifest_hash
        assert run.summary["workflow_version_number"] == version.version_number
        stored_manifest = run.manifest_snapshot
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["workflow_run_id"] == run_id
    assert manifest_data["workflow_version_id"] == vid
    assert manifest_data["manifest_mode"] == "saved_version"
    assert manifest_data["manifest"] == stored_manifest
    assert manifest_data["manifest_hash"]


def _hitl_template_manifest(workflow_id: str) -> dict[str, object]:
    """Python mirror of the FE ``templateManifest("hitl_review", …)`` so we can
    assert the governance-showcase template parses + validates server-side."""
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "name": "HITL Review",
        "runtime": {
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
        },
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "agent": {
                "id": "agent",
                "type": "agent",
                "name": "main-agent",
                "model": "inherit",
                "instructions": {"type": "inline", "text": "You are a helpful assistant."},
                "tools": [],
                "inputs": {"input": {"type": "string"}},
                "outputs": {"final_output": {"type": "string"}},
            },
            "pii_guard": {
                "id": "pii_guard",
                "type": "guardrail",
                "mode": "post_agent",
                "inputs": {"response": {"type": "string"}},
                "outputs": {"clean": {"type": "string"}},
                "on_failure": "redact",
                "checks": [
                    {"pii_detection": {"entities": ["email", "ssn", "phone", "credit_card"]}}
                ],
            },
            "review": {
                "id": "review",
                "type": "human_approval",
                "inputs": {"response": {"type": "string"}},
                "outputs": {"approved": {"type": "string"}},
            },
            "final": {"id": "final", "type": "output", "inputs": {"response": {"type": "string"}}},
        },
        "edges": [
            {
                "id": "e_start_agent",
                "from": "start",
                "to": "agent",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_agent_guard",
                "from": "agent",
                "to": "pii_guard",
                "map": {"final_output": "response"},
            },
            {
                "id": "e_guard_review",
                "from": "pii_guard",
                "to": "review",
                "map": {"clean": "response"},
            },
            {
                "id": "e_review_final",
                "from": "review",
                "to": "final",
                "map": {"approved": "response"},
            },
        ],
        "tools": {},
    }


def test_hitl_template_manifest_creates_and_validates(client: TestClient) -> None:
    """P5 governance template: agent → PII-redact guardrail → human approval →
    output must create (parse) and validate cleanly, or the gallery card 400s."""
    wid = create_workflow(client)
    r = client.post(
        f"{PREFIX}/workflows/{wid}/versions", json={"manifest": _hitl_template_manifest(wid)}
    )
    assert r.status_code == 201, r.text  # parses structurally
    vid = r.json()["data"]["version_id"]
    v = client.post(f"{PREFIX}/workflow-versions/{vid}/validate")
    assert v.status_code == 200, v.text
    report = v.json()["data"]
    assert report["valid"] is True, report["errors"]


def test_preview_run_with_inline_manifest_override(client: TestClient) -> None:
    """Copilot iterate loop: preview an unsaved edit. The override manifest runs
    instead of the stored version, and the stored version stays untouched."""
    wid, vid, _ = _support_workflow(client)
    override = make_manifest(wid)  # start → agent → final (distinct node ids)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/preview-run",
        json={"input": "hello", "manifest": override},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    run_id = body["workflow_run_id"]
    assert body["status"] == "completed"
    step_nodes = {s["node_id"] for s in body["steps"]}
    assert "agent" in step_nodes  # from the override
    assert "support_agent" not in step_nodes  # the stored version was NOT executed
    # The stored manifest is unchanged by a preview.
    stored = client.get(f"{PREFIX}/workflow-versions/{vid}").json()["data"]["manifest"]
    assert "support_agent" in stored["nodes"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert run is not None
        assert version is not None
        assert run.manifest_snapshot == override
        assert run.summary is not None
        assert run.summary["preview"] is True
        assert run.summary["manifest_mode"] == "snapshot"
        assert run.summary["manifest_hash"] == compute_manifest_hash(override)
        assert "workflow_version_number" not in run.summary
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["workflow_run_id"] == run_id
    assert manifest_data["workflow_version_id"] == vid
    assert manifest_data["manifest_mode"] == "snapshot"
    assert manifest_data["manifest_hash"] == compute_manifest_hash(override)
    assert manifest_data["manifest"] == override


def test_run_version_records_persisted_run_with_steps(client: TestClient) -> None:
    wid, vid, _ = _support_workflow(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["preview"] is False
    assert body["workflow_run_id"].startswith("WR-")
    assert body["status"] == "completed"
    assert body["steps"][0]["node_id"] == "start"

    runs = client.get(f"{PREFIX}/workflows/{wid}/runs").json()["data"]
    row = next(run for run in runs if run["workflow_run_id"] == body["workflow_run_id"])
    assert row["status"] == "completed"
    assert row["summary"]["preview"] is False
    assert row["summary"]["steps"][0]["node_id"] == "start"


def test_run_version_succeeds_when_event_bus_publish_raises(
    client: TestClient, monkeypatch
) -> None:
    wid, vid, _ = _support_workflow(client)

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"event bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflow_versions_routes.logger, "warning", _warning)

    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["preview"] is False
    assert body["workflow_run_id"].startswith("WR-")
    assert body["status"] == "completed"

    runs = client.get(f"{PREFIX}/workflows/{wid}/runs").json()["data"]
    row = next(run for run in runs if run["workflow_run_id"] == body["workflow_run_id"])
    assert row["status"] == "completed"
    assert captured["message"] == "failed to publish workflow event type='workflow.run.completed'"
    assert captured["kwargs"] == {"exc_info": True}


def test_run_version_publishes_node_started_event_and_updates_current_node(
    client: TestClient,
    monkeypatch,
) -> None:
    _wid, vid, _ = _support_workflow(client)
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,  # type: ignore[attr-defined]
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    def _fake_execute(
        plan,
        input_text: str,
        *,
        executor,
        session_id=None,
        preview=False,
        on_step=None,
        on_node_start=None,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del plan, executor, session_id, preview, extra_tools
        if on_node_start is not None:
            on_node_start(
                "support_agent",
                SimpleNamespace(node_type="agent"),
                {"input": input_text},
            )
        step = NodeStep(
            "support_agent",
            "agent",
            "ok",
            output="Policy-backed answer",
            input_by_port={"input": input_text},
            output_by_port={"final_output": "Policy-backed answer"},
        )
        if on_step is not None:
            on_step(step)
        return WorkflowRunResult(
            status="completed",
            output="Policy-backed answer",
            steps=[step],
        )

    monkeypatch.setattr("caliber.routes.workflow_versions.execute", _fake_execute)

    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:  # type: ignore[attr-defined]
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        assert run.current_node_id == "support_agent"

    node_started = next(
        payload for payload in published if payload.get("type") == "workflow.run.node_started"
    )
    assert node_started["workflow_run_id"] == run_id
    assert node_started["node_id"] == "support_agent"
    assert node_started["node_type"] == "agent"


def test_run_version_persists_saved_manifest_snapshot_for_replay_and_survives_version_deletion(
    client: TestClient,
) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 200, r.text
    run_id = r.json()["data"]["workflow_run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        version = session.get(CaliberWorkflowVersion, vid)
        assert run is not None
        assert version is not None
        assert run.manifest_snapshot == version.manifest
        assert run.summary is not None
        assert run.summary["manifest_mode"] == "saved_version"
        assert run.summary["manifest_hash"] == version.manifest_hash
        assert run.summary["workflow_version_number"] == version.version_number
        stored_manifest = run.manifest_snapshot
        session.delete(version)
        session.commit()

    manifest_response = client.get(f"{PREFIX}/workflow-runs/{run_id}/manifest")
    assert manifest_response.status_code == 200, manifest_response.text
    manifest_data = manifest_response.json()["data"]
    assert manifest_data["workflow_run_id"] == run_id
    assert manifest_data["workflow_version_id"] == vid
    assert manifest_data["manifest_mode"] == "saved_version"
    assert manifest_data["manifest"] == stored_manifest
    assert manifest_data["manifest_hash"]


def test_run_version_rejects_paused_workflow(client: TestClient) -> None:
    wid, vid, _ = _support_workflow(client)
    r = client.patch(f"{PREFIX}/workflows/{wid}", json={"status": "paused"})
    assert r.status_code == 200

    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/run",
        json={"input": "What is your refund policy?"},
    )
    assert r.status_code == 409


def test_propose_patch_generates_guardrail(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/propose-patch",
        json={
            "evidence": {
                "category": "tool_use",
                "node_id": "support_agent",
                "required_tools": ["lookup_policy"],
                "observed_tool_calls": [],
            }
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["patch_kind"] == "workflow_manifest"
    assert data["candidate_valid"] is True
    assert any(o["op"] == "add_node_after" for o in data["semantic_ops"])
    assert data["diagnosis"]["affected_components"] == ["tool_contract"]
    assert data["patch_id"].startswith("WP-")


def test_propose_patch_prompt_only(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    r = client.post(
        f"{PREFIX}/workflow-versions/{vid}/propose-patch",
        json={"evidence": {"category": "hallucination", "node_id": "support_agent"}},
    )
    assert r.status_code == 200
    assert r.json()["data"]["patch_kind"] == "prompt"


def test_list_patches(client: TestClient) -> None:
    wid, vid, _ = _support_workflow(client)
    client.post(
        f"{PREFIX}/workflow-versions/{vid}/propose-patch",
        json={
            "evidence": {
                "category": "tool_use",
                "node_id": "support_agent",
                "required_tools": ["lookup_policy"],
                "observed_tool_calls": [],
            }
        },
    )
    r = client.get(f"{PREFIX}/workflows/{wid}/patches")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1


def test_list_versions_missing_workflow_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows/WF-nonexistent/versions")
    assert r.status_code == 404


def test_create_version_missing_workflow_404(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/workflows/WF-nonexistent/versions",
        json={"manifest": make_manifest("nonexistent")},
    )
    assert r.status_code == 404


def test_get_version_missing_404(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflow-versions/VER-nonexistent")
    assert r.status_code == 404


def test_update_version_missing_404(client: TestClient) -> None:
    r = client.patch(
        f"{PREFIX}/workflow-versions/VER-nonexistent",
        json={"manifest": make_manifest("x"), "manifest_hash": "abc"},
    )
    assert r.status_code == 404


def test_publish_missing_version_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflow-versions/VER-nonexistent/publish")
    assert r.status_code == 404


def test_validate_missing_version_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflow-versions/VER-nonexistent/validate")
    assert r.status_code == 404


def test_compile_missing_version_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflow-versions/VER-nonexistent/compile")
    assert r.status_code == 404


def test_preview_missing_version_404(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/workflow-versions/VER-nonexistent/preview-run",
        json={"input": "test"},
    )
    assert r.status_code == 404


def test_propose_patch_missing_version_404(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/workflow-versions/VER-nonexistent/propose-patch",
        json={"evidence": {"category": "hallucination", "node_id": "x"}},
    )
    assert r.status_code == 404


def test_viewer_cannot_create_version(client: TestClient) -> None:
    wid = create_workflow(client)
    r = client.post(
        f"{PREFIX}/workflows/{wid}/versions",
        json={"manifest": make_manifest(wid)},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_viewer_cannot_update_version(client: TestClient) -> None:
    wid = create_workflow(client)
    vid, h = create_draft(client, wid, make_manifest(wid))
    r = client.patch(
        f"{PREFIX}/workflow-versions/{vid}",
        json={"manifest": make_manifest(wid), "manifest_hash": h},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_export_manifest_and_python(client: TestClient) -> None:
    _wid, vid, _ = _support_workflow(client)
    client.post(f"{PREFIX}/workflow-versions/{vid}/publish")
    r = client.get(f"{PREFIX}/workflow-versions/{vid}/export/manifest")
    assert r.status_code == 200
    assert "workflow_id" in r.text
    r = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python")
    assert r.status_code == 200
    assert "def run(" in r.text


def test_export_python_uses_runtime_export_for_non_agent_workflow(
    client: TestClient,
) -> None:
    wid = create_workflow(client, "Non-agent export")
    manifest = make_manifest(wid)
    manifest["nodes"].pop("agent")
    manifest["edges"] = [
        {
            "id": "e_start_final",
            "from": "start",
            "to": "final",
            "map": {"msg": "response"},
        }
    ]
    vid, _ = create_draft(client, wid, manifest)

    r = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python")

    assert r.status_code == 200
    assert "run_exported_workflow(" in r.text
    assert "review-only for non-agent workflows" not in r.text
