"""B12.4a (#754): the reference profile refuses executable code, decided from the compiler's own IR kind registry.

Every kind in :class:`caliber.workflows.ir.NodeType` is classified exactly once (code, tool carrier or inert), a tool binding
is judged by its execution form, and one refusal code, ``unsupported_in_reference_profile``, is raised at validation, compile,
promotion and runtime entry. The tests here run offline and without PostgreSQL; the public-path scenario on a real reference
app is ``tests/egi_scenarios/test_b12_code_refusal.py``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from caliber import server as caliber_server
from caliber.config import CaliberConfig, ConfigError
from caliber.effects.dispatcher import UnsupportedEffectPath
from caliber.workflows import compiler as compiler_module
from caliber.workflows import runtime as runtime_module
from caliber.workflows.code_policy import (
    CODE_NODE_KINDS,
    INERT_NODE_KINDS,
    REFUSAL_CODE,
    TOOL_NODE_KINDS,
    CodePolicy,
    CodeRefusal,
    CodeRefusalError,
    bind_code_policy,
    bound_code_policy,
    ir_refusals,
    manifest_refusals,
    node_kind_refusal,
    require_no_code,
    require_sandboxed_tool_binding,
    resolve_code_policy,
    summarize,
    tool_form_refusal,
)
from caliber.workflows.compiler import (
    CompileError,
    build_ir,
    clear_compile_cache,
    compile_workflow,
)
from caliber.workflows.ir import IRTool, IRToolBinding, NodeType
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.promoter import (
    AliasPreflightError,
    require_alias_target_ready,
)
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from caliber.workflows.validation import validate_manifest
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    fake_resolver,
    make_manifest,
)

REFERENCE = CodePolicy(reference=True, sandbox_enabled=True)
REFERENCE_UNSANDBOXED = CodePolicy(reference=True, sandbox_enabled=False)
DEVELOPMENT = CodePolicy()


@pytest.fixture(autouse=True)
def _fresh_compile_cache() -> Iterator[None]:
    clear_compile_cache()
    yield
    clear_compile_cache()


@pytest.fixture
def external_app_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """An installed module the ``external_app`` fixture manifest points at; every call it receives is recorded."""
    import sys
    import types

    calls: list[str] = []
    module = types.ModuleType("workflow_external_app")

    def run(input: Any = None, **_kwargs: Any) -> dict[str, Any]:
        calls.append(str(input))
        return {"text": f"ran {input}"}

    module.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "workflow_external_app", module)
    return calls


# --------------------------------------------------------------------------------------------------------------------
# Manifests: one per registered executable kind, and tool forms.
# --------------------------------------------------------------------------------------------------------------------


def _python_code_manifest() -> dict[str, Any]:
    data = make_manifest()
    data["nodes"]["code"] = {
        "id": "code",
        "type": "python_code",
        "code": 'return {"text": (input or "").upper(), "result": {"ok": True}}',
        "timeout_seconds": 5,
        "inputs": {"input": {"type": "string"}, "context": {"type": "structured"}},
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "code", "map": {"msg": "input"}},
        {"id": "e2", "from": "code", "to": "final", "map": {"text": "response"}},
    ]
    return data


def _external_app_manifest() -> dict[str, Any]:
    data = make_manifest()
    data["nodes"]["bridge"] = {
        "id": "bridge",
        "type": "external_app",
        "entrypoint": "workflow_external_app:run",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "bridge", "map": {"msg": "input"}},
        {"id": "e2", "from": "bridge", "to": "final", "map": {"text": "response"}},
    ]
    return data


#: One manifest per node kind in ``CODE_NODE_KINDS``; a test fails when a registered executable kind has none.
CODE_KIND_MANIFESTS: dict[NodeType, Callable[[], dict[str, Any]]] = {
    NodeType.PYTHON_CODE: _python_code_manifest,
    NodeType.EXTERNAL_APP: _external_app_manifest,
}
CODE_KIND_NODE_IDS = {NodeType.PYTHON_CODE: "code", NodeType.EXTERNAL_APP: "bridge"}


def _tool_manifest(*, via: str = "agent") -> dict[str, Any]:
    """A workflow whose ``lookup`` tool is carried by the agent, or by a ``tool`` node."""
    data = make_manifest()
    data["tools"] = {"lookup": {"type": "registered_function", "registry_ref": "tool.lookup.v1"}}
    if via == "agent":
        data["nodes"]["agent"]["tools"] = ["lookup"]
    else:
        data["nodes"]["tool"] = {"id": "tool", "type": "tool", "tool_name": "lookup"}
        data["edges"] = [
            {"id": "e1", "from": "start", "to": "tool", "map": {"msg": "input"}},
            {"id": "e2", "from": "tool", "to": "final", "map": {"text": "response"}},
        ]
    return data


#: The exact (module, callable) pair the registry seed writes for the shipped ``grok_parse`` built-in (B04.7, #774).
SHIPPED_MODULE, SHIPPED_CALLABLE = "caliber.workflows.demo_tools", "grok_parse"


def _resolver(backend: str, *, shipped: bool = False) -> InMemoryToolResolver:
    """A registry with one tool named ``lookup``: an administrator's module, or (``shipped``) one of CALIBER's own."""
    return InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name="lookup",
                version="1.0",
                module_path=SHIPPED_MODULE if shipped else "mycompany.tools",
                callable_name=SHIPPED_CALLABLE if shipped else "lookup",
                execution_backend=backend,
            )
        ]
    )


def _mcp_manifest() -> dict[str, Any]:
    data = make_manifest()
    data["tools"] = {
        "remote": {"type": "mcp_tool", "server_id": "srv", "tool_name": "search"},
    }
    data["nodes"]["agent"]["tools"] = ["remote"]
    return data


def _ir_tool_binding(backend: str, *, binding_type: str = "registered_function") -> IRToolBinding:
    return IRToolBinding(
        local_name="lookup",
        registry_ref="tool.lookup.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=False,
        module_path="mycompany.tools",
        callable_name="lookup",
        execution_backend=backend,
        binding_type=binding_type,
    )


def _plan(
    manifest: dict[str, Any], resolver: InMemoryToolResolver | None = None, **kw: Any
) -> RuntimePlan:
    """A runtime plan built by hand from a manifest compiled *outside* the reference profile."""
    resolver = resolver or fake_resolver()
    ir = build_ir(parse_manifest(manifest), resolver, version="7")
    kw.setdefault("external_app_entrypoint_allowlist", "workflow_external_*")
    return RuntimePlan(ir=ir, resolver=resolver, **kw)


# --------------------------------------------------------------------------------------------------------------------
# The kind registry
# --------------------------------------------------------------------------------------------------------------------


def test_every_registered_kind_is_classified_exactly_once() -> None:
    """The ratchet: a kind added to the compiler's registry without a classification fails here."""
    code, tool, inert = set(CODE_NODE_KINDS), set(TOOL_NODE_KINDS), set(INERT_NODE_KINDS)
    assert not (code & tool or code & inert or tool & inert), "a kind is classified twice"
    assert code | tool | inert == set(NodeType), (
        f"unclassified kinds: {sorted(k.value for k in set(NodeType) - (code | tool | inert))}"
    )


def test_the_code_kinds_are_the_ones_that_execute_code() -> None:
    assert set(CODE_NODE_KINDS) == {NodeType.PYTHON_CODE, NodeType.EXTERNAL_APP}
    assert set(TOOL_NODE_KINDS) == {NodeType.AGENT, NodeType.TOOL}


def test_every_manifest_node_type_is_a_registered_kind() -> None:
    """The manifest's node ``type`` strings are read through the registry, so each must be a registry value."""
    from typing import get_args

    from caliber.workflows.manifest import WorkflowNode

    union_types = {
        get_args(member.model_fields["type"].annotation)[0]
        for member in get_args(get_args(WorkflowNode)[0])
    }
    assert union_types <= {kind.value for kind in NodeType}


def test_every_code_kind_has_a_manifest_fixture() -> None:
    assert set(CODE_KIND_MANIFESTS) == set(CODE_NODE_KINDS)


