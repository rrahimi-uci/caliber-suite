"""B04.7 (#774): the reference profile's refusal switch (``caliber.effects.refusals``) and the places that consume it.

The per-row oracle is ``tests/test_effect_path_matrix.py`` (one probe per ``refuse`` row, driven through its real entry point).
This file covers the switch itself and the behaviours around it that a probe does not: what a profile name means, what the
message holds and never holds, the shipped-tool exemption's boundary, how an agent, a preview, a route and a node surface a
refusal, and that a refusal never reaches the retry or error-wrapping layers that sit around a tool.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any

import pytest
from starlette.exceptions import HTTPException

from caliber.config import CaliberConfig
from caliber.effects.dispatcher import UnsupportedEffectPath
from caliber.effects.inventory import InventoryError, load_inventory, parse_inventory
from caliber.effects.refusals import (
    ALLOWING_SETTING,
    UNSUPPORTED_IN_REFERENCE_PROFILE,
    profile_from_environment,
    reference_profile_active,
    refusal,
    refuse_in_reference_profile,
    refuse_unshipped_tool_in_reference_profile,
    refused_row,
    shipped_tool,
)
from caliber.routes._errors import CaliberHTTPException, refuse_route_in_reference_profile
from caliber.workflows import runtime as runtime_module
from caliber.workflows.builtin_tools import BUILTIN_TOOL_MODULE_BY_NAME, BUILTIN_TOOL_NAMES
from caliber.workflows.ir import IRAgent, IRToolBinding, NodeType
from caliber.workflows.runtime import _execute_model_tool_call, _resolve_tool_callables
from caliber.workflows.tools import InMemoryToolResolver
from tests.refusal_probes import ADMIN_CALLABLE, ADMIN_MODULE, DEVELOPMENT, REFERENCE

REFUSE_ROWS = [row.id for row in load_inventory() if row.disposition == "refuse"]


# ---- the switch ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "active"),
    [
        (None, False),
        (REFERENCE, True),
        (DEVELOPMENT, False),
        (types.SimpleNamespace(profile="reference"), True),
        (types.SimpleNamespace(profile="Reference"), False),
        (types.SimpleNamespace(profile=" reference"), False),
        (types.SimpleNamespace(profile=""), False),
        (types.SimpleNamespace(), False),
        ("reference", False),
    ],
)
def test_only_a_configuration_that_says_exactly_reference_is_the_reference_profile(
    config: object, active: bool
) -> None:
    assert reference_profile_active(config) is active


@pytest.mark.parametrize(
    ("environ", "profile"),
    [
        ({"CALIBER_PROFILE": "reference"}, "reference"),
        ({"CALIBER_PROFILE": "  reference  "}, "reference"),
        ({"CALIBER_PROFILE": "development"}, "development"),
        ({"CALIBER_PROFILE": ""}, ""),
        ({}, ""),
    ],
)
def test_a_separate_process_names_its_profile_from_its_own_environment(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], profile: str
) -> None:
    monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)

    assert profile_from_environment().profile == profile
    assert reference_profile_active(profile_from_environment()) is (profile == "reference")


def test_the_refusal_names_the_row_the_entry_point_and_the_setting_and_nothing_else() -> None:
    error = refusal("EP-011")

    assert isinstance(error, UnsupportedEffectPath)
    assert error.code == UNSUPPORTED_IN_REFERENCE_PROFILE == "unsupported_in_reference_profile"
    assert str(error) == (
        "unsupported_in_reference_profile: EP-011 (caliber/src/caliber/workflows/runtime.py::_run_node) "
        "is not supported in the reference profile; set CALIBER_PROFILE=development to use it outside the reference profile"
    )
    assert ALLOWING_SETTING in str(error)


@pytest.mark.parametrize("row_id", REFUSE_ROWS)
def test_every_refuse_row_has_a_refusal_that_cites_its_own_entry_point(row_id: str) -> None:
    row = refused_row(row_id)
    message = str(refusal(row_id))

    assert row.disposition == "refuse"
    assert f"{row_id} ({row.entry_point})" in message


@pytest.mark.parametrize(
    "row_id", ["EP-001", "EP-033", "EP-043", "EP-999", "", "ep-011", "EP-011 "]
)
def test_a_row_that_is_not_a_refuse_row_cannot_be_named_by_a_refusal(row_id: str) -> None:
    """A typo, a row someone re-classified or a made-up id fails loudly, in **every** profile, instead of refusing nothing."""
    with pytest.raises(ValueError, match=r"EP-|not a row"):
        refused_row(row_id)
    for config in (None, DEVELOPMENT, REFERENCE):
        with pytest.raises(ValueError, match=r"EP-|not a row"):
            refuse_in_reference_profile(row_id, config)
        with pytest.raises(ValueError, match=r"EP-|not a row"):
            refuse_unshipped_tool_in_reference_profile(
                row_id, config, module_path="m", callable_name="c"
            )


def test_a_refusal_is_a_no_op_outside_the_reference_profile_and_raises_in_it() -> None:
    assert refuse_in_reference_profile("EP-011", None) is None
    assert refuse_in_reference_profile("EP-011", DEVELOPMENT) is None
    with pytest.raises(UnsupportedEffectPath) as caught:
        refuse_in_reference_profile("EP-011", REFERENCE)
    assert caught.value.code == UNSUPPORTED_IN_REFERENCE_PROFILE


# ---- the shipped-tool exemption -----------------------------------------------------------------------------------------

REFUSED_BUILTINS = {
    "sandbox_python",
    "read_text_file",
    "list_folder_files",
    "grep_files",
    "regex_search",
}


@pytest.mark.parametrize("name", sorted(BUILTIN_TOOL_NAMES))
def test_a_seeded_builtin_is_shipped_unless_the_inventory_refuses_its_name(name: str) -> None:
    module = BUILTIN_TOOL_MODULE_BY_NAME[name]

    assert shipped_tool(module, name) is (name not in REFUSED_BUILTINS)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        (ADMIN_MODULE, ADMIN_CALLABLE),
        # A built-in's name in an administrator's module, and a built-in's module with another callable: the pair, not
        # either half, is what CALIBER ships.
        (ADMIN_MODULE, "grok_parse"),
        ("caliber.workflows.demo_tools", ADMIN_CALLABLE),
        ("caliber.workflows.demo_tools", "send_confirmation_email"),
        ("caliber.workflows.ingestion_tools", "grok_parse"),
        ("caliber.workflows.demo_tools", ""),
        ("", "grok_parse"),
        ("", ""),
        ("caliber.workflows.demo_tools ", "grok_parse"),
        ("<in-memory>", "grok_parse"),
    ],
)
def test_a_module_and_callable_that_are_not_a_seeded_pair_are_not_shipped(
    module: str, name: str
) -> None:
    assert shipped_tool(module, name) is False


def test_the_exemption_applies_only_when_the_profile_is_the_reference_one() -> None:
    for config in (None, DEVELOPMENT):
        refuse_unshipped_tool_in_reference_profile(
            "EP-015", config, module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE
        )
    refuse_unshipped_tool_in_reference_profile(
        "EP-015", REFERENCE, module_path="caliber.workflows.demo_tools", callable_name="grok_parse"
    )
    with pytest.raises(UnsupportedEffectPath):
        refuse_unshipped_tool_in_reference_profile(
            "EP-015", REFERENCE, module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE
        )
    with pytest.raises(UnsupportedEffectPath):
        refuse_unshipped_tool_in_reference_profile(
            "EP-015",
            REFERENCE,
            module_path="caliber.workflows.demo_tools",
            callable_name="grep_files",
        )


def test_the_inventory_rows_a_refusal_can_name_are_exactly_the_refuse_rows() -> None:
    assert [row.id for row in load_inventory() if row.disposition == "refuse"] == REFUSE_ROWS
    for row in load_inventory():
        if row.disposition == "refuse":
            assert refused_row(row.id) is row


def test_the_inventory_carries_the_b047_final_dispositions() -> None:
    rows = {row.id: row for row in load_inventory()}

    # EP-043 only emits text at compile time (every publish seals it); EP-044 and EP-045 serve it and are refused.
    assert rows["EP-043"].disposition == "exempt"
    assert rows["EP-044"].disposition == rows["EP-045"].disposition == "refuse"
    # The two module functions are refused; the registry names stay routed from EP-049, so a tool node is classified as before.
    assert rows["EP-047"].disposition == rows["EP-048"].disposition == "refuse"
    assert rows["EP-047"].tool_names == rows["EP-048"].tool_names == ()
    assert rows["EP-049"].disposition == "dispatch"
    assert {"write_workdir_file", "create_artifact"} <= set(rows["EP-049"].tool_names)
    with pytest.raises(InventoryError):
        parse_inventory("not a table")


# ---- how an agent, a preview, a node and a route surface a refusal -------------------------------------------------------


def _binding(module: str, name: str, *, level: str = "read") -> IRToolBinding:
    return IRToolBinding(
        local_name=name,
        registry_ref=f"tool.{name}.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level=level,
        allow_in_preview=True,
        module_path=module,
        callable_name=name,
    )


def _agent(*bindings: IRToolBinding) -> IRAgent:
    return IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        instructions="x",
        model="m",
        tools=list(bindings),
    )


def _agent_callables(
    monkeypatch: pytest.MonkeyPatch,
    config: CaliberConfig,
    *bindings: IRToolBinding,
    preview: bool = False,
) -> dict[str, Callable[..., Any]]:
    monkeypatch.setattr(runtime_module, "_ACTIVE_SANDBOX_CONFIG", config)
    return _resolve_tool_callables(_agent(*bindings), InMemoryToolResolver([]), preview=preview)


def test_an_agent_keeps_running_when_one_of_its_tools_is_refused_and_every_call_of_it_is_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.refusal_probes import Spawns

    spawns = Spawns(monkeypatch)
    shipped = _binding("caliber.workflows.demo_tools", "grok_parse")
    admin = _binding(ADMIN_MODULE, ADMIN_CALLABLE)

    callables = _agent_callables(monkeypatch, REFERENCE, shipped, admin)

    # The agent is built; the shipped tool is bound for real (and was not run to bind it); the other is the refusal.
    assert set(callables) == {"grok_parse", ADMIN_CALLABLE}
    assert spawns.count == 0
    for _ in range(3):
        with pytest.raises(UnsupportedEffectPath) as caught:
            callables[ADMIN_CALLABLE]()
        assert caught.value.code == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert spawns.count == 0, (
        "a call of a refused tool starts nothing, and is never retried into starting something"
    )


def test_the_model_is_told_the_refusal_and_nothing_of_its_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _binding(ADMIN_MODULE, ADMIN_CALLABLE)
    callables = _agent_callables(monkeypatch, REFERENCE, admin)

    result, text = _execute_model_tool_call(
        name=ADMIN_CALLABLE,
        arguments={"obj": "SECRET-ARGUMENT"},
        tool_callables=callables,
        bindings={ADMIN_CALLABLE: admin},
        fallback_input="",
    )

    assert result["tool"] == ADMIN_CALLABLE
    assert result["_error"].startswith(
        "UnsupportedInReferenceProfile: unsupported_in_reference_profile: EP-015 ("
    )
    assert "SECRET-ARGUMENT" not in text


def test_the_same_agent_binds_the_same_tool_in_the_development_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _binding(ADMIN_MODULE, ADMIN_CALLABLE)

    callables = _agent_callables(monkeypatch, DEVELOPMENT, admin)

    assert callable(callables[ADMIN_CALLABLE])
    # Not the refusal: nothing is raised by binding, and the callable is not the refusing one.
    assert callables[ADMIN_CALLABLE].__name__ != "_refuse"


def test_a_preview_mocks_a_tool_the_reference_profile_refuses_instead_of_failing_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.refusal_probes import Spawns

    spawns = Spawns(monkeypatch)
    admin = _binding(ADMIN_MODULE, ADMIN_CALLABLE)

    callables = _agent_callables(monkeypatch, REFERENCE, admin, preview=True)

    assert callables[ADMIN_CALLABLE]("anything") is not None
    assert spawns.count == 0


def test_a_tool_node_that_cannot_be_bound_is_refused_before_any_proposal_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_ACTIVE_SANDBOX_CONFIG", REFERENCE)

    with pytest.raises(UnsupportedEffectPath) as caught:
        runtime_module._resolve_bound_tool_callable(
            _binding(ADMIN_MODULE, ADMIN_CALLABLE, level="write"),
            InMemoryToolResolver([]),
            preview=False,
            required=True,
        )

    assert caught.value.code == UNSUPPORTED_IN_REFERENCE_PROFILE


def test_the_in_process_binder_does_not_swallow_a_refusal_but_still_swallows_an_import_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox_off = CaliberConfig(profile="reference", registered_tool_sandbox_enabled=False)
    monkeypatch.setattr(runtime_module, "_ACTIVE_SANDBOX_CONFIG", sandbox_off)
    with pytest.raises(UnsupportedEffectPath):
        runtime_module._bind_in_process(_binding(ADMIN_MODULE, ADMIN_CALLABLE))

    monkeypatch.setattr(
        runtime_module,
        "_ACTIVE_SANDBOX_CONFIG",
        CaliberConfig(registered_tool_sandbox_enabled=False),
    )
    assert runtime_module._bind_in_process(_binding("no_such_module_anywhere", "x")) is None


def test_the_reference_profile_never_starts_a_process_for_a_module_the_operator_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module allowlist narrows what an operator permits; it is not a way to the sandbox in the reference profile."""
    from tests.refusal_probes import Spawns

    spawns = Spawns(monkeypatch)
    config = CaliberConfig(profile="reference", registered_tool_module_allowlist=f"{ADMIN_MODULE}")
    monkeypatch.setattr(runtime_module, "_ACTIVE_SANDBOX_CONFIG", config)

    with pytest.raises(UnsupportedEffectPath):
        runtime_module._bind(_binding(ADMIN_MODULE, ADMIN_CALLABLE), InMemoryToolResolver([]))
    assert spawns.count == 0


