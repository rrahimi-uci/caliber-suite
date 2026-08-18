"""Compiler + IR tests (plan §19.4)."""

from __future__ import annotations

import sys
import types

import pytest

from caliber.workflows import compiler
from caliber.workflows.compiler import (
    COMPILER_VERSION,
    CompileError,
    build_ir,
    compile_workflow,
    generate_python,
)
from caliber.workflows.ir import (
    IRAgent,
    IRErrorBoundary,
    IRExternalApp,
    IRFileInput,
    IRFolderInput,
    IRForEach,
    IRGuardrail,
    IRHumanApproval,
    IRJoin,
    IRLoop,
    IRMcpResource,
    IRParallel,
    IRPythonCode,
    IRRouter,
    IRSubworkflow,
    IRWaitForEvent,
    IRWaitUntil,
    NodeType,
)
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry
from tests.workflow_helpers import fake_resolver, make_manifest, make_support_manifest


def test_single_agent_compiles_to_ir_agent() -> None:
    manifest = parse_manifest(make_manifest())
    ir = build_ir(manifest, fake_resolver())
    agent = ir.nodes["agent"]
    assert isinstance(agent, IRAgent)
    assert agent.name == "test-agent"
    assert ir.entry_node_id == "agent"
    assert ir.output_node_id == "final"


def test_support_manifest_resolves_tools_and_guardrail() -> None:
    manifest = parse_manifest(make_support_manifest())
    ir = build_ir(manifest, fake_resolver())
    agent = ir.nodes["support_agent"]
    assert isinstance(agent, IRAgent)
    assert sorted(b.local_name for b in agent.tools) == ["escalate", "get_order", "lookup_policy"]
    guard = ir.nodes["policy_guardrail"]
    assert isinstance(guard, IRGuardrail)
    assert guard.checks[0].kind == "tool_required_before_claim"


def test_handoff_compiles_to_ir_handoff() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["handoffs"] = [
        {
            "target": "billing",
            "description": "billing issues",
            "condition": "input == 'billing'",
            "input_filter": "Billing summary: {{input}}",
        }
    ]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "b"},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }
    ir = build_ir(parse_manifest(data), fake_resolver())
    agent = ir.nodes["agent"]
    assert isinstance(agent, IRAgent)
    assert agent.handoffs[0].target_node_id == "billing"
    assert agent.handoffs[0].description == "billing issues"
    assert agent.handoffs[0].condition == "input == 'billing'"
    assert agent.handoffs[0].input_filter == "Billing summary: {{input}}"


def test_generated_code_emits_rich_handoff_metadata() -> None:
    data = make_manifest("handoff_codegen_wf")
    data["nodes"]["agent"]["handoffs"] = [
        {
            "target": "billing",
            "description": "billing issues",
            "condition": "input == 'billing'",
            "input_filter": "Billing summary: {{input}}",
        }
    ]
    data["nodes"]["billing"] = {
        "id": "billing",
        "type": "agent",
        "name": "billing-agent",
        "model": "inherit",
        "instructions": {"type": "inline", "text": "Handle billing."},
        "inputs": {"input": {"type": "string"}},
        "outputs": {"final_output": {"type": "string"}},
    }

    code = generate_python(build_ir(parse_manifest(data), fake_resolver(), version="7"))

    assert "workflow_handoff_input_filter" in code
    assert "workflow_handoff_is_enabled" in code
    assert (
        'handoff(billing, tool_description_override="billing issues", '
        'input_filter=workflow_handoff_input_filter("Billing summary: {{input}}"), '
        "is_enabled=workflow_handoff_is_enabled(\"input == 'billing'\"))"
    ) in code


def test_compile_workflow_produces_report_and_code() -> None:
    manifest = parse_manifest(make_support_manifest())
    result = compile_workflow(manifest, resolver=fake_resolver(), version="7")
    assert result.report["compiler_version"] == COMPILER_VERSION
    assert result.report["export_mode"] == "agents_sdk_direct"
    assert result.report["agent_count"] == 1
    assert result.report["validation"]["valid"] is True
    assert "def run(" in result.generated_python
    assert result.manifest_hash == manifest.manifest_hash()


