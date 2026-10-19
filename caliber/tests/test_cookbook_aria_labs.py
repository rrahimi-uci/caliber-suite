"""Cookbooks 12 to 15 (the Aria labs): their data files agree with the real planner and capabilities.

The labs' scripts are run against a live server (see each README) and, offline, against a stand-in
(``sdk/caliber-sdk/tests/test_cookbook_1*_example.py``). These tests pin the files to the *real*
code with no server: every goal in ``assets/dataset/goals.jsonl`` is planned by the shipped
``HeuristicPlanner`` and must draw the steps the file says, every form spec is a valid input for the
capability it feeds (so a typo in a spec fails here, not in front of a reader), and the rows the labs
load into test sets are well formed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caliber.assistant.capabilities import get_capability, registered_capabilities
from caliber.assistant.executor import _validate_capability_inputs
from caliber.assistant.plans import HeuristicPlanner
from caliber.schemas import (
    EvalDatasetCreateRequest,
    JudgeCreateRequest,
    ReviewQueueCreateRequest,
)

COOKBOOKS = Path(__file__).resolve().parents[2] / "docs-site" / "cookbooks"
LABS = {
    "12": "12-aria-evaluation-harness",
    "13": "13-aria-review-governance-queue",
    "14": "14-aria-governance-starter-kit",
    "15": "15-aria-triage-recalibrate-loop",
}
GOAL_COUNTS = {"12": 28, "13": 26, "14": 28, "15": 28}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _form(path: Path) -> dict[str, Any]:
    values = json.loads(path.read_text("utf-8"))
    values.pop("notes", None)
    return values


def _assets(lab: str) -> Path:
    return COOKBOOKS / LABS[lab] / "assets"


@pytest.mark.parametrize("lab", sorted(LABS))
def test_every_goal_draws_the_plan_the_dataset_says(lab: str) -> None:
    goals = _jsonl(_assets(lab) / "dataset" / "goals.jsonl")
    assert len(goals) == GOAL_COUNTS[lab]
    assert len({g["id"] for g in goals}) == len(goals)
    assert len({g["goal"] for g in goals}) == len(goals)
    planner = HeuristicPlanner()
    capabilities = registered_capabilities()
    for goal in goals:
        drawn = [s.capability_key for s in planner.plan(goal["goal"], capabilities=capabilities)]
        assert drawn == goal["expected_steps"], f"{lab} {goal['id']}: {goal['goal']}"
        assert goal["note"], goal["id"]
    assert {g["outcome"] for g in goals} <= {"as_asked", "unasked", "missed", "nothing"}
    # "nothing" is exactly the goals that draw no steps, so none of them can be approved.
    assert {g["id"] for g in goals if not g["expected_steps"]} == {
        g["id"] for g in goals if g["outcome"] == "nothing"
    }


@pytest.mark.parametrize("lab", sorted(LABS))
def test_the_canonical_intent_is_goal_g01_and_draws_the_expected_plan(lab: str) -> None:
    assets = _assets(lab)
    expected = json.loads((assets / "expected-plan.json").read_text("utf-8"))
    first = _jsonl(assets / "dataset" / "goals.jsonl")[0]
    assert first["id"] == "G01" and first["goal"] == expected["goal"]
    assert first["expected_steps"] == [s["capability_key"] for s in expected["expected_steps"]]
    assert f"> {expected['goal']}" in (assets / "intent.md").read_text("utf-8")
    assert expected["autonomy"] == "ask_each"


def test_no_planned_step_wires_a_name_only_a_reference() -> None:
    """The planner used to hand a step the previous step's ``name`` (labs 12 and 14)."""
    steps = HeuristicPlanner().plan(
        "a judge, an eval dataset, a review queue and a workflow",
        capabilities=registered_capabilities(),
    )
    for step in steps:
        assert all(field.endswith("_id") for field in step.inputs), step.capability_key
        assert "name" not in step.inputs


