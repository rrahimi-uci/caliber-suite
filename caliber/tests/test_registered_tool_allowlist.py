"""Registered tool modules are allowlistable and execute out of process.

These tests cover the allowlist and the process boundary around a registered tool's
``module_path``. The route and workflow runtime used to import registered Python in the
control plane; both now bind through the local subprocess sandbox.

The module allowlist is enforced before child import, and the endpoint reports whether a
call was subprocess-executed, mocked without import, or not run. This remains a same-host
process/resource boundary, not container/seccomp isolation.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.workflows.runtime as runtime_module
from caliber.config import CaliberConfig
from caliber.db.models import CaliberToolRegistry
from caliber.workflows.tools import (
    ToolBindingError,
    ToolRegistryEntry,
    bind_module_allowlist,
    bind_registered_tool,
    registered_tool_module_allowed,
)
from tests.workflow_helpers import PREFIX


@pytest.fixture(autouse=True)
def _reset_allowlist():
    """The allowlist is process-wide, so every test restores it.

    Leaking a restrictive allowlist into another test would make unrelated suites fail
    in a way that is very hard to attribute.
    """
    yield
    bind_module_allowlist(None)


# ---------------------------------------------------------------------------
# Matching semantics
# ---------------------------------------------------------------------------


def test_an_unset_allowlist_permits_everything() -> None:
    """The shipped default, stated as a test so it cannot drift silently.

    Fail-closed here would break every existing install on upgrade; registration
    already requires the admin scope. The unset state is surfaced rather than hidden.
    """
    assert registered_tool_module_allowed("anything.at.all", "") is True
    assert registered_tool_module_allowed("anything.at.all", None) is True


def test_an_exact_entry_matches_only_itself() -> None:
    allowlist = "mycompany.tools.billing"
    assert registered_tool_module_allowed("mycompany.tools.billing", allowlist) is True
    assert registered_tool_module_allowed("mycompany.tools.billing.sub", allowlist) is False
    assert registered_tool_module_allowed("mycompany.tools", allowlist) is False


def test_a_star_suffix_allows_a_prefix() -> None:
    allowlist = "mycompany.tools.*"
    assert registered_tool_module_allowed("mycompany.tools.billing", allowlist) is True
    assert registered_tool_module_allowed("mycompany.tools.a.b.c", allowlist) is True
    assert registered_tool_module_allowed("othercompany.tools.billing", allowlist) is False


def test_a_bare_star_does_not_silently_allow_everything() -> None:
    """A single ``*`` would turn a configured allowlist into no allowlist at all — the
    most dangerous possible typo, because it looks deliberate."""
    assert registered_tool_module_allowed("anything", "*") is False


def test_an_empty_module_path_is_refused_when_an_allowlist_is_set() -> None:
    assert registered_tool_module_allowed("", "mycompany.*") is False


def test_multiple_entries_and_whitespace_are_handled() -> None:
    allowlist = " caliber.workflows.* , mycompany.tools.billing "
    assert registered_tool_module_allowed("caliber.workflows.demo_tools", allowlist) is True
    assert registered_tool_module_allowed("mycompany.tools.billing", allowlist) is True
    assert registered_tool_module_allowed("json", allowlist) is False


# ---------------------------------------------------------------------------
# Enforcement at bind time
# ---------------------------------------------------------------------------


def test_binding_a_disallowed_module_is_refused_before_import() -> None:
    """``json`` is importable, so a passing bind would prove the check is absent."""
    bind_module_allowlist("caliber.workflows.*")
    entry = ToolRegistryEntry(
        name="sneaky", version="1.0", module_path="json", callable_name="dumps"
    )

    with pytest.raises(ToolBindingError) as excinfo:
        bind_registered_tool(entry)
    message = str(excinfo.value)
    assert "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST" in message
    # The message says why this is guarded at all, so the operator can judge whether
    # to widen the allowlist or move the tool.
    assert "in the CALIBER process" in message


def test_binding_an_allowed_module_still_works() -> None:
    """The guard must stay narrow: a permitted module must bind exactly as before."""
    bind_module_allowlist("json")
    entry = ToolRegistryEntry(name="dump", version="1.0", module_path="json", callable_name="dumps")
    assert bind_registered_tool(entry)({"a": 1}) == '{"a": 1}'


def test_an_override_callable_bypasses_the_allowlist_by_design() -> None:
    """An override is an in-memory callable the caller already holds — there is no
    import to guard, and refusing it would break the in-memory resolver and previews
    for no security gain."""
    bind_module_allowlist("nothing.matches.*")
    entry = ToolRegistryEntry(name="t", version="1.0", module_path="json", callable_name="dumps")
    bound = bind_registered_tool(entry, callable_override=lambda **_kwargs: "ok")
    assert bound() == "ok"


def test_the_config_field_exists_and_defaults_to_unrestricted() -> None:
    """Guards against the field being renamed away from the env var that sets it."""
    assert CaliberConfig().registered_tool_module_allowlist == ""


# ---------------------------------------------------------------------------
# The test-run endpoint: honest about its boundary
# ---------------------------------------------------------------------------


def _register_tool(client: TestClient, module_path: str = "caliber.workflows.demo_tools") -> str:
    response = client.post(
        f"{PREFIX}/tools",
        json={
            "name": "allowlist_probe",
            "version": "1.0",
            "description": "probe",
            "module_path": module_path,
            "callable_name": "lookup_policy",
            "side_effect_level": "read",
            "allow_in_preview": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["tool_id"]


def test_test_run_reports_its_subprocess_boundary(client: TestClient) -> None:
    """The route uses the same child boundary as normal registered-tool execution."""
    tool_id = _register_tool(client)

    response = client.post(f"{PREFIX}/tools/{tool_id}/test-run", json={"input": {}})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["isolation"] == "subprocess"


def test_test_run_imports_and_executes_the_module_outside_the_api_process(
    client: TestClient,
) -> None:
    """Ask the route's tool for its PID; labels alone cannot prove isolation."""
    response = client.post(
        f"{PREFIX}/tools",
        json={
            "name": "route_pid_probe",
            "version": "1.0",
            "description": "prove the test route process boundary",
            "module_path": "os",
            "callable_name": "getpid",
            "side_effect_level": "read",
            "allow_in_preview": True,
        },
    )
    assert response.status_code == 201, response.text
    tool_id = response.json()["data"]["tool_id"]

    invoked = client.post(f"{PREFIX}/tools/{tool_id}/test-run", json={"input": {}})

    assert invoked.status_code == 200, invoked.text
    data = invoked.json()["data"]
    assert data["error"] is None
    assert data["isolation"] == "subprocess"
    assert data["output"] != os.getpid()


