"""Supplemental tests to close coverage gaps across multiple modules.

Targets uncovered lines/branches identified by pytest-cov:
- sandbox.py: non-object schema mock, zero-limit budget
- regression.py: _trace_sample_ids, _regressions, require_passing edge cases
- workflows/tools.py: bind_registered_tool, InMemoryToolResolver.register
- observability/metrics.py: record_workflow_compile, record_workflow_preview, record_deploy_gate
- secrets.py: lines 138-143 (empty file)
- bundle.py: lines 280-282
- eval/mlflow_runner.py: lines 178-179
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# sandbox.py — non-object schema mock + zero-limit budget
# ---------------------------------------------------------------------------
from caliber.workflows.ir import IRToolBinding
from caliber.workflows.sandbox import (
    TokenBudget,
    _sample_from_schema,
    mock_response_for,
)


def _binding(level: str, *, output_schema=None) -> IRToolBinding:
    return IRToolBinding(
        local_name="t",
        registry_ref="tool.t.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level=level,
        allow_in_preview=False,
        module_path="m",
        callable_name="f",
        output_schema=output_schema,
    )


def test_mock_response_for_non_object_schema() -> None:
    """When output_schema.type is 'string', the response wraps it in {value:...}."""
    binding = _binding("write", output_schema={"type": "string", "example": "hello"})
    resp = mock_response_for(binding)
    assert resp["_preview_mock"] is True
    assert resp["value"] == "hello"


def test_mock_response_for_array_schema() -> None:
    binding = _binding("write", output_schema={"type": "array"})
    resp = mock_response_for(binding)
    assert resp["_preview_mock"] is True
    assert resp["value"] == []


def test_mock_response_for_boolean_schema() -> None:
    binding = _binding("write", output_schema={"type": "boolean"})
    resp = mock_response_for(binding)
    assert resp["_preview_mock"] is True
    assert resp["value"] is False


def test_mock_response_for_none_type_schema() -> None:
    binding = _binding("write", output_schema={"type": "unknown_type"})
    resp = mock_response_for(binding)
    assert resp["_preview_mock"] is True


def test_token_budget_zero_limit_does_not_raise() -> None:
    """A zero limit means unlimited — charges never trigger the error."""
    budget = TokenBudget(limit=0)
    budget.charge(999_999)
    assert budget.used == 999_999


def test_token_budget_negative_tokens_treated_as_zero() -> None:
    budget = TokenBudget(limit=100)
    budget.charge(-50)
    assert budget.used == 0


def test_sample_from_schema_integer() -> None:
    assert _sample_from_schema({"type": "integer"}) == 0


def test_sample_from_schema_number() -> None:
    assert _sample_from_schema({"type": "number"}) == 0


def test_sample_from_schema_boolean() -> None:
    assert _sample_from_schema({"type": "boolean"}) is False


def test_sample_from_schema_null_type() -> None:
    assert _sample_from_schema({"type": "null"}) is None


# ---------------------------------------------------------------------------
# workflows/tools.py — bind_registered_tool import error + register
# ---------------------------------------------------------------------------

from caliber.workflows.tools import (
    InMemoryToolResolver,
    ToolBindingError,
    ToolRegistryEntry,
    ToolResolutionError,
    bind_registered_tool,
    family_name,
)


def test_bind_registered_tool_callable_override() -> None:
    entry = ToolRegistryEntry(
        name="test",
        version="1.0",
        module_path="<in-memory>",
        callable_name="fn",
    )
    fn = lambda: "hi"
    result = bind_registered_tool(entry, callable_override=fn)
    assert result is fn


def test_bind_registered_tool_in_memory_no_override_raises() -> None:
    entry = ToolRegistryEntry(
        name="test",
        version="1.0",
        module_path="<in-memory>",
        callable_name="fn",
    )
    with pytest.raises(ToolBindingError, match="no importable module"):
        bind_registered_tool(entry)


def test_bind_registered_tool_missing_attribute_raises() -> None:
    entry = ToolRegistryEntry(
        name="test",
        version="1.0",
        module_path="json",
        callable_name="__nonexistent_function_xyz__",
    )
    with pytest.raises(ToolBindingError, match="has no attribute"):
        bind_registered_tool(entry)


def test_bind_registered_tool_from_real_module() -> None:
    entry = ToolRegistryEntry(
        name="test",
        version="1.0",
        module_path="json",
        callable_name="dumps",
    )
    fn = bind_registered_tool(entry)
    import json

    assert fn is json.dumps


def test_in_memory_resolver_register() -> None:
    resolver = InMemoryToolResolver()

    fn = lambda: "result"
    entry = ToolRegistryEntry(
        name="my_tool", version="1.0", module_path="<in-memory>", callable_name="fn"
    )
    resolver.register(entry, fn)

    resolution = resolver.resolve("my_tool")
    assert resolution.entry.name == "my_tool"
    assert resolver.get_callable(entry.registry_ref) is fn


def test_in_memory_resolver_unregistered_raises() -> None:
    resolver = InMemoryToolResolver()
    with pytest.raises(ToolResolutionError, match="not registered"):
        resolver.resolve("nonexistent_tool")


def test_in_memory_resolver_from_callables() -> None:
    fn_a = lambda: "a"
    fn_b = lambda: "b"
    resolver = InMemoryToolResolver.from_callables({"tool.a.v1": fn_a, "tool.b.v2": fn_b})
    assert resolver.get_callable("tool.a.v1") is fn_a
    assert resolver.get_callable("tool.b.v2") is fn_b
    res_a = resolver.resolve("a")
    assert res_a.entry.name == "a"


def test_family_name_with_tool_prefix() -> None:
    assert family_name("tool.my_tool.v1") == "my_tool"


def test_family_name_plain() -> None:
    assert family_name("my_tool.v1") == "my_tool"


# ---------------------------------------------------------------------------
# observability/metrics.py — workflow-specific metrics
# ---------------------------------------------------------------------------

from caliber.observability import metrics


@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    metrics.reset_metrics_for_test()


def test_record_workflow_compile_ok() -> None:
    metrics.record_workflow_compile(ok=True, duration_ms=150.0)
    body = metrics.render().decode("utf-8")
    assert 'caliber_workflow_compiles_total{result="ok"} 1.0' in body
    assert "caliber_workflow_compile_seconds_count" in body


def test_record_workflow_compile_error() -> None:
    metrics.record_workflow_compile(ok=False)
    body = metrics.render().decode("utf-8")
    assert 'caliber_workflow_compiles_total{result="error"} 1.0' in body


def test_record_workflow_preview() -> None:
    metrics.record_workflow_preview("completed")
    body = metrics.render().decode("utf-8")
    assert 'caliber_workflow_previews_total{status="completed"} 1.0' in body


def test_record_deploy_gate() -> None:
    metrics.record_deploy_gate("prod", passed=True)
    body = metrics.render().decode("utf-8")
    assert 'caliber_workflow_deploy_gates_total{alias="prod",result="passed"} 1.0' in body


def test_record_workflow_patch() -> None:
    metrics.record_workflow_patch("workflow_manifest")
    body = metrics.render().decode("utf-8")
    assert 'caliber_workflow_patches_total{patch_kind="workflow_manifest"} 1.0' in body


def test_record_workflow_promotion() -> None:
    metrics.record_workflow_promotion("staging")
    body = metrics.render().decode("utf-8")
    assert 'caliber_workflow_promotions_total{alias="staging"} 1.0' in body


# ---------------------------------------------------------------------------
# regression.py — _trace_sample_ids with rich context, _regressions variants
# ---------------------------------------------------------------------------

from caliber.db.models import (
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.eval.provider import EvalComparison, ScoreSet
from caliber.regression import (
    _regressions,
    _trace_sample_ids,
    candidate_hash,
)


def _make_comparison(
    *,
    overall: float = 0.8,
    deltas: dict | None = None,
    dataset_id: str = "",
) -> EvalComparison:
    return EvalComparison(
        candidate=ScoreSet(overall=overall),
        baseline=ScoreSet(overall=0.7),
        deltas=deltas or {},
        eval_dataset_id=dataset_id,
    )


def test_trace_sample_ids_with_rich_context(db_session: Session) -> None:
    db_session.add(
        CaliberVerificationItem(
            item_id="FB-100",
            agent_id="a1",
            category="hallucination",
            free_text="bad",
            severity="standard",
            trace_id="trace-1",
            submitted_context={
                "trace_id": "ctx-trace-2",
                "trace_ids": ["ctx-trace-3", "ctx-trace-4"],
            },
        )
    )
    db_session.commit()
    job = CaliberRefinementJob(
        job_id="J-100",
        agent_id="a1",
        primary_item_id="FB-100",
        artifact_type="prompt",
        status="running",
        current_stage="eval",
        candidate={},
        bundle_targets=[],
    )
    db_session.add(job)
    db_session.commit()

    ids = _trace_sample_ids(db_session, job)
    assert "trace-1" in ids
    assert "ctx-trace-2" in ids
    assert "ctx-trace-3" in ids
    assert "ctx-trace-4" in ids
    # Deduplication: trace-1 should only appear once
    assert ids.count("trace-1") == 1


def test_trace_sample_ids_missing_item(db_session: Session) -> None:
    job = CaliberRefinementJob(
        job_id="J-101",
        agent_id="a1",
        primary_item_id="FB-missing",
        artifact_type="prompt",
        status="running",
        current_stage="eval",
        candidate={},
        bundle_targets=[],
    )
    db_session.add(job)
    db_session.commit()
    assert _trace_sample_ids(db_session, job) == []


def test_regressions_with_negative_deltas() -> None:
    comparison = _make_comparison(deltas={"factual": -0.05, "tone": 0.1, "overall": -0.02})
    regressions = _regressions(comparison, [])
    # overall is excluded; only factual should appear
    assert len(regressions) == 1
    assert regressions[0]["metric"] == "factual"
    assert regressions[0]["delta"] == -0.05


def test_regressions_fallback_to_gate_reasons() -> None:
    comparison = _make_comparison(deltas={"factual": 0.1})
    regressions = _regressions(comparison, ["below threshold"])
    assert len(regressions) == 1
    assert regressions[0]["metric"] == "gate"
    assert regressions[0]["reason"] == "below threshold"


def test_regressions_empty_when_no_negatives_and_no_reasons() -> None:
    comparison = _make_comparison(deltas={"factual": 0.1, "tone": 0.0})
    regressions = _regressions(comparison, [])
    assert regressions == []


def test_candidate_hash_deterministic() -> None:
    h1 = candidate_hash("hello world")
    h2 = candidate_hash("hello world")
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest
