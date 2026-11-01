"""B04.6 (#768): every place a model's tool call is executed, with what governs it.

A model-chosen call reaches a tool in a handful of places. Each is pinned here with its status, so a new one (an ungoverned
alternate call site) fails this test until it is gated, routed or deliberately listed with its reason, exactly as
``RAW_OPENAPI_CALLERS`` and ``RAW_MCP_CALLERS`` pin the transports.

Statuses:

* ``gated``: the callables it runs were put behind the per-call gate (:func:`caliber.workflows.runtime._gate_model_callables`) by
  the interpreter that resolved them. A protected call is refused with a typed code; a read runs.
* ``governed-step``: an adapter of a governed agent step. It is handed only the callables the platform classified ``read`` and
  everything else is a proposal (B08.2, B04.6), so it cannot run a protected tool.
* ``legacy``: not governed, and named so: the development profile with no governed context, a plan not wired to the registry
  (a standalone export, a caller-built plan). Documented in ``docs/operate/reference-profile.md``.
* ``out-of-scope``: Aria, the assistant, which has no workflow run to propose into (inventory EP-034; owned by #1063).
"""

from __future__ import annotations

import ast
from pathlib import Path

import caliber

ROOT = Path(caliber.__file__).resolve().parent

#: The names that execute a tool a model chose, and where each is named: ``file -> (names, status, why)``.
MODEL_TOOL_CALL_SITES: dict[str, tuple[frozenset[str], str, str]] = {
    "workflows/runtime.py": (
        frozenset({"_execute_model_tool_call", "_invoke_agent_tools", "_call_tool"}),
        "gated",
        "the four provider loops, the Agents SDK callback and the fake executor all run a tool through "
        "_execute_model_tool_call or _invoke_agent_tools over callables that _gate_model_callables wrapped; "
        "_call_tool is the shape chooser they and the tool node share",
    ),
    "workflows/step_openai_chat.py": (
        frozenset({"_execute_model_tool_call"}),
        "governed-step",
        "runs only AgentStepInput.read_tools (the platform's read-class callables)",
    ),
    "workflows/step_openai_responses.py": (
        frozenset({"_execute_model_tool_call"}),
        "governed-step",
        "runs only AgentStepInput.read_tools",
    ),
    "workflows/step_openai_agents.py": (
        frozenset({"_call_tool"}),
        "governed-step",
        "runs only AgentStepInput.read_tools",
    ),
    "workflows/step_adapters/anthropic_chat.py": (
        frozenset({"_call_tool"}),
        "governed-step",
        "runs only AgentStepInput.read_tools",
    ),
}

#: Aria's tool loops call ``dispatch`` on the assistant toolset: no workflow run, so nothing to propose into (EP-034, #1063).
ASSISTANT_TOOL_CALL_SITES: dict[str, str] = {
    "assistant/anthropic_engine.py": "out-of-scope: Aria's own tool loop (#1063)",
    "assistant/openai_engine.py": "out-of-scope: Aria's own tool loop (#1063)",
}

STATUSES = {"gated", "governed-step", "legacy", "out-of-scope"}
_NAMES = frozenset({"_execute_model_tool_call", "_invoke_agent_tools", "_call_tool"})


def _names_in(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _NAMES:
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in _NAMES:
            found.add(node.attr)
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _NAMES
        ):
            found.add(node.value)
    return found


def _calls(node: ast.AST, name: str) -> int:
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name) and child.func.id == name)
            or (isinstance(child.func, ast.Attribute) and child.func.attr == name)
        )
    )


def test_every_module_that_executes_a_model_chosen_tool_call_is_pinned_with_its_status() -> None:
    found: dict[str, set[str]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        names = _names_in(ast.parse(path.read_text(encoding="utf-8")))
        # a module's own definition of a name is not a call site of another module's
        if names:
            found[path.relative_to(ROOT).as_posix()] = names
    assert found == {file: set(names) for file, (names, _s, _w) in MODEL_TOOL_CALL_SITES.items()}
    assert {status for _n, status, _w in MODEL_TOOL_CALL_SITES.values()} <= STATUSES
    assert all(why for _n, _s, why in MODEL_TOOL_CALL_SITES.values())


def test_the_assistants_tool_loops_are_the_only_other_executors_and_are_pinned() -> None:
    found = {
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "assistant").glob("*_engine.py"))
        if any(
            isinstance(node, ast.Attribute) and node.attr == "dispatch"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    }
    assert found == set(ASSISTANT_TOOL_CALL_SITES)


def test_every_function_that_resolves_an_agents_callables_also_gates_them() -> None:
    """An agent's callables are resolved at four interpreter sites (a node, a handoff target, the handoff map, a governed node):
    each must put the gate in front of the final map, so a new resolver cannot hand a model an ungated protected tool."""
    tree = ast.parse((ROOT / "workflows" / "runtime.py").read_text(encoding="utf-8"))
    resolvers = {
        node.name: _calls(node, "_resolve_tool_callables")
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _calls(node, "_resolve_tool_callables")
    }
    assert set(resolvers) == {
        "_run_governed_agent_node",
        "_collect_agent_handoff_specs",
        "_callables_for",  # the handoff map's resolver, nested in the one above
        "_run_node",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in resolvers:
            assert _calls(node, "_gate_model_callables") >= resolvers[node.name], node.name
