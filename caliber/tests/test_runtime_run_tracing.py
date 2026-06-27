"""Tests for the root workflow-run trace + per-agent spans (Wave 1, increment 3).

Asserts ``execute`` opens a root MLflow run/span and populates
``WorkflowRunResult.mlflow_run_id`` / ``mlflow_trace_id``, that ``_run_agent_traced``
records model/tokens/cost on an ``AGENT`` span, that an already-active run is
reused (not nested), and that tracing is fully inert when unconfigured.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from caliber.audit import configure_redactor
from caliber.observability.mlflow_tracing import Tracer, set_tracer
from caliber.redaction import build_redactor
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    AgentTurnResult,
    FakeWorkflowExecutor,
    RuntimePlan,
    _run_agent_traced,
    execute,
)
from tests.workflow_helpers import fake_resolver, make_manifest

from .test_mlflow_tracing import FakeMlflow, _Run


@pytest.fixture(autouse=True)
def _redactor():
    configure_redactor(build_redactor(enabled=True, extra_patterns="", replacement="[REDACTED]"))
    yield
    configure_redactor(build_redactor(enabled=False, extra_patterns="", replacement="[REDACTED]"))


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    set_tracer(None)


def _plan(manifest: dict) -> RuntimePlan:
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest), resolver, version="7")
    return RuntimePlan(ir=ir, resolver=resolver)


def test_execute_opens_root_run_and_populates_linkage() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    result = execute(_plan(make_manifest()), "hello", executor=FakeWorkflowExecutor())

    assert result.status == "completed"
    assert result.mlflow_run_id == "run-1"  # a root run was opened
    assert result.mlflow_trace_id == "trace-abc"
    assert fake.started_runs  # start_run was called
    names = [s.name for s in fake.spans]
    assert "workflow.run" in names
    assert any(n.startswith("agent.") for n in names)


def test_execute_reuses_active_run() -> None:
    fake = FakeMlflow(active=_Run("outer-run"))
    set_tracer(Tracer(mlflow_module=fake))

    result = execute(_plan(make_manifest()), "hi", executor=FakeWorkflowExecutor())

    assert result.mlflow_run_id == "outer-run"
    assert fake.started_runs == []  # reused the enclosing run, did not nest


def test_execute_is_inert_without_tracing() -> None:
    set_tracer(Tracer(mlflow_module=None))
    result = execute(_plan(make_manifest()), "hello", executor=FakeWorkflowExecutor())
    assert result.status == "completed"
    assert result.mlflow_run_id is None
    assert result.mlflow_trace_id is None


def test_run_agent_traced_records_model_tokens_cost() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    agent = SimpleNamespace(node_id="greeter", name="Greeter", model="gpt-4o")

    class _Exec:
        def run_agent(self, agent, input_text, *, tool_callables, preview):
            return AgentTurnResult(
                final_output="hi",
                tokens=30,
                prompt_tokens=10,
                completion_tokens=20,
                cached_prompt_tokens=6,
                model="gpt-4o",
                prompt_version="v1",
            )

    result = _run_agent_traced(_Exec(), agent, "hello", tool_callables={}, preview=False)

    assert result.final_output == "hi"
    span = next(s for s in fake.spans if s.name == "agent.greeter")
    assert span.span_type == "AGENT"
    assert span.attributes["caliber.agent"] == "Greeter"
    assert span.attributes["caliber.model"] == "gpt-4o"
    assert span.attributes["caliber.tokens"] == 30
    assert span.attributes["caliber.prompt_tokens"] == 10
    assert span.attributes["caliber.completion_tokens"] == 20
    assert span.attributes["caliber.cached_prompt_tokens"] == 6
    # 4/1000*0.0025 + 6/1000*0.00125 + 20/1000*0.01 = 0.0002175
    assert span.attributes["caliber.cost_usd"] == 0.000218
    assert span.attributes["caliber.prompt_version"] == "v1"
    assert span.attributes["caliber.status"] == "completed"


def test_run_agent_traced_propagates_executor_error() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))
    agent = SimpleNamespace(node_id="n1", name="A", model="inherit")

    class _Boom:
        def run_agent(self, *a, **k):
            raise RuntimeError("agent failed")

    with pytest.raises(RuntimeError, match="agent failed"):
        _run_agent_traced(_Boom(), agent, "x", tool_callables={}, preview=False)

    span = next(s for s in fake.spans if s.name == "agent.n1")
    assert span.attributes["caliber.status"] == "failed"
    assert span.attributes["caliber.error_type"] == "RuntimeError"


def test_execute_emits_per_node_spans_for_non_agent_nodes() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    result = execute(_plan(make_manifest()), "hello", executor=FakeWorkflowExecutor())
    assert result.status == "completed"

    names = [s.name for s in fake.spans]
    # The default manifest is start -> agent -> output. Non-agent nodes now get a
    # NODE span; the agent keeps its AGENT span (no double-wrap, no node.* span).
    assert "node.start" in names
    assert "node.final" in names
    assert "agent.agent" in names
    assert "node.agent" not in names  # agent is not double-wrapped

    output_span = next(s for s in fake.spans if s.name == "node.final")
    assert output_span.attributes["caliber.node_type"] == "output"
    assert output_span.attributes["caliber.node_id"] == "final"


def test_execute_emits_resolution_span_under_agent() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    execute(_plan(make_manifest()), "hello", executor=FakeWorkflowExecutor())

    names = [s.name for s in fake.spans]
    assert "resolve.agent" in names
    resolve_span = next(s for s in fake.spans if s.name == "resolve.agent")
    assert resolve_span.attributes["caliber.node_id"] == "agent"
    # The default manifest uses inline instructions.
    assert resolve_span.attributes["caliber.prompt.kind"] == "inline"


def test_trace_agent_resolution_records_mlflow_prompt_and_skills() -> None:
    fake = FakeMlflow()
    set_tracer(Tracer(mlflow_module=fake))

    from caliber.workflows.ir import IRAgent, NodeType, PromptRef
    from caliber.workflows.runtime import _trace_agent_resolution

    agent = IRAgent(
        node_id="researcher",
        node_type=NodeType.AGENT,
        name="Researcher",
        instructions=PromptRef(
            kind="mlflow_prompt", registry_name="research_prompt", alias="prod"
        ),
        skill_instructions=["# Web Search\nSearch the web.", "## Summarize\nSummarize results."],
    )

    _trace_agent_resolution(Tracer(mlflow_module=fake), agent)

    span = next(s for s in fake.spans if s.name == "resolve.researcher")
    assert span.attributes["caliber.prompt.kind"] == "mlflow_prompt"
    assert span.attributes["caliber.prompt.registry_name"] == "research_prompt"
    assert span.attributes["caliber.prompt.alias"] == "prod"
    assert span.attributes["caliber.prompt.ref"] == "prompts:/research_prompt@prod"
    assert span.attributes["caliber.skill_count"] == 2
    # Skill labels are derived from each block's leading line (sanitized json list).
    assert "Web Search" in str(span.attributes["caliber.skills"])
    assert "Summarize" in str(span.attributes["caliber.skills"])
