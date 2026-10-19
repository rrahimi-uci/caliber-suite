"""A workflow run that fails must leave an ERROR trace, not an OK one.

``execute`` reports a node failure through ``WorkflowRunResult(status="error")`` instead of
raising, so the root ``workflow.run`` span used to close cleanly. MLflow takes a trace's status
from its root span, so Operate > Observability listed every failed run as ``OK`` and its
Status = Error filter never found one. Cookbook labs 09 and 16 depend on that filter.
"""

from __future__ import annotations

import pytest

from caliber.observability.mlflow_tracing import Tracer, TraceSpan, set_tracer
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from tests.workflow_helpers import fake_resolver, make_manifest

from .test_mlflow_tracing import FakeMlflow, FakeSpan


class StatusSpan(FakeSpan):
    """A span double that records ``set_status`` the way MLflow's live span does."""

    status: str | None = None
    inputs: object = None
    outputs: object = None

    def set_status(self, status: str) -> None:
        self.status = status

    def set_inputs(self, value: object) -> None:
        self.inputs = value

    def set_outputs(self, value: object) -> None:
        self.outputs = value


class StatusMlflow(FakeMlflow):
    def start_span(self, *, name, span_type, attributes=None):  # type: ignore[no-untyped-def]
        cm = super().start_span(name=name, span_type=span_type, attributes=attributes)
        upgraded = StatusSpan(name, span_type, attributes)
        self.spans[-1] = upgraded
        cm._span = upgraded
        return cm


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    set_tracer(None)


def _plan(code: str) -> RuntimePlan:
    manifest = make_manifest()
    manifest["nodes"]["check"] = {
        "id": "check",
        "type": "python_code",
        "code": code,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    manifest["nodes"].pop("agent")
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "check", "map": {"msg": "input"}},
        {"id": "e2", "from": "check", "to": "final", "map": {"text": "response"}},
    ]
    resolver = fake_resolver()
    return RuntimePlan(
        ir=build_ir(parse_manifest(manifest), resolver, version="1"), resolver=resolver
    )


def _root(fake: StatusMlflow) -> StatusSpan:
    return next(s for s in fake.spans if s.name == "workflow.run")  # type: ignore[return-value]


def test_a_failed_run_marks_its_root_span_error() -> None:
    fake = StatusMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    result = execute(
        _plan('raise ValueError("amount must be a number")'), "x", executor=FakeWorkflowExecutor()
    )

    assert result.status == "error"
    root = _root(fake)
    assert root.status == "ERROR"
    assert root.attributes["caliber.status"] == "failed"
    assert "amount must be a number" in root.attributes["caliber.error"]


def test_a_completed_run_keeps_an_ok_root_span() -> None:
    fake = StatusMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    result = execute(_plan('return {"text": "fine"}'), "x", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    root = _root(fake)
    assert root.status is None
    assert root.attributes["caliber.status"] == "completed"


def test_mark_error_without_a_live_span_is_a_no_op() -> None:
    span = TraceSpan(name="inert")
    span.mark_error("boom")  # tracing off: nothing to mark, nothing raised
    assert span.attributes["caliber.status"] == "failed"
    assert span.attributes["caliber.error"] == "boom"


def test_mark_error_survives_a_span_that_refuses_the_status() -> None:
    class Refuses:
        def set_status(self, status: str) -> None:
            raise RuntimeError("closed span")

        def set_attribute(self, key: str, value: object) -> None:
            pass

    span = TraceSpan(name="refuses", _span=Refuses())
    span.mark_error()  # tracing never breaks a run
    assert span.attributes["caliber.status"] == "failed"
    assert "caliber.error" not in span.attributes


def test_the_root_span_records_the_run_input_and_its_output() -> None:
    """Without these, Observability shows an empty request and "Add to test set" has nothing."""
    fake = StatusMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    execute(_plan('return {"text": "fine"}'), "the ticket text", executor=FakeWorkflowExecutor())

    root = _root(fake)
    assert root.inputs == "the ticket text"
    assert root.outputs == "fine"


def test_a_failed_run_records_its_error_as_the_response() -> None:
    fake = StatusMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    execute(_plan('raise ValueError("bad ticket")'), "x", executor=FakeWorkflowExecutor())

    assert "bad ticket" in _root(fake).outputs


def test_the_recorded_input_is_redacted() -> None:
    from caliber.audit import configure_redactor
    from caliber.redaction import build_redactor

    configure_redactor(build_redactor(enabled=True, extra_patterns="", replacement="[REDACTED]"))
    try:
        fake = StatusMlflow()
        set_tracer(Tracer(mlflow_module=fake))
        execute(
            _plan('return {"text": "ok"}'), "mail jane@example.com", executor=FakeWorkflowExecutor()
        )
        assert "jane@example.com" not in _root(fake).inputs
    finally:
        configure_redactor(
            build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]")
        )


def test_set_io_without_a_live_span_or_with_a_refusing_one_is_a_no_op() -> None:
    TraceSpan(name="inert").set_io(inputs="x", outputs="y")

    class Refuses:
        def set_inputs(self, value: object) -> None:
            raise RuntimeError("closed span")

        set_outputs = None

    TraceSpan(name="refuses", _span=Refuses()).set_io(inputs="x", outputs="y")
