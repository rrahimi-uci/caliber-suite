"""Shared factories for Workflow Studio tests (plan §19.18).

Not collected by pytest (no ``test_`` prefix). Mirrors the ``_make_*`` /
``_create_*`` convention used elsewhere in the suite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample
from caliber.ids import new_eval_dataset_id, new_eval_example_id
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry

PREFIX = "/ajax-api/2.0/mlflow/caliber"

# Tool family -> callable, used by the in-memory resolver in engine tests.
FAKE_CALLABLES = {
    "tool.lookup_policy.v1": lambda q="": {"policy": "30-day refund"},
    "tool.get_order.v1": lambda q="": {"order_id": "123", "status": "delivered"},
    "tool.escalate.v1": lambda q="": {"escalated": True},
}


def fake_resolver() -> InMemoryToolResolver:
    return InMemoryToolResolver.from_callables(FAKE_CALLABLES)


def registry_resolver() -> InMemoryToolResolver:
    """A resolver of metadata-only entries (no callables) for validation tests."""
    return InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name="lookup_policy", version="1.5", module_path="m", callable_name="f"
            ),
            ToolRegistryEntry(name="get_order", version="1.0", module_path="m", callable_name="g"),
            ToolRegistryEntry(
                name="escalate",
                version="1.0",
                module_path="m",
                callable_name="e",
                side_effect_level="external_action",
            ),
        ]
    )


def make_manifest(workflow_id: str = "test_wf", **overrides: Any) -> dict[str, Any]:
    """Minimal valid Start -> Agent -> Output manifest."""
    base: dict[str, Any] = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "name": "Test Workflow",
        "runtime": {
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
        },
        "nodes": {
            "start": {"id": "start", "type": "start", "outputs": {"msg": {"type": "string"}}},
            "agent": {
                "id": "agent",
                "type": "agent",
                "name": "test-agent",
                "model": "inherit",
                "instructions": {"type": "inline", "text": "You are helpful."},
                "tools": [],
                "inputs": {"input": {"type": "string"}},
                "outputs": {"final_output": {"type": "string"}},
            },
            "final": {"id": "final", "type": "output", "inputs": {"response": {"type": "string"}}},
        },
        "edges": [
            {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
            {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        ],
        "tools": {},
    }
    base.update(overrides)
    return base


def make_support_manifest(workflow_id: str = "support_wf", **overrides: Any) -> dict[str, Any]:
    """Support-style workflow: agent + tools + post-agent policy guardrail."""
    base: dict[str, Any] = {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "name": "Support Workflow",
        "artifacts": {
            "prompts": {
                "support_agent_prompt": {"registry_name": "support-agent", "alias": "prod"}
            },
            "eval_datasets": {"support_eval": {"dataset_name": "support-eval-v3"}},
        },
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "support_agent": {
                "id": "support_agent",
                "type": "agent",
                "name": "support-agent",
                "model": "inherit",
                "instructions": {"type": "mlflow_prompt", "ref": "support_agent_prompt"},
                "tools": ["lookup_policy", "get_order", "escalate"],
                "inputs": {"input": {"type": "string"}},
                "outputs": {
                    "final_output": {"type": "string"},
                    "tool_calls": {"type": "structured"},
                },
            },
            "policy_guardrail": {
                "id": "policy_guardrail",
                "type": "guardrail",
                "mode": "post_agent",
                "inputs": {"response": {"type": "string"}},
                "outputs": {"passthrough": {"type": "string"}},
                "checks": [
                    {
                        "tool_required_before_claim": {
                            "tool": "lookup_policy",
                            "categories": ["refund_policy", "warranty_policy"],
                        }
                    }
                ],
            },
            "final": {"id": "final", "type": "output", "inputs": {"response": {"type": "string"}}},
        },
        "edges": [
            {
                "id": "e_start_support",
                "from": "start",
                "to": "support_agent",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_support_guardrail",
                "from": "support_agent",
                "to": "policy_guardrail",
                "map": {"final_output": "response"},
            },
            {
                "id": "e_guardrail_final",
                "from": "policy_guardrail",
                "to": "final",
                "map": {"passthrough": "response"},
            },
        ],
        "tools": {
            "lookup_policy": {
                "registry_ref": "tool.lookup_policy.v1",
                "version_constraint": ">=1.0,<2.0",
            },
            "get_order": {"registry_ref": "tool.get_order.v1", "version_constraint": ">=1.0,<2.0"},
            "escalate": {
                "registry_ref": "tool.escalate.v1",
                "version_constraint": ">=1.0",
                "requires_approval": True,
            },
        },
    }
    base.update(overrides)
    return base


def make_tool_payload(name: str = "lookup_policy", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": name,
        "version": "1.0",
        "description": f"{name} tool",
        "module_path": "caliber.workflows.demo_tools",
        "callable_name": name,
        "side_effect_level": "read",
    }
    payload.update(overrides)
    return payload


def register_demo_tools(client: TestClient) -> None:
    """Register lookup_policy, get_order, escalate against the demo module."""
    client.post(f"{PREFIX}/tools", json=make_tool_payload("lookup_policy", allow_in_preview=True))
    client.post(f"{PREFIX}/tools", json=make_tool_payload("get_order", allow_in_preview=True))
    client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload("escalate", side_effect_level="external_action"),
    )


def create_workflow(client: TestClient, name: str = "Support Workflow") -> str:
    r = client.post(f"{PREFIX}/workflows", json={"name": name, "owner": "@test"})
    assert r.status_code == 201, r.text
    return r.json()["data"]["workflow_id"]


def create_draft(client: TestClient, workflow_id: str, manifest: dict[str, Any]) -> tuple[str, str]:
    r = client.post(f"{PREFIX}/workflows/{workflow_id}/versions", json={"manifest": manifest})
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    return data["version_id"], data["manifest_hash"]


def seed_eval_dataset(
    session: Session,
    name: str = "support-eval-v3",
    inputs: list[str] | None = None,
    expected: list[str] | None = None,
) -> str:
    """Insert an eval dataset with example rows; returns the dataset_id.

    ``expected`` supplies the graded answers a deploy gate or evaluation scores
    against. Without it the examples carry no expectation, so quality is
    *unmeasurable* — a gate configured with a quality threshold then fails closed
    rather than reporting a meaningless 0.
    """
    inputs = inputs or ["What is your refund policy?", "Can I return my laptop?"]
    dataset = CaliberEvalDataset(
        dataset_id=new_eval_dataset_id(), name=name, owner="@test", version=1, status="active"
    )
    session.add(dataset)
    session.flush()
    for index, text in enumerate(inputs):
        answers = expected or []
        session.add(
            CaliberEvalDatasetExample(
                example_id=new_eval_example_id(),
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input={"input": text},
                expected=({"expected": answers[index]} if index < len(answers) else {}),
            )
        )
    session.commit()
    return dataset.dataset_id


def relax_release_quality_gate(client: TestClient) -> None:
    """Allow a production promotion without an attached deploy gate.

    The shipped default refuses one: rotating a production alias onto a version
    with no graded evidence is the false-release-evidence defect. Tests whose
    subject is something else (service publishing, run inspection, trace linkage)
    reach ``prod`` only incidentally and should not have to build a scored eval
    dataset, so they opt out explicitly here rather than having the product
    default weakened for everyone. The default itself is covered by
    ``tests/test_deploy_gate_evidence.py``.
    """
    client.app.state.config = client.app.state.config.model_copy(
        update={"release_require_quality_gate_for_environment_classes": ""}
    )


def deploy_prod(client: TestClient, workflow_id: str, version_id: str) -> str | None:
    """Deploy a version to the ``prod`` alias, leaving it rotated either way.

    Works regardless of release policy: when ``prod`` requires human approval (the
    ``gated_prod`` fixture) the promote returns a pending promotion that we
    approve; otherwise it rotates immediately. Returns the promotion_id when one
    was created, otherwise ``None``.

    Relaxes the production quality-gate requirement first — see
    :func:`relax_release_quality_gate`.
    """
    relax_release_quality_gate(client)
    r = client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/prod/promote",
        json={"version_id": version_id},
    )
    if r.status_code == 202:
        promotion_id = r.json()["data"]["promotion"]["promotion_id"]
        approve = client.post(f"{PREFIX}/workflow-promotions/{promotion_id}/approve")
        assert approve.status_code == 200, approve.text
        return promotion_id
    assert r.status_code == 200, r.text
    return None


def create_and_publish(
    client: TestClient,
    *,
    workflow_name: str = "Support Workflow",
    manifest: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Register tools, create workflow + draft, compile, publish. Returns (workflow_id, version_id)."""
    register_demo_tools(client)
    workflow_id = create_workflow(client, workflow_name)
    if manifest is None:
        manifest = make_support_manifest(workflow_id)
    version_id, _ = create_draft(client, workflow_id, manifest)
    r = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert r.status_code == 200, r.text
    return workflow_id, version_id