# --------------------------------------------------------------------------------------------------------------------
# Validation, compile, promotion and runtime entry, for every registered executable kind
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", sorted(CODE_NODE_KINDS, key=lambda k: k.value), ids=lambda k: k.value
)
class TestEveryRegisteredExecutableKind:
    def test_validation_refuses_with_the_one_code(self, kind: NodeType) -> None:
        manifest = parse_manifest(CODE_KIND_MANIFESTS[kind]())
        report = validate_manifest(manifest, resolver=fake_resolver(), code_policy=REFERENCE)
        refused = [e for e in report.errors if e.code == REFUSAL_CODE]
        assert [e.path for e in refused] == [f"nodes.{CODE_KIND_NODE_IDS[kind]}"]
        assert kind.value in refused[0].message
        assert "reference profile" in refused[0].message
        assert not report.valid
        assert report.to_dict()["errors"][0]["code"] == REFUSAL_CODE

    def test_validation_outside_the_profile_is_unchanged(self, kind: NodeType) -> None:
        manifest = parse_manifest(CODE_KIND_MANIFESTS[kind]())
        report = validate_manifest(manifest, resolver=fake_resolver(), code_policy=DEVELOPMENT)
        assert report.valid, report.to_dict()

    def test_compile_refuses_with_the_one_code(self, kind: NodeType) -> None:
        manifest = parse_manifest(CODE_KIND_MANIFESTS[kind]())
        with pytest.raises(CompileError) as raised:
            compile_workflow(manifest, resolver=fake_resolver(), code_policy=REFERENCE)
        report = raised.value.report
        assert report is not None and report["valid"] is False
        assert [e["code"] for e in report["errors"] if e["code"] == REFUSAL_CODE]
        # The manifest is refused by validation, the first boundary of compile; the IR check is only the second.
        assert str(raised.value) == "manifest failed validation; cannot compile"

    def test_compile_outside_the_profile_succeeds(self, kind: NodeType) -> None:
        manifest = parse_manifest(CODE_KIND_MANIFESTS[kind]())
        assert compile_workflow(manifest, resolver=fake_resolver(), code_policy=DEVELOPMENT).ir

    def test_compile_also_refuses_the_built_ir_when_validation_was_bypassed(
        self, kind: NodeType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The IR check is its own boundary: a validator that said nothing does not let a code kind through."""
        from caliber.workflows.validation import ValidationReport

        monkeypatch.setattr(
            compiler_module, "validate_manifest", lambda *a, **k: ValidationReport()
        )
        manifest = parse_manifest(CODE_KIND_MANIFESTS[kind]())
        with pytest.raises(CompileError) as raised:
            compile_workflow(manifest, resolver=fake_resolver(), code_policy=REFERENCE)
        assert raised.value.report is not None
        assert raised.value.report["errors"][0]["code"] == REFUSAL_CODE
        assert raised.value.report["errors"][0]["path"] == f"nodes.{CODE_KIND_NODE_IDS[kind]}"

    def test_runtime_entry_refuses_a_hand_built_plan_and_runs_nothing(
        self, kind: NodeType, monkeypatch: pytest.MonkeyPatch, external_app_calls: list[str]
    ) -> None:
        """Direct runtime bypass: the IR was compiled outside the profile and handed straight to ``execute``."""
        reached: list[str] = []
        for name in ("sandbox_from_optional_config", "_run_external_app_entrypoint"):
            monkeypatch.setattr(runtime_module, name, lambda *a, _n=name, **k: reached.append(_n))
        executor = FakeWorkflowExecutor()
        plan = _plan(CODE_KIND_MANIFESTS[kind](), code_policy=REFERENCE)

        result = execute(plan, "hello", executor=executor)

        assert result.status == "error"
        assert result.error_code == REFUSAL_CODE
        assert result.error is not None and result.error.startswith(f"{REFUSAL_CODE}: ")
        assert result.output == ""
        assert [(s.node_id, s.node_type, s.status) for s in result.steps] == [
            (CODE_KIND_NODE_IDS[kind], kind.value, "error")
        ]
        assert reached == [] and executor.calls == [] and external_app_calls == []

    def test_runtime_entry_refuses_through_the_process_wide_policy(self, kind: NodeType) -> None:
        """A plan with no policy of its own (hand-built in a reference process) is refused all the same."""
        bind_code_policy(REFERENCE)
        result = execute(
            _plan(CODE_KIND_MANIFESTS[kind]()), "hello", executor=FakeWorkflowExecutor()
        )
        assert result.status == "error" and result.error_code == REFUSAL_CODE

    def test_the_process_wide_policy_refuses_validation_and_compile_too(
        self, kind: NodeType
    ) -> None:
        bind_code_policy(REFERENCE)
        manifest = parse_manifest(CODE_KIND_MANIFESTS[kind]())
        assert not validate_manifest(manifest, resolver=fake_resolver()).valid
        with pytest.raises(CompileError):
            compile_workflow(manifest, resolver=fake_resolver())

    def test_runtime_outside_the_profile_still_runs_the_node(
        self, kind: NodeType, external_app_calls: list[str]
    ) -> None:
        result = execute(
            _plan(CODE_KIND_MANIFESTS[kind]()), "hello", executor=FakeWorkflowExecutor()
        )
        assert result.status == "completed", result.error
        assert result.error_code is None
        assert (
            result.output
            == {NodeType.PYTHON_CODE: "HELLO", NodeType.EXTERNAL_APP: "ran hello"}[kind]
        )
        assert external_app_calls == (["hello"] if kind is NodeType.EXTERNAL_APP else [])


def test_code_node_refused() -> None:
    """The named acceptance test: a ``python_code`` node is refused at every boundary with the one structured code."""
    manifest = parse_manifest(_python_code_manifest())
    codes = {
        "validation": [
            e.code
            for e in validate_manifest(
                manifest, resolver=fake_resolver(), code_policy=REFERENCE
            ).errors
        ],
    }
    with pytest.raises(CompileError) as compile_error:
        compile_workflow(manifest, resolver=fake_resolver(), code_policy=REFERENCE)
    codes["compile"] = [e["code"] for e in compile_error.value.report["errors"]]  # type: ignore[index]
    runtime = execute(
        _plan(_python_code_manifest(), code_policy=REFERENCE), "x", executor=FakeWorkflowExecutor()
    )
    codes["runtime"] = [runtime.error_code]  # type: ignore[list-item]
    assert codes == {
        "validation": [REFUSAL_CODE],
        "compile": [REFUSAL_CODE],
        "runtime": [REFUSAL_CODE],
    }


# --------------------------------------------------------------------------------------------------------------------
# Tool forms
# --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["agent", "tool"])
def test_unsandboxed_tool_refused(via: str) -> None:
    """The named acceptance test: a registered Python tool with the sandbox off would run inside the control plane."""
    resolver = _resolver("python_callable")
    manifest = parse_manifest(_tool_manifest(via=via))

    report = validate_manifest(manifest, resolver=resolver, code_policy=REFERENCE_UNSANDBOXED)
    refused = [e for e in report.errors if e.code == REFUSAL_CODE]
    assert len(refused) == 1
    assert refused[0].path == f"nodes.{'agent' if via == 'agent' else 'tool'}.tools.lookup"
    assert "CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED is false" in refused[0].message
    assert "unsandboxed" in refused[0].message

    with pytest.raises(CompileError) as raised:
        compile_workflow(manifest, resolver=resolver, code_policy=REFERENCE_UNSANDBOXED)
    assert raised.value.report["errors"][0]["code"] == REFUSAL_CODE  # type: ignore[index]

    plan = _plan(_tool_manifest(via=via), resolver, code_policy=REFERENCE_UNSANDBOXED)
    executor = FakeWorkflowExecutor()
    result = execute(plan, "x", executor=executor)
    assert result.status == "error" and result.error_code == REFUSAL_CODE
    assert executor.calls == [] and [s.status for s in result.steps] == ["error"]


@pytest.mark.parametrize("via", ["agent", "tool"])
def test_a_sandbox_label_on_the_tool_does_not_establish_isolation(via: str) -> None:
    """A ``sandbox`` flag a registry row carries is data an author controls: the platform's own path decides."""
    labelled = InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name="lookup",
                version="1.0",
                module_path=SHIPPED_MODULE,
                callable_name=SHIPPED_CALLABLE,
                backend_config={"sandbox": True, "isolated": True},
            )
        ]
    )
    manifest = parse_manifest(_tool_manifest(via=via))
    refused = [
        e
        for e in validate_manifest(
            manifest, resolver=labelled, code_policy=REFERENCE_UNSANDBOXED
        ).errors
        if e.code == REFUSAL_CODE
    ]
    assert len(refused) == 1
    assert "A sandbox label on the tool does not change this" in refused[0].message
    ir = build_ir(manifest, labelled)
    assert ir.nodes[via if via == "tool" else "agent"] is not None
    assert [r.code for r in ir_refusals(ir, REFERENCE_UNSANDBOXED)] == [REFUSAL_CODE]
    # And with the platform's sandbox on, the same row (a shipped tool) is admitted: the label never entered into it.
    assert ir_refusals(ir, REFERENCE) == []
    # The label does not make an administrator's module admitted either: the platform's seed decides what is shipped.
    admin = InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name="lookup",
                version="1.0",
                module_path="mycompany.tools",
                callable_name="lookup",
                backend_config={"sandbox": True, "isolated": True},
            )
        ]
    )
    refused_admin = ir_refusals(build_ir(manifest, admin), REFERENCE)
    assert [r.code for r in refused_admin] == [REFUSAL_CODE]
    assert "not one of CALIBER's own shipped tools" in refused_admin[0].message


@pytest.mark.parametrize("via", ["agent", "tool"])
def test_a_shipped_registered_tool_is_admitted_in_the_sandbox(via: str) -> None:
    """CALIBER's own shipped tool, run in the sandbox child, is admitted at every author-time boundary."""
    resolver = _resolver("python_callable", shipped=True)
    manifest = parse_manifest(_tool_manifest(via=via))
    assert validate_manifest(manifest, resolver=resolver, code_policy=REFERENCE).valid
    assert compile_workflow(manifest, resolver=resolver, code_policy=REFERENCE).ir
    assert ir_refusals(build_ir(manifest, resolver), REFERENCE) == []


@pytest.mark.parametrize("via", ["agent", "tool"])
def test_an_administrators_python_tool_is_refused_at_author_time_with_the_sandbox_on(
    via: str,
) -> None:
    """B04.7 (#774): administrator Python is a refused path in the reference profile (EP-015, EP-016, EP-019).

    An author gets the refusal at validation, compile and promotion, not a clean publish followed by a refused run.
    """
    resolver = _resolver("python_callable")
    manifest = parse_manifest(_tool_manifest(via=via))

    report = validate_manifest(manifest, resolver=resolver, code_policy=REFERENCE)
    refused = [e for e in report.errors if e.code == REFUSAL_CODE]
    assert len(refused) == 1 and not report.valid
    assert "not one of CALIBER's own shipped tools" in refused[0].message
    assert "openapi_http" in refused[0].message  # the recovery
    with pytest.raises(CompileError):
        compile_workflow(manifest, resolver=resolver, code_policy=REFERENCE)
    assert [r.code for r in ir_refusals(build_ir(manifest, resolver), REFERENCE)] == [REFUSAL_CODE]
    # The development profile is unchanged.
    assert validate_manifest(manifest, resolver=resolver, code_policy=DEVELOPMENT).valid


def test_the_policy_reads_the_sandbox_setting_and_the_profile() -> None:
    assert REFERENCE.sandbox_enabled and not REFERENCE_UNSANDBOXED.sandbox_enabled
    assert REFERENCE.reference and REFERENCE_UNSANDBOXED.reference
    assert not DEVELOPMENT.reference and DEVELOPMENT.sandbox_enabled


def test_an_openapi_tool_runs_no_registered_python_and_is_admitted() -> None:
    resolver = _resolver("openapi_http")
    manifest = parse_manifest(_tool_manifest())
    assert validate_manifest(manifest, resolver=resolver, code_policy=REFERENCE).valid
    assert compile_workflow(manifest, resolver=resolver, code_policy=REFERENCE).ir


def test_an_mcp_binding_is_not_registered_python_and_is_admitted() -> None:
    manifest = parse_manifest(_mcp_manifest())
    assert manifest_refusals(manifest, fake_resolver(), REFERENCE) == []
    ir = build_ir(manifest, fake_resolver())
    assert ir_refusals(ir, REFERENCE) == []


def test_an_unknown_execution_backend_is_refused_fail_closed() -> None:
    resolver = _resolver("containerized_thing")
    manifest = parse_manifest(_tool_manifest())
    refused = list(validate_manifest(manifest, resolver=resolver, code_policy=REFERENCE).errors)
    assert [e.code for e in refused] == [REFUSAL_CODE]
    assert "containerized_thing" in refused[0].message
    assert "openapi_http" in refused[0].message  # the recovery


def test_an_empty_backend_on_a_resolved_entry_is_the_python_callable_default() -> None:
    """A registry row with no backend recorded is a Python tool (the schema default), not an unknown form."""
    resolver = _resolver("", shipped=True)
    manifest = parse_manifest(_tool_manifest())
    assert manifest_refusals(manifest, resolver, REFERENCE) == []
    refused = manifest_refusals(manifest, resolver, REFERENCE_UNSANDBOXED)
    assert [r.code for r in refused] == [REFUSAL_CODE]
    assert "python_callable" in refused[0].message


def test_a_tool_no_resolver_can_resolve_is_left_to_the_missing_tool_check() -> None:
    manifest = parse_manifest(_tool_manifest())
    empty = InMemoryToolResolver([])
    assert manifest_refusals(manifest, empty, REFERENCE) == []
    report = validate_manifest(manifest, resolver=empty, code_policy=REFERENCE)
    assert [e.code for e in report.errors] == ["missing_tool"]
    with pytest.raises(CompileError):
        compile_workflow(manifest, resolver=empty, code_policy=REFERENCE)


def test_registered_tools_cannot_be_judged_without_a_resolver_at_validation() -> None:
    manifest = parse_manifest(_tool_manifest())
    assert manifest_refusals(manifest, None, REFERENCE) == []


def test_an_unbound_tool_reference_is_left_to_the_unbound_tool_check() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["ghost"]
    manifest = parse_manifest(data)
    assert manifest_refusals(manifest, fake_resolver(), REFERENCE) == []


