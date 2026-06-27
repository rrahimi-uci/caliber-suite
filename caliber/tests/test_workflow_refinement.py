"""Workflow-aware refinement engine tests (plan §17, §19.10)."""

from __future__ import annotations

import pytest

from caliber.workflows.manifest import parse_manifest
from caliber.workflows.refinement import (
    COMPONENT_GUARDRAIL,
    COMPONENT_WORKFLOW_EDGE,
    WorkflowDiagnosis,
    WorkflowRunResult,
    evaluate_candidate,
    generate_workflow_patch,
    localize_failure,
)
from caliber.workflows.runtime import AgentTurnResult, FakeWorkflowExecutor
from tests.workflow_helpers import fake_resolver, make_manifest, make_support_manifest

# --- diagnosis localization (§19.10 test_orchestrator_workflow_diagnosis) ----


def test_diagnose_agent_missed_tool_call() -> None:
    diag = localize_failure(
        {
            "category": "tool_use",
            "node_id": "support_agent",
            "required_tools": ["lookup_policy"],
            "observed_tool_calls": [],
        }
    )
    assert diag.affected_components == ["tool_contract"]
    assert diag.localized_to["node_ids"] == ["support_agent"]
    assert diag.recommended_patch_type == "workflow_manifest"


def test_diagnose_wrong_handoff() -> None:
    diag = localize_failure(
        {"category": "handoff", "wrong_handoff": True, "edge_id": "e_router_billing"}
    )
    assert diag.affected_components == ["workflow_edge"]
    assert diag.localized_to["edge_ids"] == ["e_router_billing"]


def test_diagnose_guardrail_false_positive() -> None:
    diag = localize_failure(
        {"category": "guardrail", "guardrail_false_positive": True, "node_id": "policy_guardrail"}
    )
    assert diag.affected_components == ["guardrail"]
    assert diag.localized_to["node_ids"] == ["policy_guardrail"]


def test_diagnose_prompt_vague() -> None:
    diag = localize_failure({"category": "hallucination", "node_id": "support_agent"})
    assert diag.affected_components == ["prompt"]
    assert diag.recommended_patch_type == "prompt"


def test_diagnose_multiple_components() -> None:
    diag = localize_failure(
        {
            "category": "tool_use",
            "node_id": "support_agent",
            "required_tools": ["lookup_policy"],
            "observed_tool_calls": [],
            "hallucination": True,
        }
    )
    assert diag.affected_components == ["prompt", "tool_contract"]


def test_diagnose_defaults_unknown_evidence_to_prompt() -> None:
    diag = localize_failure({})

    assert diag.affected_components == ["prompt"]
    assert diag.localized_to["node_ids"] == []
    assert diag.root_cause == "workflow-level failure"


# --- candidate generation (§19.10 test_orchestrator_workflow_candidate) -------


def _manifest():
    return parse_manifest(make_support_manifest("wf"))


def test_candidate_prompt_only() -> None:
    diag = localize_failure({"category": "hallucination", "node_id": "support_agent"})
    patch = generate_workflow_patch(_manifest(), diag, resolver=fake_resolver())
    assert patch.patch_kind == "prompt"
    assert patch.semantic_ops == []
    assert patch.prompt_suggestion


def test_candidate_adds_guardrail() -> None:
    diag = localize_failure(
        {
            "category": "tool_use",
            "node_id": "support_agent",
            "required_tools": ["lookup_policy"],
            "observed_tool_calls": [],
        }
    )
    patch = generate_workflow_patch(_manifest(), diag, resolver=fake_resolver())
    assert patch.patch_kind == "workflow_manifest"
    ops = [o["op"] for o in patch.semantic_ops]
    assert "add_node_after" in ops
    assert any(n["id"].endswith("grounding_guard") for n in patch.graph_diff["added_nodes"])


def test_candidate_compiles_and_preserves_unrelated_nodes() -> None:
    base = _manifest()
    diag = localize_failure(
        {
            "category": "tool_use",
            "node_id": "support_agent",
            "required_tools": ["lookup_policy"],
            "observed_tool_calls": [],
        }
    )
    patch = generate_workflow_patch(base, diag, resolver=fake_resolver())
    candidate = parse_manifest(patch.candidate_manifest)
    # support_agent and final unchanged; only the new guardrail added.
    assert candidate.nodes["support_agent"].model == base.nodes["support_agent"].model
    assert "final" in candidate.nodes
    assert any(nid.endswith("grounding_guard") for nid in candidate.nodes)


def test_candidate_updates_handoff_when_diagnosis_suggests_route() -> None:
    data = make_support_manifest("wf")
    data["nodes"]["billing_agent"] = {
        "id": "billing_agent",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "Handle billing."},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    manifest = parse_manifest(data)
    diagnosis = WorkflowDiagnosis(
        root_cause="wrong handoff",
        affected_components=[COMPONENT_WORKFLOW_EDGE],
        localized_to={"node_ids": ["support_agent"], "suggested_handoff": "billing_agent"},
        recommended_patch_type="workflow_manifest",
    )

    patch = generate_workflow_patch(manifest, diagnosis, resolver=fake_resolver())
    candidate = parse_manifest(patch.candidate_manifest)

    assert patch.patch_kind == "workflow_manifest"
    assert candidate.nodes["support_agent"].handoffs[0].target == "billing_agent"


def test_candidate_relaxes_guardrail_failure_behavior() -> None:
    diagnosis = WorkflowDiagnosis(
        root_cause="guardrail false positive",
        affected_components=[COMPONENT_GUARDRAIL],
        localized_to={"node_ids": ["policy_guardrail"]},
        recommended_patch_type="workflow_manifest",
    )

    patch = generate_workflow_patch(_manifest(), diagnosis, resolver=fake_resolver())
    candidate = parse_manifest(patch.candidate_manifest)

    assert candidate.nodes["policy_guardrail"].on_failure == "redact"


