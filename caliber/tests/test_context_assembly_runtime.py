"""The runtime hands the context recorder the right provenance, on every path to an agent (B07.3b, #710)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from caliber.context.authorize import ContextDenied
from caliber.workflows.runtime import (
    AgentContextAssembly,
    FakeWorkflowExecutor,
    RuntimeResumeCheckpoint,
    execute,
)
from tests.test_workflow_runtime import _plan
from tests.workflow_helpers import make_manifest


def _knowledge(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "versions": [{"answer": '["x","y"]', "retrieval_mode": "dense"}],
        "resolved_version_ids": ["KBV-1"],
        "question": payload["question"],
    }


def _knowledge_node() -> dict[str, Any]:
    return {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": ["KBV-1"],
        "retrieval_modes": ["dense"],
    }


class Recorder:
    def __init__(self, refuse: bool = False) -> None:
        self.assemblies: list[AgentContextAssembly] = []
        self.refuse = refuse

    def __call__(self, assembly: AgentContextAssembly) -> None:
        self.assemblies.append(assembly)
        if self.refuse:
            raise ContextDenied("manifest_invalid")


def _origins(recorder: Recorder) -> list[tuple[str | None, str | None]]:
    return [(i.origin_node_id, i.origin_node_type) for a in recorder.assemblies for i in a.inputs]


def test_the_edge_that_delivered_the_value_names_its_origin_not_the_first_edge_to_the_port() -> (
    None
):
    data = make_manifest()
    data["nodes"]["knowledge"] = _knowledge_node()
    data["edges"] = [
        {"id": "e0", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {"id": "e2", "from": "knowledge", "to": "agent", "map": {"answer": "input"}},
        {"id": "e3", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    recorder = Recorder()
    result = execute(
        _plan(data, knowledge_query_runner=_knowledge, context_recorder=recorder),
        "hello",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "completed", result.error
    assert _origins(recorder) == [("knowledge", "knowledge_query")]
    assert recorder.assemblies[0].inputs[0].text == '["x","y"]'
    assert recorder.assemblies[0].inputs[0].origin_result["resolved_version_ids"] == ["KBV-1"]


def test_only_the_input_the_provider_is_sent_is_recorded() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["inputs"] = {"input": {"type": "string"}, "extra": {"type": "string"}}
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e1b", "from": "start", "to": "agent", "map": {"msg": "extra"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    recorder = Recorder()
    execute(_plan(data, context_recorder=recorder), "hello", executor=FakeWorkflowExecutor())
    (assembly,) = recorder.assemblies
    assert [(i.port, i.text) for i in assembly.inputs] == [("input", "hello")]


@pytest.mark.parametrize("wrapper", ["for_each", "error_boundary"])
def test_a_refused_context_fails_the_run_even_inside_a_for_each_or_error_boundary(
    wrapper: str,
) -> None:
    data = make_manifest()
    if wrapper == "for_each":
        data["nodes"]["wrap"] = {
            "id": "wrap",
            "type": "for_each",
            "target_node_id": "agent",
            "inputs": {"items": {"type": "structured"}},
            "outputs": {"text": {"type": "string"}, "results": {"type": "structured"}},
        }
        port, out = "items", "text"
    else:
        data["nodes"]["wrap"] = {
            "id": "wrap",
            "type": "error_boundary",
            "target_node_id": "agent",
            "fallback_text": "fallback",
            "inputs": {"input": {"type": "string"}},
            "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
        }
        port, out = "input", "output"
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "wrap", "map": {"msg": port}},
        {"id": "e2", "from": "wrap", "to": "final", "map": {out: "response"}},
    ]
    executor = FakeWorkflowExecutor()
    result = execute(
        _plan(data, context_recorder=Recorder(refuse=True)),
        '["a","b"]',
        executor=executor,
    )
    assert result.status == "error"
    assert (result.error or "").startswith("ContextDenied: manifest_invalid")
    assert executor.calls == [], "the provider was never called"


def test_a_refused_compensation_context_is_not_absorbed_by_the_boundary() -> None:
    data = make_manifest()
    data["nodes"]["boom"] = {
        "id": "boom",
        "type": "python_code",
        "code": "raise RuntimeError('boom')",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["wrap"] = {
        "id": "wrap",
        "type": "error_boundary",
        "target_node_id": "boom",
        "compensate_with": "agent",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "wrap", "map": {"msg": "input"}},
        {"id": "e2", "from": "wrap", "to": "final", "map": {"output": "response"}},
    ]
    result = execute(
        _plan(data, context_recorder=Recorder(refuse=True)), "hi", executor=FakeWorkflowExecutor()
    )
    assert result.status == "error" and (result.error or "").startswith("ContextDenied: ")


def test_an_item_a_for_each_runs_an_agent_on_has_no_known_origin() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = _knowledge_node()
    data["nodes"]["wrap"] = {
        "id": "wrap",
        "type": "for_each",
        "target_node_id": "agent",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {"text": {"type": "string"}, "results": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {"id": "e2", "from": "knowledge", "to": "wrap", "map": {"answer": "items"}},
        {"id": "e3", "from": "wrap", "to": "final", "map": {"text": "response"}},
    ]
    recorder = Recorder()
    plan = _plan(data, knowledge_query_runner=_knowledge, context_recorder=recorder)
    plan.context_provenance_resolver = lambda version_id: f"decision:{version_id}"
    result = execute(
        plan,
        "hello",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "completed", result.error
    assert [(i.text, i.origin_node_type) for a in recorder.assemblies for i in a.inputs] == [
        ("x", None),
        ("y", None),
    ], "never labelled as the run's own input"
    assert all(
        item.transform == {"name": "derived", "version": "runtime-v1"}
        and item.authorization_decision_ids == ("decision:KBV-1",)
        for assembly in recorder.assemblies
        for item in assembly.inputs
    )


def test_replayed_knowledge_output_preserves_source_read_provenance() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = _knowledge_node()
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {"id": "e2", "from": "knowledge", "to": "agent", "map": {"answer": "input"}},
        {"id": "e3", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    first_recorder = Recorder()
    first_plan = _plan(data, knowledge_query_runner=_knowledge, context_recorder=first_recorder)
    first_plan.context_provenance_resolver = lambda version_id: f"decision:{version_id}"
    first_plan.context_source_guard_present = True
    first = execute(first_plan, "hello", executor=FakeWorkflowExecutor())
    assert first.status == "completed", first.error
    knowledge_step = next(step for step in first.steps if step.node_id == "knowledge")
    assert knowledge_step.output_by_port is not None
    checkpoint_result = knowledge_step.output_by_port["result"]
    assert checkpoint_result["context_provenance"] == [
        {
            "authorization_decision_id": "decision:KBV-1",
            "source": {"kind": "knowledge_base", "version_id": "KBV-1"},
        }
    ]

    replay_recorder = Recorder()
    replay_plan = _plan(data, context_recorder=replay_recorder)
    replay_plan.context_source_guard_present = True
    replay = execute(
        replay_plan,
        "hello",
        executor=FakeWorkflowExecutor(),
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="knowledge",
            output_by_port=knowledge_step.output_by_port,
            replay_output=True,
        ),
    )
    assert replay.status == "completed", replay.error
    assert replay_recorder.assemblies[0].inputs[0].authorization_decision_ids == ("decision:KBV-1",)


def test_replayed_knowledge_output_without_authority_fails_before_provider() -> None:
    data = make_manifest()
    data["nodes"]["knowledge"] = _knowledge_node()
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {"id": "e2", "from": "knowledge", "to": "agent", "map": {"answer": "input"}},
        {"id": "e3", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    executor = FakeWorkflowExecutor()
    plan = _plan(data, context_recorder=Recorder())
    plan.context_source_guard_present = False
    result = execute(
        plan,
        "hello",
        executor=executor,
        resume_checkpoint=RuntimeResumeCheckpoint(
            node_id="knowledge",
            output_by_port={
                "answer": "answer",
                "result": {"resolved_version_ids": ["KBV-1"]},
            },
            replay_output=True,
        ),
    )
    assert result.status == "error"
    assert (result.error or "").startswith("ContextDenied: authority_unavailable")
    assert executor.calls == []


class _MemoryStore:
    def __init__(self) -> None:
        self.loaded = 0
        self.saved = 0
        self.history = [{"role": "assistant", "content": "previous"}]

    def load_history(self, **_kwargs: Any) -> list[dict[str, str]]:
        self.loaded += 1
        return [dict(item) for item in self.history]

    def save_history(self, *, history: list[dict[str, str]], **_kwargs: Any) -> None:
        self.saved += 1
        self.history = [dict(item) for item in history]


def test_memory_is_authorized_before_loading_and_recorded_with_the_step() -> None:
    data = make_manifest(
        "memory-manifest-wf",
        runtime={
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "persistent"},
        },
    )
    store = _MemoryStore()
    recorder = Recorder()
    plan = _plan(
        data,
        context_recorder=recorder,
        memory_read_authorizer=lambda reference: f"decision:{reference}",
        session_memory_store=store,
    )

    result = execute(plan, "hello", executor=FakeWorkflowExecutor(), session_id="session-1")

    assert result.status == "completed", result.error
    assert store.loaded == 1 and store.saved == 1
    (assembly,) = recorder.assemblies
    assert assembly.memory_reference == ("workflow:memory-manifest-wf/node:agent/session:session-1")
    assert assembly.memory_authorization_decision_id == (
        "decision:workflow:memory-manifest-wf/node:agent/session:session-1"
    )
    assert assembly.memory_history == ({"role": "assistant", "content": "previous"},)


def test_memory_without_an_authorizer_fails_before_the_store_is_loaded() -> None:
    data = make_manifest(
        "memory-denied-wf",
        runtime={
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "persistent"},
        },
    )
    store = _MemoryStore()
    result = execute(
        _plan(data, context_recorder=Recorder(), session_memory_store=store),
        "hello",
        executor=FakeWorkflowExecutor(),
        session_id="session-1",
    )

    assert result.status == "error"
    assert (result.error or "").startswith("ContextDenied: authority_unavailable")
    assert store.loaded == 0 and store.saved == 0


def test_every_agent_a_native_handoff_may_reach_is_recorded_before_the_provider_runs() -> None:
    from caliber.workflows.runtime import _run_agent_with_history

    data = make_manifest()
    plan = _plan(data)
    agent = plan.ir.nodes["agent"]
    other = replace(agent, node_id="other")  # type: ignore[type-var]
    order: list[str] = []

    class Executor(FakeWorkflowExecutor):
        def run_agent(self, agent: Any, input_text: str, **kwargs: Any) -> Any:
            order.append(f"provider:{agent.node_id}")
            return super().run_agent(agent, input_text, **kwargs)

    recorder = Recorder()
    plan.context_recorder = lambda a: (order.append(f"record:{a.node_id}"), recorder(a))[1]
    _run_agent_with_history(
        plan,
        Executor(),
        agent,  # type: ignore[arg-type]
        "hello",
        handoff_agents={"agent": (agent, {}), "other": (other, {})},  # type: ignore[dict-item]
        tool_callables={},
        preview=False,
    )
    assert order == ["record:agent", "record:other", "provider:agent"]
    assert {a.node_id for a in recorder.assemblies} == {"agent", "other"}
    assert all(a.instruction_text == "You are helpful." for a in recorder.assemblies)


def test_a_refusal_from_a_non_agent_for_each_target_is_not_isolated_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child workflow's agent can be refused under a ``for_each`` whose own target is not an agent."""
    from caliber.workflows import runtime as runtime_module

    data = make_manifest()
    data["nodes"]["code"] = {
        "id": "code",
        "type": "python_code",
        "code": "return {'text': input}",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["nodes"]["wrap"] = {
        "id": "wrap",
        "type": "for_each",
        "target_node_id": "code",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {"text": {"type": "string"}, "results": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "wrap", "map": {"msg": "items"}},
        {"id": "e2", "from": "wrap", "to": "final", "map": {"text": "response"}},
    ]

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise ContextDenied("manifest_invalid")

    monkeypatch.setattr(runtime_module, "_run_inline_target_node", refuse)
    result = execute(_plan(data), '["a"]', executor=FakeWorkflowExecutor())
    assert result.status == "error" and (result.error or "").startswith("ContextDenied: ")