def test_each_node_that_carries_a_refused_tool_is_reported_once() -> None:
    data = _tool_manifest()
    data["nodes"]["tool"] = {"id": "tool", "type": "tool", "tool_name": "lookup"}
    manifest = parse_manifest(data)
    paths = [
        r.path
        for r in manifest_refusals(manifest, _resolver("python_callable"), REFERENCE_UNSANDBOXED)
    ]
    assert paths == ["nodes.agent.tools.lookup", "nodes.tool.tools.lookup"]


def test_the_ir_check_walks_every_node_not_only_the_reachable_ones() -> None:
    """A dormant code node (no edge reaches it) is refused: a graph edit must not make it live."""
    data = _python_code_manifest()
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    ir = build_ir(parse_manifest(data), fake_resolver())
    assert [r.path for r in ir_refusals(ir, REFERENCE)] == ["nodes.code"]


def test_ir_refusals_cover_the_tool_node_binding() -> None:
    ir = build_ir(parse_manifest(_tool_manifest(via="tool")), _resolver("python_callable"))
    tool_node = ir.nodes["tool"]
    assert isinstance(tool_node, IRTool) and tool_node.binding is not None
    assert [r.path for r in ir_refusals(ir, REFERENCE_UNSANDBOXED)] == ["nodes.tool.tools.lookup"]
    tool_node.binding = None
    assert ir_refusals(ir, REFERENCE_UNSANDBOXED) == []


@pytest.mark.parametrize(
    ("backend", "binding_type", "policy", "shipped", "refused"),
    [
        # A shipped tool in the sandbox is admitted; an administrator's module is refused (B04.7, #774); either is refused
        # with the sandbox off.
        ("python_callable", "registered_function", REFERENCE, True, False),
        ("python_callable", "registered_function", REFERENCE, False, True),
        ("python_callable", "registered_function", REFERENCE_UNSANDBOXED, True, True),
        ("python_callable", "registered_function", REFERENCE_UNSANDBOXED, False, True),
        ("python_callable", "mcp_tool", REFERENCE_UNSANDBOXED, False, False),
        ("openapi_http", "registered_function", REFERENCE_UNSANDBOXED, False, False),
        ("anything_else", "registered_function", REFERENCE, True, True),
        ("anything_else", "mcp_tool", REFERENCE, False, False),
    ],
)
def test_tool_form_decision_table(
    backend: str, binding_type: str, policy: CodePolicy, shipped: bool, refused: bool
) -> None:
    result = tool_form_refusal(
        "lookup",
        node_id="n",
        path="nodes.n.tools.lookup",
        execution_backend=backend,
        binding_type=binding_type,
        policy=policy,
        module_path=SHIPPED_MODULE if shipped else "mycompany.tools",
        callable_name=SHIPPED_CALLABLE if shipped else "lookup",
    )
    assert (result is not None) is refused
    if result is not None:
        assert result.code == REFUSAL_CODE and result.node_id == "n" and result.kind == "tool"


# --------------------------------------------------------------------------------------------------------------------
# The in-process binders: the last boundary before an unsandboxed import
# --------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def sandbox_config_restored() -> Iterator[None]:
    previous = runtime_module._ACTIVE_SANDBOX_CONFIG
    yield
    runtime_module.bind_sandbox_config(previous)


def _entry() -> ToolRegistryEntry:
    """A shipped tool (the seed's own module and callable): the one kind the reference profile binds."""
    return ToolRegistryEntry(
        name="grok_parse",
        version="1.0",
        module_path=SHIPPED_MODULE,
        callable_name=SHIPPED_CALLABLE,
    )


def _administrator_entry() -> ToolRegistryEntry:
    return ToolRegistryEntry(
        name="lookup_policy",
        version="1.0",
        module_path="caliber.workflows.demo_tools",
        callable_name="lookup_policy",
    )


def test_the_runtime_binder_never_imports_a_tool_in_process_under_the_reference_profile(
    sandbox_config_restored: None,
) -> None:
    """Attempted direct bypass: sandbox off, binder called by hand, a ``sandbox`` label on the binding."""
    binding = _ir_tool_binding("python_callable")
    labelled = IRToolBinding(
        **{**binding.__dict__, "module_path": "os", "callable_name": "getpid"},
    )
    runtime_module.bind_sandbox_config(CaliberConfig(registered_tool_sandbox_enabled=False))
    bind_code_policy(REFERENCE_UNSANDBOXED)

    with pytest.raises(CodeRefusalError) as raised:
        runtime_module._bind(labelled, InMemoryToolResolver([]))
    assert raised.value.code == REFUSAL_CODE
    assert "unsandboxed" in str(raised.value)

    # Outside the profile the legacy in-process binder is still reachable, as before.
    bind_code_policy(None)
    fn = runtime_module._bind(labelled, InMemoryToolResolver([]))
    assert fn is not None
    import os

    assert fn() == os.getpid()


def test_a_generated_export_never_imports_a_tool_in_process_under_the_reference_profile() -> None:
    reference = CaliberConfig(profile="reference", registered_tool_sandbox_enabled=False)
    with pytest.raises(CodeRefusalError) as raised:
        runtime_module.bind_exported_tool(_entry(), config=reference)
    assert raised.value.code == REFUSAL_CODE

    # The sandboxed path of the same profile is untouched, and so is the same export outside the profile.
    sandboxed = CaliberConfig(profile="reference", registered_tool_sandbox_enabled=True)
    assert callable(runtime_module.bind_exported_tool(_entry(), config=sandboxed))
    unsandboxed_dev = CaliberConfig(registered_tool_sandbox_enabled=False)
    assert callable(runtime_module.bind_exported_tool(_entry(), config=unsandboxed_dev))


def test_a_generated_export_never_binds_an_administrators_module_under_the_reference_profile() -> (
    None
):
    """B04.7 (#774, EP-016): an administrator's module is refused by the export binder, whatever the sandbox says."""
    from caliber.effects.dispatcher import UnsupportedInReferenceProfile

    for sandbox_enabled in (True, False):
        reference = CaliberConfig(
            profile="reference", registered_tool_sandbox_enabled=sandbox_enabled
        )
        with pytest.raises(UnsupportedInReferenceProfile) as raised:
            runtime_module.bind_exported_tool(_administrator_entry(), config=reference)
        assert raised.value.code == REFUSAL_CODE
    # Outside the profile it still binds, as before.
    assert callable(
        runtime_module.bind_exported_tool(_administrator_entry(), config=CaliberConfig())
    )


def test_the_binder_guard_uses_the_process_wide_policy_when_the_export_config_is_a_development_one() -> (
    None
):
    bind_code_policy(REFERENCE_UNSANDBOXED)
    with pytest.raises(CodeRefusalError):
        runtime_module.bind_exported_tool(
            _entry(), config=CaliberConfig(registered_tool_sandbox_enabled=False)
        )


def test_require_sandboxed_tool_binding_is_a_no_op_outside_the_profile() -> None:
    require_sandboxed_tool_binding("lookup")
    require_sandboxed_tool_binding("lookup", DEVELOPMENT)
    with pytest.raises(CodeRefusalError) as raised:
        require_sandboxed_tool_binding("lookup", REFERENCE)
    assert [r.path for r in raised.value.refusals] == ["tools.lookup"]


# --------------------------------------------------------------------------------------------------------------------
# The policy object and its resolution
# --------------------------------------------------------------------------------------------------------------------


def test_the_policy_is_inactive_until_bound_and_resolves_to_the_stricter_source() -> None:
    assert bound_code_policy() == DEVELOPMENT
    assert resolve_code_policy(None) == DEVELOPMENT
    assert resolve_code_policy(DEVELOPMENT) == DEVELOPMENT
    # An explicit reference policy applies in a process that was not bound.
    assert resolve_code_policy(REFERENCE) == REFERENCE
    bind_code_policy(REFERENCE)
    assert resolve_code_policy(None) == REFERENCE
    # A caller handing in a development policy does not relax a reference process.
    assert resolve_code_policy(DEVELOPMENT) == REFERENCE
    # The sandbox counts as enabled only when both sources say so.
    assert resolve_code_policy(REFERENCE_UNSANDBOXED) == REFERENCE_UNSANDBOXED
    bind_code_policy(REFERENCE_UNSANDBOXED)
    assert resolve_code_policy(REFERENCE) == REFERENCE_UNSANDBOXED
    assert resolve_code_policy(None) == REFERENCE_UNSANDBOXED
    bind_code_policy(None)
    assert bound_code_policy() == DEVELOPMENT


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, CodePolicy(reference=False, sandbox_enabled=True)),
        ({"profile": "reference"}, CodePolicy(reference=True, sandbox_enabled=True)),
        (
            {"profile": "reference", "registered_tool_sandbox_enabled": False},
            CodePolicy(reference=True, sandbox_enabled=False),
        ),
        (
            {"profile": "development", "registered_tool_sandbox_enabled": False},
            CodePolicy(reference=False, sandbox_enabled=False),
        ),
    ],
)
def test_policy_from_config(overrides: dict[str, Any], expected: CodePolicy) -> None:
    assert CodePolicy.from_config(CaliberConfig(**overrides)) == expected


def test_only_a_reference_profile_setting_refuses_a_stub_without_one_does_not() -> None:
    """Pins the comparison on config stubs (a generated export hands the binder one) so it cannot silently invert."""
    from types import SimpleNamespace

    assert CodePolicy.from_config(SimpleNamespace(profile="reference")).reference is True
    assert CodePolicy.from_config(SimpleNamespace(profile="development")).reference is False
    assert CodePolicy.from_config(SimpleNamespace(profile="staging")).reference is False
    assert CodePolicy.from_config(SimpleNamespace()).reference is False


def _without_classification(monkeypatch: pytest.MonkeyPatch, kind: NodeType) -> None:
    """Simulate a kind added to the registry that nobody classified yet."""
    from caliber.workflows import code_policy as module

    monkeypatch.setattr(module, "INERT_NODE_KINDS", frozenset(module.INERT_NODE_KINDS - {kind}))


