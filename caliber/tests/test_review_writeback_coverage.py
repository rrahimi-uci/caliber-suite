"""Unit tests for :class:`MLflowReviewWriteBackClient`.

The production client lazily ``import mlflow`` / ``from mlflow.entities import
AssessmentSource`` inside :meth:`write_answers`, so those imports resolve
whatever lives in :data:`sys.modules` at call time. We install a minimal
in-process MLflow stub there — never touching a real MLflow server — and assert
the client maps each :class:`AnswerWriteBack` to the right ``log_expectation`` /
``log_feedback`` call, collects assessment ids (including the ``id`` fallback),
and tolerates assessments that expose no id at all.

The stub mirrors the pattern in ``tests/test_mlflow_client_impl.py``.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from caliber.review.writeback import (
    AnswerWriteBack,
    MLflowReviewWriteBackClient,
    _assessment_id,
)


@dataclass
class _RecordedCall:
    kind: str  # "expectation" | "feedback"
    kwargs: dict[str, Any]


@dataclass
class _StubAssessment:
    """An object whose id is exposed via ``assessment_id`` (production shape)."""

    assessment_id: str


@dataclass
class _StubAssessmentIdOnly:
    """Older shape: only the generic ``id`` attribute carries the id."""

    id: str


@dataclass
class _StubAssessmentNoId:
    """An assessment that surfaces no usable id at all."""

    note: str = "no id here"


@dataclass
class _StubAssessmentSource:
    source_type: str
    source_id: str


@dataclass
class _StubMLflowState:
    calls: list[_RecordedCall] = field(default_factory=list)
    # Pre-programmed return values, consumed in order across all log_* calls.
    returns: list[Any] = field(default_factory=list)
    # If set, the next log_* call raises this instead of recording.
    raise_on_call: BaseException | None = None
    sources: list[_StubAssessmentSource] = field(default_factory=list)


@pytest.fixture
def stub_mlflow(monkeypatch: pytest.MonkeyPatch) -> Iterator[_StubMLflowState]:
    state = _StubMLflowState()

    module = types.ModuleType("mlflow")
    entities = types.ModuleType("mlflow.entities")

    def _next_return() -> Any:
        # Pop from the front so tests program returns in call order.
        if state.returns:
            return state.returns.pop(0)
        return _StubAssessment(assessment_id="default-id")

    def log_expectation(**kwargs: Any) -> Any:
        if state.raise_on_call is not None:
            raise state.raise_on_call
        state.calls.append(_RecordedCall(kind="expectation", kwargs=kwargs))
        return _next_return()

    def log_feedback(**kwargs: Any) -> Any:
        if state.raise_on_call is not None:
            raise state.raise_on_call
        state.calls.append(_RecordedCall(kind="feedback", kwargs=kwargs))
        return _next_return()

    def _assessment_source(*, source_type: str, source_id: str) -> _StubAssessmentSource:
        src = _StubAssessmentSource(source_type=source_type, source_id=source_id)
        state.sources.append(src)
        return src

    module.log_expectation = log_expectation  # type: ignore[attr-defined]
    module.log_feedback = log_feedback  # type: ignore[attr-defined]
    entities.AssessmentSource = _assessment_source  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mlflow", module)
    monkeypatch.setitem(sys.modules, "mlflow.entities", entities)
    yield state


def test_write_answers_routes_expectation_and_feedback(stub_mlflow: _StubMLflowState) -> None:
    stub_mlflow.returns = [
        _StubAssessment(assessment_id="exp-1"),
        _StubAssessment(assessment_id="fb-1"),
    ]
    client = MLflowReviewWriteBackClient()

    ids = client.write_answers(
        trace_id="tr-42",
        answers=[
            AnswerWriteBack(
                name="expected_answer",
                value="Paris",
                target="expectation",
            ),
            AnswerWriteBack(
                name="helpfulness",
                value="yes",
                target="feedback",
                rationale="clear and correct",
            ),
        ],
        user="@reviewer",
    )

    assert ids == ["exp-1", "fb-1"]
    assert [c.kind for c in stub_mlflow.calls] == ["expectation", "feedback"]

    exp_call, fb_call = stub_mlflow.calls
    assert exp_call.kwargs == {
        "trace_id": "tr-42",
        "name": "expected_answer",
        "value": "Paris",
        "source": stub_mlflow.sources[0],
    }
    # log_expectation must NOT receive a rationale kwarg.
    assert "rationale" not in exp_call.kwargs

    assert fb_call.kwargs == {
        "trace_id": "tr-42",
        "name": "helpfulness",
        "value": "yes",
        "source": stub_mlflow.sources[0],
        "rationale": "clear and correct",
    }

    # Exactly one HUMAN source built, carrying the reviewer id.
    assert len(stub_mlflow.sources) == 1
    assert stub_mlflow.sources[0].source_type == "HUMAN"
    assert stub_mlflow.sources[0].source_id == "@reviewer"


def test_write_answers_defaults_to_feedback_for_unknown_target(
    stub_mlflow: _StubMLflowState,
) -> None:
    # Any target other than the literal "expectation" falls through to feedback.
    stub_mlflow.returns = [_StubAssessment(assessment_id="fb-x")]
    client = MLflowReviewWriteBackClient()

    ids = client.write_answers(
        trace_id="tr-7",
        answers=[AnswerWriteBack(name="q", value=1, target="something-else")],
        user="@u",
    )

    assert ids == ["fb-x"]
    assert [c.kind for c in stub_mlflow.calls] == ["feedback"]
    # rationale defaults to None and is still passed through.
    assert stub_mlflow.calls[0].kwargs["rationale"] is None


def test_write_answers_uses_id_fallback_and_skips_idless(
    stub_mlflow: _StubMLflowState,
) -> None:
    # Three answers: one with assessment_id, one with only `id`, one with neither.
    stub_mlflow.returns = [
        _StubAssessment(assessment_id="a-1"),
        _StubAssessmentIdOnly(id="a-2"),
        _StubAssessmentNoId(),
    ]
    client = MLflowReviewWriteBackClient()

    ids = client.write_answers(
        trace_id="tr-99",
        answers=[
            AnswerWriteBack(name="n1", value="v1", target="feedback"),
            AnswerWriteBack(name="n2", value="v2", target="feedback"),
            AnswerWriteBack(name="n3", value="v3", target="feedback"),
        ],
        user="@u",
    )

    # The id-less assessment contributes nothing; the other two are collected.
    assert ids == ["a-1", "a-2"]
    assert len(stub_mlflow.calls) == 3


def test_write_answers_empty_answers_makes_no_calls(
    stub_mlflow: _StubMLflowState,
) -> None:
    client = MLflowReviewWriteBackClient()

    ids = client.write_answers(trace_id="tr-0", answers=[], user="@u")

    assert ids == []
    assert stub_mlflow.calls == []
    # No source is needed when there is nothing to write... but the client
    # still constructs one up front; just assert no log_* was invoked.


def test_write_answers_propagates_mlflow_error(
    stub_mlflow: _StubMLflowState,
) -> None:
    stub_mlflow.raise_on_call = RuntimeError("mlflow exploded")
    client = MLflowReviewWriteBackClient()

    with pytest.raises(RuntimeError, match="mlflow exploded"):
        client.write_answers(
            trace_id="tr-err",
            answers=[AnswerWriteBack(name="n", value="v", target="feedback")],
            user="@u",
        )


def test_assessment_id_prefers_assessment_id_then_id_then_none() -> None:
    assert _assessment_id(_StubAssessment(assessment_id="primary")) == "primary"
    assert _assessment_id(_StubAssessmentIdOnly(id="secondary")) == "secondary"
    assert _assessment_id(_StubAssessmentNoId()) is None
    # Falsy values (empty string) are treated as "no id".
    assert _assessment_id(_StubAssessment(assessment_id="")) is None
    # Non-string ids are coerced to str.
    assert _assessment_id(_StubAssessmentIdOnly(id=123)) == "123"  # type: ignore[arg-type]
