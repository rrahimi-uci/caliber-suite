"""B04.6 (#768): a tool the *model* chooses never runs a protected effect directly.

In an agent loop the model picks the tool and its arguments. In a governed plan that choice can only ever be a **proposal**
(a governed agent step: one recorded ``proposed`` effect, one approval request, the run parked) or a **refusal** with a typed,
payload-free code, and it is classified by the same helpers a workflow tool node uses (the trusted registry row, or the MCP
server record and the administrator's tool policy), never by the model's claim. A read-class call keeps running inline.

SQLite throughout; the PostgreSQL races of the proposal service live in ``tests/egi_scenarios`` and the public-path scenario in
``tests/egi_scenarios/test_b04_model_selected_calls.py``.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.contracts.agent_step import StepLimits, StepUsage
from caliber.db.models import (
    CaliberEffectProposal,
    CaliberRuntimeApprovalRequest,
    CaliberToolRegistry,
)
from caliber.effects.model_tool_calls import (
    MODEL_TOOL_ROUTE_REFUSED,
    PROTECTED_TOOL_REFUSED,
    REFUSAL_CODES,
    ModelToolCallRefused,
    refusal_marker,
)
from caliber.effects.registered_tools import registry_identity_reader
from caliber.egress import EgressPolicy
from caliber.workflows import runtime as rt
from caliber.workflows.builtin_tools import register_builtin_tools
from caliber.workflows.compiler import build_ir
from caliber.workflows.ir import IRAgent
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import (
    EffectAuthorization,
    FakeWorkflowExecutor,
    GovernedEffectContext,
    OpenAIChatWorkflowExecutor,
    RuntimePlan,
    execute,
)
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.mcp_fakes import SERVER_ID, FakeMcp, use_database
from tests.test_agentic_tool_loop import (
    _FakeOpenAI,
    _final_resp,
    _OAMsg,
    _OAResp,
    _OAToolCall,
    _OAUsage,
)
from tests.test_registered_tool_dispatch import (
    RUN_ID,
    Tool,
    _world,
    seed_row,
    tool_entry,
)
from tests.workflow_helpers import make_manifest

pytestmark = pytest.mark.usefixtures("fixed_step_clock")

SENTINEL = "ARGUMENT-SENTINEL-77c1"
LOOPBACK = EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
LIMITS = StepLimits(
    max_turns=8, max_tool_calls=4, max_tokens=20000, deadline="2099-01-01T00:00:00Z"
)
USAGE = StepUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, turns=1, tool_calls=0)


@pytest.fixture
def factory(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    with _world("sqlite", tmp_path, monkeypatch) as made:
        use_database(monkeypatch, made)
        yield made


@pytest.fixture
def mcp(monkeypatch: pytest.MonkeyPatch) -> FakeMcp:
    from caliber.mcp_policy import _reset_rate_limits_for_tests

    _reset_rate_limits_for_tests()
    return FakeMcp().install(monkeypatch)


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALIBER_PROFILE", "reference")


# --------------------------------------------------------------------------------- the workflow under test


def agent_manifest(
    tools: dict[str, dict[str, Any]], *, handoff_to_billing: bool = False
) -> dict[str, Any]:
    """One agent node bound to ``tools`` (name -> binding); optionally handing off to a ``billing`` agent with the last tool."""
    data = make_manifest("model_selected")
    names = list(tools)
    data["nodes"]["agent"]["tools"] = names[:-1] if handoff_to_billing else names
    data["tools"] = tools
    if handoff_to_billing:
        data["nodes"]["agent"]["handoffs"] = [{"target": "billing"}]
        data["nodes"]["billing"] = {
            "id": "billing",
            "type": "agent",
            "name": "billing-agent",
            "model": "inherit",
            "instructions": {"type": "inline", "text": "b"},
            "tools": [names[-1]],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        }
    return data


def registered(name: str) -> dict[str, Any]:
    return {"registry_ref": f"tool.{name}.v1", "version_constraint": ">=1.0,<2.0"}


def mcp_binding(tool: str) -> dict[str, Any]:
    return {"type": "mcp_tool", "server_id": SERVER_ID, "tool_name": tool}


class World:
    """A plan over one agent node, wired like ``build_plan`` wires a deployed workflow (a registry reader, no governed context)."""

    def __init__(
        self,
        factory: sessionmaker[Session],
        manifest: dict[str, Any],
        entries: list[ToolRegistryEntry],
        **plan_fields: Any,
    ) -> None:
        self.factory = factory
        resolver = InMemoryToolResolver(entries)
        fields: dict[str, Any] = {
            "ir": build_ir(parse_manifest(manifest), resolver, version="1"),
            "resolver": resolver,
            "workflow_version_id": "version-b04-6",
            "registered_tool_identity": registry_identity_reader(factory),
            "egress_policy": LOOPBACK,
        }
        fields.update(plan_fields)
        self.plan = RuntimePlan(**fields)

    def agent(self, node_id: str = "agent") -> IRAgent:
        node = self.plan.ir.nodes[node_id]
        assert isinstance(node, IRAgent)
        return node

    def tool_results(self, result: Any, node_id: str = "agent") -> dict[str, Any]:
        (step,) = [s for s in result.steps if s.node_id == node_id]
        return {call["tool"]: call["result"] for call in step.tool_calls}

    def rows(self) -> tuple[list[Any], list[Any]]:
        with self.factory() as session:
            return (
                list(session.execute(select(CaliberEffectProposal)).scalars()),
                list(session.execute(select(CaliberRuntimeApprovalRequest)).scalars()),
            )


def refused(tool: str, code: str = PROTECTED_TOOL_REFUSED) -> dict[str, Any]:
    return {"_refused": True, "tool": tool, "code": code}


def no_authority(world: World) -> GovernedEffectContext:
    """A governed effect context whose authority fails the test if anything asks it to authorize."""

    def authorize(_proposal: Any) -> EffectAuthorization | None:
        raise AssertionError("a model-chosen call asked for an authorization")

    return GovernedEffectContext(
        session_factory=world.factory,
        workflow_run_id=RUN_ID,
        authorize=authorize,
        receipt_verifier=lambda _r, _x: None,
    )


# ------------------------------------------------------------- the fake executor's helper (every tool once)


def _registered_world(
    factory: sessionmaker[Session], **plan_fields: Any
) -> tuple[World, dict[str, Tool]]:
    levels = {"lookup_ticket": "read", "open_ticket": "write", "escalate": "external_action"}
    entries = [tool_entry(name, level) for name, level in levels.items()]
    with factory() as session:
        seeded = set(session.execute(select(CaliberToolRegistry.name)).scalars())
    for entry in entries:
        if entry.name not in seeded:
            seed_row(factory, entry)
    world = World(
        factory, agent_manifest({name: registered(name) for name in levels}), entries, **plan_fields
    )
    return world, {name: Tool() for name in levels}


def test_a_protected_registered_tool_is_refused_in_a_reference_loop_and_a_read_runs(
    factory: sessionmaker[Session],
) -> None:
    world, tools = _registered_world(factory)
    result = execute(world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=tools)

    assert result.status == "completed", result.error
    seen = world.tool_results(result)
    assert seen["lookup_ticket"] == {"ticket": "T-1"}, "a read-class tool still runs inline"
    assert seen["open_ticket"] == refused("open_ticket")
    assert seen["escalate"] == refused("escalate")
    assert [len(tools[name].calls) for name in ("lookup_ticket", "open_ticket", "escalate")] == [
        1,
        0,
        0,
    ]
    assert world.rows() == ([], []), "a refusal records no proposal and asks for no approval"


def test_a_bound_context_never_lets_a_loop_run_or_authorize_a_protected_tool(
    factory: sessionmaker[Session],
) -> None:
    world, tools = _registered_world(factory)
    world.plan.governed_effects = no_authority(world)
    result = execute(world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=tools)

    assert world.tool_results(result)["open_ticket"] == refused("open_ticket")
    assert [len(tools[name].calls) for name in ("open_ticket", "escalate")] == [0, 0]
    assert world.rows() == ([], [])


def test_the_development_profile_keeps_the_legacy_inline_call_and_a_bound_context_ends_it(
    factory: sessionmaker[Session],
) -> None:
    """The documented legacy behaviour (#1068) is unchanged: the loop runs a protected registered tool, as before the dispatcher."""
    world, tools = _registered_world(factory, legacy_guarded_effects=True)
    result = execute(world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=tools)
    assert world.tool_results(result)["open_ticket"] == {"ticket": "T-1"}
    assert [len(tools[name].calls) for name in ("open_ticket", "escalate")] == [1, 1]

    bound, bound_tools = _registered_world(factory, legacy_guarded_effects=True)
    bound.plan.governed_effects = no_authority(bound)
    refused_result = execute(
        bound.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=bound_tools
    )
    assert bound.tool_results(refused_result)["open_ticket"] == refused("open_ticket")
    assert bound_tools["open_ticket"].calls == []


def test_a_plan_that_is_not_wired_to_the_registry_is_not_governed_and_says_so(
    factory: sessionmaker[Session],
) -> None:
    world, tools = _registered_world(factory, registered_tool_identity=None)
    result = execute(world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=tools)
    assert world.tool_results(result)["open_ticket"] == {"ticket": "T-1"}
    assert len(tools["open_ticket"].calls) == 1


def test_an_injected_callable_does_not_escape_the_gate(factory: sessionmaker[Session]) -> None:
    """``extra_tools`` replaces a bound tool's callable; the classification is the binding's, so the replacement is gated too."""
    world, _ = _registered_world(factory)
    injected = Tool()
    tools = {name: Tool() for name in ("lookup_ticket", "open_ticket")}
    result = execute(
        world.plan,
        "go",
        executor=FakeWorkflowExecutor(),
        extra_tools={**tools, "escalate": injected},
    )
    assert world.tool_results(result)["escalate"] == refused("escalate")
    assert injected.calls == []


def test_a_handoff_targets_protected_tool_is_gated_too(factory: sessionmaker[Session]) -> None:
    levels = {"lookup_ticket": "read", "escalate": "external_action"}
    entries = [tool_entry(name, level) for name, level in levels.items()]
    for entry in entries:
        seed_row(factory, entry)
    manifest = agent_manifest({name: registered(name) for name in levels}, handoff_to_billing=True)
    world = World(factory, manifest, entries)
    tools = {name: Tool() for name in levels}
    result = execute(world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=tools)

    # the target's tool calls are merged into the node's step: the read ran, the target's protected tool was refused
    assert world.tool_results(result, "agent") == {
        "lookup_ticket": {"ticket": "T-1"},
        "escalate": refused("escalate"),
    }
    assert tools["escalate"].calls == []


def test_a_registry_row_that_is_not_the_bindings_is_refused_with_its_code(
    factory: sessionmaker[Session],
) -> None:
    entry = tool_entry("open_ticket", "write")
    seed_row(factory, replace(entry, module_path="evil.module"))
    world = World(factory, agent_manifest({"open_ticket": registered("open_ticket")}), [entry])
    tool = Tool()
    result = execute(
        world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools={"open_ticket": tool}
    )
    assert world.tool_results(result)["open_ticket"] == refused(
        "open_ticket", "registry_row_mismatch"
    )
    assert tool.calls == []


def test_an_unreadable_registry_is_a_refusal_not_a_run(factory: sessionmaker[Session]) -> None:
    world, tools = _registered_world(factory)

    def broken(_name: str, _version: str) -> Any:
        raise RuntimeError(f"db down {SENTINEL}")

    world.plan.registered_tool_identity = broken
    result = execute(world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools=tools)
    seen = world.tool_results(result)
    assert seen["open_ticket"] == refused("open_ticket", "registry_row_unreadable")
    assert SENTINEL not in json.dumps(seen), "the reader's message reaches no marker"
    assert tools["open_ticket"].calls == []


def test_a_built_in_the_inventory_refuses_is_refused_in_the_reference_loop(
    factory: sessionmaker[Session],
) -> None:
    with factory() as session:
        register_builtin_tools(session)
        session.commit()
    entry = ToolRegistryEntry(
        name="read_text_file",
        version="1.0",
        module_path="caliber.workflows.builtin_tools",
        callable_name="read_text_file",
        side_effect_level="read",
    )
    from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE

    entry = replace(entry, module_path=BUILTIN_TOOL_MODULE)
    world = World(
        factory, agent_manifest({"read_text_file": registered("read_text_file")}), [entry]
    )
    host_read = Tool()
    result = execute(
        world.plan, "go", executor=FakeWorkflowExecutor(), extra_tools={"read_text_file": host_read}
    )
    assert world.tool_results(result)["read_text_file"] == refused(
        "read_text_file", "unsupported_effect_path"
    )
    assert host_read.calls == []


# ------------------------------------------------------------------- the provider loops (the real OpenAI chat loop)


def _calls(*calls: tuple[str, dict[str, Any]]) -> _OAResp:
    return _OAResp(
        _OAMsg(
            tool_calls=[
                _OAToolCall(f"call-{index}", name, json.dumps(args))
                for index, (name, args) in enumerate(calls)
            ]
        ),
        _OAUsage(5, 5, 10),
    )


def chat_loop(
    world: World, *calls: tuple[str, dict[str, Any]], tools: dict[str, Any] | None = None
) -> Any:
    client = _FakeOpenAI([_calls(*calls), _final_resp("done")])
    executor = OpenAIChatWorkflowExecutor(api_key="unused", default_model="m", client=client)
    result = execute(world.plan, "go", executor=executor, extra_tools=tools)
    assert result.status == "completed", result.error
    return result, client


def test_the_openai_chat_loop_gets_the_refusal_marker_and_not_the_tool(
    factory: sessionmaker[Session],
) -> None:
    world, tools = _registered_world(factory)
    result, client = chat_loop(
        world,
        ("lookup_ticket", {"q": "a"}),
        ("open_ticket", {"title": SENTINEL}),
        tools=tools,
    )
    seen = world.tool_results(result)
    assert seen["open_ticket"] == refused("open_ticket")
    assert tools["open_ticket"].calls == []
    assert [len(tools["lookup_ticket"].calls)] == [1]
    # what went back to the model: the marker, no argument and no exception text
    answers = [m for m in client.chat.completions.calls[-1]["messages"] if m["role"] == "tool"]
    sent = json.dumps(answers)
    assert "protected_tool_requires_governed_step" in sent
    assert SENTINEL not in sent, "the model's own arguments are not echoed back in the answer"
    assert world.rows() == ([], [])


# ---------------------------------------------------------------------------- the choke point, unit by unit


def test_the_choke_point_reports_a_refusal_as_a_marker_and_never_leaks_an_exception() -> None:
    def refuse(**_kwargs: Any) -> None:
        raise ModelToolCallRefused("protected_tool_requires_governed_step")

    from caliber.workflows.ir import IRToolBinding

    binding = IRToolBinding(
        local_name="t",
        registry_ref="tool.t.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="t",
    )
    result, text = rt._execute_model_tool_call(
        name="t",
        arguments={"x": SENTINEL},
        tool_callables={"t": refuse},
        bindings={"t": binding},
        fallback_input="",
    )
    assert result == refusal_marker("t", "protected_tool_requires_governed_step")
    assert json.loads(text) == result and SENTINEL not in text


def test_the_gate_keeps_the_tools_signature_so_the_call_shape_is_still_chosen_right(
    factory: sessionmaker[Session],
) -> None:
    world, _ = _registered_world(factory)
    seen: list[Any] = []

    def lookup(q: str = "") -> dict[str, str]:
        seen.append(q)
        return {"ok": q}

    gated = rt._gate_model_callables(
        world.plan, world.agent(), {"lookup_ticket": lookup}, preview=False
    )["lookup_ticket"]
    assert gated is not lookup
    assert list(inspect.signature(gated).parameters) == ["q"]
    assert rt._call_tool(gated, {"q": "a"}, fallback_input="") == {"ok": "a"}
    assert seen == ["a"]


def test_the_gate_is_in_front_of_a_tool_that_selects_its_own_call_shape(
    factory: sessionmaker[Session],
) -> None:
    """A sandboxed tool picks its convention in the child (``caliber_shape_selector``): the gate must be ahead of that too."""
    world, _ = _registered_world(factory)
    selected: list[Any] = []

    def sandboxed(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("must not be called directly")

    def selector(shapes: list[Any]) -> Any:
        selected.append(shapes)
        return "ran"

    sandboxed.caliber_shape_selector = selector  # type: ignore[attr-defined]
    gated = rt._gate_model_callables(
        world.plan,
        world.agent(),
        {"open_ticket": sandboxed, "lookup_ticket": sandboxed},
        preview=False,
    )
    with pytest.raises(ModelToolCallRefused) as stopped:
        rt._call_tool(gated["open_ticket"], {"title": "x"}, fallback_input="")
    assert stopped.value.code == PROTECTED_TOOL_REFUSED and selected == []
    assert rt._call_tool(gated["lookup_ticket"], {"q": "x"}, fallback_input="") == "ran"
    assert len(selected) == 1


@pytest.mark.parametrize(
    ("args", "kwargs", "expected"),
    [
        ((), {"a": 1}, {"a": 1}),
        (({"a": 1},), {}, {"a": 1}),
        (("text",), {}, {}),
        ((), {}, {}),
        (({"a": 1},), {"b": 2}, {}),
    ],
)
def test_the_arguments_a_call_carries_are_read_from_every_shape(
    args: tuple[Any, ...], kwargs: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert rt._model_call_arguments(args, kwargs) == expected


def test_a_route_that_fails_for_any_reason_is_a_refusal(factory: sessionmaker[Session]) -> None:
    world, tools = _registered_world(factory)
    binding = next(b for b in world.agent().tools if b.local_name == "lookup_ticket")
    check = rt._model_call_check(world.plan, "agent", binding)
    assert check is not None

    def explode(*_a: Any, **_k: Any) -> Any:
        raise ValueError(SENTINEL)

    original = rt._governed_tool_route
    rt._governed_tool_route = explode  # type: ignore[assignment]
    try:
        with pytest.raises(ModelToolCallRefused) as stopped:
            check({})
    finally:
        rt._governed_tool_route = original  # type: ignore[assignment]
    assert stopped.value.code == MODEL_TOOL_ROUTE_REFUSED and SENTINEL not in str(stopped.value)
    assert tools["lookup_ticket"].calls == []


def test_a_preview_is_not_gated_because_its_callables_never_reach_an_effect(
    factory: sessionmaker[Session],
) -> None:
    world, _ = _registered_world(factory)
    callables = {"open_ticket": Tool()}
    assert rt._gate_model_callables(world.plan, world.agent(), callables, preview=True) is callables


def test_every_refusal_code_is_a_fixed_lowercase_token() -> None:
    assert len(set(REFUSAL_CODES)) == len(REFUSAL_CODES)
    assert all(code.replace("_", "").isalpha() and code == code.lower() for code in REFUSAL_CODES)