def test_an_unclassified_registry_kind_is_refused_until_it_is_classified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: the default for a kind in no set is a refusal, not admission."""
    assert node_kind_refusal("n", "note") is None
    _without_classification(monkeypatch, NodeType.NOTE)
    refusal = node_kind_refusal("n", "note")
    assert refusal is not None and refusal.code == REFUSAL_CODE
    assert "has not classified" in refusal.message

    data = make_manifest()
    data["nodes"]["memo"] = {"id": "memo", "type": "note", "text": "hello"}
    manifest = parse_manifest(data)
    assert [r.path for r in manifest_refusals(manifest, fake_resolver(), REFERENCE)] == [
        "nodes.memo"
    ]
    with pytest.raises(CompileError) as raised:
        compile_workflow(manifest, resolver=fake_resolver(), code_policy=REFERENCE)
    assert raised.value.report["errors"][0]["code"] == REFUSAL_CODE  # type: ignore[index]
    # And at the runtime entry, for an IR compiled before the kind was unclassified-refused.
    monkeypatch.undo()
    plan = _plan(data, code_policy=REFERENCE)
    _without_classification(monkeypatch, NodeType.NOTE)
    result = execute(plan, "x", executor=FakeWorkflowExecutor())
    assert result.status == "error" and result.error_code == REFUSAL_CODE


def test_outside_the_reference_profile_nothing_is_refused_anywhere() -> None:
    manifest = parse_manifest(_python_code_manifest())
    assert manifest_refusals(manifest, fake_resolver(), DEVELOPMENT) == []
    ir = build_ir(manifest, fake_resolver())
    assert ir_refusals(ir, DEVELOPMENT) == []
    require_no_code(ir, DEVELOPMENT)


def test_require_no_code_raises_with_the_structured_code() -> None:
    ir = build_ir(parse_manifest(_python_code_manifest()), fake_resolver())
    with pytest.raises(CodeRefusalError) as raised:
        require_no_code(ir, REFERENCE)
    assert raised.value.code == REFUSAL_CODE
    assert [r.path for r in raised.value.refusals] == ["nodes.code"]
    assert str(raised.value).startswith(f"{REFUSAL_CODE}: python_code node 'code'")


def test_refusal_shape_and_summary() -> None:
    refusal = CodeRefusal(path="nodes.x", kind="python_code", message="m", node_id="x")
    assert refusal.to_dict() == {
        "code": REFUSAL_CODE,
        "path": "nodes.x",
        "message": "m",
        "severity": "error",
    }
    assert (
        summarize([refusal, CodeRefusal(path="p", kind="k", message="n", node_id="y")])
        == f"{REFUSAL_CODE}: m; n"
    )


def test_node_kind_refusal_for_a_known_inert_and_an_unregistered_kind() -> None:
    assert node_kind_refusal("n", "webhook") is None
    assert node_kind_refusal("n", "agent") is None
    unknown = node_kind_refusal("n", "shell_command")
    assert unknown is not None
    assert unknown.code == REFUSAL_CODE and unknown.node_id == "n"
    assert "shell_command" in unknown.message and "has not classified" in unknown.message


def test_a_manifest_node_of_an_unregistered_kind_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a kind the registry does not know is refused, not waved through."""
    manifest = parse_manifest(make_manifest())
    object.__setattr__(manifest.nodes["agent"], "type", "shell_command")
    refused = manifest_refusals(manifest, fake_resolver(), REFERENCE)
    assert [r.path for r in refused] == ["nodes.agent"]
    assert "shell_command" in refused[0].message


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (None, "runtime_error"),
        ({}, "runtime_error"),
        ({"errors": "not a list"}, "runtime_error"),
        ({"errors": [{"code": "missing_tool"}, "not a dict"]}, "runtime_error"),
        ({"errors": [{"code": "missing_tool"}, {"code": REFUSAL_CODE}]}, REFUSAL_CODE),
    ],
)
def test_a_run_whose_compile_failed_keeps_the_refusal_code_and_no_other(
    report: dict[str, Any] | None, expected: str
) -> None:
    from caliber.orchestrator.workflow_run_worker import _compile_error_code

    assert _compile_error_code(CompileError("failed", report=report)) == expected


# --------------------------------------------------------------------------------------------------------------------
# Compile cache
# --------------------------------------------------------------------------------------------------------------------


def test_a_cached_compile_from_outside_the_profile_is_not_handed_back_inside_it() -> None:
    manifest = parse_manifest(_python_code_manifest())
    first = compile_workflow(
        manifest, resolver=fake_resolver(), use_cache=True, code_policy=DEVELOPMENT
    )
    assert first.cached is False
    assert compile_workflow(
        manifest, resolver=fake_resolver(), use_cache=True, code_policy=DEVELOPMENT
    ).cached
    with pytest.raises(CompileError) as raised:
        compile_workflow(manifest, resolver=fake_resolver(), use_cache=True, code_policy=REFERENCE)
    assert raised.value.report["errors"][0]["code"] == REFUSAL_CODE  # type: ignore[index]


# --------------------------------------------------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------------------------------------------------


def _published_version(
    client: TestClient, manifest: dict[str, Any], name: str = "Support Workflow"
) -> tuple[str, str]:
    """A published version of ``manifest``, made while the profile was not active (a version from before the switch)."""
    workflow_id = create_workflow(client, name)
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    return workflow_id, version_id


def test_promotion_refuses_a_version_that_holds_code(client: TestClient) -> None:
    workflow_id, version_id = _published_version(client, _python_code_manifest())
    bind_code_policy(REFERENCE)

    promoted = client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/staging/promote",
        json={"version_id": version_id},
    )

    assert promoted.status_code == 400, promoted.text
    detail = promoted.json()["detail"]
    assert REFUSAL_CODE in detail and "python_code node 'code'" in detail
    assert client.get(f"{PREFIX}/workflows/{workflow_id}/deployments").json()["data"] == []


def test_every_alias_move_preflights_code_not_only_promotion(client: TestClient) -> None:
    """``require_alias_target_ready`` runs on rollback, approval and candidate rotation too."""
    _, version_id = _published_version(client, _external_app_manifest())
    bind_code_policy(REFERENCE)
    with (
        client.app.state.session_factory() as session,
        pytest.raises(AliasPreflightError) as raised,
    ):
        require_alias_target_ready(session, "prod", version_id)
    assert raised.value.blockers and all(
        b.startswith(f"{REFUSAL_CODE}: ") for b in raised.value.blockers
    )
    assert "external_app node 'bridge'" in raised.value.blockers[0]


def test_alias_preflight_uses_an_explicit_reference_config_without_a_bound_policy(
    client: TestClient,
) -> None:
    _, version_id = _published_version(client, _python_code_manifest())
    assert bound_code_policy() == DEVELOPMENT
    reference = CaliberConfig(profile="reference")
    with client.app.state.session_factory() as session:
        with pytest.raises(AliasPreflightError) as raised:
            require_alias_target_ready(session, "prod", version_id, config=reference)
        assert REFUSAL_CODE in str(raised.value)
        # Outside the profile the same version rotates as before.
        require_alias_target_ready(session, "prod", version_id, config=CaliberConfig())


def test_the_promotion_check_reads_nothing_outside_the_profile_and_ignores_an_unparseable_manifest() -> (
    None
):
    from caliber.workflows.promoter import _reference_code_blockers

    def registry_was_queried() -> InMemoryToolResolver:
        raise AssertionError("the tool registry must not be read")

    garbage: dict[str, Any] = {"not": "a manifest"}
    assert _reference_code_blockers(garbage, registry_was_queried, config=None) == []
    assert _reference_code_blockers(garbage, registry_was_queried, config=CaliberConfig()) == []
    # Even a manifest that holds code reads nothing, and refuses nothing, outside the profile.
    assert (
        _reference_code_blockers(
            _python_code_manifest(), registry_was_queried, config=CaliberConfig()
        )
        == []
    )
    # In the profile an unparseable manifest is left to the checks that report it, before any registry read.
    reference = CaliberConfig(profile="reference")
    assert _reference_code_blockers(garbage, registry_was_queried, config=reference) == []
    # A parseable one with code is refused, and only then is the registry asked.
    blockers = _reference_code_blockers(
        _python_code_manifest(), lambda: fake_resolver(), config=reference
    )
    assert [b.split(":", 1)[0] for b in blockers] == [REFUSAL_CODE]


def test_a_parent_run_over_a_child_that_holds_code_fails_by_the_same_code(
    client: TestClient,
) -> None:
    """A subworkflow is not expanded at the parent; its own boundaries refuse it and the parent run keeps the code."""
    from caliber.db.models import CaliberWorkflowVersion
    from caliber.workflows.promoter import build_plan

    child_id, child_version = _published_version(client, _python_code_manifest())
    rotated = client.post(
        f"{PREFIX}/workflows/{child_id}/deployments/dev/promote", json={"version_id": child_version}
    )
    assert rotated.status_code == 200, rotated.text

    parent = make_manifest()
    parent["nodes"]["sub"] = {
        "id": "sub",
        "type": "subworkflow",
        "workflow_id": child_id,
        "alias": "dev",
    }
    parent["edges"] = [
        {"id": "e1", "from": "start", "to": "sub", "map": {"msg": "input"}},
        {"id": "e2", "from": "sub", "to": "final", "map": {"output": "response"}},
    ]
    parent_id, parent_version_id = _published_version(client, parent, "Parent Workflow")
    reference = CaliberConfig(profile="reference")
    with client.app.state.session_factory() as session:
        parent_version = session.get(CaliberWorkflowVersion, parent_version_id)
        assert parent_version is not None
        # The parent holds no code, so it validates, compiles and promotes; only the child is refused.
        assert list(manifest_refusals(parse_manifest(parent), fake_resolver(), REFERENCE)) == []
        plan = build_plan(session, parent_version, config=reference)
        result = execute(plan, "hello", executor=FakeWorkflowExecutor())

    assert parent_id
    assert result.status == "error"
    assert result.error_code == REFUSAL_CODE
    assert result.error is not None
    assert REFUSAL_CODE in result.error and "python_code node 'code'" in result.error
    assert [s.status for s in result.steps if s.node_id == "sub"] != ["ok"]


def test_a_child_the_runtime_entry_refuses_fails_the_parent_by_the_same_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's own runtime-entry refusal (a plan that compiled, then was refused) reaches the parent with its code."""
    from caliber.db.models import CaliberWorkflowVersion
    from caliber.workflows import promoter as promoter_module
    from caliber.workflows.runtime import WorkflowRunResult

    child_id, child_version = _published_version(client, make_manifest(), "Clean Child")
    rotated = client.post(
        f"{PREFIX}/workflows/{child_id}/deployments/dev/promote", json={"version_id": child_version}
    )
    assert rotated.status_code == 200, rotated.text
    parent = make_manifest()
    parent["nodes"]["sub"] = {
        "id": "sub",
        "type": "subworkflow",
        "workflow_id": child_id,
        "alias": "dev",
    }
    parent["edges"] = [
        {"id": "e1", "from": "start", "to": "sub", "map": {"msg": "input"}},
        {"id": "e2", "from": "sub", "to": "final", "map": {"output": "response"}},
    ]
    _, parent_version_id = _published_version(client, parent, "Parent Of Clean Child")
    refused = WorkflowRunResult(
        status="error", output="", error=f"{REFUSAL_CODE}: child", error_code=REFUSAL_CODE
    )
    monkeypatch.setattr(promoter_module, "execute", lambda *a, **k: refused)
    with client.app.state.session_factory() as session:
        version = session.get(CaliberWorkflowVersion, parent_version_id)
        assert version is not None
        plan = promoter_module.build_plan(session, version, config=CaliberConfig())
        result = execute(plan, "hello", executor=FakeWorkflowExecutor())
    assert result.status == "error" and result.error_code == REFUSAL_CODE


