"""One probe per ``refuse`` row of the protected-effect inventory (B04.7, #774).

Each probe drives the **real entry point** of its row under the reference profile and asserts the refusal
(``UnsupportedEffectPath``, code ``unsupported_in_reference_profile``) **and** that nothing forbidden happened: no process was
spawned, no module imported, no file or directory created, no database touched. A probe with a development counterpart
(:data:`DEVELOPMENT_PROBES`) shows the same entry point still works under the default profile, so the refusal is a
reference-profile control and not a regression. ``tests/test_effect_path_matrix.py`` runs both over every ``refuse`` row.
"""

from __future__ import annotations

import re
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from caliber.config import CaliberConfig
from caliber.effects.dispatcher import UnsupportedEffectPath
from caliber.effects.refusals import UNSUPPORTED_IN_REFERENCE_PROFILE
from caliber.tool_sandbox import server as sandbox_server
from caliber.tool_sandbox.models import (
    ToolSandboxInspectRequest,
    ToolSandboxRunRequest,
    ToolSandboxTestCase,
    ToolSandboxTestSuiteRequest,
)
from caliber.tool_sandbox.service import LocalSubprocessToolSandbox
from caliber.workflows import export_runtime
from caliber.workflows import runtime as runtime_module
from caliber.workflows.compiler import build_ir
from caliber.workflows.ir import IRExternalApp, IRType, IRWorkflow, NodeType
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, _run_node, execute
from caliber.workflows.tools import InMemoryToolResolver
from tests.workflow_helpers import fake_resolver, make_manifest

REFERENCE = CaliberConfig(profile="reference")
DEVELOPMENT = CaliberConfig(profile="development")

#: A module an administrator could register: it is not one of CALIBER's shipped tools.
ADMIN_MODULE = "json"
ADMIN_CALLABLE = "dumps"
#: One of CALIBER's own shipped read-class tools, as the registry seed writes it.
SHIPPED_MODULE = "caliber.workflows.demo_tools"
SHIPPED_CALLABLE = "grok_parse"


@dataclass
class ProbeEnv:
    """What a probe may use: a scratch directory, the monkeypatcher and (lazily) the route client."""

    tmp_path: Path
    monkeypatch: pytest.MonkeyPatch
    request: pytest.FixtureRequest

    def fixture(self, name: str) -> Any:
        return self.request.getfixturevalue(name)


def expect_refused(row_id: str, call: Callable[[], object]) -> UnsupportedEffectPath:
    """Run ``call``, which must raise the refusal for ``row_id`` (and name it, and no payload)."""
    with pytest.raises(UnsupportedEffectPath) as caught:
        call()
    refusal = caught.value
    assert refusal.code == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert str(refusal).startswith(f"{UNSUPPORTED_IN_REFERENCE_PROFILE}: {row_id} (")
    assert "CALIBER_PROFILE=development" in str(refusal)
    return refusal