def test_a_refusal_surfaces_through_a_route_as_409_with_a_reason_code_and_no_request_value() -> (
    None
):
    config = types.SimpleNamespace(profile="reference")

    with pytest.raises(CaliberHTTPException) as caught:
        refuse_route_in_reference_profile("EP-044", config)

    assert isinstance(caught.value, HTTPException)
    assert caught.value.status_code == 409
    assert caught.value.reason_code == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert str(caught.value.detail).startswith("unsupported_in_reference_profile: EP-044 (")
    assert (
        refuse_route_in_reference_profile("EP-044", types.SimpleNamespace(profile="development"))
        is None
    )
    assert refuse_route_in_reference_profile("EP-044", None) is None
    with pytest.raises(ValueError, match="EP-043"):
        refuse_route_in_reference_profile("EP-043", types.SimpleNamespace(profile="development"))


def _reference_client(client: Any) -> Any:
    client.app.state.config = client.app.state.config.model_copy(update={"profile": "reference"})
    return client


def test_the_tool_source_and_test_routes_report_a_refusal_as_their_error_in_the_reference_profile(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EP-021 and EP-020 through the tool routes: an administrator's module is neither inspected nor run."""
    from tests.refusal_probes import Spawns
    from tests.workflow_helpers import PREFIX, make_tool_payload

    spawns = Spawns(monkeypatch)
    tool_id = client.post(
        f"{PREFIX}/tools",
        json=make_tool_payload(
            "run_lookup_policy", callable_name="lookup_policy", allow_in_preview=True
        ),
    ).json()["data"]["tool_id"]
    _reference_client(client)

    source = client.get(f"{PREFIX}/tools/{tool_id}/source")
    assert source.status_code == 200
    body = source.json()["data"]
    assert body["available"] is False
    assert body["error"].startswith("unsupported_in_reference_profile: EP-021 (")
    assert body["source"] == ""

    ran = client.post(f"{PREFIX}/tools/{tool_id}/test-run", json={"input": {"query": "refund"}})
    assert ran.status_code == 200, ran.text
    result = ran.json()["data"]
    assert result["isolation"] == "refused"
    assert result["output"] is None
    assert result["error"].startswith("unsupported_in_reference_profile: EP-020 (")
    assert "refund" not in result["error"]
    assert spawns.count == 0


def test_the_tool_source_route_still_describes_a_shipped_builtin_in_the_reference_profile(
    client: Any,
) -> None:
    """The exemption through a route: CALIBER's own ``grok_parse`` is inspected, for real, by the reference sandbox."""
    from tests.workflow_helpers import PREFIX

    _reference_client(client)
    seeded = next(
        row
        for row in client.get(f"{PREFIX}/tools").json()["data"]
        if row["builtin"] is True and row["name"] == "grok_parse"
    )

    source = client.get(f"{PREFIX}/tools/{seeded['tool_id']}/source")

    assert source.status_code == 200
    body = source.json()["data"]
    assert body["available"] is True, body
    assert "def grok_parse" in body["source"]
    assert body["error"] is None


# ---- a refusal is not an item's own error --------------------------------------------------------------------------------

_PYTHON_TARGET: dict[str, Any] = {
    "id": "python",
    "type": "python_code",
    "code": 'return {"text": str(input).upper(), "result": {"seen": input}}',
    "inputs": {"input": {"type": "string"}},
    "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
}


def _for_each_manifest() -> dict[str, Any]:
    from tests.workflow_helpers import make_manifest

    data = make_manifest()
    data["nodes"]["python"] = dict(_PYTHON_TARGET)
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "python",
        "inputs": {"items": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "results": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_loop", "from": "start", "to": "for_each", "map": {"msg": "items"}},
        {"id": "e_loop_final", "from": "for_each", "to": "final", "map": {"text": "response"}},
    ]
    return data


def _error_boundary_manifest() -> dict[str, Any]:
    from tests.workflow_helpers import make_manifest

    data = make_manifest()
    data["nodes"]["python"] = dict(_PYTHON_TARGET)
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "python",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return data


def _run(data: dict[str, Any], config: CaliberConfig, run_input: str) -> Any:
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
    from tests.workflow_helpers import fake_resolver

    ir = build_ir(parse_manifest(data), fake_resolver(), version="7")
    plan = RuntimePlan(ir=ir, resolver=fake_resolver(), sandbox_config=config)
    return execute(plan, run_input, executor=FakeWorkflowExecutor())


def _compensation_manifest() -> dict[str, Any]:
    from tests.workflow_helpers import make_manifest

    data = make_manifest()
    data["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "retrieval_modes": ["dense"],
        "top_k": 3,
        "inputs": {"question": {"type": "string"}},
        "outputs": {"text": {"type": "string"}, "result": {"type": "structured"}},
    }
    data["nodes"]["python"] = dict(_PYTHON_TARGET)
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "knowledge",
        "compensate_with": "python",
        "fallback_text": "fallback",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}, "error": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e_start_boundary", "from": "start", "to": "boundary", "map": {"msg": "input"}},
        {
            "id": "e_boundary_final",
            "from": "boundary",
            "to": "final",
            "map": {"output": "response"},
        },
    ]
    return data


