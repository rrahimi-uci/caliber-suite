"""B12.4a (#754): a reference-profile deployment refuses executable code at every public boundary, with one code.

The reference profile refuses PostgreSQL-less starts, so this runs on the PostgreSQL instance of ``reference_app`` (the
"EGI scenarios (PostgreSQL)" job; the SQLite instance runs the development profile, which keeps the code nodes). A project
editor authors a workflow with a ``python_code`` or an ``external_app`` node through the public API:

* validate reports ``unsupported_in_reference_profile`` at the node, compile and publish refuse, and the version stays a draft;
* a version published before the profile was switched on cannot be promoted onto an alias, and a run of it fails closed by
  that code with the node never executed;
* a workflow whose agent uses an administrator-registered Python tool is refused the same way at validation and publish
  (B04.7, #774); with the registered-tool sandbox switched off even CALIBER's own shipped tool is refused, and with it on
  (the default) the shipped tool is admitted.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

import caliber.workflows.runtime as runtime_module
from caliber.config import CaliberConfig
from caliber.db.models import CaliberToolRegistry, CaliberWorkflowDeployment, CaliberWorkflowVersion
from caliber.ids import new_tool_id
from caliber.server import create_app
from caliber.workflows.builtin_tools import register_builtin_tools
from caliber.workflows.code_policy import REFUSAL_CODE, CodePolicy, bind_code_policy
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.egi_scenarios.test_b03_run_identity import _run, _start, _work
from tests.workflow_helpers import make_manifest

pytestmark = pytest.mark.xdist_group("egi-scenario-processes")


def _python_code_manifest(workflow_id: str) -> dict[str, Any]:
    data = make_manifest(workflow_id)
    data["nodes"]["code"] = {
        "id": "code",
        "type": "python_code",
        "code": 'return {"text": (input or "").upper(), "result": {}}',
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


def _external_app_manifest(workflow_id: str) -> dict[str, Any]:
    data = make_manifest(workflow_id)
    data["nodes"]["bridge"] = {
        "id": "bridge",
        "type": "external_app",
        "entrypoint": "egi_external_app:run",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "bridge", "map": {"msg": "input"}},
        {"id": "e2", "from": "bridge", "to": "final", "map": {"text": "response"}},
    ]
    return data


KINDS = [
    pytest.param(_python_code_manifest, "code", "python_code", id="python_code"),
    pytest.param(_external_app_manifest, "bridge", "external_app", id="external_app"),
]


def _need_reference(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip("the reference profile, and so its refusal of code, needs PostgreSQL")


def _draft(app: ReferenceApp, manifest_for: Any) -> tuple[str, str]:
    created = _call(app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": "EGI code refusal"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": manifest_for(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    return workflow_id, str(draft.json()["data"]["version_id"])


def _status(app: ReferenceApp, version_id: str) -> str:
    with app.session_factory() as session:
        row = session.get(CaliberWorkflowVersion, version_id)
        assert row is not None
        return str(row.status)


@pytest.mark.parametrize(("manifest_for", "node_id", "kind"), KINDS)
def test_authoring_a_code_node_is_refused_by_validate_compile_and_publish(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    manifest_for: Any,
    node_id: str,
    kind: str,
) -> None:
    _need_reference(reference_app)
    _, version_id = _draft(reference_app, manifest_for)

    validated = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/validate"
    )
    assert validated.status_code == 200, validated.text
    errors = validated.json()["data"]["errors"]
    assert [(e["code"], e["path"]) for e in errors] == [(REFUSAL_CODE, f"nodes.{node_id}")]
    assert kind in errors[0]["message"] and "reference profile" in errors[0]["message"]

    compiled = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/compile"
    )
    assert compiled.status_code == 400, compiled.text
    assert compiled.json()["report"]["errors"][0]["code"] == REFUSAL_CODE

    published = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish"
    )
    assert published.status_code == 400, published.text
    assert REFUSAL_CODE in published.json()["detail"]
    assert _status(reference_app, version_id) == "draft"


@pytest.mark.parametrize(("manifest_for", "node_id", "kind"), KINDS)
def test_a_version_published_before_the_profile_cannot_be_promoted_or_run(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
    manifest_for: Any,
    node_id: str,
    kind: str,
) -> None:
    _need_reference(reference_app)
    workflow_id, version_id = _draft(reference_app, manifest_for)

    # The version was published while the process did not refuse code: switch the policy off for that one call.
    bind_code_policy(None)
    published = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish"
    )
    bind_code_policy(CodePolicy.from_config(reference_app.config))
    assert published.status_code == 200, published.text

    # Promotion onto an alias is refused with the one code, and no alias moved.
    promoted = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/deployments/staging/promote",
        {"version_id": version_id},
    )
    assert promoted.status_code == 400, promoted.text
    assert REFUSAL_CODE in promoted.json()["detail"] and kind in promoted.json()["detail"]
    with reference_app.session_factory() as session:
        assert (
            session.execute(
                select(func.count())
                .select_from(CaliberWorkflowDeployment)
                .where(CaliberWorkflowDeployment.workflow_id == workflow_id)
            ).scalar_one()
            == 0
        )

    # A run of it fails closed by that code and the node never executes.
    executed: list[str] = []
    monkeypatch.setattr(
        runtime_module,
        "sandbox_from_optional_config",
        lambda *a, **k: executed.append("sandbox") or (_ for _ in ()).throw(AssertionError("ran")),
    )
    monkeypatch.setattr(
        runtime_module,
        "_run_external_app_entrypoint",
        lambda *a, **k: (
            executed.append("external_app") or (_ for _ in ()).throw(AssertionError("ran"))
        ),
    )
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"workflow_run_queue_enabled": True, "admission_enabled": True}
    )
    run_id = _start(reference_app, workflow_id, f"b12-4a-{kind}")
    _work(reference_app)
    status, code, summary = _run(reference_app, run_id)
    assert (status, code) == ("failed", REFUSAL_CODE), summary
    assert kind in str(summary)
    assert executed == []


def _registered_tool_workflow(app: ReferenceApp, *, shipped: bool = False) -> tuple[str, str]:
    """A workflow whose agent uses a registered Python tool: an administrator's (the row is seeded: registering needs admin),
    or, with ``shipped``, CALIBER's own seeded ``grok_parse`` built-in."""
    with app.session_factory() as session:
        if shipped:
            register_builtin_tools(session)
        else:
            session.add(
                CaliberToolRegistry(
                    tool_id=new_tool_id(),
                    name="lookup_policy",
                    version="1.0",
                    module_path="mycompany.tools",
                    callable_name="lookup_policy",
                    side_effect_level="read",
                    owner="@ops",
                    visibility="public",
                )
            )
        session.commit()
    data = make_manifest("placeholder")
    data["tools"] = {
        "lookup": {
            "type": "registered_function",
            "registry_ref": "tool.grok_parse.v1" if shipped else "tool.lookup_policy.v1",
        }
    }
    data["nodes"]["agent"]["tools"] = ["lookup"]
    return _draft(app, lambda workflow_id: {**data, "workflow_id": workflow_id})