def test_generated_code_is_deterministic() -> None:
    manifest = parse_manifest(make_support_manifest())
    a = generate_python(build_ir(manifest, fake_resolver(), version="7"))
    b = generate_python(build_ir(manifest, fake_resolver(), version="7"))
    assert a == b


def test_generated_code_loads_prompt_and_binds_tools() -> None:
    manifest = parse_manifest(make_support_manifest())
    code = compile_workflow(manifest, resolver=fake_resolver(), version="7").generated_python
    assert 'mlflow.genai.load_prompt("prompts:/support-agent@prod")' in code
    assert "ToolRegistryEntry(**{" in code
    # ``bind_exported_tool``, not ``bind_registered_tool``: a generated export takes the
    # same sandbox/allowlist decision the platform takes, so a workflow does not change
    # behaviour by being exported.
    assert 'bind_exported_tool(ToolRegistryEntry(**{"allow_in_preview": False' in code


def test_generated_code_executes_with_importable_registered_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = make_manifest("tool_codegen_exec_wf")
    data["runtime"]["default_model_ref"] = "openai:/pinned-export-model"
    data["nodes"]["agent"]["tools"] = ["lookup_policy"]
    data["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    resolver = InMemoryToolResolver(
        [
            ToolRegistryEntry(
                name="lookup_policy",
                version="1.5",
                module_path="caliber.workflows.demo_tools",
                callable_name="lookup_policy",
            )
        ]
    )
    code = compile_workflow(parse_manifest(data), resolver=resolver, version="7").generated_python

    class FakeAgent:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeRunner:
        @staticmethod
        def run_sync(agent: FakeAgent, prompt: str, **kwargs: object) -> object:
            assert agent.kwargs["model"] == "openai:/pinned-export-model"
            tool_result = agent.kwargs["tools"][0](prompt)
            return types.SimpleNamespace(final_output=tool_result["policy"])

    agents_mod = types.ModuleType("agents")
    agents_mod.Agent = FakeAgent
    agents_mod.Runner = FakeRunner
    agents_mod.RunConfig = lambda **kw: kw
    agents_mod.handoff = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    monkeypatch.setitem(sys.modules, "agents", agents_mod)

    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.genai = types.SimpleNamespace(
        load_prompt=lambda _uri: types.SimpleNamespace(template="unused")
    )
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)

    from caliber.workflows import runtime as workflow_runtime

    seen_configs: list[object | None] = []
    original_bind = workflow_runtime.bind_exported_tool

    def _recording_bind(entry: object, *, config: object | None = None):
        seen_configs.append(config)
        return original_bind(entry, config=config)

    monkeypatch.setattr(workflow_runtime, "bind_exported_tool", _recording_bind)

    namespace: dict[str, object] = {}
    exec(code, namespace)

    # Importing the generated module must not bind against ambient configuration before
    # an explicit per-run sandbox configuration is available.
    assert seen_configs == []
    explicit_config = types.SimpleNamespace(
        registered_tool_module_allowlist="caliber.workflows.*",
        registered_tool_sandbox_enabled=True,
        registered_tool_sandbox_timeout_seconds=30.0,
        tool_sandbox_backend="",
    )
    assert namespace["run"]("refund?", config=explicit_config) == (
        "Purchases within 30 days are eligible for a full refund."
    )
    assert seen_configs == [explicit_config]


