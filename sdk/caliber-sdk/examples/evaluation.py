"""Build evidence, define a judge, and score against it."""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def build_and_score(caliber: CaliberClient, *, owner: str = "@you") -> dict[str, Any]:
    """Create a dataset, add a row, define a judge, and run an evaluation."""
    dataset = caliber.eval_datasets.create("intake-golden", owner=owner)
    caliber.eval_datasets.add_example(
        dataset.dataset_id,
        input={"ticket": "I was charged twice"},
        expected={"intent": "billing"},
    )

    # Instructions must reference an evaluation variable. A judge with no
    # variable grades nothing — it returns the same verdict every time — so
    # the server rejects it rather than letting you collect meaningless scores.
    judge = caliber.judges.create(
        "valid-intent",
        instructions="Given {{ inputs }} and {{ outputs }}, return true if intent is allowed.",
        feedback_value_type="bool",
    )

    # A judge is selected as a scorer by name (``Judge.<id>``), not by a bare
    # ``judge_id`` field -- the request schema has no such field and rejects it.
    evaluation = caliber.evaluations.create(dataset.dataset_id, scorers=[f"Judge.{judge.judge_id}"])
    return {
        "dataset_id": dataset.dataset_id,
        "judge_id": judge.judge_id,
        "evaluation_id": evaluation.evaluation_id,
    }
