"""End-to-end coverage for the deterministic Data Transform workflow node."""

from __future__ import annotations

from typing import Any

import pytest

from caliber.workflows import component_catalog as cc
from caliber.workflows.compiler import build_ir, compile_workflow
from caliber.workflows.data_transform import DataTransformError, apply_data_transform
from caliber.workflows.ir import IRDataTransform
from caliber.workflows.manifest import DataTransformNode, parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver
from caliber.workflows.validation import validate_manifest


def _manifest(
    operation: str, config: dict[str, Any], *, fail_on_invalid: bool = True
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": "wf_transform",
        "name": "Data transform demo",
        "runtime": {
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "none"},
        },
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "transform": {
                "id": "transform",
                "type": "data_transform",
                "operation": operation,
                "config": config,
                "fail_on_invalid": fail_on_invalid,
                "inputs": {
                    "value": {"type": "structured"},
                    "text": {"type": "string"},
                },
                "outputs": {
                    "text": {"type": "string"},
                    "result": {"type": "structured"},
                    "valid": {"type": "boolean"},
                    "metadata": {"type": "structured"},
                },
            },
            "out": {
                "id": "out",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {"id": "e1", "from": "start", "to": "transform", "map": {"user_message": "text"}},
            {"id": "e2", "from": "transform", "to": "out", "map": {"text": "response"}},
        ],
        "tools": {},
    }


def _run(
    operation: str,
    config: dict[str, Any],
    value: str,
    *,
    fail_on_invalid: bool = True,
) -> Any:
    manifest = parse_manifest(_manifest(operation, config, fail_on_invalid=fail_on_invalid))
    resolver = InMemoryToolResolver({})
    return execute(
        RuntimePlan(ir=build_ir(manifest, resolver, version="1"), resolver=resolver),
        value,
        executor=FakeWorkflowExecutor(),
    )


def test_apply_mapping_with_defaults_and_nested_paths() -> None:
    transformed = apply_data_transform(
        "mapping",
        {"customer": {"name": "Ada"}, "items": [{"sku": "A-1"}]},
        {
            "fields": {"customer_name": "customer.name", "first_sku": "items.0.sku"},
            "defaults": {"status": "new"},
        },
    )
    assert transformed == {
        "result": {"status": "new", "customer_name": "Ada", "first_sku": "A-1"},
        "valid": True,
        "metadata": {"operation": "mapping"},
    }


def test_apply_fixture_is_deterministic() -> None:
    config = {"fixture": {"service": "payments", "healthy": False}}
    assert apply_data_transform("fixture", {"ignored": True}, config) == apply_data_transform(
        "fixture", None, config
    )


def test_apply_json_schema_reports_all_validation_evidence() -> None:
    transformed = apply_data_transform(
        "json_schema",
        {"amount": "not-a-number"},
        {
            "schema": {
                "type": "object",
                "properties": {"amount": {"type": "number"}},
                "required": ["amount", "currency"],
            }
        },
    )
    assert transformed["valid"] is False
    assert transformed["result"] == {"amount": "not-a-number"}
    assert len(transformed["metadata"]["errors"]) == 2
    assert {error["validator"] for error in transformed["metadata"]["errors"]} == {
        "required",
        "type",
    }


def test_apply_decision_table_uses_first_match_and_default() -> None:
    config = {
        "rules": [
            {
                "name": "large-enterprise",
                "when": [
                    {"path": "amount", "operator": "greater_or_equal", "value": 10_000},
                    {"path": "tier", "operator": "equals", "value": "enterprise"},
                ],
                "result": "approval_required",
            }
        ],
        "default": "auto_approve",
    }
    matched = apply_data_transform(
        "decision_table", {"amount": 25_000, "tier": "enterprise"}, config
    )
    unmatched = apply_data_transform("decision_table", {"amount": 20, "tier": "starter"}, config)
    assert matched["result"] == {
        "decision": "approval_required",
        "matched": True,
        "matched_rule": "large-enterprise",
    }
    assert unmatched["result"]["decision"] == "auto_approve"
    assert unmatched["result"]["matched"] is False


def test_apply_confidence_clamps_score_and_records_signals() -> None:
    transformed = apply_data_transform(
        "confidence",
        {"citation_count": 3, "source_verified": True},
        {
            "bias": 0.2,
            "review_threshold": 0.8,
            "signals": [
                {
                    "name": "cited",
                    "path": "citation_count",
                    "operator": "greater_than",
                    "value": 0,
                    "weight": 0.45,
                },
                {
                    "name": "verified",
                    "path": "source_verified",
                    "operator": "equals",
                    "value": True,
                    "weight": 0.4,
                },
            ],
        },
    )
    assert transformed["result"] == {
        "confidence": 1.0,
        "needs_review": False,
        "review_threshold": 0.8,
        "matched_signals": ["cited", "verified"],
    }


def test_bad_transform_configuration_fails_closed() -> None:
    with pytest.raises(DataTransformError, match="unsupported condition operator"):
        apply_data_transform(
            "decision_table",
            {"amount": 1},
            {"rules": [{"when": [{"path": "amount", "operator": "execute", "value": 1}]}]},
        )


def test_data_transform_lifecycle_parse_validate_catalog_and_compile() -> None:
    parsed = parse_manifest(_manifest("mapping", {"fields": {"name": "customer.name"}}))
    assert isinstance(parsed.nodes["transform"], DataTransformNode)
    assert validate_manifest(parsed).errors == []

    component = next(
        item
        for item in cc.build_workflow_component_catalog()["components"]
        if item["type"] == "data_transform"
    )
    assert component["label"] == "Data Transform"
    assert component["category"] == "Logic"
    assert component["starter_node"]["type"] == "data_transform"
    assert {field["key"] for field in component["fields"]} >= {
        "operation",
        "config",
        "fail_on_invalid",
    }

    resolver = InMemoryToolResolver({})
    ir = build_ir(parsed, resolver, version="1")
    assert isinstance(ir.nodes["transform"], IRDataTransform)
    compiled = compile_workflow(parsed, resolver=resolver, version="1")
    assert compiled.report["validation"]["errors"] == []
    assert "data_transform" in compiled.generated_python.lower()


def test_runtime_maps_json_input_and_emits_stable_text() -> None:
    result = _run(
        "mapping",
        {"fields": {"name": "customer.name"}, "defaults": {"kind": "intake"}},
        '{"customer": {"name": "Ada"}}',
    )
    assert result.status == "completed"
    assert result.output == '{"kind": "intake", "name": "Ada"}'
    transform = next(step for step in result.steps if step.node_id == "transform")
    assert transform.status == "ok"
    assert transform.detail == "applied mapping"


def test_runtime_schema_fail_on_invalid_controls_execution() -> None:
    config = {
        "schema": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        }
    }
    blocked = _run("json_schema", config, '{"amount": "bad"}')
    assert blocked.status == "error"
    assert "validation failed" in (blocked.error or "")

    observed = _run("json_schema", config, '{"amount": "bad"}', fail_on_invalid=False)
    assert observed.status == "completed"
    assert observed.output == '{"amount": "bad"}'