def test_generated_code_executes_with_mcp_tool_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = make_manifest("mcp_codegen_exec_wf")
    data["nodes"]["agent"]["tools"] = ["lookup_docs"]
    data["tools"] = {
        "lookup_docs": {
            "type": "mcp_tool",
            "server_id": "MCP-DOCS",
            "tool_name": "search_docs",
            "side_effect_level": "read",
        }
    }
    code = compile_workflow(
        parse_manifest(data), resolver=fake_resolver(), version="7"
    ).generated_python

    assert "invoke_tool_by_server_id_sync" in code
    assert "_mcp_runtime_placeholder" not in code

    class FakeAgent:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeRunner:
        @staticmethod
        def run_sync(agent: FakeAgent, prompt: str, **kwargs: object) -> object:
            tool_result = agent.kwargs["tools"][0](query=prompt)
            return types.SimpleNamespace(final_output=tool_result["result"]["answer"])

    def _fake_invoke(
        *,
        server_id: str,
        tool_name: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        assert server_id == "MCP-DOCS"
        assert tool_name == "search_docs"
        assert arguments == {"query": "refund?"}
        return {"answer": "docs: refund?"}

    agents_mod = types.ModuleType("agents")
    agents_mod.Agent = FakeAgent
    agents_mod.Runner = FakeRunner
    agents_mod.RunConfig = lambda **kw: kw
    agents_mod.handoff = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
    monkeypatch.setitem(sys.modules, "agents", agents_mod)
    monkeypatch.setattr("caliber.mcp_gateway.invoke_tool_by_server_id_sync", _fake_invoke)

    mlflow_mod = types.ModuleType("mlflow")
    mlflow_mod.genai = types.SimpleNamespace(
        load_prompt=lambda _uri: types.SimpleNamespace(template="unused")
    )
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)

    namespace: dict[str, object] = {}
    exec(code, namespace)

    lookup_docs = namespace["lookup_docs"]
    assert callable(lookup_docs)
    assert namespace["run"]("refund?") == "docs: refund?"


def test_build_ir_and_generated_code_preserve_mlflow_context_settings() -> None:
    manifest = parse_manifest(
        make_support_manifest(
            mlflow={
                "experiment_name": "caliber/support",
                "trace_group_tags": {"team": "ops"},
            }
        )
    )
    ir = build_ir(manifest, fake_resolver(), version="7")
    code = generate_python(ir)

    assert ir.mlflow_experiment_name == "caliber/support"
    assert ir.mlflow_trace_group_tags == {"team": "ops"}
    assert 'default_model_ref="CALIBER_WORKFLOW_DEFAULT_MODEL"' in code
    assert 'extra_tags={"team": "ops"}' in code


def test_compile_fails_on_invalid_manifest() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["ghost"]  # unbound tool -> validation error
    with pytest.raises(CompileError) as exc:
        compile_workflow(parse_manifest(data), resolver=fake_resolver())
    assert exc.value.report is not None


def test_compile_fails_on_unsupported_sdk_policy() -> None:
    # sdk_version_policy is locked at the model layer; assert the compiler also
    # guards a manifest constructed to bypass it.
    manifest = parse_manifest(make_manifest())
    object.__setattr__(manifest.runtime, "sdk_version_policy", "manifest-pinned")
    with pytest.raises(CompileError):
        compile_workflow(manifest, resolver=fake_resolver())


def test_build_ir_reports_missing_prompt_artifact() -> None:
    data = make_support_manifest()
    data["artifacts"]["prompts"] = {}

    with pytest.raises(CompileError, match="references prompt"):
        build_ir(parse_manifest(data), fake_resolver())


def test_build_ir_reports_tool_binding_and_resolution_errors() -> None:
    missing_binding = make_manifest()
    missing_binding["nodes"]["agent"]["tools"] = ["ghost"]
    with pytest.raises(CompileError, match="has no binding"):
        build_ir(parse_manifest(missing_binding), fake_resolver())

    unresolved = make_manifest()
    unresolved["nodes"]["agent"]["tools"] = ["ghost"]
    unresolved["tools"] = {
        "ghost": {"registry_ref": "tool.ghost.v1", "version_constraint": ">=1.0"}
    }
    with pytest.raises(CompileError, match="not registered"):
        build_ir(parse_manifest(unresolved), fake_resolver())