class Spawns:
    """Counts child processes the sandbox would have started, and refuses to start any."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0

        def _popen(*_args: object, **_kwargs: object) -> object:
            self.count += 1
            raise AssertionError("the sandbox spawned a child process")

        monkeypatch.setattr("caliber.tool_sandbox.service.subprocess.Popen", _popen)


def _reference_sandbox() -> LocalSubprocessToolSandbox:
    return LocalSubprocessToolSandbox.from_config(REFERENCE)


def _manifest_with(node: dict[str, Any], *, inputs: bool = True) -> dict[str, Any]:
    data = make_manifest()
    data["nodes"]["probe"] = node
    data["edges"] = [
        {"id": "e_start_probe", "from": "start", "to": "probe", "map": {"msg": "input"}},
        {"id": "e_probe_final", "from": "probe", "to": "final", "map": {"text": "response"}},
    ]
    return data


def _run_manifest(data: dict[str, Any], config: CaliberConfig, **plan_kwargs: Any) -> Any:
    ir = build_ir(parse_manifest(data), fake_resolver(), version="7")
    plan = RuntimePlan(ir=ir, resolver=fake_resolver(), sandbox_config=config, **plan_kwargs)
    return execute(plan, "refund", executor=FakeWorkflowExecutor())


# ---- EP-011: the python_code node ---------------------------------------------------------------------------------------


_PYTHON_NODE: dict[str, Any] = {
    "id": "probe",
    "type": "python_code",
    "code": 'return {"text": (input or "").upper()}',
    "timeout_seconds": 5,
    "inputs": {"input": {"type": "string"}},
    "outputs": {"text": {"type": "string"}},
}


def probe_ep_011(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    result = _run_manifest(_manifest_with(dict(_PYTHON_NODE)), REFERENCE)
    assert result.status == "error"
    assert UNSUPPORTED_IN_REFERENCE_PROFILE in (result.error or "")
    assert "EP-011" in (result.error or "")
    assert spawns.count == 0


def development_ep_011(env: ProbeEnv) -> None:
    result = _run_manifest(_manifest_with(dict(_PYTHON_NODE)), DEVELOPMENT)
    assert result.status == "completed", result.error
    assert result.output == "REFUND"


# ---- EP-012: the external_app node --------------------------------------------------------------------------------------

_EXTERNAL_MODULE = "refusal_probe_external_app"


def _external_app_run(env: ProbeEnv, config: CaliberConfig) -> tuple[list[str], Callable[[], Any]]:
    imported: list[str] = []
    module = types.ModuleType(_EXTERNAL_MODULE)

    def handle(**_kwargs: object) -> dict[str, object]:
        imported.append("called")
        return {"text": "handled"}

    module.handle = handle  # type: ignore[attr-defined]
    env.monkeypatch.setitem(sys.modules, _EXTERNAL_MODULE, module)
    node = IRExternalApp(
        node_id="external",
        node_type=NodeType.EXTERNAL_APP,
        inputs={"input": IRType("string")},
        outputs={"text": IRType("string")},
        entrypoint=f"{_EXTERNAL_MODULE}:handle",
    )
    ir = IRWorkflow(
        workflow_id="wf-external",
        version="1",
        nodes={"external": node},
        edges=[],
        entry_node_id="external",
        output_node_id="external",
    )

    def run() -> Any:
        return _run_node(
            node,
            ir,
            RuntimePlan(
                ir=ir,
                resolver=InMemoryToolResolver([]),
                external_app_entrypoint_allowlist=f"{_EXTERNAL_MODULE}:*",
                sandbox_config=config,
            ),
            executor=FakeWorkflowExecutor(),
            preview=False,
            inputs={"input": "approve"},
            run_input="approve",
            port_values={},
            guardrail_results=[],
        )

    return imported, run


def probe_ep_012(env: ProbeEnv) -> None:
    called, run = _external_app_run(env, REFERENCE)
    # The entrypoint is allowlisted: the allowlist is not what refuses it.
    expect_refused("EP-012", run)
    assert called == []


def development_ep_012(env: ProbeEnv) -> None:
    called, run = _external_app_run(env, DEVELOPMENT)
    _, step = run()
    assert step.status == "ok"
    assert called == ["called"]


# ---- EP-014: the output_folder node -------------------------------------------------------------------------------------


def _folder_args(env: ProbeEnv) -> tuple[Path, dict[str, Any]]:
    target = env.tmp_path / "out" / "nested"
    env.monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(env.tmp_path))
    return target, {
        "path": str(target),
        "overwrite": True,
        "port_values": {},
        "direct_input": "hello",
    }


def probe_ep_014(env: ProbeEnv) -> None:
    target, kwargs = _folder_args(env)
    # The file root is configured and the path is inside it: confinement is not what refuses it.
    expect_refused(
        "EP-014", lambda: runtime_module._write_output_folder_node(config=REFERENCE, **kwargs)
    )
    assert not target.exists()
    assert not (env.tmp_path / "out").exists()


def development_ep_014(env: ProbeEnv) -> None:
    target, kwargs = _folder_args(env)
    written, _metadata = runtime_module._write_output_folder_node(config=DEVELOPMENT, **kwargs)
    assert written
    assert target.is_dir()


# ---- EP-015 / EP-016 / EP-019: binding a registered module ---------------------------------------------------------------


def _binding(module: str, callable_name: str) -> Any:
    from caliber.workflows.ir import IRToolBinding

    return IRToolBinding(
        local_name=callable_name,
        registry_ref=f"tool.{callable_name}.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path=module,
        callable_name=callable_name,
    )


def probe_ep_015(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    expect_refused(
        "EP-015",
        lambda: runtime_module._sandboxed_registered_tool(
            _binding(ADMIN_MODULE, ADMIN_CALLABLE), config=REFERENCE
        ),
    )
    # Through the binder every real caller uses, with the process-wide configuration the app binds at startup.
    env.monkeypatch.setattr(runtime_module, "_ACTIVE_SANDBOX_CONFIG", REFERENCE)
    expect_refused(
        "EP-015",
        lambda: runtime_module._bind(
            _binding(ADMIN_MODULE, ADMIN_CALLABLE), InMemoryToolResolver([])
        ),
    )
    assert spawns.count == 0


def development_ep_015(env: ProbeEnv) -> None:
    bound = runtime_module._sandboxed_registered_tool(
        _binding(ADMIN_MODULE, ADMIN_CALLABLE), config=DEVELOPMENT
    )
    assert callable(bound)


def _export_entry(module: str, callable_name: str) -> Any:
    return types.SimpleNamespace(
        name=callable_name,
        registry_ref=f"tool.{callable_name}.v1",
        module_path=module,
        callable_name=callable_name,
        execution_backend="python_callable",
        side_effect_level="read",
        allow_in_preview=True,
    )


def probe_ep_016(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    expect_refused(
        "EP-016",
        lambda: runtime_module.bind_exported_tool(
            _export_entry(ADMIN_MODULE, ADMIN_CALLABLE), config=REFERENCE
        ),
    )
    assert spawns.count == 0


def development_ep_016(env: ProbeEnv) -> None:
    config = CaliberConfig(profile="development", registered_tool_sandbox_enabled=False)
    bound = runtime_module.bind_exported_tool(
        _export_entry(ADMIN_MODULE, ADMIN_CALLABLE), config=config
    )
    assert bound is __import__(ADMIN_MODULE).dumps


def probe_ep_019(env: ProbeEnv) -> None:
    from caliber.workflows.tools import ToolRegistryEntry, bind_registered_tool

    imported: list[str] = []
    real_import = __import__("importlib").import_module
    env.monkeypatch.setattr(
        "importlib.import_module",
        lambda name, *a, **k: imported.append(name) or real_import(name, *a, **k),
    )
    entry = ToolRegistryEntry(
        name=ADMIN_CALLABLE, version="1.0", module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE
    )
    expect_refused("EP-019", lambda: bind_registered_tool(entry, config=REFERENCE))
    assert imported == []


def development_ep_019(env: ProbeEnv) -> None:
    from caliber.workflows.tools import ToolRegistryEntry, bind_registered_tool

    entry = ToolRegistryEntry(
        name=ADMIN_CALLABLE, version="1.0", module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE
    )
    assert bind_registered_tool(entry, config=DEVELOPMENT) is __import__(ADMIN_MODULE).dumps


# ---- EP-020 to EP-026: the local subprocess sandbox ----------------------------------------------------------------------


def probe_ep_020(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    sandbox = _reference_sandbox()
    expect_refused(
        "EP-020",
        lambda: sandbox.run_tool(
            ToolSandboxRunRequest(source_code="def f(): return 1", callable_name="f")
        ),
    )
    expect_refused(
        "EP-020",
        lambda: sandbox.run_tool(
            ToolSandboxRunRequest(module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE)
        ),
    )
    assert spawns.count == 0


def development_ep_020(env: ProbeEnv) -> None:
    sandbox = LocalSubprocessToolSandbox.from_config(DEVELOPMENT)
    result = sandbox.run_tool(
        ToolSandboxRunRequest(source_code="def f(): return 1", callable_name="f")
    )
    assert result.status == "completed"
    assert result.output == 1


def probe_ep_021(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    expect_refused(
        "EP-021",
        lambda: _reference_sandbox().inspect_tool(
            ToolSandboxInspectRequest(module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE)
        ),
    )
    assert spawns.count == 0


def development_ep_021(env: ProbeEnv) -> None:
    result = LocalSubprocessToolSandbox.from_config(DEVELOPMENT).inspect_tool(
        ToolSandboxInspectRequest(module_path=ADMIN_MODULE, callable_name=ADMIN_CALLABLE)
    )
    assert result.available


def _test_suite() -> ToolSandboxTestSuiteRequest:
    return ToolSandboxTestSuiteRequest(
        source_code="def f(): return 1",
        callable_name="f",
        tests=[ToolSandboxTestCase(name="one", input={}, expected=1)],
    )


def probe_ep_022(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    expect_refused("EP-022", lambda: _reference_sandbox().run_tests(_test_suite()))
    assert spawns.count == 0


def development_ep_022(env: ProbeEnv) -> None:
    result = LocalSubprocessToolSandbox.from_config(DEVELOPMENT).run_tests(_test_suite())
    assert result.status == "passed"


def probe_ep_023(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    sandbox = _reference_sandbox()
    for payload in (
        {"mode": "run", "source_code": "def f(): return 1", "callable_name": "f"},
        {"mode": "run", "module_path": ADMIN_MODULE, "callable_name": ADMIN_CALLABLE},
        {"mode": "inspect", "module_path": ADMIN_MODULE, "callable_name": ADMIN_CALLABLE},
        {"mode": "tests", "callable_name": "f"},
        {"mode": "run"},
    ):
        expect_refused(
            "EP-023", lambda payload=payload: sandbox._run_payload(dict(payload), timeout_seconds=1)
        )
    assert spawns.count == 0


def development_ep_023(env: ProbeEnv) -> None:
    raw = LocalSubprocessToolSandbox.from_config(DEVELOPMENT)._run_payload(
        {"mode": "run", "source_code": "def f(): return 7", "callable_name": "f"},
        timeout_seconds=10,
    )
    assert raw["status"] == "completed"
    assert raw["output"] == 7


def probe_ep_025(env: ProbeEnv) -> None:
    """The child's source loader is reachable only through ``_run_payload``, which refuses authored source first."""
    spawns = Spawns(env.monkeypatch)
    expect_refused(
        "EP-023",
        lambda: _reference_sandbox()._run_payload(
            {"mode": "run", "source_code": "import os", "callable_name": "f"}, timeout_seconds=1
        ),
    )
    assert spawns.count == 0


