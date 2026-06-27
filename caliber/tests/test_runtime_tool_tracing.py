"""Tests for per-tool-call MLflow spans in the workflow runtime (Wave 1).

Exercises ``_invoke_agent_tools`` — the shared seam used by both the fake and
OpenAI executors — asserting each tool call emits a ``TOOL`` span with name,
redacted input/output, latency, and correct status, that tracing is inert by
default, and that tool errors propagate while being recorded.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caliber.audit import configure_redactor
from caliber.observability.mlflow_tracing import Tracer, get_tracer, set_tracer
from caliber.redaction import build_redactor
from caliber.workflows.runtime import _invoke_agent_tools

from .test_mlflow_tracing import FakeMlflow


@pytest.fixture(autouse=True)
def _redactor():
    configure_redactor(build_redactor(enabled=True, extra_patterns="", replacement="[REDACTED]"))
    yield
    configure_redactor(build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]"))


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    set_tracer(None)


def _agent(*tool_names: str) -> SimpleNamespace:
    return SimpleNamespace(tools=[SimpleNamespace(local_name=name) for name in tool_names])


def test_default_tracer_is_inert() -> None:
    # When unset, get_tracer() constructs an inert tracer so instrumented code
    # paths are no-ops in any context that has not opted in.
    set_tracer(None)
    assert get_tracer().enabled is False


def test_tool_call_emits_span_with_latency_and_io() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    calls = {"lookup_order": lambda text: {"status": "delivered", "echo": text}}
    result = _invoke_agent_tools(_agent("lookup_order"), "where is order 7", calls)

    assert result == [
        {"tool": "lookup_order", "result": {"status": "delivered", "echo": "where is order 7"}}
    ]
    assert len(fake.spans) == 1
    span = fake.spans[0]
    assert span.name == "tool.lookup_order"
    assert span.span_type == "TOOL"
    assert span.attributes["caliber.tool"] == "lookup_order"
    assert span.attributes["caliber.tool.input"] == "where is order 7"
    assert "caliber.tool.output" in span.attributes
    assert isinstance(span.attributes["caliber.tool.latency_ms"], float)
    assert span.attributes["caliber.tool.latency_ms"] >= 0.0
    assert span.attributes["caliber.status"] == "completed"


def test_tool_input_output_are_redacted() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    calls = {"echo": lambda text: f"received {text}"}

    _invoke_agent_tools(_agent("echo"), "my email is jane@example.com", calls)

    span = fake.spans[0]
    assert "jane@example.com" not in span.attributes["caliber.tool.input"]
    assert "jane@example.com" not in str(span.attributes["caliber.tool.output"])


def test_zero_arg_tool_falls_back() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    calls = {"now": lambda: "2026-06-08"}  # takes no args → TypeError then fn()

    result = _invoke_agent_tools(_agent("now"), "ignored", calls)

    assert result == [{"tool": "now", "result": "2026-06-08"}]
    assert fake.spans[0].attributes["caliber.status"] == "completed"


def test_missing_callable_is_skipped() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    result = _invoke_agent_tools(_agent("absent"), "x", {})
    assert result == []
    assert fake.spans == []


def test_tool_error_propagates_and_records_failed() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    def boom(_text: str) -> str:
        raise ValueError("tool blew up")

    with pytest.raises(ValueError, match="tool blew up"):
        _invoke_agent_tools(_agent("boom"), "x", {"boom": boom})

    span = fake.spans[0]
    assert span.attributes["caliber.status"] == "failed"
    assert span.attributes["caliber.error_type"] == "ValueError"
    # Latency is still recorded even on failure (set in the finally).
    assert "caliber.tool.latency_ms" in span.attributes


def test_inert_tracer_still_runs_tools() -> None:
    set_tracer(Tracer(mlflow_module=None))  # tracing off
    calls = {"t": lambda text: text.upper()}
    result = _invoke_agent_tools(_agent("t"), "hi", calls)
    assert result == [{"tool": "t", "result": "HI"}]


def test_multiple_tools_each_get_a_span() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    calls = {"a": lambda t: 1, "b": lambda t: 2}
    result = _invoke_agent_tools(_agent("a", "b"), "x", calls)
    assert [c["tool"] for c in result] == ["a", "b"]
    assert [s.name for s in fake.spans] == ["tool.a", "tool.b"]