def test_build_ir_supports_mcp_tool_bindings() -> None:
    data = make_manifest()
    data["nodes"]["agent"]["tools"] = ["lookup_docs"]
    data["tools"] = {
        "lookup_docs": {
            "type": "mcp_tool",
            "server_id": "MCP-DOCS",
            "tool_name": "search_docs",
            "side_effect_level": "read",
        }
    }

    ir = build_ir(parse_manifest(data), fake_resolver())
    agent = ir.nodes["agent"]
    assert isinstance(agent, IRAgent)
    assert agent.tools[0].binding_type == "mcp_tool"
    assert agent.tools[0].mcp_server_id == "MCP-DOCS"
    assert agent.tools[0].mcp_tool_name == "search_docs"


def test_build_ir_supports_mcp_resource_nodes() -> None:
    data = make_manifest()
    data["nodes"]["mcp_lookup"] = {
        "id": "mcp_lookup",
        "type": "mcp_resource",
        "server_id": "MCP-DOCS",
        "tool_name": "search_docs",
        "timeout_seconds": 30,
    }

    ir = build_ir(parse_manifest(data), fake_resolver())

    mcp = ir.nodes["mcp_lookup"]
    assert isinstance(mcp, IRMcpResource)
    assert mcp.node_type == NodeType.MCP_RESOURCE
    assert mcp.server_id == "MCP-DOCS"
    assert mcp.tool_name == "search_docs"


def test_build_ir_supports_new_orchestration_nodes() -> None:
    data = make_manifest()
    data["nodes"]["wait_until"] = {
        "id": "wait_until",
        "type": "wait_until",
        "wait_until": "2099-01-01T00:00:00Z",
    }
    data["nodes"]["wait_event"] = {
        "id": "wait_event",
        "type": "wait_for_event",
        "event_name": "resume_event",
    }
    data["nodes"]["parallel"] = {"id": "parallel", "type": "parallel"}
    data["nodes"]["join"] = {"id": "join", "type": "join"}
    data["nodes"]["for_each"] = {
        "id": "for_each",
        "type": "for_each",
        "target_node_id": "agent",
    }
    data["nodes"]["loop"] = {
        "id": "loop",
        "type": "loop",
        "target_node_id": "agent",
        "max_iterations": 4,
        "stop_condition": "iteration >= 2",
    }
    data["nodes"]["boundary"] = {
        "id": "boundary",
        "type": "error_boundary",
        "target_node_id": "agent",
    }
    data["nodes"]["subflow"] = {
        "id": "subflow",
        "type": "subworkflow",
        "workflow_id": "wf_other",
        "alias": "prod",
    }
    data["nodes"]["python"] = {
        "id": "python",
        "type": "python_code",
        "code": 'return {"text": input, "result": {"ok": True}}',
        "timeout_seconds": 7,
    }

    ir = build_ir(parse_manifest(data), fake_resolver())

    assert isinstance(ir.nodes["wait_until"], IRWaitUntil)
    assert isinstance(ir.nodes["wait_event"], IRWaitForEvent)
    assert isinstance(ir.nodes["parallel"], IRParallel)
    assert isinstance(ir.nodes["join"], IRJoin)
    assert isinstance(ir.nodes["for_each"], IRForEach)
    assert isinstance(ir.nodes["loop"], IRLoop)
    assert isinstance(ir.nodes["boundary"], IRErrorBoundary)
    assert isinstance(ir.nodes["subflow"], IRSubworkflow)
    assert isinstance(ir.nodes["python"], IRPythonCode)
    assert ir.nodes["python"].node_type == NodeType.PYTHON_CODE


def test_build_ir_rejects_mutated_unsupported_instruction_type() -> None:
    manifest = parse_manifest(make_manifest())
    object.__setattr__(manifest.nodes["agent"], "instructions", object())

    with pytest.raises(CompileError, match="unsupported instructions type"):
        build_ir(manifest, fake_resolver())