FORMS = [
    ("12", "judges/answer-faithfulness.judge.json", "judge.create"),
    ("12", "datasets/support-faithfulness.dataset.json", "eval_dataset.create"),
    ("13", "review-queues/agent-reply-review.queue.json", "review_queue.create"),
    ("14", "judges/answer-faithfulness.judge.json", "judge.create"),
    ("14", "review-queues/governance-review.queue.json", "review_queue.create"),
    ("14", "datasets/release-candidates.dataset.json", "eval_dataset.create"),
    ("15", "review-queues/triage-review.queue.json", "review_queue.create"),
]


@pytest.mark.parametrize(("lab", "relative", "capability"), FORMS)
def test_every_form_spec_is_a_valid_input_for_its_capability(
    lab: str, relative: str, capability: str
) -> None:
    values = _form(_assets(lab) / relative)
    cap = get_capability(capability)
    assert cap is not None
    # What the plan's form accepts (schema subset) ...
    _validate_capability_inputs(cap, values)
    # ... and what the capability then builds from it.
    if capability == "judge.create":
        JudgeCreateRequest.model_validate(values)
        assert "\n" not in values["instructions"]  # the form's field is a single line
    elif capability == "review_queue.create":
        ReviewQueueCreateRequest.model_validate(values)
    else:
        EvalDatasetCreateRequest.model_validate({**values, "owner": "@reza"})


def test_the_enqueue_and_calibrate_specs_fit_their_capabilities_once_filled() -> None:
    enqueue = _form(_assets("13") / "enqueue.json")
    cap = get_capability("review_queue.add_items")
    assert cap is not None
    _validate_capability_inputs(cap, {**enqueue, "queue_id": "RVQ-1", "trace_ids": ["tr-1"]})
    assert set(enqueue) == {"trace_ids", "assigned_to"}
    assert set(_form(_assets("15") / "enqueue.json")) == {"trace_ids", "assigned_to"}
    calibrate = _form(_assets("15") / "calibrate.json")
    cap = get_capability("workflow.calibrate")
    assert cap is not None
    _validate_capability_inputs(cap, {"workflow_id": "WF-1", "agent_id": "agent-1"})
    assert set(calibrate) == {"workflow_id", "agent_id"}


def test_the_rows_loaded_into_test_sets_are_well_formed() -> None:
    from caliber.schemas import EvalExampleCreateRequest

    files = {
        "12": ("dataset/faithfulness-cases.jsonl", 30),
        "14": ("dataset/release-candidate-cases.jsonl", 30),
    }
    for lab, (relative, count) in files.items():
        rows = _jsonl(_assets(lab) / relative)
        assert len(rows) == count
        assert len({r["id"] for r in rows}) == count
        assert len({r["input"]["question"] for r in rows}) == count
        for row in rows:
            EvalExampleCreateRequest.model_validate(
                {"input": row["input"], "expected": row["expected"], "tags": row["tags"]}
            )
    runs = _jsonl(_assets("15") / "dataset" / "support-runs.jsonl")
    assert len(runs) == 30 and len({r["question"] for r in runs}) == 30
    for run in runs:
        EvalExampleCreateRequest.model_validate(
            {
                "input": {"input": run["question"]},
                "expected": {"expected": run["reference"], "required_tools": run["required_tools"]},
                "tags": ["weak" if run["weak"] else "ok"],
            }
        )


def test_the_review_answers_in_the_lab_13_dataset_fit_the_queue_schema() -> None:
    queue = ReviewQueueCreateRequest.model_validate(
        _form(_assets("13") / "review-queues" / "agent-reply-review.queue.json")
    )
    by_key = {q.key: q for q in queue.questions}
    replies = _jsonl(_assets("13") / "dataset" / "agent-replies.jsonl")
    assert len(replies) == 30
    for reply in replies:
        answers = reply["review"]
        assert set(answers) <= set(by_key)
        assert {k for k, q in by_key.items() if q.required} <= set(answers)
        for key, value in answers.items():
            question = by_key[key]
            if question.type == "pass_fail":
                assert isinstance(value, bool)
            elif question.type == "categorical":
                assert value in question.options
            elif question.type == "text":
                assert isinstance(value, str)
