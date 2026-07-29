"""Registered tool modules are allowlistable, and the test-run path stops overclaiming.

Two C8 residuals are covered here. Both are about the *in-process* import of a
registered tool's ``module_path``:

    The workflow runtime resolves a registered tool by importing its Python module and
    returning the callable for direct execution. … an explicit allowlisted entrypoint
    registry is absent.

    The Tool test-run path describes itself as sandbox-isolated, but imports the module
    and invokes ``wrapped(**tool_input)`` in the web process.

So: (1) an operator can declare which modules are legitimate and everything else is
refused *before* the import — module-level code runs on import, so checking afterwards
would be too late; and (2) the endpoint no longer claims a boundary it does not have,
and reports ``isolation: "in_process"`` explicitly.

**What this does not claim.** Registered tool callables still execute in the
control-plane process. Process/container isolation for them remains open, and the
honest statement of that is exactly why the response field exists. An allowlist is
defence in depth on an admin-only registration path, not a sandbox.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

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


def test_test_run_reports_that_it_is_not_process_isolated(client: TestClient) -> None:
    """The endpoint used to call itself "sandbox-isolated" while importing and calling
    the module in the web process. A client rendering that claim would tell the
    operator something untrue, so the response now states the boundary."""
    tool_id = _register_tool(client)

    response = client.post(f"{PREFIX}/tools/{tool_id}/test-run", json={"input": {}})

    assert response.status_code == 200, response.text
    assert response.json()["data"]["isolation"] == "in_process"


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