def test_test_run_refuses_a_module_outside_the_allowlist(
    client: TestClient, db_session: Session
) -> None:
    """This path imports operator-supplied module paths too, so the allowlist has to
    cover it — an allowlist honoured by only some import paths reads as enforced while
    leaving the others open."""
    tool_id = _register_tool(client)
    bind_module_allowlist("nothing.matches.*")

    response = client.post(f"{PREFIX}/tools/{tool_id}/test-run", json={"input": {}})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["error"] is not None
    assert "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST" in data["error"]
    assert data["output"] is None
    # And the row is untouched — a refused import must not look like a tool result.
    assert db_session.get(CaliberToolRegistry, tool_id) is not None


# ---------------------------------------------------------------------------
# C8 core: registered tools execute OUT of the control-plane process
# ---------------------------------------------------------------------------


def test_a_registered_tool_module_can_run_in_a_separate_process() -> None:
    """The C8 *mechanism*, proven the only way that is not self-referential: ask the
    tool what process it is in.

    ``LocalSubprocessToolSandbox`` gained a ``module_path`` mode, so an installed
    admin-registered module is imported **inside** the subprocess rather than in the
    API server. The import moves too, which matters as much as the call: module-level
    code used to execute in the control plane on first bind.

    This pins the *mechanism*. That the runtime actually routes through it — and does so
    by default rather than on request — is pinned separately by
    ``test_the_runtime_binds_registered_tools_out_of_process_by_default`` below, because
    a working mechanism nothing calls is the exact defect this repository has been
    audited for.
    """
    import os

    from caliber.tool_sandbox.models import ToolSandboxRunRequest
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    # Generous timeout for the same reason the demo-tools sandbox test uses one: the
    # subject here is *which process* the module runs in, not how fast a cold
    # `python -I` start completes. The 5s class default is decided by machine load once
    # the rest of the suite is running alongside it.
    result = LocalSubprocessToolSandbox(default_timeout_seconds=60.0).run_tool(
        ToolSandboxRunRequest(module_path="os", callable_name="getpid")
    )

    assert result.status == "completed", result.error
    assert result.output != os.getpid(), "the module must not be imported in this process"


