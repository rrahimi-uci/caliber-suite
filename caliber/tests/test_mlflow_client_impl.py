"""Tests for ``MLflowAssessmentClientImpl`` against a stubbed MLflow SDK.

The production client is normally exercised only via the integration
suite (which needs a live MLflow server). These tests cover the parts
that *don't* need a server — the trace→assessment translation logic
that decides what lands on ``AssessmentInfo`` — by stubbing ``mlflow``
in :data:`sys.modules` with a minimal in-process double.

The bug this guards against (deep-review Finding 1):

* ``_to_assessment_info`` used to hardcode ``experiment_id=""``.
* The poller routes by experiment_id, so every production assessment
  silently dropped — CALIBER ran "healthy" while ingest was disabled.

The fix passes the trace's ``experiment_id`` into the per-assessment
translation. These tests pin both the happy path and the duck-typed
fallback (``trace.info.experiment_id`` vs. ``trace.experiment_id``).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypedDict

import pytest

# ---------------------------------------------------------------------------
# Stub MLflow before importing the Impl. The lazy ``import mlflow`` inside
# ``MLflowAssessmentClientImpl.list_assessments_since`` resolves whatever is
# in ``sys.modules`` at call time, so a per-test stub is sufficient.
# ---------------------------------------------------------------------------


@dataclass
class _StubFeedback:
    """One feedback-typed assessment exposed via ``trace.search_assessments``."""

    assessment_id: str
    trace_id: str
    create_time_ms: int
    rationale: str = ""
    metadata: dict[str, object] | None = None
    feedback: object | None = None


@dataclass
class _StubTraceInfo:
    experiment_id: str


@dataclass
class _StubTrace:
    """Production-shape trace: experiment_id lives under ``.info``."""

    info: _StubTraceInfo
    feedbacks: list[_StubFeedback]
    raise_on_search: bool = False

    def search_assessments(self, *, type: str) -> list[_StubFeedback]:
        if self.raise_on_search:
            raise RuntimeError("search_assessments boom")
        return self.feedbacks


@dataclass
class _StubTraceFlat:
    """Older-shape trace: experiment_id lives directly on the trace."""

    experiment_id: str
    feedbacks: list[_StubFeedback]

    def search_assessments(self, *, type: str) -> list[_StubFeedback]:
        return self.feedbacks


@dataclass
class _StubExperiment:
    experiment_id: str


class _StubMLflowState(TypedDict):
    traces: list[object]
    searched_experiment_ids: list[list[str]]
    experiments_by_name: dict[str, object]


@pytest.fixture
def stub_mlflow(monkeypatch: pytest.MonkeyPatch) -> Iterator[_StubMLflowState]:
    """Install a minimal ``mlflow`` stub in ``sys.modules`` and surface a
    mutable ``traces`` list so each test can program the returned data."""
    traces: list[object] = []
    searched_experiment_ids: list[list[str]] = []
    experiments_by_name: dict[str, object] = {}
    module = types.ModuleType("mlflow")

    def search_traces(
        *,
        experiment_ids: list[str],
        max_results: int,
        order_by: list[str],
        return_type: str,
    ) -> list[object]:
        _ = max_results, order_by, return_type
        searched_experiment_ids.append(experiment_ids)
        return list(traces)

    def get_experiment_by_name(name: str) -> object | None:
        return experiments_by_name.get(name)

    module.search_traces = search_traces  # type: ignore[attr-defined]
    module.get_experiment_by_name = get_experiment_by_name  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", module)
    yield {
        "traces": traces,
        "searched_experiment_ids": searched_experiment_ids,
        "experiments_by_name": experiments_by_name,
    }


def _import_impl() -> type:
    """Lazy import so the stub fixture runs before module import side effects."""
    from caliber.mlflow_client import MLflowAssessmentClientImpl

    return MLflowAssessmentClientImpl


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_impl_returns_empty_when_no_experiments(stub_mlflow: _StubMLflowState) -> None:
    _ = stub_mlflow
    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=[], since=datetime.now(timezone.utc)))
    assert out == []


def test_impl_populates_experiment_id_from_trace_info(
    stub_mlflow: _StubMLflowState,
) -> None:
    """The fix: ``experiment_id`` lands on ``AssessmentInfo``, not ``""``."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    stub_mlflow["traces"].append(
        _StubTrace(
            info=_StubTraceInfo(experiment_id="42"),
            feedbacks=[
                _StubFeedback(
                    assessment_id="a-1",
                    trace_id="tr-1",
                    create_time_ms=int(base.timestamp() * 1000) + 5_000,
                    rationale="bad answer",
                    metadata={"category": "hallucination"},
                ),
            ],
        )
    )
    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=["42"], since=base))
    assert len(out) == 1
    assert out[0].experiment_id == "42"
    assert out[0].assessment_id == "a-1"
    assert out[0].category == "hallucination"


