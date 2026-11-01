"""B12.4a (#754): the reference profile refuses executable code, decided from the compiler's own IR kind registry.

Every kind in :class:`caliber.workflows.ir.NodeType` is classified exactly once (code, tool carrier or inert), a tool binding
is judged by its execution form, and one refusal code, ``unsupported_in_reference_profile``, is raised at validation, compile,
promotion and runtime entry. The tests here run offline and without PostgreSQL; the public-path scenario on a real reference
app is ``tests/egi_scenarios/test_b12_code_refusal.py``.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from caliber import server as caliber_server
from caliber.config import CaliberConfig
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