def test_the_runtime_binds_registered_tools_out_of_process_by_default() -> None:
    """C8's closure: ``_bind`` must hand back a sandboxed callable, with nothing enabled.

    The mechanism test above proves the sandbox can run a module elsewhere. This proves
    the runtime *uses* it — the step that was missing for three editions of the review,
    during which the boundary existed, passed its own tests, and enforced nothing because
    no caller reached it.

    Asked the only non-self-referential way: bind ``os.getpid`` as a registered tool
    through the real binder and compare the answer with this process. Deliberately with a
    ``None`` sandbox config, because an unconfigured process must get containment by
    default; a boundary you have to switch on is not a boundary.
    """
    import os

    from caliber.workflows.ir import IRToolBinding
    from caliber.workflows.runtime import _bind, bind_sandbox_config
    from caliber.workflows.tools import InMemoryToolResolver

    binding = IRToolBinding(
        local_name="getpid",
        registry_ref="tool.getpid.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="os",
        callable_name="getpid",
    )
    previous = runtime_module._ACTIVE_SANDBOX_CONFIG
    bind_sandbox_config(None)
    try:
        fn = _bind(binding, InMemoryToolResolver([]))
        assert fn is not None, "a registered tool must bind"
        assert fn() != os.getpid(), "the runtime bound an in-process callable, so C8 is open"
    finally:
        bind_sandbox_config(previous)


def test_a_sandboxed_tool_actually_receives_its_arguments() -> None:
    """The end-to-end assertion whose absence let a silent-argument-loss bug ship.

    `_call_tool` handed candidate shapes to the sandbox and `service.run_tool` dropped
    `shapes` from the child payload, so the child fell back to `fn(*args, **input)` with
    both empty. `lookup_policy(query="refund policy")` executed as `lookup_policy()` and
    returned an answer computed from an empty query — a plausible-looking result, which is
    worse than an error because nothing surfaces.

    Every existing test missed it, and the combination is the lesson: the pid test calls a
    zero-argument function, so it passes either way, and the worker tests that *do* pass
    arguments run with the sandbox disabled by their own fixture. Coverage of the mechanism
    and coverage of the wiring are different things.
    """
    from caliber.workflows.ir import IRToolBinding
    from caliber.workflows.runtime import _bind, _call_tool, bind_sandbox_config
    from caliber.workflows.tools import InMemoryToolResolver

    binding = IRToolBinding(
        local_name="lookup_policy",
        registry_ref="tool.lookup_policy.v1",
        version_constraint=">=1",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="caliber.workflows.demo_tools",
        callable_name="lookup_policy",
    )
    previous = runtime_module._ACTIVE_SANDBOX_CONFIG
    bind_sandbox_config(None)
    try:
        fn = _bind(binding, InMemoryToolResolver([]))
        assert fn is not None
        result = _call_tool(fn, {"query": "refund policy"}, fallback_input="refund policy")
    finally:
        bind_sandbox_config(previous)

    assert isinstance(result, dict), result
    assert result.get("query") == "refund policy", (
        f"the tool ran but its arguments were dropped: {result}"
    )


def test_the_sandbox_path_still_enforces_the_module_allowlist() -> None:
    """Routing execution into a subprocess must not quietly widen *which* modules run.

    `bind_registered_tool` refuses a module outside
    `CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST`; the sandbox path had dropped that check, so
    with an allowlist of `caliber.workflows.*` an independent probe still executed
    `os.getcwd`. The subprocess narrows *where* code runs — it does not decide what an
    operator sanctioned, and containment should not silently cost authorization.
    """
    from caliber.workflows.ir import IRToolBinding
    from caliber.workflows.runtime import _bind, bind_sandbox_config
    from caliber.workflows.tools import InMemoryToolResolver, bind_module_allowlist

    def _binding(module_path: str, callable_name: str) -> IRToolBinding:
        return IRToolBinding(
            local_name=callable_name,
            registry_ref=f"tool.{callable_name}.v1",
            version_constraint=">=1",
            requires_approval=False,
            side_effect_level="read",
            allow_in_preview=True,
            module_path=module_path,
            callable_name=callable_name,
        )

    previous = runtime_module._ACTIVE_SANDBOX_CONFIG
    bind_sandbox_config(None)
    bind_module_allowlist("caliber.workflows.*")
    try:
        resolver = InMemoryToolResolver([])
        assert _bind(_binding("os", "getcwd"), resolver) is None, (
            "a module outside the allowlist must not bind, sandboxed or not"
        )
        # The guard has to stay narrow: an allowlisted module must still bind.
        assert (
            _bind(_binding("caliber.workflows.demo_tools", "lookup_policy"), resolver) is not None
        )
    finally:
        bind_module_allowlist("")
        bind_sandbox_config(previous)


