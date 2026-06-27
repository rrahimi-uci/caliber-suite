"""Unit coverage for workflow calibration helpers."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample, CaliberVerificationItem
from caliber.workflows.calibration import (
    WorkflowCalibrationCandidate,
    WorkflowCalibrationError,
    WorkflowCalibrationExample,
    WorkflowCalibrationSpec,
    aggregate_weighted_scores,
    evaluate_workflow_calibration_candidates,
    generate_workflow_calibration_candidates,
    is_low_confidence_calibration,
    resolve_workflow_calibration_examples,
    score_tool_adherence,
    score_workflow_calibration_run,
)
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.refinement import WorkflowDiagnosis
from caliber.workflows.runtime import AgentTurnResult, NodeStep, WorkflowRunResult
from tests.workflow_helpers import fake_resolver, make_manifest, make_support_manifest


def _manifest():
    return parse_manifest(
        make_support_manifest(
            "wf",
            deploy_gates={
                "support_eval_gate": {
                    "type": "deploy_gate",
                    "dataset_ref": "support_eval",
                    "required_for_aliases": ["dev"],
                    "thresholds": {},
                }
            },
        )
    )


def _example(
    *,
    expected: dict[str, object] | None = None,
    weight: float = 1.0,
    tags: list[str] | None = None,
) -> WorkflowCalibrationExample:
    return WorkflowCalibrationExample(
        input_text="refund?",
        expected=expected or {},
        weight=weight,
        tags=tags or [],
        example_id="ex",
    )


def _run(
    output: str,
    *,
    status: str = "completed",
    tools: list[str] | None = None,
    guardrails: list[dict[str, object]] | None = None,
) -> WorkflowRunResult:
    return WorkflowRunResult(
        status=status,
        output=output,
        steps=[
            NodeStep(
                node_id="support_agent",
                node_type="agent",
                status="ok",
                tool_calls=[{"tool": tool} for tool in (tools or [])],
            )
        ],
        guardrail_results=guardrails or [],
    )


def _seed_dataset(
    session: Session,
    *,
    status: str = "active",
    examples: list[dict[str, object]] | None = None,
) -> str:
    dataset = CaliberEvalDataset(
        dataset_id="ds-cal",
        name="support-eval-v3",
        owner="@test",
        version=1,
        status=status,
    )
    session.add(dataset)
    session.flush()
    for idx, payload in enumerate(examples or [], start=1):
        session.add(
            CaliberEvalDatasetExample(
                example_id=f"ex-{idx}",
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input=payload.get("input", {"input": f"case {idx}"}),
                expected=payload.get("expected", {}),
                weight=float(payload.get("weight", 1.0)),
                tags=list(payload.get("tags", [])),
                superseded_at=payload.get("superseded_at"),
            )
        )
    session.commit()
    return dataset.dataset_id


def test_quality_match_supports_json_subset() -> None:
    scored = score_workflow_calibration_run(
        _run('{"answer": {"policy": "30 days", "region": "US"}, "extra": true}'),
        _example(expected={"json_subset": {"answer": {"policy": "30 days"}}}),
    )

    assert scored.scores["quality"] == 1.0
    assert scored.details["quality"] == "output matched expected value"


@pytest.mark.parametrize(
    ("expected", "tags", "output"),
    [
        ({"contains": "refund policy"}, [], "The refund policy is 30 days."),
        ({"output": "Exact answer", "match_kind": "exact"}, [], "Exact answer"),
        ({"output": "refund\\s+policy"}, ["match:regex"], "Refund policy applies."),
    ],
)
def test_quality_match_supports_exact_contains_and_regex(
    expected: dict[str, object],
    tags: list[str],
    output: str,
) -> None:
    scored = score_workflow_calibration_run(_run(output), _example(expected=expected, tags=tags))

    assert scored.scores["quality"] == 1.0


def test_weights_affect_aggregate() -> None:
    scores = [{"quality": 0.0}, {"quality": 1.0}]
    examples = [_example(weight=9.0), _example(weight=1.0)]

    assert aggregate_weighted_scores(scores, examples)["quality"] == pytest.approx(0.1)


def test_missing_expected_yields_clear_detail_and_zero_score() -> None:
    scored = score_workflow_calibration_run(_run("anything"), _example())

    assert scored.scores["quality"] == 0.0
    assert scored.details["quality"] == "expected output is missing"


def test_tool_adherence_penalizes_missing_forbidden_order_and_undeclared_tools() -> None:
    assert (
        score_tool_adherence(
            _run("ok", tools=["lookup_policy"]),
            _example(expected={"required_tools": ["lookup_policy"]}),
        )["tool_adherence"]
        == 1.0
    )
    assert (
        score_tool_adherence(
            _run("ok", tools=["get_order"]),
            _example(expected={"required_tools": ["lookup_policy"]}),
        )["tool_adherence"]
        == 0.0
    )
    assert (
        score_tool_adherence(
            _run("ok", tools=["lookup_policy", "escalate"]),
            _example(expected={"forbidden_tools": ["escalate"]}),
        )["tool_adherence"]
        == 0.0
    )
    assert (
        score_tool_adherence(
            _run("ok", tools=["get_order", "lookup_policy"]),
            _example(expected={"tool_order": ["lookup_policy", "get_order"]}),
        )["tool_adherence"]
        == 0.5
    )

    scored = score_workflow_calibration_run(
        _run("ok", tools=["lookup_policy", "escalate"]),
        _example(expected={"allowed_tools": ["lookup_policy"]}),
    )
    assert scored.scores["tool_adherence"] == 0.0
    assert "undeclared" in scored.details["tool_adherence"]


def test_active_dataset_required(db_session: Session) -> None:
    _seed_dataset(db_session, status="archived", examples=[{"input": {"input": "hi"}}])

    with pytest.raises(WorkflowCalibrationError, match="active deploy-gate eval dataset"):
        resolve_workflow_calibration_examples(db_session, _manifest(), None, None)


def test_resolver_preserves_non_superseded_examples_only(db_session: Session) -> None:
    _seed_dataset(
        db_session,
        examples=[
            {
                "input": {"message": "active"},
                "expected": {"contains": "active"},
                "weight": 2.0,
                "tags": ["match:contains"],
            },
            {
                "input": {"message": "retired"},
                "expected": {"contains": "retired"},
                "superseded_at": datetime.now(),
            },
        ],
    )

    examples = resolve_workflow_calibration_examples(db_session, _manifest(), None, None)

    assert [example.input_text for example in examples] == ["active"]
    assert examples[0].expected == {"contains": "active"}
    assert examples[0].weight == 2.0
    assert examples[0].tags == ["match:contains"]


def test_empty_deploy_gate_dataset_rejects(db_session: Session) -> None:
    _seed_dataset(db_session, examples=[])

    with pytest.raises(WorkflowCalibrationError, match="no non-superseded examples"):
        resolve_workflow_calibration_examples(db_session, _manifest(), None, None)


def test_single_example_dataset_marks_low_confidence(db_session: Session) -> None:
    _seed_dataset(db_session, examples=[{"input": {"input": "one"}}])
    spec = WorkflowCalibrationSpec(budget={"max_eval_examples": 20, "min_examples": 2})

    examples = resolve_workflow_calibration_examples(db_session, _manifest(), None, spec)

    assert is_low_confidence_calibration(examples, spec) is True


def test_flagged_feedback_never_replaces_dataset_when_budget_is_one(db_session: Session) -> None:
    _seed_dataset(db_session, examples=[{"input": {"input": "dataset case"}}])
    item = CaliberVerificationItem(
        item_id="FB-1",
        agent_id="support-agent",
        category="workflow_calibration",
        free_text="flagged case",
        severity="standard",
        workflow_id="wf",
        submitted_context={"expected": {"contains": "flagged"}},
    )
    db_session.add(item)
    db_session.commit()
    spec = WorkflowCalibrationSpec(budget={"max_eval_examples": 1, "min_examples": 1})

    examples = resolve_workflow_calibration_examples(db_session, _manifest(), item, spec)

    assert [example.input_text for example in examples] == ["dataset case"]
    assert all("calibration:flagged" not in example.tags for example in examples)


def test_candidate_generation_respects_budget_and_existing_patch_ops() -> None:
    manifest = _manifest()
    diagnosis = WorkflowDiagnosis(
        root_cause="Missing tool.",
        affected_components=["tool_contract"],
        localized_to={"node_ids": ["support_agent"], "tool_refs": ["lookup_policy"]},
        recommended_patch_type="workflow_manifest",
    )
    spec = WorkflowCalibrationSpec(
        budget={"max_candidates": 1, "max_eval_examples": 20, "min_examples": 1}
    )

    candidates = generate_workflow_calibration_candidates(
        manifest,
        diagnosis,
        [_example(expected={"required_tools": ["lookup_policy"]})],
        spec,
        resolver=fake_resolver(),
    )

    assert len(candidates) == 1
    assert candidates[0].candidate_id == "cal-0"
    assert len(candidates[0].semantic_ops) <= 2
    assert all(
        op["op"] in {"add_node_after", "update_tool_constraint"}
        for op in candidates[0].semantic_ops
    )


def test_invalid_candidate_variant_is_dropped_before_replay() -> None:
    manifest = _manifest()
    diagnosis = WorkflowDiagnosis(
        root_cause="Wrong handoff.",
        affected_components=["workflow_edge"],
        localized_to={"node_ids": ["support_agent"], "suggested_handoff": "missing_agent"},
        recommended_patch_type="workflow_manifest",
    )
    spec = WorkflowCalibrationSpec(move_set=["reroute_handoff"])

    candidates = generate_workflow_calibration_candidates(
        manifest,
        diagnosis,
        [_example(expected={"contains": "x"})],
        spec,
        resolver=fake_resolver(),
    )

    assert candidates == []


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def run_agent(self, agent, input_text: str, *, tool_callables, preview: bool):
        self.calls.append(input_text)
        return AgentTurnResult(final_output=f"processed {input_text}")


class _InstructionExecutor:
    def run_agent(self, agent, input_text: str, *, tool_callables, preview: bool):
        instructions = getattr(agent.instructions, "inline_text", "") or agent.name
        return AgentTurnResult(final_output=instructions)


class _FactoryInstructionExecutor:
    def __init__(self, output: str) -> None:
        self._output = output

    def run_agent(self, agent, input_text: str, *, tool_callables, preview: bool):
        _ = (agent, input_text, tool_callables, preview)
        return AgentTurnResult(final_output=self._output)


def _candidate(candidate_id: str, manifest: dict[str, object], summary: str = "candidate"):
    return WorkflowCalibrationCandidate(
        candidate_id=candidate_id,
        patch_kind="workflow_manifest",
        semantic_ops=[{"op": "update_node_field", "target_node_id": "agent"}],
        candidate_manifest=manifest,
        summary=summary,
        graph_diff={},
    )


def test_evaluator_replays_baseline_once_for_multiple_candidates() -> None:
    manifest = make_manifest("wf")
    examples = [
        _example(expected={"contains": "processed"}, weight=1.0),
        WorkflowCalibrationExample(
            input_text="case two",
            expected={"contains": "processed"},
            weight=1.0,
            tags=[],
            example_id="ex-2",
        ),
    ]
    baseline_executor = _CountingExecutor()

    result = evaluate_workflow_calibration_candidates(
        manifest,
        [_candidate("cal-0", manifest), _candidate("cal-1", manifest)],
        examples,
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.0}),
        resolver=fake_resolver(),
        baseline_executor=baseline_executor,
        candidate_executor=_CountingExecutor(),
    )

    assert baseline_executor.calls == ["refund?", "case two"]
    assert result.passed is True


def test_best_passing_candidate_wins_by_target_delta() -> None:
    baseline = make_manifest("wf")
    weak = make_manifest("wf")
    weak["nodes"]["agent"]["instructions"]["text"] = "weak answer"
    strong = make_manifest("wf")
    strong["nodes"]["agent"]["instructions"]["text"] = "strong answer"

    result = evaluate_workflow_calibration_candidates(
        baseline,
        [_candidate("cal-weak", weak, "weak"), _candidate("cal-strong", strong, "strong")],
        [_example(expected={"contains": "strong answer"})],
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.5}),
        resolver=fake_resolver(),
        baseline_executor=_InstructionExecutor(),
        candidate_executor=_InstructionExecutor(),
    )

    assert result.passed is True
    assert result.winner is not None
    assert result.winner.candidate_id == "cal-strong"
    assert result.winner.deltas["quality"] == 1.0


def test_calibration_scorer_overrides_quality_dimension() -> None:
    """Wave 5.2: an injected (judge) scorer overrides only the quality dim."""
    manifest = make_manifest("wf")
    examples = [_example(expected={"contains": "processed"}, weight=1.0)]

    def _judge(result, *, input_text=""):  # mirrors LLMJudgeScorer's shape
        _ = (result, input_text)
        return {"completion_rate": 1.0, "tool_adherence": 1.0, "quality": 0.123}

    result = evaluate_workflow_calibration_candidates(
        manifest,
        [_candidate("cal-0", manifest)],
        examples,
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.0}),
        resolver=fake_resolver(),
        baseline_executor=_CountingExecutor(),
        candidate_executor=_CountingExecutor(),
        scorer=_judge,
    )

    # quality is the judge's distinctive value; structural dims are retained.
    assert result.baseline_scores["quality"] == 0.123
    assert "tool_adherence" in result.baseline_scores
    assert "completion_rate" in result.baseline_scores


def test_no_improvement_rejects_with_near_miss() -> None:
    manifest = make_manifest("wf")

    result = evaluate_workflow_calibration_candidates(
        manifest,
        [_candidate("cal-0", manifest)],
        [_example(expected={"contains": "never appears"})],
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.1}),
        resolver=fake_resolver(),
        baseline_executor=_CountingExecutor(),
        candidate_executor=_CountingExecutor(),
    )

    assert result.passed is False
    assert result.winner is not None
    assert result.winner.candidate_id == "cal-0"
    assert "no candidate met calibration gates" in result.gate["reasons"][0]


def test_compile_failure_candidate_is_rejected_not_fatal() -> None:
    baseline = make_manifest("wf")
    strong = make_manifest("wf")
    strong["nodes"]["agent"]["instructions"]["text"] = "strong answer"

    result = evaluate_workflow_calibration_candidates(
        baseline,
        [_candidate("cal-bad", {"schema_version": 1}), _candidate("cal-good", strong)],
        [_example(expected={"contains": "strong answer"})],
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.5}),
        resolver=fake_resolver(),
        baseline_executor=_InstructionExecutor(),
        candidate_executor=_InstructionExecutor(),
    )

    assert result.passed is True
    assert result.candidates[0].accepted is False
    assert "compile/replay failed" in (result.candidates[0].rejected_reason or "")
    assert result.winner is not None
    assert result.winner.candidate_id == "cal-good"


def test_calibration_supports_manifest_aware_candidate_executor_factory() -> None:
    baseline = make_manifest("wf")
    weak = make_manifest("wf")
    weak["name"] = "weak"
    strong = make_manifest("wf")
    strong["name"] = "strong"
    seen: list[str] = []

    def _factory(manifest_dict: dict[str, object]):
        name = str(manifest_dict.get("name") or "candidate")
        seen.append(name)
        return _FactoryInstructionExecutor(f"factory::{name}")

    result = evaluate_workflow_calibration_candidates(
        baseline,
        [_candidate("cal-weak", weak, "weak"), _candidate("cal-strong", strong, "strong")],
        [_example(expected={"contains": "factory::strong"})],
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.5}),
        resolver=fake_resolver(),
        baseline_executor=_InstructionExecutor(),
        candidate_executor=_InstructionExecutor(),
        candidate_executor_factory=_factory,
    )

    assert result.passed is True
    assert result.winner is not None
    assert result.winner.candidate_id == "cal-strong"
    assert seen == ["weak", "strong"]


def test_calibration_supports_injected_knowledge_runners() -> None:
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

    result = evaluate_workflow_calibration_candidates(
        manifest,
        [_candidate("cal-kb", manifest)],
        [_example(expected={"contains": "30 days"})],
        WorkflowCalibrationSpec(objective={"maximize": "quality", "epsilon": 0.0}),
        resolver=fake_resolver(),
        knowledge_query_runner=_knowledge_query_runner,
    )

    assert result.passed is True
    assert len(calls) == 2
    assert all(call["question"] == "refund?" for call in calls)
