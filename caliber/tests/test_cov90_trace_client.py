"""Focused coverage tests for ``caliber.trace_client``.

Targets the branches left uncovered by the existing suite (span-tree edge
cases, MLflow-metadata parsing helpers, assessment extraction, and the
``fetch_trace_detail`` / ``fetch_trace_spans`` guarded error paths). Follows
the mocking convention from ``test_trace_client_spans.py``: no real MLflow
server or network — the only external boundary mocked is the ``mlflow``
module itself, via ``monkeypatch.setitem(sys.modules, "mlflow", fake)`` (for
code that does a function-local ``import mlflow``) so a fresh, ad-hoc fake
module can be swapped in per test.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from caliber.trace_client import (
    FakeTraceClient,
    TraceDetail,
    TraceSummary,
    _assessments,
    _mlflow_trace_url,
    _num_attr,
    _rollup_usage,
    _span_attributes,
    _span_status,
    _span_time_ns,
    _trace_state,
    _trace_tags,
    fetch_trace_detail,
    fetch_trace_spans,
    trace_metadata_cost,
    trace_metadata_tokens,
)


class _ExplodingDataTrace:
    """A trace whose ``.data`` access raises, to exercise mapping-error paths."""

    info = SimpleNamespace(
        state=None,
        status="OK",
        tags={},
        trace_metadata={},
        request_preview="",
        response_preview="",
        request_time=None,
        execution_duration=None,
        assessments=None,
    )

    @property
    def data(self) -> Any:
        raise RuntimeError("data explosion")


class _RaisingAttrModule:
    """A fake ``mlflow`` module whose ``get_tracking_uri`` attribute access raises.

    ``getattr(obj, name, default)`` only swallows ``AttributeError``; any other
    exception raised from ``__getattr__`` propagates straight through, which is
    how this reaches the ``except Exception`` guard wrapped around
    ``_mlflow_trace_url`` in ``fetch_trace_detail`` / ``fetch_trace_spans``.
    """

    def __init__(self, trace: Any) -> None:
        self._trace = trace

    def get_trace(self, trace_id: str, silent: bool = True) -> Any:
        return self._trace

    def __getattr__(self, name: str) -> Any:
        if name == "get_tracking_uri":
            raise RuntimeError("attribute lookup boom")
        raise AttributeError(name)


# ───────────────────── FakeTraceClient.add ─────────────────────


def test_fake_trace_client_add_stores_and_returns_summary() -> None:
    client = FakeTraceClient()
    summary = TraceSummary(status="OK", span_count=1)

    client.add("T1", summary)

    assert client.get_trace_summary("T1") is summary
    assert client.get_trace_summary("missing") is None


# ───────────────────── _span_time_ns ─────────────────────


def test_span_time_ns_none_when_attribute_missing() -> None:
    span = SimpleNamespace()
    assert _span_time_ns(span, "start_time_ns") is None


def test_span_time_ns_none_when_value_not_numeric() -> None:
    span = SimpleNamespace(start_time_ns="not-a-number")
    assert _span_time_ns(span, "start_time_ns") is None


# ───────────────────── _span_status ─────────────────────


def test_span_status_falls_back_to_status_when_no_status_code_attr() -> None:
    # ``status`` is a plain string (no ``.status_code``): code falls back to
    # the status value itself.
    span = SimpleNamespace(status="OK")
    assert _span_status(span) == "OK"


def test_span_status_unknown_when_status_missing() -> None:
    span = SimpleNamespace()
    assert _span_status(span) == "UNKNOWN"


# ───────────────────── _span_attributes ─────────────────────


def test_span_attributes_keeps_raw_string_when_not_json() -> None:
    span = SimpleNamespace(attributes={"caliber.note": "not valid json {"})
    result = _span_attributes(span)
    assert result["caliber.note"] == "not valid json {"


def test_span_attributes_empty_when_not_a_dict() -> None:
    span = SimpleNamespace(attributes=None)
    assert _span_attributes(span) == {}


# ───────────────────── _mlflow_trace_url ─────────────────────


def test_mlflow_trace_url_none_when_get_tracking_uri_missing() -> None:
    mod = SimpleNamespace()  # no get_tracking_uri attribute at all
    assert _mlflow_trace_url(mod, "T1") is None


def test_mlflow_trace_url_none_when_get_tracking_uri_raises() -> None:
    def _boom() -> str:
        raise RuntimeError("boom")

    mod = SimpleNamespace(get_tracking_uri=_boom)
    assert _mlflow_trace_url(mod, "T1") is None


# ───────────────────── trace_metadata_tokens ─────────────────────


def test_trace_metadata_tokens_invalid_json_returns_none() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.tokenUsage": "not-json"})
    assert trace_metadata_tokens(info) is None


def test_trace_metadata_tokens_dict_without_known_key_returns_none() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.tokenUsage": json.dumps({"unrelated": 1})})
    assert trace_metadata_tokens(info) is None


def test_trace_metadata_tokens_extracts_total_tokens() -> None:
    info = SimpleNamespace(
        trace_metadata={"mlflow.trace.tokenUsage": json.dumps({"total_tokens": 42})}
    )
    assert trace_metadata_tokens(info) == 42


def test_trace_metadata_tokens_non_dict_json_returns_none() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.tokenUsage": json.dumps([1, 2, 3])})
    assert trace_metadata_tokens(info) is None


# ───────────────────── trace_metadata_cost ─────────────────────


def test_trace_metadata_cost_none_when_absent() -> None:
    info = SimpleNamespace(trace_metadata={})
    assert trace_metadata_cost(info) is None


def test_trace_metadata_cost_numeric_raw_returned_directly() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.cost": 1.5})
    assert trace_metadata_cost(info) == 1.5


def test_trace_metadata_cost_invalid_json_returns_none() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.cost": "nope"})
    assert trace_metadata_cost(info) is None


def test_trace_metadata_cost_json_numeric_string() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.cost": "2.25"})
    assert trace_metadata_cost(info) == 2.25


def test_trace_metadata_cost_dict_with_total_cost() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.cost": json.dumps({"total_cost": 3.5})})
    assert trace_metadata_cost(info) == 3.5


def test_trace_metadata_cost_dict_without_match_returns_none() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.cost": json.dumps({"unrelated": "x"})})
    assert trace_metadata_cost(info) is None


def test_trace_metadata_cost_non_dict_json_returns_none() -> None:
    info = SimpleNamespace(trace_metadata={"mlflow.trace.cost": json.dumps(["a", "b"])})
    assert trace_metadata_cost(info) is None


# ───────────────────── _trace_state / _trace_tags / _num_attr ─────────────────────


def test_trace_state_falls_back_to_status_when_state_missing() -> None:
    info = SimpleNamespace(state=None, status="ERROR")
    assert _trace_state(info) == "ERROR"


def test_trace_tags_empty_when_not_a_dict() -> None:
    info = SimpleNamespace(tags=None)
    assert _trace_tags(info) == {}


def test_num_attr_returns_none_for_bool_value() -> None:
    # bool is a subclass of int; must be rejected explicitly.
    assert _num_attr({"caliber.tokens": True}, "caliber.tokens") is None


# ───────────────────── _rollup_usage ─────────────────────


def test_rollup_usage_skips_spans_without_attributes_dict() -> None:
    spans: list[dict[str, Any]] = [
        {"attributes": None},
        {"no_attributes_key": True},
        "not-even-a-dict",  # type: ignore[list-item]
    ]
    usage = _rollup_usage(spans)
    assert usage.total_tokens is None
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.cost_usd is None


def test_rollup_usage_sums_numeric_attributes_across_spans() -> None:
    spans = [
        {"attributes": {"caliber.tokens": 10, "caliber.cost_usd": 0.01}},
        {"attributes": {"caliber.tokens": True}},  # bool must not count as tokens
        {"attributes": {"caliber.tokens": 5, "caliber.cost_usd": 0.02}},
    ]
    usage = _rollup_usage(spans)
    assert usage.total_tokens == 15
    assert usage.cost_usd == 0.03


# ───────────────────── _assessments ─────────────────────


def test_assessments_falls_back_to_search_assessments_when_info_empty() -> None:
    info = SimpleNamespace(assessments=None)
    assessment = SimpleNamespace(value="good", rationale="", source=None, name="quality")
    trace = SimpleNamespace(info=info, search_assessments=lambda: [assessment])

    result = _assessments(trace)

    assert result == [{"name": "quality", "value": "good", "rationale": None, "source": None}]


def test_assessments_swallows_search_assessments_errors() -> None:
    info = SimpleNamespace(assessments=None)

    def _boom() -> list[Any]:
        raise RuntimeError("search boom")

    trace = SimpleNamespace(info=info, search_assessments=_boom)

    assert _assessments(trace) == []


def test_assessments_falls_back_to_feedback_value_when_value_none() -> None:
    feedback = SimpleNamespace(value="from-feedback")
    assessment = SimpleNamespace(
        value=None, feedback=feedback, rationale="r", source=None, name="n"
    )
    info = SimpleNamespace(assessments=[assessment])
    trace = SimpleNamespace(info=info)

    result = _assessments(trace)

    assert result[0]["value"] == "from-feedback"
    assert result[0]["rationale"] == "r"


def test_assessments_stringifies_non_primitive_value() -> None:
    assessment = SimpleNamespace(value={"complex": True}, rationale=None, source=None, name="n")
    info = SimpleNamespace(assessments=[assessment])
    trace = SimpleNamespace(info=info)

    result = _assessments(trace)

    assert result[0]["value"] == str({"complex": True})


def test_assessments_uses_source_type_when_source_id_absent() -> None:
    source = SimpleNamespace(source_id=None, source_type="HUMAN")
    assessment = SimpleNamespace(value="ok", rationale=None, source=source, name="n")
    info = SimpleNamespace(assessments=[assessment])
    trace = SimpleNamespace(info=info)

    result = _assessments(trace)

    assert result[0]["source"] == "HUMAN"


def test_assessments_skips_malformed_entry() -> None:
    class _ExplodingAssessment:
        @property
        def value(self) -> Any:
            raise RuntimeError("boom")

    info = SimpleNamespace(assessments=[_ExplodingAssessment()])
    trace = SimpleNamespace(info=info)

    assert _assessments(trace) == []


# ───────────────────── fetch_trace_detail ─────────────────────


def test_fetch_trace_detail_empty_when_no_trace_id() -> None:
    assert fetch_trace_detail(None) == TraceDetail(trace_id=None)
    assert fetch_trace_detail("") == TraceDetail(trace_id=None)


def test_fetch_trace_detail_swallows_get_trace_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(trace_id: str, silent: bool = True) -> Any:
        raise RuntimeError("get_trace boom")

    fake_mlflow = SimpleNamespace(get_trace=_boom)
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    detail = fetch_trace_detail("T1")

    assert detail == TraceDetail(trace_id="T1")


def test_fetch_trace_detail_empty_when_trace_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mlflow = SimpleNamespace(get_trace=lambda trace_id, silent=True: None)
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    detail = fetch_trace_detail("T1")

    assert detail == TraceDetail(trace_id="T1")


def test_fetch_trace_detail_swallows_mapping_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mlflow = SimpleNamespace(get_trace=lambda trace_id, silent=True: _ExplodingDataTrace())
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    detail = fetch_trace_detail("T1")

    assert detail == TraceDetail(trace_id="T1")


def test_fetch_trace_detail_swallows_mlflow_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    info = SimpleNamespace(
        state=None,
        status="OK",
        tags={},
        trace_metadata={},
        request_preview="",
        response_preview="",
        request_time=None,
        execution_duration=None,
        assessments=None,
    )
    data = SimpleNamespace(spans=[], request=None, response=None)
    trace = SimpleNamespace(data=data, info=info)
    fake_mlflow = _RaisingAttrModule(trace)
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    detail = fetch_trace_detail("T1")

    assert detail.trace_id == "T1"
    assert detail.mlflow_url is None


def test_fetch_trace_detail_handles_non_numeric_time_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = SimpleNamespace(
        state=None,
        status="OK",
        tags={},
        trace_metadata={},
        request_preview="",
        response_preview="",
        request_time="not-a-number",
        execution_duration="also-bad",
        assessments=None,
    )
    data = SimpleNamespace(spans=[], request="q", response="a")
    trace = SimpleNamespace(data=data, info=info)
    fake_mlflow = SimpleNamespace(
        get_trace=lambda trace_id, silent=True: trace, get_tracking_uri=lambda: ""
    )
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    detail = fetch_trace_detail("T1")

    assert detail.request_time_ms is None
    assert detail.execution_time_ms is None


# ───────────────────── fetch_trace_spans ─────────────────────


def test_fetch_trace_spans_swallows_mapping_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mlflow = SimpleNamespace(get_trace=lambda trace_id, silent=True: _ExplodingDataTrace())
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    tree = fetch_trace_spans("T1")

    assert tree.trace_id == "T1"
    assert tree.spans == []


def test_fetch_trace_spans_swallows_mlflow_url_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    data = SimpleNamespace(spans=[])
    trace = SimpleNamespace(data=data)
    fake_mlflow = _RaisingAttrModule(trace)
    monkeypatch.setitem(sys.modules, "mlflow", fake_mlflow)

    tree = fetch_trace_spans("T1")

    assert tree.trace_id == "T1"
    assert tree.spans == []
    assert tree.mlflow_url is None