def test_disabling_the_sandbox_is_possible_but_not_the_default() -> None:
    """The escape hatch exists for tests that monkeypatch a tool's module attribute, which
    cannot work across a process boundary. It must stay opt-*out*, never opt-in."""
    from caliber.config import CaliberConfig

    assert CaliberConfig().registered_tool_sandbox_enabled is True


def test_standalone_export_refuses_invalid_sandbox_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed setting must not discard the allowlist and continue unrestricted."""
    from caliber.workflows.runtime import bind_exported_tool, bind_sandbox_config

    entry = ToolRegistryEntry(
        name="getpid",
        version="1.0",
        module_path="os",
        callable_name="getpid",
    )
    previous = runtime_module._ACTIVE_SANDBOX_CONFIG
    bind_sandbox_config(None)
    monkeypatch.setenv("CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST", "nothing.matches.*")
    monkeypatch.setenv("CALIBER_TOOL_SANDBOX_TIMEOUT_SECONDS", "not-a-number")
    try:
        with pytest.raises(ToolBindingError, match="configuration is invalid"):
            bind_exported_tool(entry)
    finally:
        bind_sandbox_config(previous)


def test_disabled_standalone_export_still_enforces_its_explicit_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling containment cannot also disable module authorization."""
    from caliber.workflows.runtime import bind_exported_tool, bind_sandbox_config

    entry = ToolRegistryEntry(
        name="getpid",
        version="1.0",
        module_path="os",
        callable_name="getpid",
    )
    previous = runtime_module._ACTIVE_SANDBOX_CONFIG
    bind_sandbox_config(None)
    monkeypatch.setenv("CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED", "")
    monkeypatch.setenv("CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST", "nothing.matches.*")
    try:
        with pytest.raises(ToolBindingError, match="CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST"):
            bind_exported_tool(entry)
    finally:
        bind_sandbox_config(previous)


def test_disabled_export_explicit_allowlist_does_not_fall_back_to_global_policy() -> None:
    """Explicit ``run(config=...)`` policy must win before in-process binding too."""
    from types import SimpleNamespace

    from caliber.workflows.runtime import bind_exported_tool
    from caliber.workflows.tools import bind_module_allowlist

    entry = ToolRegistryEntry(
        name="lookup_policy",
        version="1.0",
        module_path="caliber.workflows.demo_tools",
        callable_name="lookup_policy",
    )
    bind_module_allowlist("forbidden.*")
    explicit = SimpleNamespace(
        registered_tool_module_allowlist="caliber.workflows.*",
        registered_tool_sandbox_enabled=False,
    )
    try:
        bound = bind_exported_tool(entry, config=explicit)
        assert bound("refund?")["policy"].startswith("Purchases within 30 days")
    finally:
        # Avoid leaking the deliberate global denial into later tests.
        bind_module_allowlist("")


def test_disabled_export_explicit_empty_allowlist_is_not_replaced_by_global_policy() -> None:
    """An empty policy in the supplied config means unrestricted, not 'inherit global'."""
    from types import SimpleNamespace

    from caliber.workflows.runtime import bind_exported_tool
    from caliber.workflows.tools import bind_module_allowlist

    entry = ToolRegistryEntry(
        name="lookup_policy",
        version="1.0",
        module_path="caliber.workflows.demo_tools",
        callable_name="lookup_policy",
    )
    bind_module_allowlist("forbidden.*")
    explicit = SimpleNamespace(
        registered_tool_module_allowlist="",
        registered_tool_sandbox_enabled=False,
    )
    try:
        bound = bind_exported_tool(entry, config=explicit)
        assert bound("refund?")["policy"].startswith("Purchases within 30 days")
    finally:
        bind_module_allowlist("")


def test_the_sandbox_refuses_an_ambiguous_request() -> None:
    """Exactly one of ``source_code``/``module_path``. A request carrying both would
    run *something* and which one is not obvious from the call site."""
    import pytest as _pytest

    from caliber.tool_sandbox.models import ToolSandboxRunRequest

    with _pytest.raises(ValueError, match="exactly one of"):
        ToolSandboxRunRequest(source_code="def f(): pass", module_path="os", callable_name="f")
    with _pytest.raises(ValueError, match="exactly one of"):
        ToolSandboxRunRequest(callable_name="f")