# --- candidate eval gate (§19.10 test_orchestrator_workflow_eval) -------------


def _const_scorer(quality: float, tone: float):
    def _scorer(_result: WorkflowRunResult) -> dict[str, float]:
        return {"quality": quality, "tone": tone}

    return _scorer


class _ManifestAwareExecutor:
    def __init__(self, output: str) -> None:
        self._output = output

    def run_agent(self, agent, input_text: str, *, tool_callables, preview: bool):
        _ = (agent, input_text, tool_callables, preview)
        return AgentTurnResult(final_output=self._output)


def test_eval_candidate_beats_baseline() -> None:
    base = _manifest()
    res = evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        ["q1", "q2"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.87, 0.90),
        baseline_scores={"quality": 0.84, "tone": 0.91},
        thresholds={"min_overall_delta": 0.02, "max_tone_regression": 0.01},
    )
    assert res.passed is True
    assert res.deltas["quality"] == pytest.approx(0.03, abs=1e-6)
    assert res.to_dict()["gate"]["passed"] is True


def test_eval_candidate_regresses() -> None:
    base = _manifest()
    res = evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        ["q1"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.80, 0.90),
        baseline_scores={"quality": 0.90, "tone": 0.90},
        thresholds={"min_overall_delta": 0.0},
    )
    assert res.passed is False


def test_eval_below_min_delta_fails() -> None:
    base = _manifest()
    res = evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        ["q1"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.85, 0.90),
        baseline_scores={"quality": 0.84, "tone": 0.90},
        thresholds={"min_overall_delta": 0.02},
    )
    assert res.passed is False  # delta 0.01 < 0.02


def test_eval_tone_regression_within_tolerance() -> None:
    base = _manifest()
    res = evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        ["q1"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.90, 0.895),
        baseline_scores={"quality": 0.84, "tone": 0.90},
        thresholds={"min_overall_delta": 0.02, "max_tone_regression": 0.01},
    )
    assert res.passed is True  # tone -0.005 within -0.01


def test_eval_tone_regression_exceeds_tolerance() -> None:
    base = _manifest()
    res = evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        ["q1"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.90, 0.88),
        baseline_scores={"quality": 0.84, "tone": 0.90},
        thresholds={"min_overall_delta": 0.02, "max_tone_regression": 0.01},
    )
    assert res.passed is False  # tone -0.02 exceeds -0.01


def test_eval_baseline_cached_skips_baseline_run() -> None:
    base = _manifest()
    baseline_executor = FakeWorkflowExecutor()
    evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        ["q1"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.9, 0.9),
        baseline_scores={"quality": 0.84, "tone": 0.9},
        baseline_executor=baseline_executor,
        thresholds={"min_overall_delta": 0.0},
    )
    assert baseline_executor.calls == []  # cached baseline → not replayed


def test_eval_candidate_compile_failure() -> None:
    base = _manifest()
    broken = base.to_dict()
    broken["nodes"]["support_agent"]["tools"] = ["nonexistent_tool"]  # unbound -> invalid
    res = evaluate_candidate(
        base.to_dict(),
        broken,
        ["q1"],
        resolver=fake_resolver(),
        thresholds={"min_overall_delta": 0.0},
    )
    assert res.passed is False
    assert res.error is not None


def test_eval_candidate_supports_manifest_aware_candidate_executor_factory() -> None:
    base = _manifest()
    candidate = base.to_dict()

    def _candidate_factory(manifest_dict: dict[str, object]):
        name = str(manifest_dict.get("name") or "candidate")
        return _ManifestAwareExecutor(f"factory::{name}")

    res = evaluate_candidate(
        base.to_dict(),
        candidate,
        ["q1"],
        resolver=fake_resolver(),
        baseline_scores={"quality": 0.0, "tone": 0.0},
        thresholds={"min_overall_delta": 0.5},
        scorer=lambda result: {
            "quality": 1.0 if "factory::" in result.output else 0.0,
            "tone": 1.0,
        },
        candidate_executor_factory=_candidate_factory,
        candidate_executor=_ManifestAwareExecutor("not from factory"),
    )

    assert res.passed is True
    assert res.candidate_scores["quality"] == 1.0


def test_eval_with_no_examples_produces_empty_scores() -> None:
    base = _manifest()
    res = evaluate_candidate(
        base.to_dict(),
        base.to_dict(),
        [],
        resolver=fake_resolver(),
        baseline_scores={},
        thresholds={"min_overall_delta": 0.0},
    )

    assert res.passed is True
    assert res.candidate_scores == {}
    assert res.deltas == {}


def test_eval_candidate_supports_injected_knowledge_runners() -> None:
    manifest = make_manifest("wf")
    del manifest["nodes"]["agent"]
    manifest["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "retrieval_modes": ["dense"],
        "inputs": {"question": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    manifest["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    calls: list[dict[str, object]] = []

    def _knowledge_query_runner(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "text": "Refund policy is 30 days.",
            "answer": "Refund policy is 30 days.",
            "chunks": [],
            "citations": [],
            "result": {"ok": True},
        }

    res = evaluate_candidate(
        manifest,
        manifest,
        ["refund?"],
        resolver=fake_resolver(),
        scorer=_const_scorer(0.90, 0.90),
        baseline_scores={"quality": 0.84, "tone": 0.90},
        thresholds={"min_overall_delta": 0.0},
        knowledge_query_runner=_knowledge_query_runner,
    )

    assert res.passed is True
    assert len(calls) == 1
    assert calls[0]["question"] == "refund?"