def test_an_administrators_python_tool_is_refused_at_author_time_with_the_sandbox_on(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """B04.7 (#774): administrator Python is a refused path in the reference profile, so an author hears it at validation
    and publish, not as a refused run after a clean publish."""
    _need_reference(reference_app)
    _, version_id = _registered_tool_workflow(reference_app)

    validated = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/validate"
    )
    assert validated.status_code == 200, validated.text
    errors = validated.json()["data"]["errors"]
    assert [(e["code"], e["path"]) for e in errors] == [(REFUSAL_CODE, "nodes.agent.tools.lookup")]
    assert "not one of CALIBER's own shipped tools" in errors[0]["message"]
    published = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish"
    )
    assert published.status_code == 400, published.text
    assert REFUSAL_CODE in published.json()["detail"]


def test_a_shipped_python_tool_is_refused_only_while_the_sandbox_is_off(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _need_reference(reference_app)
    _, version_id = _registered_tool_workflow(reference_app, shipped=True)
    path = f"/workflow-versions/{version_id}/validate"

    # The shipped default: CALIBER's own tool runs in the sandbox child, so it is admitted.
    admitted = _call(reference_app, "POST", EDITOR_A, PROJECT_A, path)
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["data"]["errors"] == []

    # An operator switches the sandbox off: the tool would be imported into the control plane. Same database, new process
    # configuration; a sandbox label on the tool changes nothing.
    unsandboxed = create_app(
        CaliberConfig.load(
            environ={**reference_app.env, "CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED": "false"}
        )
    )
    try:
        from starlette.testclient import TestClient

        with TestClient(unsandboxed) as client:
            headers = reference_app.headers(EDITOR_A, PROJECT_A)
            token = client.get("/ajax-api/2.0/mlflow/caliber/csrf", headers=headers).json()["data"][
                "token"
            ]
            refused = client.post(
                f"/ajax-api/2.0/mlflow/caliber{path}",
                headers={**headers, "X-CALIBER-CSRF": token},
            )
        assert refused.status_code == 200, refused.text
        errors = refused.json()["data"]["errors"]
        assert [(e["code"], e["path"]) for e in errors] == [
            (REFUSAL_CODE, "nodes.agent.tools.lookup")
        ]
        assert "CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED" in errors[0]["message"]
    finally:
        unsandboxed.state.session_factory.kw["bind"].dispose()
        bind_code_policy(CodePolicy.from_config(reference_app.config))


def test_the_reference_app_binds_the_policy_the_refusals_use(reference_app: ReferenceApp) -> None:
    _need_reference(reference_app)
    from caliber.workflows.code_policy import bound_code_policy

    assert bound_code_policy() == CodePolicy(reference=True, sandbox_enabled=True)


def test_a_run_the_runtime_entry_refuses_fails_by_the_same_code(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last boundary: the plan was built, then the runtime entry refused, and the run keeps the code it was refused by."""
    from caliber.orchestrator import workflow_run_worker as worker_module
    from caliber.workflows.runtime import WorkflowRunResult
    from tests.egi_scenarios.test_b02_admission import _published_workflow

    _need_reference(reference_app)
    workflow_id = _published_workflow(reference_app)
    refused = WorkflowRunResult(
        status="error",
        output="",
        error=f"{REFUSAL_CODE}: python_code node 'code' ...",
        error_code=REFUSAL_CODE,
    )
    monkeypatch.setattr(worker_module, "execute", lambda *a, **k: refused)
    run_id = _start(reference_app, workflow_id, "b12-4a-entry")
    _work(reference_app)
    status, code, summary = _run(reference_app, run_id)
    assert (status, code) == ("failed", REFUSAL_CODE), summary