def test_a_child_failure_that_is_not_a_refusal_keeps_no_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the reference profile's refusal travels as a code; any other child failure is the parent's plain error."""
    from caliber.db.models import CaliberWorkflowVersion
    from caliber.workflows import promoter as promoter_module
    from caliber.workflows.runtime import WorkflowRunResult

    child_id, child_version = _published_version(client, make_manifest(), "Plain Child")
    rotated = client.post(
        f"{PREFIX}/workflows/{child_id}/deployments/dev/promote", json={"version_id": child_version}
    )
    assert rotated.status_code == 200, rotated.text
    parent = make_manifest()
    parent["nodes"]["sub"] = {
        "id": "sub",
        "type": "subworkflow",
        "workflow_id": child_id,
        "alias": "dev",
    }
    parent["edges"] = [
        {"id": "e1", "from": "start", "to": "sub", "map": {"msg": "input"}},
        {"id": "e2", "from": "sub", "to": "final", "map": {"output": "response"}},
    ]
    _, parent_version_id = _published_version(client, parent, "Parent Of Plain Child")

    with client.app.state.session_factory() as session:
        version = session.get(CaliberWorkflowVersion, parent_version_id)
        assert version is not None
        plan = promoter_module.build_plan(session, version, config=CaliberConfig())

        # The child runs and fails without a code.
        monkeypatch.setattr(
            promoter_module,
            "execute",
            lambda *a, **k: WorkflowRunResult(status="error", output="", error="child broke"),
        )
        failed = execute(plan, "hello", executor=FakeWorkflowExecutor())
        assert failed.status == "error" and failed.error_code is None
        assert failed.error is not None and "child broke" in failed.error

        # The child does not compile for a reason that is not a refusal: the compile error propagates unchanged.
        def broken(*args: Any, **kwargs: Any) -> Any:
            raise CompileError(
                "child does not compile", report={"errors": [{"code": "missing_tool"}]}
            )

        monkeypatch.setattr(promoter_module, "build_plan", broken)
        broken_run = execute(plan, "hello", executor=FakeWorkflowExecutor())
        assert broken_run.status == "error" and broken_run.error_code is None
        assert broken_run.error is not None and "child does not compile" in broken_run.error


def test_the_refusal_code_is_found_on_the_exception_or_what_it_was_raised_from() -> None:
    from caliber.workflows.runtime import ChildRefusedError, ToolExecutionError, _refusal_code_of

    assert _refusal_code_of(None) is None
    assert _refusal_code_of(ValueError("plain")) is None
    assert _refusal_code_of(ChildRefusedError("child")) == REFUSAL_CODE
    assert _refusal_code_of(CodeRefusalError([])) == REFUSAL_CODE
    try:
        try:
            raise ChildRefusedError("inner")
        except ChildRefusedError as inner:
            raise ToolExecutionError("outer") from inner
    except ToolExecutionError as outer:
        assert _refusal_code_of(outer) == REFUSAL_CODE
    # A refusal reachable only through ``__context__`` (raised inside an ``except``, no ``from``) is found too.
    try:
        try:
            raise ChildRefusedError("inner")
        except ChildRefusedError:
            raise ToolExecutionError("outer, implicit context")
    except ToolExecutionError as implicit_only:
        assert implicit_only.__cause__ is None
        assert isinstance(implicit_only.__context__, ChildRefusedError)
        assert _refusal_code_of(implicit_only) == REFUSAL_CODE
    # A cycle in the chain ends the walk.
    first, second = ValueError("a"), ValueError("b")
    first.__cause__, second.__cause__ = second, first
    assert _refusal_code_of(first) is None


def test_the_deploy_gate_compile_uses_the_callers_reference_config(client: TestClient) -> None:
    """A gate replay never compiles code under a reference configuration, even in a process whose policy is not bound."""
    from caliber.workflows.promoter import evaluate_deploy_gates

    data = _python_code_manifest()
    data["deploy_gates"] = {
        "g": {"type": "deploy_gate", "dataset_ref": "ds", "required_for_aliases": ["prod"]}
    }
    manifest = parse_manifest(data)
    assert bound_code_policy() == DEVELOPMENT
    with client.app.state.session_factory() as session, pytest.raises(CompileError) as raised:
        evaluate_deploy_gates(
            session,
            manifest,
            "prod",
            resolver=fake_resolver(),
            executor=FakeWorkflowExecutor(),
            config=CaliberConfig(profile="reference"),
        )
    assert REFUSAL_CODE in {e["code"] for e in raised.value.report["errors"]}  # type: ignore[index]


def test_promote_refuses_code_before_a_deploy_gate_replays_it(client: TestClient) -> None:
    """With a gate attached, the refusal is the preflight's, not a compile failure from inside the gate replay."""
    from caliber.db.models import CaliberWorkflowVersion
    from caliber.workflows.promoter import promote

    data = _python_code_manifest()
    data["artifacts"] = {"eval_datasets": {"ds": {"dataset_name": "support-eval-v3"}}}
    data["deploy_gates"] = {
        "g": {"type": "deploy_gate", "dataset_ref": "ds", "required_for_aliases": ["staging"]}
    }
    workflow_id, version_id = _published_version(client, data)
    with client.app.state.session_factory() as session:
        version = session.get(CaliberWorkflowVersion, version_id)
        assert version is not None
        with pytest.raises(AliasPreflightError) as raised:
            promote(
                session,
                workflow_id,
                "staging",
                version,
                actor="@test",
                config=CaliberConfig(profile="reference"),
            )
    assert raised.value.blockers and raised.value.blockers[0].startswith(f"{REFUSAL_CODE}: ")


def test_promotion_is_unchanged_outside_the_reference_profile(client: TestClient) -> None:
    workflow_id, version_id = _published_version(client, _python_code_manifest())
    promoted = client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/dev/promote", json={"version_id": version_id}
    )
    assert promoted.status_code == 200, promoted.text


def test_promotion_checks_a_registered_python_tool_against_the_registry_row(
    client: TestClient,
) -> None:
    from tests.workflow_helpers import make_support_manifest, make_tool_payload

    for name in ("lookup_policy", "get_order", "escalate"):
        extra = {"side_effect_level": "external_action"} if name == "escalate" else {}
        assert client.post(
            f"{PREFIX}/tools", json=make_tool_payload(name, **extra)
        ).status_code in (200, 201)
    workflow_id = create_workflow(client)
    version_id, _ = create_draft(client, workflow_id, make_support_manifest(workflow_id))
    assert client.post(f"{PREFIX}/workflow-versions/{version_id}/publish").status_code == 200

    bind_code_policy(REFERENCE_UNSANDBOXED)
    promoted = client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/staging/promote",
        json={"version_id": version_id},
    )
    assert promoted.status_code == 400
    assert REFUSAL_CODE in promoted.json()["detail"]
    assert "CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED" in promoted.json()["detail"]

    # With the sandbox on, the same administrator-registered tools are still refused at promotion (B04.7, #774).
    bind_code_policy(REFERENCE)
    refused = client.post(
        f"{PREFIX}/workflows/{workflow_id}/deployments/staging/promote",
        json={"version_id": version_id},
    )
    assert refused.status_code == 400
    assert REFUSAL_CODE in refused.json()["detail"]
    assert "not one of CALIBER's own shipped tools" in refused.json()["detail"]
    with client.app.state.session_factory() as session, pytest.raises(AliasPreflightError):
        require_alias_target_ready(session, "staging", version_id)

    # Outside the reference profile the same version promotes.
    bind_code_policy(None)
    with client.app.state.session_factory() as session:
        require_alias_target_ready(session, "staging", version_id)


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (None, "failed"),
        ({"errors": []}, "failed"),
        ({"errors": [{"code": "missing_tool", "message": "no tool"}]}, "failed"),
        (
            {"errors": [{"code": REFUSAL_CODE, "message": "no code"}, "junk", None]},
            f"{REFUSAL_CODE}: no code",
        ),
        (
            {
                "errors": [
                    {"code": "missing_tool", "message": "no tool"},
                    {"code": REFUSAL_CODE, "message": "no code"},
                ]
            },
            f"{REFUSAL_CODE}: no code; other errors: no tool",
        ),
    ],
)
def test_the_publish_detail_keeps_every_refusal_and_the_other_errors(
    report: dict[str, Any] | None, expected: str
) -> None:
    from caliber.workflows.promoter import _compile_failure_detail

    assert _compile_failure_detail(CompileError("failed", report=report)) == expected


def test_publish_refuses_code_in_the_reference_profile(client: TestClient) -> None:
    workflow_id = create_workflow(client)
    version_id, _ = create_draft(client, workflow_id, _python_code_manifest())
    bind_code_policy(REFERENCE)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 400
    assert REFUSAL_CODE in published.json()["detail"]


def test_the_validate_and_compile_routes_report_the_code(client: TestClient) -> None:
    workflow_id = create_workflow(client)
    version_id, _ = create_draft(client, workflow_id, _python_code_manifest())
    bind_code_policy(REFERENCE)

    validated = client.post(f"{PREFIX}/workflow-versions/{version_id}/validate")
    assert validated.status_code == 200
    errors = validated.json()["data"]["errors"]
    assert [(e["code"], e["path"]) for e in errors] == [(REFUSAL_CODE, "nodes.code")]

    compiled = client.post(f"{PREFIX}/workflow-versions/{version_id}/compile")
    assert compiled.status_code == 400
    assert compiled.json()["report"]["errors"][0]["code"] == REFUSAL_CODE


# --------------------------------------------------------------------------------------------------------------------
# build_plan, the exporter and the server binding
# --------------------------------------------------------------------------------------------------------------------


def test_build_plan_carries_the_policy_and_refuses_before_a_plan_exists(client: TestClient) -> None:
    from caliber.db.models import CaliberWorkflowVersion
    from caliber.workflows.promoter import build_plan

    workflow_id = create_workflow(client)
    version_id, _ = create_draft(client, workflow_id, make_manifest(workflow_id))
    code_workflow_id = create_workflow(client, "Code Workflow")
    code_version_id, _ = create_draft(client, code_workflow_id, _python_code_manifest())
    with client.app.state.session_factory() as session:
        clean = session.get(CaliberWorkflowVersion, version_id)
        code = session.get(CaliberWorkflowVersion, code_version_id)
        assert clean is not None and code is not None
        reference = CaliberConfig(profile="reference")

        plan = build_plan(session, clean, config=reference)
        assert plan.code_policy == CodePolicy.from_config(reference)
        assert build_plan(session, clean, config=CaliberConfig()).code_policy == DEVELOPMENT

        with pytest.raises(CompileError) as raised:
            build_plan(session, code, config=reference)
        assert raised.value.report["errors"][0]["code"] == REFUSAL_CODE  # type: ignore[index]
        assert build_plan(session, code, config=CaliberConfig()).ir.nodes["code"]


def test_create_app_binds_the_policy_from_its_configuration(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(caliber_server, "_require_reference_profile", lambda config: None)
    db = f"sqlite:///{tmp_path / 'app.db'}"

    caliber_server.create_app(
        CaliberConfig(database_url=db, profile="reference")
    ).state.session_factory.kw["bind"].dispose()
    assert bound_code_policy() == REFERENCE

    caliber_server.create_app(
        CaliberConfig(database_url=db, profile="reference", registered_tool_sandbox_enabled=False)
    ).state.session_factory.kw["bind"].dispose()
    assert bound_code_policy() == REFERENCE_UNSANDBOXED

    caliber_server.create_app(CaliberConfig(database_url=db)).state.session_factory.kw[
        "bind"
    ].dispose()
    assert bound_code_policy() == DEVELOPMENT


def test_the_exporter_plan_refuses_code_in_the_reference_profile() -> None:
    from caliber.workflows.export_runtime import execute_exported_workflow

    ir = build_ir(parse_manifest(_python_code_manifest()), fake_resolver())
    # B04.7 (#774, EP-041): the whole export is refused before a plan is built, with the same code.
    with pytest.raises(UnsupportedEffectPath) as refused:
        execute_exported_workflow(
            ir, "hello", config=CaliberConfig(profile="reference"), resolver=fake_resolver()
        )
    assert refused.value.code == REFUSAL_CODE
    allowed = execute_exported_workflow(
        ir, "hello", config=CaliberConfig(), resolver=fake_resolver()
    )
    assert allowed.status == "completed", allowed.error


# --------------------------------------------------------------------------------------------------------------------
# The documented contract
# --------------------------------------------------------------------------------------------------------------------


def test_the_refusal_code_is_the_one_adr_0001_names() -> None:
    from pathlib import Path

    adr = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "architecture"
        / "adr"
        / "0001-reference-profile.md"
    )
    assert f"`{REFUSAL_CODE}`" in adr.read_text(encoding="utf-8")