def test_impl_falls_back_to_trace_experiment_id_attribute(
    stub_mlflow: _StubMLflowState,
) -> None:
    """When the trace shape exposes ``experiment_id`` directly (older
    MLflow or duck-typed clients), the impl still picks it up."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    stub_mlflow["traces"].append(
        _StubTraceFlat(
            experiment_id="101",
            feedbacks=[
                _StubFeedback(
                    assessment_id="a-1",
                    trace_id="tr-1",
                    create_time_ms=int(base.timestamp() * 1000) + 1_000,
                ),
            ],
        )
    )
    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=["101"], since=base))
    assert len(out) == 1
    assert out[0].experiment_id == "101"


def test_impl_resolves_experiment_names_and_routes_back_to_configured_name(
    stub_mlflow: _StubMLflowState,
) -> None:
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    stub_mlflow["experiments_by_name"]["exp-support"] = _StubExperiment(experiment_id="7")
    stub_mlflow["traces"].append(
        _StubTrace(
            info=_StubTraceInfo(experiment_id="7"),
            feedbacks=[
                _StubFeedback(
                    assessment_id="a-1",
                    trace_id="tr-1",
                    create_time_ms=int(base.timestamp() * 1000) + 1_000,
                ),
            ],
        )
    )

    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=["exp-support"], since=base))

    assert stub_mlflow["searched_experiment_ids"] == [["7"]]
    assert len(out) == 1
    assert out[0].experiment_id == "exp-support"


def test_impl_skips_unresolved_experiment_names_without_searching(
    stub_mlflow: _StubMLflowState,
) -> None:
    impl = _import_impl()()
    out = list(
        impl.list_assessments_since(
            experiment_ids=["exp-missing"],
            since=datetime.now(timezone.utc),
        )
    )
    assert out == []
    assert stub_mlflow["searched_experiment_ids"] == []


def test_impl_filters_old_assessments(stub_mlflow: _StubMLflowState) -> None:
    """Assessments at or before ``since`` are excluded — the poller's
    incremental checkpoint depends on this."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    since_ms = int(base.timestamp() * 1000)
    stub_mlflow["traces"].append(
        _StubTrace(
            info=_StubTraceInfo(experiment_id="1"),
            feedbacks=[
                _StubFeedback("a-old", "tr", since_ms - 1, "old"),
                _StubFeedback("a-eq", "tr", since_ms, "equal"),
                _StubFeedback("a-new", "tr", since_ms + 1, "new"),
            ],
        )
    )
    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=["1"], since=base))
    ids = {a.assessment_id for a in out}
    assert ids == {"a-new"}


def test_impl_swallows_trace_search_failure(stub_mlflow: _StubMLflowState) -> None:
    """A misbehaving trace doesn't break ingest for the others."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    good = _StubTrace(
        info=_StubTraceInfo(experiment_id="1"),
        feedbacks=[
            _StubFeedback("a-good", "tr-1", int(base.timestamp() * 1000) + 1_000),
        ],
    )
    broken = _StubTrace(
        info=_StubTraceInfo(experiment_id="2"),
        feedbacks=[],
        raise_on_search=True,
    )
    # Order matters: broken first, good second, so we know the loop
    # actually continues past the failure.
    stub_mlflow["traces"].extend([broken, good])
    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=["1", "2"], since=base))
    assert [a.assessment_id for a in out] == ["a-good"]


def test_impl_skips_assessments_with_no_create_time(
    stub_mlflow: _StubMLflowState,
) -> None:
    """An Assessment with ``create_time_ms=None`` can't be checkpointed
    against, so it's excluded."""
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    stub_mlflow["traces"].append(
        _StubTrace(
            info=_StubTraceInfo(experiment_id="1"),
            feedbacks=[
                _StubFeedback(
                    assessment_id="a-no-time",
                    trace_id="tr",
                    create_time_ms=0,
                ),
            ],
        )
    )
    # ``_StubFeedback.create_time_ms=0`` is < since_ms so it's already
    # filtered. Set since to epoch to verify the explicit ``None``
    # branch via duck-typing.
    stub_mlflow["traces"][0].feedbacks[0].create_time_ms = None  # type: ignore[attr-defined]
    impl = _import_impl()()
    out = list(impl.list_assessments_since(experiment_ids=["1"], since=base))
    assert out == []