def probe_ep_026(env: ProbeEnv) -> None:
    """The child's module resolver is reachable only through ``_run_payload``, which refuses an administrator's module first."""
    spawns = Spawns(env.monkeypatch)
    expect_refused(
        "EP-023",
        lambda: _reference_sandbox()._run_payload(
            {"mode": "run", "module_path": ADMIN_MODULE, "callable_name": ADMIN_CALLABLE},
            timeout_seconds=1,
        ),
    )
    assert spawns.count == 0


def shipped_tool_still_runs_in_the_reference_sandbox(env: ProbeEnv) -> None:
    """The one exemption: a shipped read-class built-in is run, for real, by a sandbox built for the reference profile."""
    result = _reference_sandbox().run_tool(
        ToolSandboxRunRequest(
            module_path=SHIPPED_MODULE,
            callable_name=SHIPPED_CALLABLE,
            input={"pattern": "%{WORD:w}", "text": "hello"},
        )
    )
    assert result.status == "completed", result.error


# ---- EP-027 / EP-028 / EP-029: the standalone sandbox service -----------------------------------------------------------


def _sandbox_client(config: CaliberConfig) -> Any:
    from starlette.testclient import TestClient

    app = sandbox_server.create_app(DEVELOPMENT, sandbox=_Unreachable())
    app.state.config = config
    return TestClient(app)


