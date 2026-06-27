"""Write review-queue answers back onto traces as MLflow assessments.

Reviewer answers don't live off to the side — each one is written straight onto
the reviewed trace using the OSS assessment primitives (``mlflow.log_feedback``
for feedback questions, ``mlflow.log_expectation`` for ground-truth/expectation
questions), so the review output is immediately usable for evaluation, judge
alignment, and dataset building. This mirrors MLflow Review Queues' "answers are
never stored off to the side" behaviour without the Databricks-only labeling API.

The boundary mirrors :mod:`caliber.eval.dataset_sync`: a Protocol with a real
MLflow implementation and a fake for tests.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnswerWriteBack:
    """One reviewer answer to write onto a trace."""

    name: str
    value: Any
    target: str  # "feedback" | "expectation"
    rationale: str | None = None


class ReviewWriteBackClient(Protocol):
    """Boundary for persisting review answers onto a trace."""

    def write_answers(
        self, *, trace_id: str, answers: Sequence[AnswerWriteBack], user: str
    ) -> list[str]: ...


class MLflowReviewWriteBackClient:
    """Production client backed by ``mlflow.log_feedback`` / ``log_expectation``."""

    def write_answers(
        self, *, trace_id: str, answers: Sequence[AnswerWriteBack], user: str
    ) -> list[str]:
        import mlflow  # noqa: PLC0415
        from mlflow.entities import AssessmentSource  # noqa: PLC0415

        source = AssessmentSource(source_type="HUMAN", source_id=user)
        assessment_ids: list[str] = []
        for answer in answers:
            if answer.target == "expectation":
                assessment = mlflow.log_expectation(
                    trace_id=trace_id,
                    name=answer.name,
                    value=answer.value,
                    source=source,
                )
            else:
                assessment = mlflow.log_feedback(
                    trace_id=trace_id,
                    name=answer.name,
                    value=answer.value,
                    source=source,
                    rationale=answer.rationale,
                )
            assessment_id = _assessment_id(assessment)
            if assessment_id:
                assessment_ids.append(assessment_id)
        return assessment_ids


class FakeReviewWriteBackClient:
    """In-memory test double — records calls, returns deterministic ids."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def write_answers(
        self, *, trace_id: str, answers: Sequence[AnswerWriteBack], user: str
    ) -> list[str]:
        self.calls.append(
            {
                "trace_id": trace_id,
                "user": user,
                "answers": [
                    {"name": a.name, "value": a.value, "target": a.target} for a in answers
                ],
            }
        )
        return [f"asmt-{trace_id}-{i}" for i, _ in enumerate(answers)]


def _assessment_id(assessment: Any) -> str | None:
    for attr in ("assessment_id", "id"):
        value = getattr(assessment, attr, None)
        if value:
            return str(value)
    return None
