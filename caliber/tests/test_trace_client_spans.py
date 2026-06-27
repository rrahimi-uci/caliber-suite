"""Tests for the span-tree mapping that powers the in-app trace viewer.

Exercises :func:`caliber.trace_client.map_trace_to_spans` and
:func:`caliber.trace_client.fetch_trace_spans` against fake MLflow trace objects
(no live MLflow server): spans with parent links map to a flat list with
``parent_id`` (the FE builds the tree), nanosecond timestamps become
millisecond durations, IO is redacted + byte-capped, and a missing trace /
absent MLflow degrades to an empty tree.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from caliber.audit import configure_redactor
from caliber.redaction import build_redactor
from caliber.trace_client import fetch_trace_spans, map_trace_to_spans


@pytest.fixture(autouse=True)
def _redactor():
    configure_redactor(build_redactor(enabled=True, extra_patterns="", replacement="[REDACTED]"))
    yield
    configure_redactor(build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]"))


def _span(
    *,
    span_id: str,
    parent_id: str | None,
    name: str,
    span_type: str,
    start_ns: int,
    end_ns: int,
    status: str = "OK",
    inputs: Any = None,
    outputs: Any = None,
    attributes: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        span_id=span_id,
        parent_id=parent_id,
        name=name,
        span_type=span_type,
        start_time_ns=start_ns,
        end_time_ns=end_ns,
        status=SimpleNamespace(status_code=SimpleNamespace(value=status)),
        inputs=inputs,
        outputs=outputs,
        attributes=attributes or {},
    )


def _trace(spans: list[Any]) -> SimpleNamespace:
    return SimpleNamespace(data=SimpleNamespace(spans=spans))


def test_map_trace_to_spans_builds_parent_links_and_durations() -> None:
    trace = _trace(
        [
            _span(
                span_id="s-root",
                parent_id=None,
                name="workflow.run",
                span_type="CHAIN",
                start_ns=1_000_000_000,
                end_ns=3_000_000_000,
            ),
            _span(
                span_id="s-agent",
                parent_id="s-root",
                name="agent.greeter",
                span_type="AGENT",
                start_ns=1_500_000_000,
                end_ns=2_500_000_000,
            ),
            _span(
                span_id="s-tool",
                parent_id="s-agent",
                name="tool.lookup",
                span_type="TOOL",
                start_ns=1_700_000_000,
                end_ns=1_900_000_000,
            ),
        ]
    )

    spans = map_trace_to_spans(trace)
    assert len(spans) == 3
    by_id = {s["span_id"]: s for s in spans}

    assert by_id["s-root"]["parent_id"] is None
    assert by_id["s-agent"]["parent_id"] == "s-root"
    assert by_id["s-tool"]["parent_id"] == "s-agent"

    # 2s root, 1s agent, 0.2s tool -> millisecond durations.
    assert by_id["s-root"]["duration_ms"] == 2000.0
    assert by_id["s-agent"]["duration_ms"] == 1000.0
    assert by_id["s-tool"]["duration_ms"] == 200.0

    assert by_id["s-agent"]["span_type"] == "AGENT"
    assert by_id["s-agent"]["status"] == "OK"
    # Sorted by start time -> root first.
    assert spans[0]["span_id"] == "s-root"


def test_map_trace_to_spans_redacts_io_and_attributes() -> None:
    trace = _trace(
        [
            _span(
                span_id="s1",
                parent_id=None,
                name="agent.x",
                span_type="AGENT",
                start_ns=0,
                end_ns=1_000_000,
                inputs={"email": "person@example.com hello"},
                outputs="contact me at other@example.com",
                attributes={"caliber.tool.input": '"call 555-867-5309 now"'},
            ),
        ]
    )

    spans = map_trace_to_spans(trace)
    payload = str(spans[0]["inputs"]) + str(spans[0]["outputs"]) + str(spans[0]["attributes"])
    assert "person@example.com" not in payload
    assert "other@example.com" not in payload
    assert "[REDACTED]" in payload


def test_map_trace_to_spans_handles_empty_and_malformed() -> None:
    assert map_trace_to_spans(None) == []
    assert map_trace_to_spans(_trace([])) == []
    # A trace missing the .data attribute but exposing .spans directly.
    assert map_trace_to_spans(SimpleNamespace(spans=[])) == []


def test_fetch_trace_spans_empty_when_no_trace_id() -> None:
    tree = fetch_trace_spans(None)
    assert tree.trace_id is None
    assert tree.spans == []


def test_fetch_trace_spans_maps_monkeypatched_mlflow(monkeypatch: pytest.MonkeyPatch) -> None:
    trace = _trace(
        [
            _span(
                span_id="s-root",
                parent_id=None,
                name="workflow.run",
                span_type="CHAIN",
                start_ns=0,
                end_ns=5_000_000,
            )
        ]
    )

    fake_mlflow = SimpleNamespace(
        get_trace=lambda trace_id, silent=True: trace,
        get_tracking_uri=lambda: "http://localhost:5000",
    )
    monkeypatch.setitem(__import__("sys").modules, "mlflow", fake_mlflow)

    tree = fetch_trace_spans("trace-xyz")
    assert tree.trace_id == "trace-xyz"
    assert len(tree.spans) == 1
    assert tree.spans[0]["name"] == "workflow.run"
    assert tree.mlflow_url == "http://localhost:5000/#/traces/trace-xyz"


def test_fetch_trace_spans_empty_when_trace_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mlflow = SimpleNamespace(
        get_trace=lambda trace_id, silent=True: None,
        get_tracking_uri=lambda: "",
    )
    monkeypatch.setitem(__import__("sys").modules, "mlflow", fake_mlflow)

    tree = fetch_trace_spans("trace-missing")
    assert tree.trace_id == "trace-missing"
    assert tree.spans == []
    assert tree.mlflow_url is None


def test_fetch_trace_spans_swallows_mlflow_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(trace_id: str, silent: bool = True) -> Any:
        raise RuntimeError("mlflow exploded")

    fake_mlflow = SimpleNamespace(get_trace=_boom, get_tracking_uri=lambda: "")
    monkeypatch.setitem(__import__("sys").modules, "mlflow", fake_mlflow)

    tree = fetch_trace_spans("trace-err")
    assert tree.spans == []