class _Unreachable:
    """A sandbox that must never be called."""

    def run_tool(self, request: object) -> object:
        raise AssertionError("the standalone service ran a tool")

    def run_tests(self, request: object) -> object:
        raise AssertionError("the standalone service ran tests")

    def inspect_tool(self, request: object) -> object:
        raise AssertionError("the standalone service inspected a tool")


def probe_ep_027(env: ProbeEnv) -> None:
    response = _sandbox_client(REFERENCE).post(
        sandbox_server.RUN_PATH, json={"source_code": "def f(): return 1", "callable_name": "f"}
    )
    assert response.status_code == 409
    assert response.json()["reason_code"] == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert "EP-027" in response.json()["detail"]


def probe_ep_028(env: ProbeEnv) -> None:
    response = _sandbox_client(REFERENCE).post(
        sandbox_server.TESTS_PATH,
        json={
            "source_code": "def f(): return 1",
            "callable_name": "f",
            "tests": [{"name": "one", "input": {}, "expected": 1}],
        },
    )
    assert response.status_code == 409
    assert response.json()["reason_code"] == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert "EP-028" in response.json()["detail"]


def development_ep_027(env: ProbeEnv) -> None:
    app = sandbox_server.create_app(
        DEVELOPMENT, sandbox=LocalSubprocessToolSandbox.from_config(DEVELOPMENT)
    )
    from starlette.testclient import TestClient

    response = TestClient(app).post(
        sandbox_server.RUN_PATH, json={"source_code": "def f(): return 1", "callable_name": "f"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["output"] == 1


def probe_ep_029(env: ProbeEnv) -> None:
    from caliber.tool_sandbox import __main__ as sandbox_main

    started: list[object] = []
    env.monkeypatch.setattr(
        sandbox_main, "uvicorn_mod", types.SimpleNamespace(run=lambda *a, **k: started.append(a))
    )
    env.monkeypatch.setattr(sys, "argv", ["caliber-tool-sandbox"])
    env.monkeypatch.setenv("CALIBER_PROFILE", "reference")
    expect_refused("EP-029", sandbox_main.main)
    # The factory it launches refuses too, so ``uvicorn ...:create_app --factory`` cannot be used round the CLI.
    expect_refused("EP-029", lambda: sandbox_server.create_app(REFERENCE))
    assert started == []


def development_ep_029(env: ProbeEnv) -> None:
    from caliber.tool_sandbox import __main__ as sandbox_main

    started: list[object] = []
    env.monkeypatch.setattr(
        sandbox_main, "uvicorn_mod", types.SimpleNamespace(run=lambda *a, **k: started.append(a))
    )
    env.monkeypatch.setattr(sys, "argv", ["caliber-tool-sandbox"])
    env.monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    sandbox_main.main()
    assert len(started) == 1


# ---- EP-041 / EP-042: standalone exports --------------------------------------------------------------------------------


def probe_ep_041(env: ProbeEnv) -> None:
    ran: list[str] = []
    env.monkeypatch.setattr(export_runtime, "execute", lambda *a, **k: ran.append("execute"))
    ir = build_ir(parse_manifest(make_manifest()), fake_resolver(), version="7")
    expect_refused(
        "EP-041",
        lambda: export_runtime.execute_exported_workflow(ir, "hello", config=REFERENCE),
    )
    expect_refused(
        "EP-041", lambda: export_runtime.run_exported_workflow(ir, "hello", config=REFERENCE)
    )
    assert ran == []


def development_ep_041(env: ProbeEnv) -> None:
    ir = build_ir(parse_manifest(make_manifest()), fake_resolver(), version="7")
    result = export_runtime.execute_exported_workflow(
        ir, "hello", config=DEVELOPMENT, executor=FakeWorkflowExecutor()
    )
    assert result.status == "completed", result.error


def probe_ep_042(env: ProbeEnv) -> None:
    built: list[str] = []
    env.monkeypatch.setattr(
        export_runtime, "KnowledgeBaseService", lambda **kwargs: built.append("service")
    )
    expect_refused(
        "EP-042",
        lambda: export_runtime._default_knowledge_build_runner(
            session_factory=object(),  # type: ignore[arg-type]
            config=REFERENCE,
            identity=export_runtime._default_identity(active_project_id=None),
        ),
    )
    assert built == []


# ---- EP-044 / EP-045: the export routes ---------------------------------------------------------------------------------


def _published_version(client: Any) -> str:
    from tests.test_routes_workflow_versions import PREFIX, _support_workflow

    _wid, vid, _ = _support_workflow(client)
    assert client.post(f"{PREFIX}/workflow-versions/{vid}/publish").status_code in (200, 201)
    return str(vid)


def _as_reference(client: Any) -> None:
    client.app.state.config = client.app.state.config.model_copy(update={"profile": "reference"})


def _assert_route_refused(response: Any, row_id: str) -> None:
    assert response.status_code == 409
    body = response.json()
    assert body["reason_code"] == UNSUPPORTED_IN_REFERENCE_PROFILE
    assert row_id in body["detail"]
    assert "CALIBER_PROFILE=development" in body["detail"]


def probe_ep_044(env: ProbeEnv) -> None:
    from tests.test_routes_workflow_versions import PREFIX

    client = env.fixture("client")
    vid = _published_version(client)
    _as_reference(client)
    response = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python")
    _assert_route_refused(response, "EP-044")
    assert "def run(" not in response.text


def development_ep_044(env: ProbeEnv) -> None:
    from tests.test_routes_workflow_versions import PREFIX

    client = env.fixture("client")
    vid = _published_version(client)
    response = client.get(f"{PREFIX}/workflow-versions/{vid}/export/python")
    assert response.status_code == 200
    assert "def run(" in response.text


def probe_ep_045(env: ProbeEnv) -> None:
    from tests.test_routes_workflow_versions import PREFIX

    client = env.fixture("client")
    vid = _published_version(client)
    _as_reference(client)
    response = client.get(f"{PREFIX}/workflow-versions/{vid}/export/deployment-bundle")
    _assert_route_refused(response, "EP-045")
    assert "generated_python" not in response.text


def development_ep_045(env: ProbeEnv) -> None:
    from tests.test_routes_workflow_versions import PREFIX

    client = env.fixture("client")
    vid = _published_version(client)
    response = client.get(f"{PREFIX}/workflow-versions/{vid}/export/deployment-bundle")
    assert response.status_code == 200
    assert "generated_python" in response.text


# ---- EP-054 / EP-055: refused built-ins ---------------------------------------------------------------------------------


def _refused_builtin_probe(row_id: str, names: tuple[str, ...]) -> Callable[[ProbeEnv], None]:
    def probe(env: ProbeEnv) -> None:
        spawns = Spawns(env.monkeypatch)
        sandbox = _reference_sandbox()
        for name in names:
            expect_refused(
                "EP-020",
                lambda name=name: sandbox.run_tool(
                    ToolSandboxRunRequest(module_path=SHIPPED_MODULE, callable_name=name)
                ),
            )
            env.monkeypatch.setattr(runtime_module, "_ACTIVE_SANDBOX_CONFIG", REFERENCE)
            expect_refused(
                "EP-015",
                lambda name=name: runtime_module._bind(
                    _binding(SHIPPED_MODULE, name), InMemoryToolResolver([])
                ),
            )
        assert spawns.count == 0

    probe.__name__ = f"probe_{row_id.lower().replace('-', '_')}"
    return probe


probe_ep_054 = _refused_builtin_probe("EP-054", ("sandbox_python",))
probe_ep_055 = _refused_builtin_probe(
    "EP-055", ("read_text_file", "list_folder_files", "grep_files", "regex_search")
)


# ---- EP-078 / EP-079 / EP-080: the assistant ----------------------------------------------------------------------------


_TOOL_DRAFT = {
    "name": "upper",
    "description": "uppercases",
    "source": "def upper(x: str) -> str:\n    return x.upper()\n",
    "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
    "tests": [{"name": "t", "input": {"x": "a"}, "expected": "A"}],
}


def _assistant(config: CaliberConfig) -> Any:
    from caliber.assistant.fake import FakeAssistantEngine
    from caliber.assistant.service import AssistantService

    return AssistantService(engine=FakeAssistantEngine(), runtime_config=config)


def _assistant_toolset(env: ProbeEnv, config: CaliberConfig) -> tuple[Any, Any, str]:
    """An Aria toolset in ``build`` mode with ``auto_safe`` approval (the lowest tier that offers the sandbox tools)."""
    from tests.test_assistant_agent_tools import _make_draft, _session, _toolset

    factory = env.fixture("session_factory")
    service = _assistant(config)
    session_id = _session(service, factory)
    draft_id = _make_draft(factory, session_id, "tool", _TOOL_DRAFT)
    return (
        _toolset(service, factory, session_id, mode="build", approval="auto_safe"),
        service,
        draft_id,
    )


def probe_ep_078(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    report = _assistant(REFERENCE)._run_tool_sandbox_tests(dict(_TOOL_DRAFT))
    assert report.passed is False
    assert report.failures == len(_TOOL_DRAFT["tests"])
    assert (report.error or "").startswith(f"{UNSUPPORTED_IN_REFERENCE_PROFILE}: EP-078 (")
    assert spawns.count == 0


def development_ep_078(env: ProbeEnv) -> None:
    report = _assistant(CaliberConfig(tool_sandbox_timeout_seconds=60.0))._run_tool_sandbox_tests(
        dict(_TOOL_DRAFT)
    )
    assert report.passed is True, report.error


def _refused_tool_answer(out: str, row_id: str) -> None:
    import json

    error = json.loads(out)["error"]
    assert error.startswith(f"{UNSUPPORTED_IN_REFERENCE_PROFILE}: {row_id} (")
    assert "CALIBER_PROFILE=development" in error
    assert "upper" not in error  # nothing of the draft is echoed


def probe_ep_079(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    toolset, _service, draft_id = _assistant_toolset(env, REFERENCE)
    out = toolset.dispatch("run_tool_sandbox", {"draft_id": draft_id, "input": {"x": "hi"}})
    _refused_tool_answer(out, "EP-079")
    assert spawns.count == 0


def development_ep_079(env: ProbeEnv) -> None:
    import json

    toolset, _service, draft_id = _assistant_toolset(
        env, CaliberConfig(tool_sandbox_timeout_seconds=60.0)
    )
    out = json.loads(
        toolset.dispatch("run_tool_sandbox", {"draft_id": draft_id, "input": {"x": "hi"}})
    )
    assert out["ok"]
    assert out["data"]["output"] == "HI"


def probe_ep_080(env: ProbeEnv) -> None:
    spawns = Spawns(env.monkeypatch)
    toolset, _service, draft_id = _assistant_toolset(env, REFERENCE)
    out = toolset.dispatch("evaluate_tool_draft", {"draft_id": draft_id, "dataset_id": "ED-none"})
    _refused_tool_answer(out, "EP-080")
    assert spawns.count == 0


def development_ep_080(env: ProbeEnv) -> None:
    import json

    toolset, _service, draft_id = _assistant_toolset(
        env, CaliberConfig(tool_sandbox_timeout_seconds=60.0)
    )
    out = json.loads(
        toolset.dispatch("evaluate_tool_draft", {"draft_id": draft_id, "dataset_id": "ED-none"})
    )
    # Past the refusal the tool goes on to look for the dataset, which does not exist: it was not refused.
    assert "dataset" in out["error"]
    assert UNSUPPORTED_IN_REFERENCE_PROFILE not in out["error"]


# ---- EP-114: the first-party database MCP server ------------------------------------------------------------------------


def probe_ep_114(env: ProbeEnv) -> None:
    from caliber.mcp_servers.db import connection

    connected: list[str] = []
    env.monkeypatch.setattr(
        connection, "_load_psycopg", lambda: connected.append("psycopg") or (None, None)
    )
    env.monkeypatch.setenv("CALIBER_PROFILE", "reference")
    for call in (
        lambda: connection.execute("INSERT INTO t VALUES (1)"),
        lambda: connection.execute_many("INSERT INTO t VALUES (%s)", [(1,)]),
        lambda: connection.execute_script(["CREATE TABLE t (a int)"]),
        lambda: connection.query("SELECT 1"),
    ):
        expect_refused("EP-114", call)
    assert connected == []


def development_ep_114(env: ProbeEnv) -> None:
    from caliber.mcp_servers.db import connection

    env.monkeypatch.delenv("CALIBER_PROFILE", raising=False)
    env.monkeypatch.setattr(
        connection,
        "_load_psycopg",
        lambda: (_ for _ in ()).throw(RuntimeError("reached the driver")),
    )
    with pytest.raises(RuntimeError, match="reached the driver"):
        connection.execute("INSERT INTO t VALUES (1)")


# ---- EP-047 / EP-048: the working-directory writes -----------------------------------------------------------------------


def probe_ep_047(env: ProbeEnv) -> None:
    from caliber.workflows import file_tools

    wrote: list[str] = []
    service = types.SimpleNamespace(write_artifact=lambda *a, **k: wrote.append("write"))
    expect_refused(
        "EP-047",
        lambda: file_tools.write_workdir_file(
            service,
            None,
            None,
            path="a.txt",
            content="x",
            config=REFERENCE,  # type: ignore[arg-type]
        ),
    )
    assert wrote == []


def development_ep_047(env: ProbeEnv) -> None:
    from caliber.workflows import file_tools

    wrote: list[str] = []
    service = types.SimpleNamespace(
        write_artifact=lambda *a, **k: types.SimpleNamespace(
            to_api=lambda: wrote.append("write") or {"ok": True}
        )
    )
    result = file_tools.write_workdir_file(
        service,
        None,
        None,
        path="a.txt",
        content="x",
        config=DEVELOPMENT,  # type: ignore[arg-type]
    )
    assert result == {"ok": True}
    assert wrote == ["write"]


def probe_ep_048(env: ProbeEnv) -> None:
    from caliber.workflows import file_tools

    wrote: list[str] = []
    service = types.SimpleNamespace(write_artifact=lambda *a, **k: wrote.append("write"))
    expect_refused(
        "EP-048",
        lambda: file_tools.create_artifact(
            service,
            None,
            None,
            path="a.txt",
            content="x",
            config=REFERENCE,  # type: ignore[arg-type]
        ),
    )
    assert wrote == []


def development_ep_048(env: ProbeEnv) -> None:
    from caliber.workflows import file_tools

    service = types.SimpleNamespace(
        write_artifact=lambda *a, **k: types.SimpleNamespace(to_api=lambda: {"kind": k["kind"]})
    )
    result = file_tools.create_artifact(
        service,
        None,
        None,
        path="a.txt",
        content="x",
        config=DEVELOPMENT,  # type: ignore[arg-type]
    )
    assert result == {"kind": "artifact"}


def _collect(prefix: str) -> dict[str, Callable[[ProbeEnv], None]]:
    pattern = re.compile(rf"{prefix}_ep_(\d{{3}})")
    return {
        f"EP-{match.group(1)}": probe
        for name, probe in sorted(globals().items())
        if (match := pattern.fullmatch(name)) is not None and callable(probe)
    }


#: Row id -> the probe that drives its real entry point under the reference profile and asserts the refusal.
PROBES: dict[str, Callable[[ProbeEnv], None]] = _collect("probe")
#: Row id -> the same entry point under the development profile, which must be unchanged.
DEVELOPMENT_PROBES: dict[str, Callable[[ProbeEnv], None]] = _collect("development")
