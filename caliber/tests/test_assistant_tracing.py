"""Tests for assistant MLflow/no-op tracing helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

import caliber.assistant.tracing as tracing_mod
from caliber import audit
from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import (
    IntentExecuteRequest,
    IntentPlanRequest,
    MessageSendRequest,
    SessionCreateRequest,
)
from caliber.assistant.service import AssistantService
from caliber.assistant.tracing import AssistantTracer, sanitize_trace_attributes
from caliber.observability.trace import bind_trace_id
from caliber.redaction import build_redactor

USER = "@trace-test"


class _FakeSpan:
    def __init__(self, *, name: str, trace_id: str, attributes: dict[str, Any]) -> None:
        self.name = name
        self.trace_id = trace_id
        self.attributes = dict(attributes)

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _FakeSpanContext:
    def __init__(self, owner: _FakeMlflow, span: _FakeSpan) -> None:
        self._owner = owner
        self._span = span

    def __enter__(self) -> _FakeSpan:
        self._owner.entered.append(self._span)
        return self._span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self._owner.exited.append(self._span)
        return False


class _FakeMlflow:
    def __init__(self) -> None:
        self.entered: list[_FakeSpan] = []
        self.exited: list[_FakeSpan] = []

    def start_span(
        self,
        *,
        name: str,
        span_type: str,
        attributes: dict[str, Any],
    ) -> _FakeSpanContext:
        assert span_type == "CHAIN"
        span = _FakeSpan(
            name=name,
            trace_id=f"mltrace-{len(self.entered) + 1}",
            attributes=attributes,
        )
        return _FakeSpanContext(self, span)

    def active_run(self) -> SimpleNamespace:
        return SimpleNamespace(info=SimpleNamespace(run_id="mlrun-123"))


class _NoStartMlflow:
    def active_run(self) -> SimpleNamespace:
        return SimpleNamespace(info=SimpleNamespace(run_id="mlrun-no-span-api"))


class _RaisingStartMlflow:
    def start_span(self, **_: Any) -> object:
        raise RuntimeError("start span failed")

    def active_run(self) -> SimpleNamespace:
        return SimpleNamespace(info=SimpleNamespace(run_id="mlrun-fallback"))


class _ExplodingSpan:
    def __init__(self) -> None:
        self.context_called = False

    def context(self) -> SimpleNamespace:
        self.context_called = True
        return SimpleNamespace(request_id="ctx-request")

    def set_attribute(self, key: str, value: Any) -> None:
        raise RuntimeError(f"cannot set {key}={value}")


class _BadClosingContext:
    def __init__(self) -> None:
        self.span = _ExplodingSpan()

    def __enter__(self) -> _ExplodingSpan:
        return self.span

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        raise RuntimeError("close failed")


class _BadClosingMlflow:
    def __init__(self) -> None:
        self.context = _BadClosingContext()

    def start_span(self, **_: Any) -> _BadClosingContext:
        return self.context

    def active_run(self) -> SimpleNamespace:
        return SimpleNamespace(info=SimpleNamespace(run_id="mlrun-close"))


def test_sanitize_trace_attributes_redacts_and_truncates() -> None:
    previous = audit.get_redactor()
    audit.configure_redactor(
        build_redactor(enabled=True, extra_patterns=r"sk-[A-Za-z0-9]+"),
    )
    try:
        attrs = sanitize_trace_attributes(
            {
                "email": "alice@example.com",
                "token": "sk-secretvalue",
                "payload": {"template": "x" * 500},
            },
            max_bytes=64,
        )
    finally:
        audit.configure_redactor(previous)

    assert attrs["email"] == "[REDACTED]"
    assert attrs["token"] == "[REDACTED]"
    assert isinstance(attrs["payload"], str)
    assert attrs["payload"].endswith("...[truncated]")
    assert len(attrs["payload"].encode("utf-8")) <= 64


def test_assistant_tracer_noops_when_mlflow_absent() -> None:
    tracer = AssistantTracer(mlflow_module=None)

    with tracer.span(
        "caliber.assistant.noop",
        trace_id="trace-noop",
        correlation_id="acorr-noop",
        attributes={"payload": {"email": "alice@example.com"}},
    ) as span:
        span.set_attribute("caliber.assistant.result_type", "noop")

    assert span.mlflow_trace_id is None
    assert span.mlflow_run_id is None
    assert span.attributes["caliber.trace_id"] == "trace-noop"
    assert span.attributes["caliber.assistant.result_type"] == "noop"


def test_assistant_tracer_marks_failed_spans_without_mlflow() -> None:
    tracer = AssistantTracer(mlflow_module=None)
    ctx = tracer.span(
        "caliber.assistant.failed",
        trace_id="trace-failed",
        correlation_id="corr-failed",
    )

    span = ctx.__enter__()
    assert ctx.__exit__(ValueError, ValueError("bad input"), None) is False

    assert span.attributes["caliber.status"] == "failed"
    assert span.attributes["caliber.error_type"] == "ValueError"


def test_assistant_tracer_uses_active_run_when_span_api_is_missing() -> None:
    tracer = AssistantTracer(mlflow_module=_NoStartMlflow())

    with tracer.span(
        "caliber.assistant.no_span_api",
        trace_id="trace-no-span",
        correlation_id="corr-no-span",
    ) as span:
        pass

    assert span.mlflow_trace_id is None
    assert span.mlflow_run_id == "mlrun-no-span-api"
    assert span.attributes["caliber.status"] == "completed"


def test_assistant_tracer_recovers_when_start_span_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracer = AssistantTracer(mlflow_module=_RaisingStartMlflow())

    with caplog.at_level(logging.DEBUG, logger="caliber.assistant.tracing"):
        with tracer.span(
            "caliber.assistant.start_failure",
            trace_id="trace-start-failure",
            correlation_id="corr-start-failure",
        ) as span:
            span.set_attribute("custom", "ok")

    assert span.mlflow_trace_id is None
    assert span.mlflow_run_id == "mlrun-fallback"
    assert span.attributes["custom"] == "ok"
    assert "unable to open assistant MLflow span" in caplog.text


def test_assistant_tracer_logs_attribute_and_close_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_mlflow = _BadClosingMlflow()
    tracer = AssistantTracer(mlflow_module=fake_mlflow)

    with caplog.at_level(logging.DEBUG, logger="caliber.assistant.tracing"):
        with tracer.span(
            "caliber.assistant.close_failure",
            trace_id="trace-close-failure",
            correlation_id="corr-close-failure",
        ) as span:
            span.set_attribute("custom", {"value": "kept locally"})

    assert span.mlflow_trace_id == "ctx-request"
    assert span.mlflow_run_id == "mlrun-close"
    assert span.attributes["custom"] == '{"value": "kept locally"}'
    assert fake_mlflow.context.span.context_called is True
    assert "failed setting assistant span attribute custom" in caplog.text
    assert "failed closing assistant MLflow span" in caplog.text


def test_assistant_tracer_import_and_active_run_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def import_missing(name: str) -> object:
        assert name == "mlflow"
        raise ImportError("missing")

    monkeypatch.setattr(tracing_mod.importlib, "import_module", import_missing)
    assert AssistantTracer().mlflow_module() is None

    def import_broken(name: str) -> object:
        assert name == "mlflow"
        raise RuntimeError("broken import")

    monkeypatch.setattr(tracing_mod.importlib, "import_module", import_broken)
    with caplog.at_level(logging.DEBUG, logger="caliber.assistant.tracing"):
        assert AssistantTracer().mlflow_module() is None
    assert "unable to import MLflow for assistant tracing" in caplog.text

    assert AssistantTracer(mlflow_module=SimpleNamespace()).active_run_id() is None

    def active_run_raises() -> object:
        raise RuntimeError("run lookup failed")

    with caplog.at_level(logging.DEBUG, logger="caliber.assistant.tracing"):
        assert (
            AssistantTracer(
                mlflow_module=SimpleNamespace(active_run=active_run_raises)
            ).active_run_id()
            is None
        )
    assert "failed reading active MLflow run" in caplog.text
    assert (
        AssistantTracer(
            mlflow_module=SimpleNamespace(
                active_run=lambda: SimpleNamespace(info=SimpleNamespace(run_id=""))
            )
        ).active_run_id()
        is None
    )


def test_assistant_tracer_extracts_trace_ids_from_span_variants() -> None:
    tracer = AssistantTracer(mlflow_module=None)

    assert tracer.extract_mlflow_trace_id(SimpleNamespace(request_id="request-id")) == "request-id"
    assert (
        tracer.extract_mlflow_trace_id(
            SimpleNamespace(context=lambda: SimpleNamespace(trace_id="context-trace"))
        )
        == "context-trace"
    )

    def broken_context() -> object:
        raise RuntimeError("context unavailable")

    assert tracer.extract_mlflow_trace_id(SimpleNamespace(context=broken_context)) is None
    assert tracer.extract_mlflow_trace_id(None) is None


def test_execute_intent_plan_emits_fake_mlflow_spans(
    session_factory: sessionmaker[Session],
) -> None:
    fake_mlflow = _FakeMlflow()
    svc = AssistantService(
        engine=FakeAssistantEngine(),
        tracer=AssistantTracer(mlflow_module=fake_mlflow),
    )
    sid = svc.create_session(
        SessionCreateRequest(
            title="trace", metadata_={"prompt_ref": "prompts:/support-agent@prod"}
        ),
        session_factory=session_factory,
        user=USER,
    ).session_id
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="generate_test_cases",
            slot_overrides={"prompt_name": "support-agent"},
        ),
        session_factory=session_factory,
        user=USER,
    )

    with bind_trace_id("trace-execute"):
        executed = svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
            session_factory=session_factory,
            user=USER,
        )

    span_names = [span.name for span in fake_mlflow.entered]
    assert "caliber.assistant.create_intent_plan" in span_names
    assert "caliber.assistant.execute_intent" in span_names
    assert "caliber.assistant.adapter.generate_test_cases" in span_names
    assert executed.result["mlflow_trace_id"] == "mltrace-3"
    assert executed.result["mlflow_run_id"] == "mlrun-123"
    assert executed.run is not None
    assert executed.run.mlflow_run_id == "mlrun-123"

    adapter_span = fake_mlflow.entered[-1]
    assert adapter_span.attributes["caliber.assistant.result_type"] == "test_cases"
    assert adapter_span.attributes["caliber.trace_id"] == "trace-execute"


def test_send_message_emits_fake_mlflow_span(
    session_factory: sessionmaker[Session],
) -> None:
    fake_mlflow = _FakeMlflow()
    svc = AssistantService(
        engine=FakeAssistantEngine(),
        tracer=AssistantTracer(mlflow_module=fake_mlflow),
    )
    sid = svc.create_session(
        SessionCreateRequest(title="trace-send"),
        session_factory=session_factory,
        user=USER,
    ).session_id

    with bind_trace_id("trace-send"):
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="Build me a tool"),
            session_factory=session_factory,
            user=USER,
        )

    assert turn.run is not None
    assert turn.run.mlflow_run_id == "mlrun-123"
    assert fake_mlflow.entered[0].name == "caliber.assistant.send_message"
    assert fake_mlflow.entered[0].attributes["caliber.assistant.question_count"] == len(
        turn.questions
    )
    assert fake_mlflow.entered[0].attributes["caliber.trace_id"] == "trace-send"
