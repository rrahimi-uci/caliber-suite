"""Integration tests for ``/caliber/workflows`` (plan §19.9)."""

from __future__ import annotations

from typing import get_args

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.workflows as workflows_routes
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRuntimeApprovalRequest,
    CaliberWorkflowBenchmarkReport,
    CaliberWorkflowRun,
    CaliberWorkflowRunCheckpoint,
    CaliberWorkflowRunEvent,
    CaliberWorkflowSessionMemory,
)
from caliber.workflows.manifest import WorkflowNode
from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    create_workflow,
    deploy_prod,
    make_support_manifest,
    seed_eval_dataset,
)


def _port(type_name: str) -> dict[str, object]:
    return {"type": type_name, "description": "", "schema": None}


def _workflow_node_types() -> set[str]:
    workflow_union = get_args(WorkflowNode)[0]
    node_models = get_args(workflow_union)
    return {get_args(model_cls.model_fields["type"].annotation)[0] for model_cls in node_models}


def _normalized_port_map(ports: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    return {name: _port(str(spec["type"])) for name, spec in ports.items()}


def test_list_empty(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows")
    assert r.status_code == 200
    assert r.json()["data"] == []


def test_list_workflow_components_catalog(client: TestClient) -> None:  # noqa: PLR0915
    response = client.get(f"{PREFIX}/workflow-components")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == 1
    components = {item["type"]: item for item in data["components"]}
    assert set(components) == _workflow_node_types()
    assert "agent" in components
    assert "wait_for_event" in components
    assert "knowledge_query" in components
    assert "loop" in components
    assert "tool" in components
    assert "template" in components

    agent = components["agent"]
    assert agent["label"] == "Agent"
    assert agent["category"] == "Agents"
    assert agent["default_inputs"] == {
        "input": _port("string"),
        "history": _port("structured"),
    }
    assert agent["default_outputs"] == {
        "final_output": _port("string"),
        "history": _port("structured"),
    }
    agent_fields = {field["key"]: field for field in agent["fields"]}
    assert agent_fields["model"]["required"] is True
    assert agent_fields["instructions"]["type"] != "object"
    assert "Instructions" in agent_fields["instructions"]["type"]
    assert agent_fields["tools"]["type"].startswith("list<")
    assert agent_fields["execution_policy"]["type"] == "Execution Policy | null"
    assert agent_fields["execution_policy"]["description"] == (
        "Optional timeout, retry, and idempotency controls applied by the workflow runtime for this node."
    )
    assert agent_fields["execution_policy"]["constraints"]["nullable"] is True

    start = components["start"]
    assert start["default_outputs"] == {"user_message": _port("string")}
    start_fields = {field["key"]: field for field in start["fields"]}
    assert "execution_policy" not in start_fields

    output = components["output"]
    assert output["default_inputs"] == {"response": _port("string")}

    wait_for_event = components["wait_for_event"]
    assert wait_for_event["default_outputs"]["event_payload"]["type"] == "structured"
    assert any("external system or operator" in doc for doc in wait_for_event["docs"])
    wait_fields = {field["key"]: field for field in wait_for_event["fields"]}
    assert wait_fields["timeout_seconds"]["constraints"]["nullable"] is True

    knowledge_query = components["knowledge_query"]
    knowledge_fields = {field["key"]: field for field in knowledge_query["fields"]}
    assert knowledge_fields["top_k"]["constraints"]["minimum"] == 1
    assert knowledge_fields["top_k"]["constraints"]["maximum"] == 20
    assert knowledge_query["default_inputs"] == {
        "question": _port("string"),
        "history": _port("structured"),
        "retrieval_modes": _port("structured"),
        "version_ids": _port("structured"),
        "graph_overrides": _port("structured"),
    }
    assert knowledge_query["setup_checks"] == [
        {
            "label": "Select a knowledge base or pinned versions",
            "help": "Choose the target knowledge base or pin explicit KB versions for this query.",
            "kind": "any_non_empty",
            "field": None,
            "fields": ["knowledge_base_id", "version_ids"],
            "minimum": None,
        },
    ]
    knowledge_build = components["knowledge_build"]
    knowledge_build_fields = {field["key"]: field for field in knowledge_build["fields"]}
    assert knowledge_build["default_inputs"] == {
        "input": _port("string"),
        "sources": _port("structured"),
        "chunking_strategy": _port("string"),
        "embedding_model": _port("string"),
        "chunking_config": _port("structured"),
        "graph_config": _port("structured"),
    }
    assert knowledge_build["default_outputs"] == {
        "text": _port("string"),
        "result": _port("structured"),
        "knowledge_base": _port("structured"),
        "version": _port("structured"),
        "run": _port("structured"),
        "status": _port("string"),
        "version_id": _port("string"),
        "run_id": _port("string"),
    }
    assert knowledge_build_fields["wait_timeout_seconds"]["constraints"]["exclusive_minimum"] == 0
    assert knowledge_build["setup_checks"] == [
        {
            "label": "Select a knowledge base",
            "help": "Choose the existing knowledge base this node should refresh.",
            "kind": "non_empty_string",
            "field": "knowledge_base_id",
            "fields": [],
            "minimum": None,
        },
        {
            "label": "Choose a chunking strategy",
            "help": "Set the chunker directly or map one into the chunking_strategy input.",
            "kind": "non_empty_string",
            "field": "chunking_strategy",
            "fields": [],
            "minimum": None,
        },
        {
            "label": "Choose an embedding model",
            "help": "Set the embedding model directly or map one into the embedding_model input.",
            "kind": "non_empty_string",
            "field": "embedding_model",
            "fields": [],
            "minimum": None,
        },
    ]
    assert components["loop"]["default_inputs"] == {
        "input": _port("string"),
        "state": _port("structured"),
    }
    assert components["loop"]["default_outputs"] == {
        "output": _port("string"),
        "result": _port("structured"),
        "iterations": _port("structured"),
        "metadata": _port("structured"),
    }
    assert components["parallel"]["setup_checks"] == [
        {
            "label": "Add at least two downstream branches",
            "help": "Connect this parallel node to at least two downstream branches before using it as a fan-out barrier.",
            "kind": "minimum_outgoing_edges",
            "field": None,
            "fields": [],
            "minimum": 2,
        },
    ]
    assert components["join"]["setup_checks"] == [
        {
            "label": "Connect at least two upstream branches",
            "help": "Feed this join from at least two upstream branches, or remove the join barrier.",
            "kind": "minimum_incoming_edges",
            "field": None,
            "fields": [],
            "minimum": 2,
        },
        {
            "label": "Use distinct join input ports per branch",
            "help": "Map each incoming branch into a distinct join input port so the merge stays traceable.",
            "kind": "distinct_incoming_target_ports",
            "field": None,
            "fields": [],
            "minimum": None,
        },
    ]
    assert components["loop"]["setup_checks"] == [
        {
            "label": "Select a loop target",
            "help": "Choose the executable node this loop should repeat.",
            "kind": "non_empty_string",
            "field": "target_node_id",
            "fields": [],
            "minimum": None,
        },
        {
            "label": "Choose an executable loop target",
            "help": "The selected loop target should point to an executable node in this workflow.",
            "kind": "target_node_executable_if_set",
            "field": "target_node_id",
            "fields": [],
            "minimum": None,
        },
    ]
    assert components["for_each"]["setup_checks"] == [
        {
            "label": "Use an executable target when set",
            "help": "If you choose a target node for this loop, it must point to an executable step.",
            "kind": "target_node_executable_if_set",
            "field": "target_node_id",
            "fields": [],
            "minimum": None,
        },
    ]
    assert components["error_boundary"]["setup_checks"] == [
        {
            "label": "Protect an executable target when set",
            "help": "If this boundary wraps a target node, that target should be an executable step.",
            "kind": "target_node_executable_if_set",
            "field": "target_node_id",
            "fields": [],
            "minimum": None,
        },
        {
            "label": "Use an executable compensation node when set",
            "help": "If you configure a compensation node, it should point to an executable recovery step.",
            "kind": "target_node_executable_if_set",
            "field": "compensate_with",
            "fields": [],
            "minimum": None,
        },
    ]

    tool = components["tool"]
    tool_fields = {field["key"]: field for field in tool["fields"]}
    assert tool["category"] == "Integrations"
    assert tool_fields["tool_name"]["description"] == (
        "Tool invoked by this node. Tool nodes use the local manifest binding name; MCP resource nodes use the remote MCP tool name."
    )
    assert any("deterministically without asking an LLM" in doc for doc in tool["docs"])
    assert tool["setup_checks"] == [
        {
            "label": "Select a tool binding",
            "help": "Choose the manifest tool binding this node should invoke directly.",
            "kind": "non_empty_string",
            "field": "tool_name",
            "fields": [],
            "minimum": None,
        },
    ]

    template = components["template"]
    template_fields = {field["key"]: field for field in template["fields"]}
    assert template_fields["template"]["description"] == (
        "Text or JSON template rendered from the current workflow inputs."
    )
    assert template_fields["output_format"]["default"] == "text"
    assert template_fields["missing_variable_mode"]["default"] == "preserve"
    assert any("JSON mode validates the rendered payload" in doc for doc in template["docs"])
    assert template["setup_checks"] == [
        {
            "label": "Provide a template",
            "help": "Write the text or JSON template this node should render.",
            "kind": "non_empty_string",
            "field": "template",
            "fields": [],
            "minimum": None,
        },
    ]

    guardrail = components["guardrail"]
    assert guardrail["default_inputs"] == {"response": _port("string")}
    assert guardrail["default_outputs"] == {"passthrough": _port("string")}

    router = components["router"]
    assert router["default_inputs"] == {"decision": _port("string")}
    assert any("outgoing edges" in doc for doc in router["docs"])
    assert router["setup_checks"] == [
        {
            "label": "Add at least one branch",
            "help": "Define the branch destinations and routing conditions.",
            "kind": "non_empty_list",
            "field": "branches",
            "fields": [],
            "minimum": None,
        },
        {
            "label": "Connect every branch target with an outgoing edge",
            "help": "Each configured branch should point to a real node and also have a matching outgoing edge from this router.",
            "kind": "router_branch_edges_connected",
            "field": None,
            "fields": [],
            "minimum": None,
        },
    ]

    approval = components["human_approval"]
    assert approval["default_inputs"] == {"request": _port("string")}
    assert approval["default_outputs"] == {"request": _port("string")}

    external = components["external_app"]
    assert external["default_inputs"] == {
        "input": _port("string"),
        "context": _port("structured"),
    }
    assert external["default_outputs"] == {
        "text": _port("string"),
        "result": _port("structured"),
        "metadata": _port("structured"),
    }

    agent_checks = components["agent"]["setup_checks"]
    assert agent_checks == [
        {
            "label": "Provide instructions or a prompt reference",
            "help": "Set inline instructions or bind the agent to a registered prompt.",
            "kind": "instructions_present",
            "field": None,
            "fields": [],
            "minimum": None,
        }
    ]

    assert agent["starter_node"]["id"] == "__CALIBER_NODE_ID__"
    assert agent["starter_node"]["name"] == "__CALIBER_NODE_ID__"
    assert agent["starter_node"]["instructions"] == {
        "type": "inline",
        "text": "You are a helpful assistant.",
    }
    assert components["wait_until"]["starter_node"]["wait_until"] == "__CALIBER_NOW_PLUS_60S_ISO__"
    assert components["wait_for_event"]["starter_node"]["outputs"] == {
        "output": {"type": "string"},
        "event_payload": {"type": "structured"},
        "event_name": {"type": "string"},
    }
    assert components["loop"]["starter_node"]["max_iterations"] == 10
    assert components["loop"]["starter_node"]["stop_condition"] == ""
    assert knowledge_query["starter_node"]["graph_overrides"] is None
    assert knowledge_build["starter_node"]["inputs"]["input"] == {"type": "string"}
    assert approval["starter_node"]["required_role"] == "caliber.approver"
    assert output["starter_node"]["inputs"] == {"response": {"type": "string"}}

    subworkflow = components["subworkflow"]
    assert any("published child workflow" in doc for doc in subworkflow["docs"])
    assert subworkflow["setup_checks"] == [
        {
            "label": "Select the workflow to invoke",
            "help": "Choose the child workflow this node should run.",
            "kind": "non_empty_string",
            "field": "workflow_id",
            "fields": [],
            "minimum": None,
        },
        {
            "label": "Avoid calling this workflow recursively",
            "help": "Choose a different published child workflow instead of pointing this node back at the current workflow.",
            "kind": "not_current_workflow_id",
            "field": "workflow_id",
            "fields": [],
            "minimum": None,
        },
    ]

    join = components["join"]
    assert join["default_outputs"] == {
        "output": _port("string"),
        "merged": _port("structured"),
    }
    assert any("edge-driven" in doc for doc in join["docs"])


def test_workflow_components_catalog_entries_are_self_describing(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/workflow-components")
    assert response.status_code == 200, response.text
    components = response.json()["data"]["components"]

    for component in components:
        assert component["label"].strip()
        assert component["category"].strip()
        assert component["description"].strip()
        assert component["docs"]
        assert component["starter_node"]["type"] == component["type"]
        assert component["starter_node"]["id"] == "__CALIBER_NODE_ID__"

        starter_inputs = component["starter_node"].get("inputs", {})
        starter_outputs = component["starter_node"].get("outputs", {})
        assert component["default_inputs"] == _normalized_port_map(starter_inputs)
        assert component["default_outputs"] == _normalized_port_map(starter_outputs)

        field_keys = {field["key"] for field in component["fields"]}
        for field in component["fields"]:
            assert field["label"].strip()
            assert field["type"].strip()
            assert field["description"]
            assert isinstance(field["examples"], list)

        for rule in component["setup_checks"]:
            if rule["field"]:
                assert rule["field"] in field_keys
            for field_key in rule["fields"]:
                assert field_key in field_keys


def test_list_workflow_templates_catalog(client: TestClient) -> None:  # noqa: PLR0915
    response = client.get(f"{PREFIX}/workflow-templates")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == 1

    templates = {item["kind"]: item for item in data["templates"]}
    assert set(templates) == {
        "single_agent",
        "multi_agent_handoff",
        "guarded_pipeline",
        "parallel_fanout",
        "hitl_review",
        "for_each_loop",
        "refinement_loop",
        "event_resume",
        "graph_hybrid_rag",
        "knowledge_rag",
        "knowledge_age",
        "knowledge_age_build",
        "blank",
    }
    scenario_map = {item["id"]: item for item in data["bakeoff_scenarios"]}
    assert set(scenario_map) == {"B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"}
    assert scenario_map["B9"]["starter_kind"] == "knowledge_age"
    assert "Graph evidence" in scenario_map["B9"]["evidence_to_capture"]
    assert [section["title"] for section in data["operator_rubric"]] == [
        "Authoring friction",
        "First-pass execution",
        "Recovery and degraded-path handling",
        "Observability and evidence",
        "Reusability and deployment",
    ]

    single_agent = templates["single_agent"]
    assert single_agent["label"] == "Single Agent"
    assert single_agent["manifest_template"]["workflow_id"] == "__CALIBER_WORKFLOW_ID__"
    assert single_agent["manifest_template"]["name"] == "__CALIBER_WORKFLOW_NAME__"
    assert single_agent["manifest_template"]["nodes"]["agent"]["instructions"] == {
        "type": "inline",
        "text": "You are a helpful assistant.",
    }

    multi_agent = templates["multi_agent_handoff"]["manifest_template"]
    assert multi_agent["nodes"]["agent"]["handoffs"] == [
        {
            "target": "billing",
            "description": "Handle billing, invoices, and refunds.",
            "condition": "'billing' in input or 'invoice' in input or 'refund' in input",
            "input_filter": (
                "Billing handoff\nCustomer request: {{input}}\nCoordinator draft: {{final_output}}"
            ),
        }
    ]
    assert multi_agent["nodes"]["billing"]["type"] == "agent"

    guarded = templates["guarded_pipeline"]["manifest_template"]
    assert guarded["nodes"]["guardrail"]["on_failure"] == "block"
    assert guarded["edges"][1]["to"] == "guardrail"

    parallel = templates["parallel_fanout"]["manifest_template"]
    assert set(parallel["nodes"]) == {
        "start",
        "parallel",
        "research",
        "writer",
        "join_all",
        "final",
    }
    assert parallel["nodes"]["join_all"]["type"] == "join"
    assert parallel["edges"][0]["to"] == "parallel"
    assert parallel["edges"][-1]["from"] == "join_all"

    review = templates["hitl_review"]["manifest_template"]
    assert "pii_guard" in review["nodes"]
    assert "review" in review["nodes"]
    assert review["edges"][-1]["from"] == "review"

    loop = templates["for_each_loop"]["manifest_template"]
    assert set(loop["nodes"]) == {"start", "for_each", "worker", "final"}
    assert loop["nodes"]["for_each"]["target_node_id"] == "worker"
    assert loop["edges"] == [
        {
            "id": "e_start_loop",
            "from": "start",
            "to": "for_each",
            "map": {"user_message": "items"},
        },
        {
            "id": "e_loop_final",
            "from": "for_each",
            "to": "final",
            "map": {"text": "response"},
        },
    ]

    refinement = templates["refinement_loop"]["manifest_template"]
    assert set(refinement["nodes"]) == {"start", "loop", "editor", "final"}
    assert refinement["nodes"]["loop"]["type"] == "loop"
    assert refinement["nodes"]["loop"]["target_node_id"] == "editor"
    assert refinement["nodes"]["loop"]["max_iterations"] == 3
    assert refinement["nodes"]["loop"]["stop_condition"] == "iteration >= 2"
    assert refinement["edges"] == [
        {
            "id": "e_start_loop",
            "from": "start",
            "to": "loop",
            "map": {"user_message": "input"},
        },
        {
            "id": "e_loop_final",
            "from": "loop",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    knowledge_rag = templates["knowledge_rag"]["manifest_template"]
    assert set(knowledge_rag["nodes"]) == {"start", "knowledge", "final"}
    assert knowledge_rag["nodes"]["knowledge"]["type"] == "knowledge_query"
    assert knowledge_rag["nodes"]["knowledge"]["retrieval_modes"] == []
    assert knowledge_rag["nodes"]["knowledge"]["inputs"]["retrieval_modes"] == {
        "type": "structured"
    }
    assert knowledge_rag["edges"][0]["map"] == {"user_message": "question"}

    graph_hybrid_rag = templates["graph_hybrid_rag"]["manifest_template"]
    assert graph_hybrid_rag["nodes"]["knowledge"]["type"] == "knowledge_query"
    assert graph_hybrid_rag["nodes"]["knowledge"]["retrieval_modes"] == ["graph_hybrid"]
    assert graph_hybrid_rag["edges"][-1]["map"] == {"answer": "response"}

    knowledge_age = templates["knowledge_age"]["manifest_template"]
    assert knowledge_age["nodes"]["knowledge"]["type"] == "knowledge_query"
    assert knowledge_age["nodes"]["knowledge"]["retrieval_modes"] == ["age_graph"]
    assert knowledge_age["nodes"]["knowledge"]["inputs"]["retrieval_modes"] == {
        "type": "structured"
    }
    assert knowledge_age["edges"][-1]["map"] == {"answer": "response"}

    knowledge_age_build = templates["knowledge_age_build"]["manifest_template"]
    assert set(knowledge_age_build["nodes"]) == {"start", "build_graph", "final"}
    assert knowledge_age_build["nodes"]["build_graph"]["type"] == "knowledge_build"
    assert knowledge_age_build["nodes"]["build_graph"]["chunking_strategy"] == "recursive"
    assert knowledge_age_build["nodes"]["build_graph"]["embedding_model"] == (
        "sentence-transformers/all-MiniLM-L6-v2"
    )
    assert knowledge_age_build["nodes"]["build_graph"]["graph_config"] == {
        "extractor_backend": "heuristic",
        "spacy_model": None,
        "max_entities_per_chunk": 12,
        "entity_types": [],
        "minimum_entity_mentions": 1,
        "minimum_relationship_weight": 1.0,
        "default_retrieval_mode": "age_graph",
        "retrieval_strength": "balanced",
        "output_target": "object_store_and_age",
        "age_seed_mode": "entity_then_text",
        "age_traversal_hops": 1,
        "age_candidate_pool_size": 24,
        "age_dense_rerank_weight": 0.35,
        "strict_age_retrieval_default": False,
    }
    assert knowledge_age_build["nodes"]["build_graph"]["wait_for_completion"] is True
    assert knowledge_age_build["nodes"]["build_graph"]["activate_when_complete"] is True
    assert knowledge_age_build["edges"][-1]["map"] == {"text": "response"}

    event_resume = templates["event_resume"]["manifest_template"]
    assert set(event_resume["nodes"]) == {"start", "wait_gate", "agent", "final"}
    assert event_resume["nodes"]["wait_gate"]["type"] == "wait_for_event"
    assert event_resume["nodes"]["wait_gate"]["event_name"] == "documents.ready"
    assert event_resume["nodes"]["wait_gate"]["correlation_key"] == "document_id"
    assert event_resume["nodes"]["wait_gate"]["timeout_seconds"] == 3600
    assert event_resume["edges"] == [
        {
            "id": "e_start_wait",
            "from": "start",
            "to": "wait_gate",
            "map": {"user_message": "input"},
        },
        {
            "id": "e_wait_agent",
            "from": "wait_gate",
            "to": "agent",
            "map": {"output": "input"},
        },
        {
            "id": "e_agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]

    blank = templates["blank"]["manifest_template"]
    assert set(blank["nodes"]) == {"start", "final"}
    assert blank["edges"] == []


def test_list_workflow_benchmark_reports_empty(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/workflow-benchmark-reports")
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_workflow_benchmark_report_crud(
    client: TestClient,
    db_session: Session,
) -> None:
    worksheet = {
        "product_name": "n8n",
        "evaluator": "Ops reviewer",
        "environment": "staging",
        "summary": "Graph retrieval behaved well.",
        "updated_at": "2026-06-15T12:00:00Z",
        "scenarios": {
            "B9": {
                "status": "passed",
                "minutes_to_first_success": "6",
                "evidence_links": "WR-AGE-1",
                "notes": "Retrieved graph neighbors and citations.",
            },
            "B4": {
                "status": "blocked",
                "minutes_to_first_success": "",
                "evidence_links": "",
                "notes": "Needed manual correlation setup.",
            },
        },
        "rubric": {
            "Authoring friction": {
                "score": "4",
                "notes": "Starter was quick to configure.",
            }
        },
    }
    create_response = client.post(
        f"{PREFIX}/workflow-benchmark-reports",
        json={
            "name": "Q2 AGE bakeoff",
            "status": "draft",
            "worksheet": worksheet,
        },
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()["data"]
    report_id = created["report_id"]
    assert created["name"] == "Q2 AGE bakeoff"
    assert created["status"] == "draft"
    assert created["product_name"] == "n8n"
    assert created["scenario_count"] == 2
    assert created["captured_count"] == 2
    assert created["passed_count"] == 1
    assert created["blocked_count"] == 1

    row = db_session.get(CaliberWorkflowBenchmarkReport, report_id)
    assert row is not None
    assert row.name == "Q2 AGE bakeoff"
    assert row.worksheet["product_name"] == "n8n"

    list_response = client.get(f"{PREFIX}/workflow-benchmark-reports")
    assert list_response.status_code == 200
    listed = list_response.json()["data"]
    assert len(listed) == 1
    assert listed[0]["report_id"] == report_id

    updated_worksheet = {
        **worksheet,
        "summary": "AGE traversal passed after retest.",
        "scenarios": {
            **worksheet["scenarios"],
            "B1": {
                "status": "in_progress",
                "minutes_to_first_success": "",
                "evidence_links": "WR-SINGLE-1",
                "notes": "Still collecting evidence.",
            },
        },
    }
    update_response = client.patch(
        f"{PREFIX}/workflow-benchmark-reports/{report_id}",
        json={
            "name": "Q2 AGE bakeoff final",
            "status": "completed",
            "worksheet": updated_worksheet,
        },
    )
    assert update_response.status_code == 200, update_response.text
    updated = update_response.json()["data"]
    assert updated["name"] == "Q2 AGE bakeoff final"
    assert updated["status"] == "completed"
    assert updated["summary"] == "AGE traversal passed after retest."
    assert updated["scenario_count"] == 3
    assert updated["captured_count"] == 3
    assert updated["passed_count"] == 1
    assert updated["blocked_count"] == 1

    delete_response = client.delete(
        f"{PREFIX}/workflow-benchmark-reports/{report_id}",
    )
    assert delete_response.status_code == 200
    assert delete_response.json()["status"] == "deleted"
    db_session.expire_all()
    assert db_session.get(CaliberWorkflowBenchmarkReport, report_id) is None


def test_list_workflow_benchmark_reports_rejects_bad_status(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/workflow-benchmark-reports?status=broken")
    assert response.status_code == 400
    assert "expected one of" in response.json()["detail"]


def test_create_workflow(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflows", json={"name": "WF One", "owner": "@test"})
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["workflow_id"].startswith("WF-")
    assert data["status"] == "active"


def test_create_workflow_publishes_created_event(client: TestClient, monkeypatch) -> None:
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    response = client.post(f"{PREFIX}/workflows", json={"name": "WF Evented", "owner": "@ignored"})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert published == [
        {
            "type": "workflow.created",
            "workflow_id": data["workflow_id"],
            "status": "active",
            "name": "WF Evented",
            "owner": "@test",
        }
    ]


def test_create_workflow_succeeds_when_event_bus_publish_raises(
    client: TestClient, monkeypatch
) -> None:
    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"event bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflows_routes.logger, "warning", _warning)

    response = client.post(
        f"{PREFIX}/workflows", json={"name": "WF Event Failure", "owner": "@ignored"}
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["workflow_id"].startswith("WF-")
    assert data["name"] == "WF Event Failure"
    assert client.get(f"{PREFIX}/workflows/{data['workflow_id']}").status_code == 200
    assert captured["message"] == "failed to publish workflow event type='workflow.created'"
    assert captured["kwargs"] == {"exc_info": True}


def test_create_duplicate_name_409(client: TestClient) -> None:
    client.post(f"{PREFIX}/workflows", json={"name": "Dup"})
    r = client.post(f"{PREFIX}/workflows", json={"name": "Dup"})
    assert r.status_code == 409


def test_get_workflow(client: TestClient) -> None:
    wid = create_workflow(client, "Gettable")
    r = client.get(f"{PREFIX}/workflows/{wid}")
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "Gettable"


def test_get_nonexistent_404(client: TestClient) -> None:
    assert client.get(f"{PREFIX}/workflows/missing").status_code == 404


def test_update_workflow(client: TestClient) -> None:
    wid = create_workflow(client, "Renamable")
    r = client.patch(f"{PREFIX}/workflows/{wid}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "Renamed"


def test_update_workflow_publishes_updated_event_for_metadata_change(
    client: TestClient, monkeypatch
) -> None:
    wid = create_workflow(client, "Renamable Event")
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    response = client.patch(
        f"{PREFIX}/workflows/{wid}",
        json={"name": "Renamed Event", "description": "new description"},
    )
    assert response.status_code == 200, response.text
    assert published == [
        {
            "type": "workflow.updated",
            "workflow_id": wid,
            "status": "active",
            "changed_fields": ["description", "name"],
        }
    ]


def test_update_workflow_metadata_succeeds_when_event_bus_publish_raises(
    client: TestClient, monkeypatch
) -> None:
    wid = create_workflow(client, "Renamable Event Failure")

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"event bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflows_routes.logger, "warning", _warning)

    response = client.patch(
        f"{PREFIX}/workflows/{wid}",
        json={"name": "Renamed Event Failure", "description": "still committed"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["workflow_id"] == wid
    assert data["name"] == "Renamed Event Failure"
    refreshed = client.get(f"{PREFIX}/workflows/{wid}")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["data"]["description"] == "still committed"
    assert captured["message"] == "failed to publish workflow event type='workflow.updated'"
    assert captured["kwargs"] == {"exc_info": True}


def test_pause_workflow(client: TestClient) -> None:
    wid = create_workflow(client, "Pausable")
    r = client.patch(f"{PREFIX}/workflows/{wid}", json={"status": "paused"})
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "paused"


def test_pause_workflow_succeeds_when_event_bus_publish_raises(
    client: TestClient, monkeypatch
) -> None:
    wid = create_workflow(client, "Pausable Event Failure")

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"event bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflows_routes.logger, "warning", _warning)

    response = client.patch(f"{PREFIX}/workflows/{wid}", json={"status": "paused"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "paused"
    refreshed = client.get(f"{PREFIX}/workflows/{wid}")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["data"]["status"] == "paused"
    assert captured["message"] == "failed to publish workflow event type='workflow.paused'"
    assert captured["kwargs"] == {"exc_info": True}


def test_archive_with_prod_deployment_409(client: TestClient, db_session: Session) -> None:
    seed_eval_dataset(db_session)
    manifest = make_support_manifest(
        "arch_wf",
        deploy_gates={
            "support_eval": {
                "type": "deploy_gate",
                "dataset_ref": "support_eval",
                "required_for_aliases": ["prod"],
                "thresholds": {"min_pass_rate": 1.0},
            }
        },
    )
    wid, vid = create_and_publish(client, workflow_name="Archivable", manifest=manifest)
    deploy_prod(client, wid, vid)
    r = client.patch(f"{PREFIX}/workflows/{wid}", json={"status": "archived"})
    assert r.status_code == 409


def test_create_workflow_viewer_forbidden(client: TestClient) -> None:
    r = client.post(
        f"{PREFIX}/workflows", json={"name": "Nope"}, headers={"X-CALIBER-User": "@viewer"}
    )
    assert r.status_code == 403


def test_list_workflows_by_status(client: TestClient) -> None:
    create_workflow(client, "Active WF")
    wid2 = create_workflow(client, "Paused WF")
    client.patch(f"{PREFIX}/workflows/{wid2}", json={"status": "paused"})
    r = client.get(f"{PREFIX}/workflows?status=active")
    assert r.status_code == 200
    names = {w["name"] for w in r.json()["data"]}
    assert "Active WF" in names
    assert "Paused WF" not in names


def test_list_workflows_invalid_status_400(client: TestClient) -> None:
    r = client.get(f"{PREFIX}/workflows?status=invalid")
    assert r.status_code == 400


def test_update_workflow_nonexistent_404(client: TestClient) -> None:
    r = client.patch(f"{PREFIX}/workflows/WF-nonexistent", json={"name": "X"})
    assert r.status_code == 404


def test_update_workflow_empty_body_400(client: TestClient) -> None:
    wid = create_workflow(client, "EmptyBody")
    r = client.patch(f"{PREFIX}/workflows/{wid}", json={})
    assert r.status_code == 400


def test_update_workflow_no_actual_change(client: TestClient) -> None:
    """When the submitted values match the current values, return 200 without audit."""
    wid = create_workflow(client, "NoChange")
    r = client.patch(f"{PREFIX}/workflows/{wid}", json={"name": "NoChange"})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "NoChange"


def test_update_workflow_viewer_forbidden(client: TestClient) -> None:
    wid = create_workflow(client, "RbacUpdate")
    r = client.patch(
        f"{PREFIX}/workflows/{wid}",
        json={"name": "New"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert r.status_code == 403


def test_list_and_clear_workflow_session_memory(
    client: TestClient,
    db_session: Session,
) -> None:
    wid = create_workflow(client, "Session Memory Workflow")
    db_session.add_all(
        [
            CaliberWorkflowSessionMemory(
                workflow_id=wid,
                node_id="support_agent",
                session_id="thread-1",
                message_history=[
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi there"},
                ],
                turn_count=1,
            ),
            CaliberWorkflowSessionMemory(
                workflow_id=wid,
                node_id="triage_agent",
                session_id="thread-1",
                message_history=[
                    {"role": "user", "content": "triage this"},
                    {"role": "assistant", "content": "triaged"},
                ],
                turn_count=1,
            ),
            CaliberWorkflowSessionMemory(
                workflow_id=wid,
                node_id="support_agent",
                session_id="thread-2",
                message_history=[
                    {"role": "user", "content": "other session"},
                    {"role": "assistant", "content": "other answer"},
                ],
                turn_count=1,
            ),
        ]
    )
    db_session.commit()

    response = client.get(f"{PREFIX}/workflows/{wid}/session-memory?session_id=thread-1")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert len(data) == 2
    assert {item["node_id"] for item in data} == {"support_agent", "triage_agent"}
    assert data[0]["message_count"] == 2
    assert data[0]["last_assistant_message"] in {"hi there", "triaged"}

    clear = client.delete(
        f"{PREFIX}/workflows/{wid}/session-memory?session_id=thread-1&node_id=support_agent"
    )
    assert clear.status_code == 200, clear.text
    result = clear.json()["data"]
    assert result["deleted_entries"] == 1
    assert result["deleted_messages"] == 2
    assert result["node_id"] == "support_agent"

    db_session.expire_all()
    remaining = (
        db_session.query(CaliberWorkflowSessionMemory)
        .filter(CaliberWorkflowSessionMemory.workflow_id == wid)
        .order_by(CaliberWorkflowSessionMemory.session_id, CaliberWorkflowSessionMemory.node_id)
        .all()
    )
    assert [(row.session_id, row.node_id) for row in remaining] == [
        ("thread-1", "triage_agent"),
        ("thread-2", "support_agent"),
    ]


def test_workflow_session_memory_requires_session_id(client: TestClient) -> None:
    wid = create_workflow(client, "Session Memory Validation")
    response = client.get(f"{PREFIX}/workflows/{wid}/session-memory")
    assert response.status_code == 400
    assert "session_id" in response.json()["detail"]


def test_delete_workflow_cascades_run_children(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    wid = create_workflow(client, "DeleteCascade")
    run_id = "WR-delete-route-1"
    db_session.add(CaliberWorkflowRun(workflow_run_id=run_id, workflow_id=wid, status="completed"))
    db_session.add(
        CaliberWorkflowRunEvent(
            workflow_run_id=run_id,
            sequence=1,
            event_type="workflow.run.completed",
        )
    )
    db_session.add(
        CaliberWorkflowRunCheckpoint(
            checkpoint_id="WRC-delete-route-1",
            workflow_run_id=run_id,
            sequence=1,
            node_id="node-1",
        )
    )
    db_session.add(
        CaliberRuntimeApprovalRequest(
            runtime_approval_id="WRA-delete-route-1",
            workflow_run_id=run_id,
            node_id="human_gate",
        )
    )
    db_session.add(
        CaliberWorkflowSessionMemory(
            workflow_id=wid,
            node_id="support_agent",
            session_id="thread-delete",
            message_history=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "bye"},
            ],
            turn_count=1,
        )
    )
    db_session.commit()

    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        client.app.state.event_bus,
        "publish",
        lambda payload: published.append(dict(payload)),
    )

    response = client.delete(f"{PREFIX}/workflows/{wid}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"
    assert published == [
        {
            "type": "workflow.deleted",
            "workflow_id": wid,
            "status": "active",
            "name": "DeleteCascade",
        }
    ]

    db_session.expire_all()
    assert db_session.get(CaliberWorkflowRun, run_id) is None
    assert (
        db_session.query(CaliberWorkflowRunEvent)
        .filter(CaliberWorkflowRunEvent.workflow_run_id == run_id)
        .count()
        == 0
    )
    assert (
        db_session.query(CaliberWorkflowRunCheckpoint)
        .filter(CaliberWorkflowRunCheckpoint.workflow_run_id == run_id)
        .count()
        == 0
    )
    assert (
        db_session.query(CaliberRuntimeApprovalRequest)
        .filter(CaliberRuntimeApprovalRequest.workflow_run_id == run_id)
        .count()
        == 0
    )
    assert (
        db_session.query(CaliberWorkflowSessionMemory)
        .filter(CaliberWorkflowSessionMemory.workflow_id == wid)
        .count()
        == 0
    )


def _synced_fleet_agents(
    db_session: Session, workflow_id: str
) -> list[CaliberAgentConfig]:
    return [
        agent
        for agent in db_session.query(CaliberAgentConfig).all()
        if isinstance(agent.optimizer_config, dict)
        and agent.optimizer_config.get("source_workflow_id") == workflow_id
    ]


def test_delete_workflow_removes_synced_fleet_agents(
    client: TestClient, db_session: Session
) -> None:
    """Deploying a workflow auto-syncs Agent Fleet entries; deleting the workflow
    must remove those synced agents while leaving manually-registered ones."""
    wid, vid = create_and_publish(client)
    # Deploy to an ungated alias: sync_fleet_from_version creates the fleet rows
    # and (unlike a prod deploy) does not trip the live-deployment delete guard.
    r = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote",
        json={"version_id": vid},
    )
    assert r.status_code == 200, r.text

    # A manually-registered agent has no source_workflow_id and must survive.
    db_session.add(
        CaliberAgentConfig(
            agent_id="manual-agent",
            experiment_id="exp-manual-agent",
            name="Manual Agent",
            owner="@qa",
        )
    )
    db_session.commit()

    db_session.expire_all()
    assert _synced_fleet_agents(db_session, wid), "deploy should have synced a fleet agent"

    response = client.delete(f"{PREFIX}/workflows/{wid}")
    assert response.status_code == 200, response.text

    db_session.expire_all()
    assert _synced_fleet_agents(db_session, wid) == []
    # The manually-registered agent is untouched.
    assert db_session.get(CaliberAgentConfig, "manual-agent") is not None


def test_delete_workflow_succeeds_when_event_bus_publish_raises(
    client: TestClient, monkeypatch
) -> None:
    wid = create_workflow(client, "Delete Event Failure")

    class _FailingEventBus:
        def publish(self, payload: dict[str, object]) -> None:
            raise RuntimeError(f"event bus offline: {payload.get('type')}")

    client.app.state.event_bus = _FailingEventBus()
    captured: dict[str, object] = {}

    def _warning(message: str, *args: object, **kwargs: object) -> None:
        captured["message"] = message % args if args else message
        captured["kwargs"] = dict(kwargs)

    monkeypatch.setattr(workflows_routes.logger, "warning", _warning)

    response = client.delete(f"{PREFIX}/workflows/{wid}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"
    assert client.get(f"{PREFIX}/workflows/{wid}").status_code == 404
    assert captured["message"] == "failed to publish workflow event type='workflow.deleted'"
    assert captured["kwargs"] == {"exc_info": True}