def test_the_manifest_is_not_mutated_by_the_checks() -> None:
    data = _python_code_manifest()
    before = copy.deepcopy(data)
    manifest = parse_manifest(data)
    manifest_refusals(manifest, fake_resolver(), REFERENCE)
    assert data == before


# --------------------------------------------------------------------------------------------------------------------
# B12.4b (#763): an installed plugin is refused unless its distribution is allowlisted, with the same code
# --------------------------------------------------------------------------------------------------------------------

ACME = "acme-caliber-optimizers"


class _Entry:
    """A scripted entry point that records every import (``load``) so a refusal can prove nothing was imported."""

    def __init__(self, name: str, loaded: Any, distribution: str | None) -> None:
        self.name = name
        self.value = f"fake:{name}"
        self._loaded = loaded
        self.loads = 0
        self.dist = None
        if distribution is not None:
            self.dist = type("_Dist", (), {"name": distribution})()

    def load(self) -> Any:
        self.loads += 1
        return self._loaded


def _spec(name: str = "AcmeOptimizer", **overrides: Any) -> Any:
    from caliber.extensibility.registry import OptimizerSpec

    fields: dict[str, Any] = {
        "name": name,
        "summary": "third-party",
        "artifact_types": frozenset({"prompt"}),
        "source": "plugin",
        "distribution": ACME,
    }
    fields.update(overrides)
    return OptimizerSpec(**fields)


@pytest.fixture
def clean_registry() -> Iterator[Any]:
    from caliber.extensibility import optimizer_registry

    instance = optimizer_registry()
    instance.reset_for_tests()
    yield instance
    instance.reset_for_tests()


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[_Entry]]:
    """Install scripted entry points (replacing any), with the allowlist unset."""
    from caliber.extensibility import entrypoints

    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    entries: list[_Entry] = []
    monkeypatch.setattr(entrypoints, "_entry_points", lambda: list(entries))

    def install(*new: _Entry) -> list[_Entry]:
        entries[:] = list(new)
        return entries

    return install