def test_build_ir_handles_non_agent_nodes_and_orphan_edge_source() -> None:
    data = make_manifest()
    data["nodes"].update(
        {
            "router": {
                "id": "router",
                "type": "router",
                "inputs": {"input": {"type": "string"}},
                "outputs": {"route": {"type": "string"}},
                "branches": [{"condition": {"contains": "billing"}, "to": "agent"}],
            },
            "approval": {
                "id": "approval",
                "type": "human_approval",
                "required_role": "caliber.admin",
                "approval_count": 2,
                # "block" rather than "escalate": the manifest now rejects unimplemented
                # timeout behaviours at parse time, because accepting one would leave a
                # control that silently does nothing. See test_approval_policy.py.
                "timeout_behavior": "block",
            },
            "external": {
                "id": "external",
                "type": "external_app",
                "entrypoint": "pkg.app:run",
            },
            "file_input": {"id": "file_input", "type": "file_input", "path": "/tmp/a.txt"},
            "folder_input": {
                "id": "folder_input",
                "type": "folder_input",
                "path": "/tmp",
                "pattern": "*.txt",
                "recursive": False,
            },
            "note": {"id": "note", "type": "note", "text": "operator note"},
        }
    )
    data["edges"].append(
        {"id": "orphan", "from": "ghost", "to": "agent", "map": {"missing": "input"}}
    )

    ir = build_ir(parse_manifest(data), fake_resolver())

    assert isinstance(ir.nodes["router"], IRRouter)
    assert ir.nodes["router"].branches[0].to == "agent"
    assert isinstance(ir.nodes["approval"], IRHumanApproval)
    assert ir.nodes["approval"].approval_count == 2
    assert isinstance(ir.nodes["external"], IRExternalApp)
    assert ir.nodes["external"].entrypoint == "pkg.app:run"
    assert isinstance(ir.nodes["file_input"], IRFileInput)
    assert ir.nodes["file_input"].path == "/tmp/a.txt"
    assert isinstance(ir.nodes["folder_input"], IRFolderInput)
    assert ir.nodes["folder_input"].pattern == "*.txt"
    assert ir.nodes["note"].node_type == NodeType.NOTE
    assert ir.edges[-1].type_check.name == "void"


def test_build_ir_preserves_managed_file_snapshot() -> None:
    data = make_manifest()
    data["nodes"]["managed"] = {
        "id": "managed",
        "type": "file_input",
        "file_ref": {
            "file_id": "FILE-1",
            "file_ref": "caliber://projects/PRJ-1/input/source.md",
            "sha256": "b" * 64,
            "name": "source.md",
            "size_bytes": 42,
            "media_type": "text/markdown",
            "object_version_id": "version-7",
        },
    }
    node = build_ir(parse_manifest(data), fake_resolver()).nodes["managed"]
    assert isinstance(node, IRFileInput)
    assert node.path == ""
    assert node.file_ref is not None
    assert node.file_ref.file_id == "FILE-1"
    assert node.file_ref.sha256 == "b" * 64
    assert node.file_ref.object_version_id == "version-7"


def test_entry_agent_falls_back_and_errors_when_absent() -> None:
    unreachable = make_manifest()
    unreachable["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}}
    ]
    assert build_ir(parse_manifest(unreachable), fake_resolver()).entry_node_id == "agent"

    no_agent = make_manifest()
    no_agent["nodes"].pop("agent")
    no_agent["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}}
    ]
    assert build_ir(parse_manifest(no_agent), fake_resolver()).entry_node_id == "start"


def test_entry_node_prefers_reachable_non_agent_when_workflow_has_no_agents() -> None:
    data = make_manifest("subworkflow_entry_wf")
    data["nodes"].pop("agent")
    data["nodes"]["child_workflow"] = {
        "id": "child_workflow",
        "type": "subworkflow",
        "workflow_id": "WF-child",
        "alias": "prod",
        "inputs": {"input": {"type": "string"}},
        "outputs": {
            "output": {"type": "string"},
            "result": {"type": "structured"},
        },
    }
    data["edges"] = [
        {"id": "e_start_child", "from": "start", "to": "child_workflow", "map": {"msg": "input"}},
        {
            "id": "e_child_final",
            "from": "child_workflow",
            "to": "final",
            "map": {"output": "response"},
        },
    ]

    ir = build_ir(parse_manifest(data), fake_resolver())

    assert ir.entry_node_id == "child_workflow"