def test_a_refused_compensation_node_is_the_runs_error_not_a_wrapped_compensation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.refusal_probes import Spawns

    spawns = Spawns(monkeypatch)

    result = _run(_compensation_manifest(), REFERENCE, "hello")

    assert result.status == "error"
    assert "unsupported_in_reference_profile: EP-011" in (result.error or "")
    assert "compensation target" not in (result.error or ""), (
        "the refusal was wrapped as a compensation failure"
    )
    assert spawns.count == 0


def test_the_same_compensation_runs_in_the_development_profile() -> None:
    result = _run(_compensation_manifest(), DEVELOPMENT, "hello")

    assert result.status == "completed", result.error
    assert result.output == "HELLO"


@pytest.mark.parametrize("shape", ["for_each", "error_boundary"])
def test_a_node_the_reference_profile_refuses_fails_the_run_instead_of_being_absorbed_as_an_item_error(
    shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.refusal_probes import Spawns

    spawns = Spawns(monkeypatch)
    data = _for_each_manifest() if shape == "for_each" else _error_boundary_manifest()

    result = _run(data, REFERENCE, '["alpha","beta"]' if shape == "for_each" else "hello")

    assert result.status == "error"
    assert "unsupported_in_reference_profile: EP-011" in (result.error or "")
    assert spawns.count == 0


def test_the_same_workflows_complete_in_the_development_profile() -> None:
    looped = _run(_for_each_manifest(), DEVELOPMENT, '["alpha","beta"]')
    bounded = _run(_error_boundary_manifest(), DEVELOPMENT, "hello")

    assert looped.status == "completed", looped.error
    assert looped.output == "ALPHA\nBETA"
    assert bounded.status == "completed", bounded.error


# ---- the sandbox's own boundary -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "run", "source_code": "def grok_parse(**kw): return 1", "module_path": "SHIPPED"},
        {"mode": "tests", "module_path": "SHIPPED"},
    ],
)
def test_a_payload_that_carries_source_or_tests_is_authored_even_when_it_names_a_shipped_tool(
    payload: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spawn point judges the payload by what it would run: a shipped tool's name never makes authored code shipped."""
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox
    from tests.refusal_probes import SHIPPED_CALLABLE, SHIPPED_MODULE, Spawns

    spawns = Spawns(monkeypatch)
    body = {
        **{k: (SHIPPED_MODULE if v == "SHIPPED" else v) for k, v in payload.items()},
        "callable_name": SHIPPED_CALLABLE,
    }

    with pytest.raises(UnsupportedEffectPath) as caught:
        LocalSubprocessToolSandbox.from_config(REFERENCE)._run_payload(body, timeout_seconds=1)

    assert caught.value.code == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert "EP-023" in str(caught.value)
    assert spawns.count == 0


def test_only_a_sandbox_built_from_the_reference_configuration_carries_the_flag() -> None:
    from caliber.tool_sandbox.service import (
        LocalSubprocessToolSandbox,
        sandbox_from_optional_config,
    )

    assert LocalSubprocessToolSandbox().reference_profile is False
    assert LocalSubprocessToolSandbox.from_config(DEVELOPMENT).reference_profile is False
    assert LocalSubprocessToolSandbox.from_config(REFERENCE).reference_profile is True
    assert sandbox_from_optional_config(None).reference_profile is False  # type: ignore[attr-defined]
    assert sandbox_from_optional_config(REFERENCE).reference_profile is True  # type: ignore[attr-defined]
    assert sandbox_from_optional_config(DEVELOPMENT).reference_profile is False  # type: ignore[attr-defined]