def _allow(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    from caliber.extensibility import entrypoints

    monkeypatch.setenv(entrypoints.ALLOWLIST_ENV_VAR, value)


def test_plugin_outside_allowlist_refused(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The acceptance oracle: a fake entry point outside the allowlist raises at load, with the one profile code."""
    from caliber.extensibility import PluginError, PluginRefused

    (entry,) = installed(_Entry("acme", _spec(), ACME))
    bind_code_policy(REFERENCE)

    with (
        caplog.at_level("WARNING", logger="caliber.extensibility.entrypoints"),
        pytest.raises(PluginRefused) as raised,
    ):
        clean_registry.load_plugins()
    # Logged as well as raised: an operator reading the log sees the refusal, with its code and the distribution.
    logged = [
        r.getMessage() for r in caplog.records if r.name == "caliber.extensibility.entrypoints"
    ]
    assert len(logged) == 1 and logged[0].startswith(REFUSAL_CODE) and ACME in logged[0]

    refused = raised.value
    assert refused.code == REFUSAL_CODE == "unsupported_in_reference_profile"
    assert refused.distribution == ACME and refused.distributions == (ACME,)
    assert isinstance(refused, PluginError)
    message = str(refused)
    assert message.startswith(f"{REFUSAL_CODE}: ") and ACME in message
    assert "CALIBER_PLUGIN_ALLOWLIST" in message
    assert entry.loads == 0, "a refused plugin's code must not be imported"
    assert clean_registry.load_errors == {ACME: message}


def test_a_refused_deployment_stays_refused_and_imports_nothing_from_any_plugin(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unlisted plugin refuses the whole load, the allowlisted one included, and every later use refuses again."""
    from caliber.extensibility import PluginRefused

    listed = _Entry("listed", _spec("Listed", distribution="listed-plugin"), "listed-plugin")
    unlisted = _Entry("acme", _spec(), ACME)
    installed(listed, unlisted)
    _allow(monkeypatch, "listed-plugin")
    bind_code_policy(REFERENCE)

    uses: list[Callable[[], Any]] = [
        clean_registry.load_plugins,
        lambda: clean_registry.get("MetaPrompt"),
        lambda: clean_registry.has("MetaPrompt"),
        clean_registry.names,
        lambda: clean_registry.selectable("prompt"),
        lambda: list(clean_registry),
        lambda: len(clean_registry),
    ]
    for use in uses:
        with pytest.raises(PluginRefused) as raised:
            use()
        assert raised.value.distributions == (ACME,)
    assert (listed.loads, unlisted.loads) == (0, 0)
    # The first refusal is the one that is kept, not rebuilt: one record.
    assert list(clean_registry.load_errors) == [ACME]


def test_every_refused_distribution_is_named_and_recorded(
    clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    from caliber.extensibility import PluginRefused, entrypoints

    installed(
        _Entry("z", _spec("Z", distribution="zeta-plugin"), "zeta-plugin"),
        _Entry("a", _spec("A", distribution="alpha-plugin"), "alpha-plugin"),
        # Two entry points of one distribution are one refusal; a name with no distribution is labelled by the entry point.
        _Entry("a2", _spec("A2", distribution="alpha-plugin"), "alpha-plugin"),
        _Entry("orphan", _spec("O"), None),
    )
    assert entrypoints.unallowlisted_plugin_labels() == ["alpha-plugin", "orphan", "zeta-plugin"]
    bind_code_policy(REFERENCE)

    with pytest.raises(PluginRefused) as raised:
        clean_registry.load_plugins()
    assert raised.value.distributions == ("alpha-plugin", "orphan", "zeta-plugin")
    assert raised.value.distribution == "alpha-plugin"
    assert set(clean_registry.load_errors) == {"alpha-plugin", "orphan", "zeta-plugin"}
    for label in ("alpha-plugin", "orphan", "zeta-plugin"):
        assert label in str(raised.value)


def test_a_plugin_whose_distribution_is_unknown_is_refused_even_with_an_allowlist(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No distribution means nothing the allowlist could name: it cannot match, and a blank list entry matches nothing."""
    from caliber.extensibility import PluginRefused

    (entry,) = installed(_Entry("orphan", _spec(), None))
    _allow(monkeypatch, f"{ACME}, ,")
    bind_code_policy(REFERENCE)
    with pytest.raises(PluginRefused) as raised:
        clean_registry.load_plugins()
    assert raised.value.distribution == "orphan" and entry.loads == 0


def test_an_allowlisted_plugin_still_loads_in_the_reference_profile(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trusted-code admission: naming the distribution is what admits it, in the reference profile as before."""
    (entry,) = installed(_Entry("acme", _spec(), ACME))
    # Normalised the way packaging does, so a correct-looking entry cannot match nothing.
    _allow(monkeypatch, "Acme_Caliber.Optimizers")
    bind_code_policy(REFERENCE)

    spec = clean_registry.get("AcmeOptimizer")
    assert spec.source == "plugin" and spec.distribution == ACME and spec.experimental
    assert entry.loads == 1
    assert clean_registry.load_errors == {}
    assert "MetaPrompt" in clean_registry.names()


def test_no_plugin_installed_is_a_working_reference_deployment(
    clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    installed()
    bind_code_policy(REFERENCE)
    clean_registry.load_plugins()
    assert clean_registry.names() == [
        "DSPyBootstrapFewShot",
        "DSPyMIPRO",
        "GEPA",
        "MetaPrompt",
        "SkillMetaPrompt",
    ]
    assert clean_registry.load_errors == {}


def test_the_development_profile_skips_an_unlisted_plugin_as_before(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (unlisted,) = installed(_Entry("acme", _spec(), ACME))
    with caplog.at_level("INFO", logger="caliber.extensibility.entrypoints"):
        assert "AcmeOptimizer" not in clean_registry.names()
    assert unlisted.loads == 0 and clean_registry.load_errors == {}
    assert any("not allowlisted" in record.getMessage() for record in caplog.records)

    # An explicitly development policy is the same as none.
    clean_registry.reset_for_tests()
    bind_code_policy(DEVELOPMENT)
    assert "AcmeOptimizer" not in clean_registry.names()

    clean_registry.reset_for_tests()
    _allow(monkeypatch, ACME)
    assert clean_registry.get("AcmeOptimizer").distribution == ACME


def test_discovery_refuses_before_its_first_yield_and_allows_a_fully_allowlisted_set(
    installed: Callable[..., list[_Entry]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.extensibility import PluginRefused
    from caliber.extensibility.entrypoints import discover_optimizer_plugins

    listed = _Entry("listed", _spec("Listed", distribution="listed-plugin"), "listed-plugin")
    unlisted = _Entry("acme", _spec(), ACME)
    installed(listed, unlisted)
    _allow(monkeypatch, "listed-plugin")
    bind_code_policy(REFERENCE)

    discovery = discover_optimizer_plugins()
    with pytest.raises(PluginRefused):
        next(discovery)
    assert (listed.loads, unlisted.loads) == (0, 0)

    installed(listed)
    assert [spec.name for spec, error in discover_optimizer_plugins() if error is None] == [
        "Listed"
    ]


def test_a_plugin_handed_to_the_registry_directly_is_admitted_on_the_same_terms(
    clean_registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.extensibility import PluginRefused, entrypoints
    from caliber.extensibility.registry import BUILTIN_OPTIMIZERS

    bind_code_policy(REFERENCE)
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    with pytest.raises(PluginRefused) as raised:
        clean_registry.register(_spec())
    assert raised.value.distribution == ACME and raised.value.code == REFUSAL_CODE
    with pytest.raises(PluginRefused) as unknown:
        clean_registry.register(_spec("NoDist", distribution=None))
    assert unknown.value.distribution == "<unknown>"
    assert "AcmeOptimizer" not in clean_registry._specs

    # A built-in is CALIBER's own code, not a plugin: re-registering one is untouched by the rule.
    clean_registry.register(BUILTIN_OPTIMIZERS[0])

    _allow(monkeypatch, ACME)
    clean_registry.register(_spec())
    assert clean_registry.get("AcmeOptimizer").distribution == ACME


def test_require_admitted_plugin_does_nothing_outside_the_reference_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from caliber.extensibility import PluginRefused, entrypoints

    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    entrypoints.require_admitted_plugin(ACME)
    entrypoints.require_admitted_plugin(None)
    bind_code_policy(REFERENCE)
    with pytest.raises(PluginRefused):
        entrypoints.require_admitted_plugin(ACME)
    _allow(monkeypatch, ACME)
    entrypoints.require_admitted_plugin(ACME)
    with pytest.raises(PluginRefused):
        entrypoints.require_admitted_plugin(None)


def test_listing_imports_nothing_and_is_unchanged_in_the_reference_profile(
    installed: Callable[..., list[_Entry]],
) -> None:
    from caliber.extensibility import entrypoints

    (entry,) = installed(_Entry("acme", _spec(), ACME))
    bind_code_policy(REFERENCE)
    assert entrypoints.unallowlisted_plugin_labels() == [ACME]
    listed = entrypoints.available_optimizer_plugins()
    assert [(p["name"], p["distribution"], p["allowlisted"]) for p in listed] == [
        ("acme", ACME, False)
    ]
    assert entry.loads == 0


def test_a_refused_plugin_carries_the_one_profile_code_not_a_second_one() -> None:
    from caliber.extensibility import PluginRefused

    refused = PluginRefused(["b", "a"], "message")
    assert (refused.code, refused.distribution, refused.distributions) == (
        REFUSAL_CODE,
        "a",
        ("a", "b"),
    )
    assert CodeRefusalError.code == refused.code


def test_the_registry_forgets_a_refusal_when_reset_for_tests(
    clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    from caliber.extensibility import PluginRefused

    installed(_Entry("acme", _spec(), ACME))
    bind_code_policy(REFERENCE)
    with pytest.raises(PluginRefused):
        clean_registry.load_plugins()
    bind_code_policy(None)
    clean_registry.reset_for_tests()
    assert clean_registry.load_errors == {} and "MetaPrompt" in clean_registry.names()


def test_a_plugin_whose_spec_the_registry_rejects_is_recorded_not_raised(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other failure a load has stays a recorded one: only a refusal by the profile stops the load."""
    installed(_Entry("acme", _spec("GEPA"), ACME))
    _allow(monkeypatch, ACME)
    bind_code_policy(REFERENCE)
    assert "GEPA" in clean_registry.names()
    assert "never redefine" in clean_registry.load_errors[ACME]


# -- where a refused plugin is seen: the capabilities page, the optimize route and the refinement provider -------------


def test_capabilities_still_answers_for_a_refused_deployment_and_says_why(
    client: TestClient, clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    installed(_Entry("acme", _spec(), ACME))
    bind_code_policy(REFERENCE)

    response = client.get(f"{PREFIX}/capabilities")
    assert response.status_code == 200, response.text
    extensibility = response.json()["data"]["extensibility"]
    assert extensibility["optimizers"] == [], (
        "no optimizer is dispatchable while the refusal stands"
    )
    (plugin,) = extensibility["plugins"]
    assert plugin["distribution"] == ACME and plugin["allowlisted"] is False
    assert plugin["error"].startswith(f"{REFUSAL_CODE}: ")


def test_capabilities_lists_everything_for_a_development_deployment_with_the_same_plugin(
    client: TestClient, clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    installed(_Entry("acme", _spec(), ACME))
    extensibility = client.get(f"{PREFIX}/capabilities").json()["data"]["extensibility"]
    assert "MetaPrompt" in [o["name"] for o in extensibility["optimizers"]]
    assert extensibility["plugins"][0]["error"] is None


def _optimization_run_body() -> dict[str, Any]:
    return {
        "agent_id": "support-agent",
        "eval_dataset_id": "EDS-PROMPT-1",
        "optimizer_type": "GEPA",
        "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
    }


def test_the_optimization_route_answers_503_with_the_code_for_a_refused_deployment(
    client: TestClient, clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    (entry,) = installed(_Entry("acme", _spec(), ACME))
    bind_code_policy(REFERENCE)

    response = client.post(f"{PREFIX}/prompts/optimization/runs", json=_optimization_run_body())
    assert response.status_code == 503, response.text
    assert response.json()["detail"].startswith(f"{REFUSAL_CODE}: ")
    assert ACME in response.json()["detail"] and entry.loads == 0


def test_the_optimization_route_is_untouched_by_an_unlisted_plugin_outside_the_profile(
    client: TestClient, clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    installed(_Entry("acme", _spec(), ACME))
    response = client.post(f"{PREFIX}/prompts/optimization/runs", json=_optimization_run_body())
    # The optimizer is known; the request then fails on the agent it names, which does not exist here.
    assert response.status_code != 503, response.text


def test_a_refinement_job_in_a_refused_deployment_fails_with_the_code(
    clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    from types import SimpleNamespace

    from caliber.llm.openai_agents import OpenAIAgentsLLMProvider
    from caliber.llm.provider import LLMProviderError

    installed(_Entry("acme", _spec(), ACME))
    bind_code_policy(REFERENCE)
    provider = OpenAIAgentsLLMProvider.__new__(OpenAIAgentsLLMProvider)
    context = SimpleNamespace(optimizer_type="GEPA", artifact_type="prompt")
    with pytest.raises(LLMProviderError, match=REFUSAL_CODE):
        provider._resolve_optimizer(context)  # type: ignore[arg-type]


# -- the way a plugin is admitted: one place, a real distribution on the path ---------------------------------------------


def test_the_entry_point_group_is_the_only_way_a_plugin_is_admitted() -> None:
    """A ratchet: no other source module reads entry points, so there is no second admission route to forget."""
    import ast

    source_root = Path(caliber_server.__file__).resolve().parent
    callers = sorted(
        str(path.relative_to(source_root))
        for path in source_root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "entry_points")
            or (isinstance(node.func, ast.Name) and node.func.id == "entry_points")
        )
    )
    assert callers == ["extensibility/entrypoints.py"]


def test_a_real_installed_distribution_is_refused_and_then_admitted_by_its_name(
    tmp_path: Path,
    clean_registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The packaging contract end to end: a dist-info on ``sys.path``, found through ``importlib.metadata``."""
    import importlib
    import sys

    from caliber.extensibility import PluginRefused, entrypoints

    (tmp_path / "acme_b763.py").write_text(
        "class _D:\n"
        "    name = 'AcmeB763'\n"
        "    summary = 'scripted'\n"
        "    artifact_types = frozenset({'prompt'})\n"
        "declaration = _D()\n",
        encoding="utf-8",
    )
    dist_info = tmp_path / "acme_b763_optimizers-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: acme-b763-optimizers\nVersion: 1.0.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(
        "[caliber.optimizers]\nacme = acme_b763:declaration\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    try:
        bind_code_policy(REFERENCE)
        with pytest.raises(PluginRefused) as raised:
            clean_registry.load_plugins()
        assert raised.value.distribution == "acme-b763-optimizers"
        assert "acme_b763" not in sys.modules, "the refused plugin's module must not be imported"

        clean_registry.reset_for_tests()
        _allow(monkeypatch, "acme-b763-optimizers")
        assert clean_registry.get("AcmeB763").distribution == "acme-b763-optimizers"
        assert "acme_b763" in sys.modules
    finally:
        sys.modules.pop("acme_b763", None)


# -- the startup rule ------------------------------------------------------------------------------------------------------


def test_a_reference_process_refuses_to_start_while_an_unlisted_plugin_is_installed(
    tmp_path: Path,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is listed with the other reference violations, by its stable code, before anything is imported."""
    (entry,) = installed(_Entry("acme", _spec(), ACME))
    config = CaliberConfig(database_url=f"sqlite:///{tmp_path / 'app.db'}", profile="reference")

    with pytest.raises(ConfigError) as raised:
        caliber_server._require_reference_profile(config)
    assert "[plugin_not_allowlisted]" in str(raised.value) and ACME in str(raised.value)
    assert entry.loads == 0

    _allow(monkeypatch, ACME)
    with pytest.raises(ConfigError) as other:
        caliber_server._require_reference_profile(config)
    assert "[plugin_not_allowlisted]" not in str(other.value)  # only the SQLite violation is left


# -- host paths: the file root --------------------------------------------------------------------------------------------


def _file_input_manifest() -> dict[str, Any]:
    data = make_manifest()
    data["nodes"]["file_input"] = {
        "id": "file_input",
        "type": "file_input",
        "inputs": {"path": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "path": {"type": "string"},
            "metadata": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "file_input", "map": {"msg": "path"}},
        {"id": "e2", "from": "file_input", "to": "agent", "map": {"text": "input"}},
        {"id": "e3", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    return data


def test_file_tool_root_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The acceptance oracle: ``../../outside.txt`` is refused, whatever the working directory is."""
    from caliber.workflows.runtime import PathOutsideRoot, _path_from_inputs, confine_to_file_root

    root = tmp_path / "root"
    (root / "inner").mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    monkeypatch.chdir(root / "inner")

    with pytest.raises(PathOutsideRoot) as raised:
        confine_to_file_root(Path("../../outside.txt"), what="file/folder input")
    assert isinstance(raised.value, ValueError)
    assert raised.value.path == (tmp_path / "outside.txt").resolve()
    assert raised.value.root == root.resolve()
    assert "outside the configured workflow file root" in str(raised.value)

    with pytest.raises(PathOutsideRoot):
        _path_from_inputs({"path": "../../outside.txt"}, configured_path="", run_input="")
    # A traversal that stays inside the root is allowed.
    inside = confine_to_file_root(Path("../x/../data.txt"), what="file/folder input")
    assert inside == (root / "data.txt").resolve()


def test_a_file_input_node_given_a_traversal_fails_the_run_and_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    (root / "ok.txt").write_text("fine", encoding="utf-8")
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    monkeypatch.chdir(root)

    refused = execute(
        _plan(_file_input_manifest()), "../outside.txt", executor=FakeWorkflowExecutor()
    )
    assert refused.status != "completed"
    assert "outside the configured workflow file root" in str(refused.error)
    assert "secret" not in str(refused.output) + str(refused.error)

    allowed = execute(_plan(_file_input_manifest()), "ok.txt", executor=FakeWorkflowExecutor())
    assert allowed.status == "completed", allowed.error
    assert "fine" in allowed.output


def test_the_reference_profile_has_no_unconfined_file_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.workflows.runtime import PathOutsideRoot, confine_to_file_root

    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", "unconfined")
    target = tmp_path / "anything.txt"
    # Development keeps the opt-out (and so does an explicitly development policy).
    assert confine_to_file_root(target, what="file/folder input") == target
    bind_code_policy(DEVELOPMENT)
    assert confine_to_file_root(target, what="file/folder input") == target

    bind_code_policy(REFERENCE)
    with pytest.raises(ValueError) as raised:
        confine_to_file_root(target, what="file/folder input")
    assert str(raised.value).startswith(f"{REFUSAL_CODE}: ")
    assert "unconfined" in str(raised.value) and "CALIBER_WORKFLOW_FILE_ROOT" in str(raised.value)
    assert not isinstance(raised.value, PathOutsideRoot)

    # A configured root, and an unset one, behave as they always did in the reference profile.
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(tmp_path))
    assert confine_to_file_root(target, what="file/folder input") == target.resolve()
    monkeypatch.delenv("CALIBER_WORKFLOW_FILE_ROOT")
    with pytest.raises(ValueError, match="unmanaged host-filesystem nodes are disabled"):
        confine_to_file_root(target, what="file/folder input")


def test_the_managed_file_tools_take_a_ref_and_refuse_a_traversal(tmp_path: Path) -> None:
    """The run-scoped file tools never take a host path: a ``..`` in the ref or the path is refused before storage."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import caliber.db.models  # noqa: F401
    from caliber.config import WorkflowStorageConfig
    from caliber.db.base import Base
    from caliber.storage import LocalStorageBackend, StorageError, WorkingDirectoryService
    from caliber.workflows import file_tools

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    service = WorkingDirectoryService(LocalStorageBackend(cfg.base_uri), cfg)
    try:
        with sessionmaker(engine)() as session:
            ctx = service.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-1")
            with pytest.raises(StorageError):
                file_tools.write_workdir_file(
                    service, session, ctx, path="../../outside.txt", content="x"
                )
            with pytest.raises(StorageError):
                file_tools.read_workdir_file(
                    service,
                    session,
                    ctx,
                    file_ref="caliber://workflow-runs/WR-1/work/../../outside.txt",
                )
    finally:
        engine.dispose()
    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path / "ws" / "outside.txt").exists()


# -- the documented contract ------------------------------------------------------------------------------------------------


def test_the_operator_page_documents_the_plugin_and_file_root_refusals() -> None:
    page = (
        Path(__file__).resolve().parents[2] / "docs" / "operate" / "reference-profile.md"
    ).read_text(encoding="utf-8")
    section = page.split("## Plugins and host paths (B12.4b, #763)", 1)[1].split("\n## ", 1)[0]
    for needle in (
        REFUSAL_CODE,
        "PluginRefused",
        "PathOutsideRoot",
        "plugin_not_allowlisted",
        "workflow_file_root_unconfined",
        "CALIBER_PLUGIN_ALLOWLIST",
        "CALIBER_WORKFLOW_FILE_ROOT",
        "trusted-code admission",
    ):
        assert needle in section, needle
    assert "code_execution_not_supported_in_reference_profile" not in page


# -- the registry under concurrency, and a registry used before the profile was bound -------------------------------------


def test_a_thread_arriving_during_a_refused_load_is_refused_too_not_served_the_builtins(
    clean_registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding: the first loader held no lock during discovery, so a concurrent caller saw the built-ins."""
    import threading

    from caliber.extensibility import PluginRefused, entrypoints

    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    entered, release = threading.Event(), threading.Event()
    entries = [_Entry("acme", _spec(), ACME)]

    def slow_entry_points() -> list[_Entry]:
        entered.set()
        assert release.wait(10), "the test never released the first loader"
        return list(entries)

    monkeypatch.setattr(entrypoints, "_entry_points", slow_entry_points)
    bind_code_policy(REFERENCE)
    outcomes: dict[str, object] = {}

    def use(name: str) -> None:
        try:
            outcomes[name] = clean_registry.names()
        except PluginRefused as refused:
            outcomes[name] = refused

    first = threading.Thread(target=use, args=("first",))
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=use, args=("second",))
    second.start()
    second.join(0.3)
    assert second.is_alive(), (
        "the second caller must wait for the load, not be served a half-loaded registry"
    )
    release.set()
    first.join(10)
    second.join(10)
    assert isinstance(outcomes["first"], PluginRefused) and isinstance(
        outcomes["second"], PluginRefused
    )


def test_a_thread_arriving_during_an_allowed_load_sees_the_plugin(
    clean_registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    from caliber.extensibility import entrypoints

    _allow(monkeypatch, ACME)
    entered, release = threading.Event(), threading.Event()
    entry = _Entry("acme", _spec(), ACME)

    def slow_entry_points() -> list[_Entry]:
        entered.set()
        assert release.wait(10)
        return [entry]

    monkeypatch.setattr(entrypoints, "_entry_points", slow_entry_points)
    seen: dict[str, list[str]] = {}
    threads = [
        threading.Thread(target=lambda n=n: seen.__setitem__(n, clean_registry.names()))
        for n in ("first", "second")
    ]
    threads[0].start()
    assert entered.wait(10)
    threads[1].start()
    threads[1].join(0.3)
    assert threads[1].is_alive()
    release.set()
    for thread in threads:
        thread.join(10)
    assert "AcmeOptimizer" in seen["first"] and "AcmeOptimizer" in seen["second"]
    assert entry.loads == 1


def test_a_registry_used_before_the_profile_was_bound_refuses_once_it_is(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A load that ran before ``create_app`` bound the profile must not latch a state the profile would refuse."""
    from caliber.extensibility import PluginRefused

    (entry,) = installed(_Entry("acme", _spec(), ACME))
    assert (
        "MetaPrompt" in clean_registry.names()
    )  # a development load: the unlisted plugin is skipped
    assert entry.loads == 0

    bind_code_policy(REFERENCE)
    with pytest.raises(PluginRefused) as raised:
        clean_registry.names()
    assert raised.value.distribution == ACME and entry.loads == 0
    with pytest.raises(PluginRefused):  # and it stays refused
        clean_registry.get("MetaPrompt")
    assert clean_registry.load_errors[ACME].startswith(REFUSAL_CODE)


def test_a_registry_loaded_before_the_profile_is_untouched_when_every_plugin_is_allowlisted(
    clean_registry: Any,
    installed: Callable[..., list[_Entry]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed(_Entry("acme", _spec(), ACME))
    _allow(monkeypatch, ACME)
    assert "AcmeOptimizer" in clean_registry.names()
    bind_code_policy(REFERENCE)
    assert "AcmeOptimizer" in clean_registry.names()
    assert clean_registry._reference_checked is True
    clean_registry.names()  # the check ran once; a later call is a plain lookup


def test_a_registry_that_is_never_in_the_reference_profile_does_not_recheck(
    clean_registry: Any, installed: Callable[..., list[_Entry]]
) -> None:
    installed(_Entry("acme", _spec(), ACME))
    clean_registry.names()
    clean_registry.names()
    assert clean_registry._reference_checked is False


def test_a_refusal_raised_by_discovery_itself_is_kept_like_any_other(
    clean_registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist can change between the up-front check and discovery: that refusal is kept and recorded too."""
    from caliber.extensibility import PluginRefused, entrypoints

    def refusing() -> Any:
        raise PluginRefused(["raced-plugin"], f"{REFUSAL_CODE}: raced")
        yield  # pragma: no cover - makes this a generator, as the real discovery is

    monkeypatch.setattr(entrypoints, "discover_optimizer_plugins", refusing)
    with pytest.raises(PluginRefused):
        clean_registry.load_plugins()
    assert clean_registry.load_errors == {"raced-plugin": f"{REFUSAL_CODE}: raced"}
    with pytest.raises(PluginRefused):
        clean_registry.load_plugins()


# -- folder_input: the files a glob names are confined, not only the folder -------------------------------------------------


def _folder_tree(tmp_path: Path) -> tuple[Path, Path]:
    """``root/`` (the configured root, with ``inside.txt``) next to ``outside/`` (``secret.txt``), plus escape links."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    (root / "inside.txt").write_text("fine", encoding="utf-8")
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    (root / "file-link.txt").symlink_to(outside / "secret.txt")
    (root / "dir-link").symlink_to(outside, target_is_directory=True)
    return root, outside


def _read_folder(
    base: Path, pattern: str, *, recursive: bool = False
) -> tuple[str, list[Any], Any]:
    return runtime_module._read_folder_input_node(
        base,
        pattern=pattern,
        recursive=recursive,
        max_files=50,
        max_bytes_per_file=1000,
        encoding="utf-8",
    )


@pytest.mark.parametrize("policy", [DEVELOPMENT, REFERENCE], ids=["development", "reference"])
@pytest.mark.parametrize(
    "pattern",
    ["../outside/*", "sub/../../outside/*", "..", "../*", "/etc/*", "/*", "\\..\\outside\\*"],
)
def test_a_folder_pattern_that_walks_out_of_the_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: CodePolicy, pattern: str
) -> None:
    from caliber.workflows.runtime import PathOutsideRoot

    root, _ = _folder_tree(tmp_path)
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    bind_code_policy(policy)
    with pytest.raises(PathOutsideRoot) as raised:
        _read_folder(root.resolve(), pattern)
    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize("policy", [DEVELOPMENT, REFERENCE], ids=["development", "reference"])
@pytest.mark.parametrize("pattern", ["*", "*.txt", "**/*"])
def test_a_symlink_inside_the_root_that_points_outside_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: CodePolicy, pattern: str
) -> None:
    from caliber.workflows.runtime import PathOutsideRoot

    root, _ = _folder_tree(tmp_path)
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    bind_code_policy(policy)
    with pytest.raises(PathOutsideRoot) as raised:
        _read_folder(root.resolve(), pattern, recursive=True)
    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize("policy", [DEVELOPMENT, REFERENCE], ids=["development", "reference"])
def test_a_folder_pattern_that_stays_inside_the_root_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: CodePolicy
) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "inside.txt").write_text("fine", encoding="utf-8")
    (root / "sub" / "nested.txt").write_text("nested", encoding="utf-8")
    # Dots that are not a ``..`` segment are ordinary names.
    (root / "archive.tar.gz").write_text("tar", encoding="utf-8")
    (root / "a..b.txt").write_text("dots", encoding="utf-8")
    (root / "..hidden.txt").write_text("hidden", encoding="utf-8")
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    bind_code_policy(policy)
    base = root.resolve()

    assert _read_folder(base, "*.txt")[2]["file_count"] == 3
    assert _read_folder(base, "*.tar.gz")[1][0]["text"] == "tar"
    assert _read_folder(base, "a..b.txt")[1][0]["text"] == "dots"
    assert _read_folder(base, "..hidden*")[1][0]["text"] == "hidden"
    text, files, _ = _read_folder(base, "sub/*.txt")
    assert [f["relative_path"] for f in files] == ["sub/nested.txt"] and "nested" in text
    assert _read_folder(base, "**/*.txt", recursive=True)[2]["file_count"] == 4


def test_the_development_profile_without_a_root_keeps_its_unconfined_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unconfined`` is the explicit opt-out: there is no boundary, so the pattern and the links are left alone."""
    root, _ = _folder_tree(tmp_path)
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", "unconfined")
    bind_code_policy(DEVELOPMENT)
    _, files, _ = _read_folder(root, "../outside/*")
    assert [f["text"] for f in files] == ["SECRET"]
    _, linked, _ = _read_folder(root, "file-link.txt")
    assert [f["text"] for f in linked] == ["SECRET"]


def test_a_folder_input_node_run_refuses_an_escaping_pattern_and_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = _folder_tree(tmp_path)
    (root / "dir-link").unlink()  # leave only the file link, so one run per cause
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))

    def manifest(pattern: str) -> dict[str, Any]:
        data = make_manifest()
        data["nodes"]["folder_input"] = {
            "id": "folder_input",
            "type": "folder_input",
            "pattern": pattern,
            "recursive": False,
            "max_files": 5,
            "inputs": {"path": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "files": {"type": "structured"},
                "metadata": {"type": "structured"},
            },
        }
        data["edges"] = [
            {"id": "e1", "from": "start", "to": "folder_input", "map": {"msg": "path"}},
            {"id": "e2", "from": "folder_input", "to": "agent", "map": {"text": "input"}},
            {"id": "e3", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        ]
        return data

    for pattern in ("../outside/*", "*"):
        refused = execute(_plan(manifest(pattern)), str(root), executor=FakeWorkflowExecutor())
        assert refused.status != "completed", pattern
        assert "outside" in str(refused.error)
        assert "SECRET" not in str(refused.output) + str(refused.error)
    (root / "file-link.txt").unlink()
    ok = execute(_plan(manifest("*.txt")), str(root), executor=FakeWorkflowExecutor())
    assert ok.status == "completed", ok.error
