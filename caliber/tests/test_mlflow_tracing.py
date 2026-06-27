"""Tests for the shared MLflow tracing helper (golden-path roadmap, Wave 0).

Covers: PII-redacted/byte-capped sanitization, per-model cost, the guarded
``Tracer`` (span + run) against a fake MLflow module, exception propagation
(traced code must never have its errors swallowed), token/cost recording,
``configure_tracing`` + autolog gating, and the no-op path when MLflow is absent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caliber.audit import configure_redactor
from caliber.observability import mlflow_tracing as mt
from caliber.observability.mlflow_tracing import (
    Tracer,
    _enable_autolog,
    configure_tracing,
    get_tracer,
    model_cost_usd,
    sanitize_trace_attributes,
    sanitize_trace_value,
    set_tracer,
)
from caliber.redaction import build_redactor


@pytest.fixture(autouse=True)
def _redactor():
    """Enable the PII redactor for the duration of each test, then reset."""
    configure_redactor(build_redactor(enabled=True, extra_patterns="", replacement="[REDACTED]"))
    yield
    configure_redactor(build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]"))


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    set_tracer(None)


# --------------------------------------------------------------------------- #
# Fake MLflow doubles
# --------------------------------------------------------------------------- #


class FakeSpan:
    def __init__(self, name: str, span_type: str, attributes: dict | None) -> None:
        self.name = name
        self.span_type = span_type
        self.attributes = dict(attributes or {})
        self.trace_id = "trace-abc"

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class _SpanCM:
    def __init__(self, span: FakeSpan, exits: list) -> None:
        self._span = span
        self._exits = exits

    def __enter__(self) -> FakeSpan:
        return self._span

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._exits.append((self._span.name, exc_type))
        return False


class _Run:
    def __init__(self, run_id: str) -> None:
        self.info = SimpleNamespace(run_id=run_id)


class _RunCM:
    def __init__(self, run: _Run, exits: list) -> None:
        self._run = run
        self._exits = exits

    def __enter__(self) -> _Run:
        return self._run

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._exits.append(exc_type)
        return False


class FakeMlflow:
    def __init__(self, *, active: _Run | None = None) -> None:
        self.spans: list[FakeSpan] = []
        self.span_exits: list = []
        self.started_runs: list[tuple[str | None, dict | None]] = []
        self.run_exits: list = []
        self.experiments: list[str] = []
        self._active = active

    def start_span(self, *, name: str, span_type: str, attributes: dict | None = None) -> _SpanCM:
        span = FakeSpan(name, span_type, attributes)
        self.spans.append(span)
        return _SpanCM(span, self.span_exits)

    def start_run(self, *, run_name: str | None = None, tags: dict | None = None) -> _RunCM:
        run = _Run(f"run-{len(self.started_runs) + 1}")
        self.started_runs.append((run_name, tags))
        return _RunCM(run, self.run_exits)

    def active_run(self) -> _Run | None:
        return self._active

    def set_experiment(self, name: str) -> None:
        self.experiments.append(name)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_model_cost_exact_prefix_and_unknown() -> None:
    assert model_cost_usd("gpt-4o", prompt_tokens=1000, completion_tokens=1000) == 0.0125
    assert model_cost_usd("gpt-4o-mini", prompt_tokens=1000, completion_tokens=1000) == 0.00075
    assert model_cost_usd("gpt-5.4", prompt_tokens=1000, completion_tokens=1000) == 0.0175
    # Versioned id resolves to its family by longest-prefix match.
    assert model_cost_usd("gpt-4o-2024-08-06", prompt_tokens=1000, completion_tokens=0) == 0.0025
    # Unknown / empty model never fabricates a cost.
    assert model_cost_usd("llama-3-70b", prompt_tokens=9999, completion_tokens=9999) == 0.0
    assert model_cost_usd("", prompt_tokens=1000, completion_tokens=1000) == 0.0


def test_model_cost_uses_cached_prompt_discount_when_available() -> None:
    assert (
        model_cost_usd(
            "gpt-4o",
            prompt_tokens=1000,
            cached_prompt_tokens=500,
            completion_tokens=1000,
        )
        == 0.011875
    )


def test_sanitize_redacts_pii() -> None:
    out = sanitize_trace_value("reach me at jane@example.com please")
    assert "jane@example.com" not in out
    assert "[REDACTED]" in out


def test_sanitize_byte_caps_long_strings() -> None:
    out = sanitize_trace_value("x" * 500, max_bytes=32)
    assert out.endswith("...[truncated]")
    assert len(out.encode("utf-8")) <= 32


def test_sanitize_passthrough_scalars_and_encodes_dicts() -> None:
    assert sanitize_trace_value(7) == 7
    assert sanitize_trace_value(True) is True
    assert sanitize_trace_value(None) is None
    encoded = sanitize_trace_value({"k": "v" * 200}, max_bytes=40)
    assert isinstance(encoded, str)
    assert len(encoded.encode("utf-8")) <= 40


def test_sanitize_attributes_stringifies_keys() -> None:
    attrs = sanitize_trace_attributes({1: "a", "b": 2})
    assert attrs == {"1": "a", "b": 2}


# --------------------------------------------------------------------------- #
# Tracer.span
# --------------------------------------------------------------------------- #


def test_disabled_tracer_is_noop_even_with_module() -> None:
    fake = FakeMlflow()
    tracer = Tracer(enabled=False, mlflow_module=fake)
    with tracer.span("agent.x") as span:
        span.set_attribute("k", "v")
    assert fake.spans == []  # no MLflow span created
    assert span.attributes["k"] == "v"  # still recorded locally
    assert span.attributes["caliber.status"] == "completed"


def test_span_creates_sanitized_span_and_records() -> None:
    fake = FakeMlflow()
    tracer = Tracer(mlflow_module=fake)
    with tracer.span(
        "agent.greeter",
        span_type="AGENT",
        attributes={"node": "n1", "leak": "ssn 123-45-6789 / a@b.com"},
    ) as span:
        span.set_attribute("tool", "lookup_order")

    assert len(fake.spans) == 1
    created = fake.spans[0]
    assert created.name == "agent.greeter"
    assert created.span_type == "AGENT"
    # Start attributes were redacted before reaching MLflow.
    assert "a@b.com" not in created.attributes["leak"]
    # Subsequent set_attribute forwarded to the live span + final status.
    assert created.attributes["tool"] == "lookup_order"
    assert created.attributes["caliber.status"] == "completed"
    assert fake.span_exits == [("agent.greeter", None)]


def test_span_propagates_exception_and_marks_failed() -> None:
    fake = FakeMlflow()
    tracer = Tracer(mlflow_module=fake)
    with pytest.raises(ValueError, match="boom"):
        with tracer.span("agent.x") as span:
            raise ValueError("boom")
    assert span.attributes["caliber.status"] == "failed"
    assert span.attributes["caliber.error_type"] == "ValueError"
    # The span context manager was closed WITH the exception.
    assert fake.span_exits[-1][0] == "agent.x"
    assert fake.span_exits[-1][1] is ValueError


def test_span_noop_when_mlflow_absent() -> None:
    tracer = Tracer(mlflow_module=None)
    with tracer.span("s") as span:
        span.set_attribute("a", 1)
    assert span.attributes["a"] == 1
    assert span.mlflow_trace_id is None


def test_span_survives_start_span_failure() -> None:
    class Boom(FakeMlflow):
        def start_span(self, **kwargs):  # type: ignore[override]
            raise RuntimeError("mlflow exploded")

    tracer = Tracer(mlflow_module=Boom())
    # Must not raise — tracing failure cannot break the traced code.
    with tracer.span("s") as span:
        span.set_attribute("a", 1)
    assert span.attributes["a"] == 1


# --------------------------------------------------------------------------- #
# Tracer.trace_run
# --------------------------------------------------------------------------- #


def test_trace_run_starts_run_with_sanitized_tags() -> None:
    fake = FakeMlflow(active=None)
    tracer = Tracer(mlflow_module=fake)
    with tracer.trace_run("wf.run", tags={"caliber.kind": "workflow", "who": "a@b.com"}) as handle:
        pass
    assert handle.run_id == "run-1"
    run_name, tags = fake.started_runs[0]
    assert run_name == "wf.run"
    assert "a@b.com" not in tags["who"]


def test_trace_run_reuses_active_run() -> None:
    fake = FakeMlflow(active=_Run("existing-run"))
    tracer = Tracer(mlflow_module=fake)
    with tracer.trace_run("wf.run") as handle:
        pass
    assert handle.run_id == "existing-run"
    assert fake.started_runs == []  # did not nest a new run


def test_trace_run_propagates_body_exception() -> None:
    fake = FakeMlflow(active=None)
    tracer = Tracer(mlflow_module=fake)
    with pytest.raises(RuntimeError, match="kaboom"):
        with tracer.trace_run("wf.run"):
            raise RuntimeError("kaboom")
    assert fake.run_exits[-1] is RuntimeError  # run closed with the exception


def test_trace_run_noop_when_mlflow_absent() -> None:
    tracer = Tracer(mlflow_module=None)
    with tracer.trace_run("wf.run") as handle:
        pass
    assert handle.run_id is None


def test_trace_run_sets_experiment() -> None:
    fake = FakeMlflow(active=None)
    tracer = Tracer(mlflow_module=fake, experiment="caliber/support")
    with tracer.trace_run("wf.run"):
        pass
    assert fake.experiments == ["caliber/support"]


# --------------------------------------------------------------------------- #
# Token / cost recording
# --------------------------------------------------------------------------- #


def test_record_usage_tokens_and_cost() -> None:
    fake = FakeMlflow()
    tracer = Tracer(mlflow_module=fake)
    with tracer.span("agent.x") as span:
        cost = span.record_usage(prompt_tokens=1000, completion_tokens=1000, model="gpt-4o")
    assert cost == 0.0125
    assert span.attributes["caliber.tokens"] == 2000
    assert span.attributes["caliber.prompt_tokens"] == 1000
    assert span.attributes["caliber.completion_tokens"] == 1000
    assert span.attributes["caliber.cost_usd"] == 0.0125


def test_record_usage_records_cached_prompt_tokens() -> None:
    fake = FakeMlflow()
    tracer = Tracer(mlflow_module=fake)
    with tracer.span("agent.x") as span:
        cost = span.record_usage(
            prompt_tokens=1000,
            completion_tokens=1000,
            cached_prompt_tokens=500,
            model="gpt-4o",
        )
    assert cost == 0.011875
    assert span.attributes["caliber.cached_prompt_tokens"] == 500
    assert span.attributes["caliber.cost_usd"] == 0.011875


def test_record_usage_total_only_no_cost() -> None:
    fake = FakeMlflow()
    tracer = Tracer(mlflow_module=fake)
    with tracer.span("agent.x") as span:
        cost = span.record_usage(total_tokens=512, model="mystery-model")
    assert cost == 0.0
    assert span.attributes["caliber.tokens"] == 512
    assert "caliber.cost_usd" not in span.attributes
    assert "caliber.prompt_tokens" not in span.attributes


# --------------------------------------------------------------------------- #
# configure_tracing / autolog
# --------------------------------------------------------------------------- #


def test_configure_tracing_sets_singleton_and_respects_flags(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(mt, "_enable_autolog", lambda module: calls.append(module) or ["openai"])

    cfg = SimpleNamespace(
        tracing_enabled=True,
        tracing_autolog_enabled=True,
        tracing_max_attribute_bytes=1024,
        tracing_experiment="",
    )
    tracer = configure_tracing(cfg)
    assert tracer.enabled is True
    assert tracer.max_attribute_bytes == 1024
    assert get_tracer() is tracer
    assert len(calls) == 1  # autolog attempted


def test_configure_tracing_skips_autolog_when_disabled(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(mt, "_enable_autolog", lambda module: calls.append(module))

    cfg = SimpleNamespace(
        tracing_enabled=True,
        tracing_autolog_enabled=False,
        tracing_max_attribute_bytes=4096,
        tracing_experiment="",
    )
    configure_tracing(cfg)
    assert calls == []  # autolog NOT attempted


def test_configure_tracing_disabled_is_inert(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(mt, "_enable_autolog", lambda module: calls.append(module))
    cfg = SimpleNamespace(
        tracing_enabled=False,
        tracing_autolog_enabled=True,
        tracing_max_attribute_bytes=4096,
        tracing_experiment="",
    )
    tracer = configure_tracing(cfg)
    assert tracer.enabled is False
    assert tracer.mlflow_module() is None
    assert calls == []


def test_enable_autolog_none_module_is_noop() -> None:
    assert _enable_autolog(None) == []


def test_enable_autolog_only_patches_installed_providers(monkeypatch) -> None:
    autologged: list[str] = []

    real_import = mt.importlib.import_module

    def fake_import(name: str):
        if name == "openai":
            return SimpleNamespace()  # pretend installed
        if name == "anthropic":
            raise ImportError("not installed")
        if name == "mlflow.openai":
            return SimpleNamespace(autolog=lambda: autologged.append("openai"))
        if name == "mlflow.anthropic":  # pragma: no cover - should not be reached
            return SimpleNamespace(autolog=lambda: autologged.append("anthropic"))
        return real_import(name)

    monkeypatch.setattr(mt.importlib, "import_module", fake_import)
    enabled = _enable_autolog(object())  # module truthy; submodules resolved via import
    assert enabled == ["openai"]
    assert autologged == ["openai"]


# --------------------------------------------------------------------------- #
# Multimodal attachments (MLflow 3.12+ mlflow.tracing.attachments)
# --------------------------------------------------------------------------- #


class _SpanWithInputs(FakeSpan):
    """A fake span that also captures ``set_inputs`` for attachment tests."""

    def __init__(self, name: str, span_type: str, attributes: dict | None) -> None:
        super().__init__(name, span_type, attributes)
        self.inputs: dict | None = None

    def set_inputs(self, value: dict) -> None:
        self.inputs = value


class _MlflowWithInputSpans(FakeMlflow):
    def start_span(self, *, name: str, span_type: str, attributes: dict | None = None) -> _SpanCM:
        span = _SpanWithInputs(name, span_type, attributes)
        self.spans.append(span)
        return _SpanCM(span, self.span_exits)


def test_attach_uploads_bytes_as_span_input_and_marker() -> None:
    fake = _MlflowWithInputSpans()
    tracer = Tracer(mlflow_module=fake)
    with tracer.span("tool.extract_document") as span:
        ok = span.attach("source.pdf", b"%PDF-1.4 fake", "application/pdf")
    assert ok is True
    captured = fake.spans[0]
    # The attachment is set as a span input under the given name…
    assert "source.pdf" in (captured.inputs or {})
    attachment = captured.inputs["source.pdf"]
    assert getattr(attachment, "content_type", None) == "application/pdf"
    # …and a plain-text marker is recorded so the redacted viewer can surface it.
    assert captured.attributes.get("caliber.attachment.source.pdf") == "application/pdf"


def test_attach_accumulates_multiple_files_without_clobbering() -> None:
    fake = _MlflowWithInputSpans()
    tracer = Tracer(mlflow_module=fake)
    with tracer.span("tool.extract_document") as span:
        span.attach("page1.png", b"\x89PNG fake", "image/png")
        span.attach("page2.png", b"\x89PNG fake2", "image/png")
    inputs = fake.spans[0].inputs or {}
    assert set(inputs) == {"page1.png", "page2.png"}


def test_attach_is_noop_when_tracing_disabled() -> None:
    tracer = Tracer(enabled=False, mlflow_module=_MlflowWithInputSpans())
    with tracer.span("tool.extract_document") as span:
        assert span.attach("source.pdf", b"data", "application/pdf") is False