def test_entry_node_prefers_reachable_tool_when_workflow_has_no_agents() -> None:
    data = make_manifest("tool_entry_wf")
    data["nodes"].pop("agent")
    data["nodes"]["tool_lookup"] = {
        "id": "tool_lookup",
        "type": "tool",
        "tool_name": "lookup_policy",
        "inputs": {
            "input": {"type": "string"},
            "arguments": {"type": "structured"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }
    data["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    data["edges"] = [
        {"id": "e_start_tool", "from": "start", "to": "tool_lookup", "map": {"msg": "input"}},
        {"id": "e_tool_final", "from": "tool_lookup", "to": "final", "map": {"text": "response"}},
    ]

    ir = build_ir(parse_manifest(data), fake_resolver())

    assert ir.entry_node_id == "tool_lookup"


def test_compile_workflow_generates_runtime_export_for_non_agent_workflow() -> None:
    data = make_manifest("non_agent_codegen_wf")
    data["nodes"].pop("agent")
    data["edges"] = [
        {"id": "e_start_final", "from": "start", "to": "final", "map": {"msg": "response"}}
    ]

    result = compile_workflow(parse_manifest(data), resolver=fake_resolver(), version="7")

    assert result.report["entry_node_id"] == "start"
    assert result.report["export_mode"] == "runtime_ir"
    assert result.report["agent_count"] == 0
    assert "def run(" in result.generated_python
    assert "run_exported_workflow(" in result.generated_python
    assert "review-only for non-agent workflows" not in result.generated_python
    compile(result.generated_python, "<generated-non-agent>", "exec")


def test_runtime_export_executes_mixed_workflow() -> None:
    data = make_manifest("mixed_codegen_exec_wf")
    data["nodes"]["wrap"] = {
        "id": "wrap",
        "type": "template",
        "template": "Wrapped: {{input}}",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "wrap", "map": {"final_output": "input"}},
        {"id": "e3", "from": "wrap", "to": "final", "map": {"text": "response"}},
    ]

    generated = compile_workflow(
        parse_manifest(data),
        resolver=fake_resolver(),
        version="7",
    ).generated_python

    assert "run_exported_workflow(" in generated
    assert "from agents import Agent, Runner, handoff" not in generated

    namespace: dict[str, object] = {}
    exec(compile(generated, "<generated-mixed>", "exec"), namespace)

    output = namespace["run"]("hello", executor=FakeWorkflowExecutor())

    assert output == "Wrapped: [test-agent] processed: hello"


def test_codegen_helpers_cover_identifier_and_empty_instruction_edges() -> None:
    assert compiler._py_identifier("123node") == "n_123node"
    assert compiler._py_identifier("class") == "class_"
    assert compiler._instructions_literal(None) == '""'


def test_compile_cache_fingerprints_unresolved_tools_and_evicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compiler.clear_compile_cache()
    unresolved = make_manifest()
    unresolved["nodes"]["agent"]["tools"] = ["ghost"]
    unresolved["tools"] = {
        "ghost": {"registry_ref": "tool.ghost.v1", "version_constraint": ">=1.0"}
    }

    with pytest.raises(CompileError):
        compile_workflow(parse_manifest(unresolved), resolver=fake_resolver(), use_cache=True)

    monkeypatch.setattr(compiler, "_COMPILE_CACHE_MAX", 0)
    result = compile_workflow(
        parse_manifest(make_manifest("cache_wf")), resolver=fake_resolver(), use_cache=True
    )

    assert result.cached is False
    assert len(compiler._COMPILE_CACHE) == 0
