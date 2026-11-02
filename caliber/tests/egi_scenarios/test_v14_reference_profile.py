"""V14: the reference profile's boundary holds against an attacker (B13.3, #673; the attack pack, B12.4c, #770).

Two things are proved here, both on PostgreSQL only (the reference profile refuses SQLite; the "EGI scenarios (PostgreSQL)"
job, locally ``scripts/ci-local.sh egi-scenarios-postgres``).

**One execution owner (B13.3, #673).** Two real CALIBER processes share one PostgreSQL database in the reference profile. The
first to start holds the execution-owner lease and runs the owner-only loops; the second reports ``secondary``, starts no loop
and never heartbeats. Killing the owner (SIGKILL, no release) lets the secondary take the lease over when it expires.

**The attack pack (B12.4c, #770).** An attacker tries to run code, install code, escape the file root and walk around the
refusals through every public route, and each attempt is denied with the named status and reason while the effect is measured
where it would have happened: ``downstream.mutations()``, a marker file the attacker's module writes when imported, and the
sandbox spawn counter (``harness/attacks.py``). Every refusal carries the one code ``unsupported_in_reference_profile``
(``caliber.workflows.code_policy.REFUSAL_CODE``, ADR 0001). On the development profile (the SQLite instance of ``reference_app``)
the same hostile input runs, which is the control that proves the payload is live. ``scripts/attack_pack_mutants.py`` mutates the
refusal code and shows this file fails for every mutant.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber import egress
from caliber import secrets as secrets_module
from caliber.csrf import CSRFTokenManager
from caliber.db.models import (
    CaliberAuditLog,
    CaliberPolicyDecision,
    CaliberRefinementJob,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberToolRegistry,
    CaliberWorkflowDeployment,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.profile import REFERENCE_REQUIREMENTS
from caliber.secret_store import SecretStore, generate_key
from caliber.extensibility import PluginRefused, entrypoints, optimizer_registry
from caliber.extensibility.registry import OptimizerRegistry, OptimizerSpec
from caliber.orchestrator.scheduler import WorkflowSchedulerTask
from caliber.server import create_app
from caliber.workflows.admission import AdmissionSettings
from caliber.workflows.code_policy import REFUSAL_CODE, CodePolicy, bind_code_policy
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    CSRF_SECRET,
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    Downstream,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.harness.attacks import (
    ATTACK_ENTRYPOINT,
    ATTACK_TOOL_NAME,
    CODE_KINDS,
    CODE_RAN_TEXT,
    World,
    code_manifest,
    tool_manifest,
)
from tests.egi_scenarios.harness.process import HEALTH_PATH, ServerProcess, ServerStartError
from tests.egi_scenarios.test_b02_admission import _published_workflow
from tests.test_secret_canary import KEY_ENV as CANARY_KEY_ENV
from tests.test_secret_canary import SECRET_NAME, SECRET_REFERENCE, Scan
from tests.test_egress_rebinding import METADATA, PUBLIC, RecordingTransport
from tests.workflow_helpers import make_manifest, seed_eval_dataset

pytestmark = pytest.mark.xdist_group("egi-scenario-processes")

CALIBER_DIR = Path(__file__).resolve().parents[2]
PREFIX = "/ajax-api/2.0/mlflow/caliber"
READINESS = "/ajax-api/2.0/mlflow/caliber/readiness"
LEASE_SECONDS = 3.0
#: The issue's bound, exactly: a SIGKILLed owner's lease lapses within one lease and the secondary's next tick
#: (a third of a lease) acquires and starts the loops, so 2 x the lease leaves slack for the HTTP poll.
TAKEOVER_BOUND_SECONDS = 2 * LEASE_SECONDS


def _role(server: ServerProcess) -> str:
    return str(httpx.get(server.base_url + READINESS, timeout=5.0).json()["data"]["execution_role"])


def _wait_for_role(server: ServerProcess, role: str, timeout: float) -> float:
    """Seconds until ``server`` reports ``role``; fails the test if it never does."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        try:
            if _role(server) == role:
                return time.monotonic() - started
        except (httpx.TransportError, KeyError, ValueError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"the server never reported {role!r} in {timeout}s\n{server.log_tail()}")


def _process_server(reference_app: ReferenceApp, tmp_path: Path, name: str) -> ServerProcess:
    env: dict[str, Any] = {
        **reference_app.env,
        "CALIBER_BACKGROUND_TASKS_ENABLED": "true",
        "CALIBER_EXECUTION_LEASE_SECONDS": str(LEASE_SECONDS),
        "CALIBER_WORKFLOW_RUN_WORKER_ENABLED": "true",
        "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED": "true",
    }
    return ServerProcess(env, app_dir=CALIBER_DIR, log_path=tmp_path / f"{name}.log")


def _lease(reference_app: ReferenceApp) -> tuple[str, int]:
    with reference_app.session_factory() as session:
        row = session.execute(
            text(
                "SELECT owner_id, fencing_token FROM caliber_execution_owner WHERE lease_name = 'execution-owner'"
            )
        ).one()
    return str(row[0]), int(row[1])


def _heartbeat_processes(reference_app: ReferenceApp) -> set[str]:
    """The process ids embedded in the worker ids of every owner-only worker that heartbeats."""
    with reference_app.session_factory() as session:
        ids = [
            str(r[0])
            for r in session.execute(text("SELECT worker_id FROM caliber_worker_heartbeats"))
        ]
    found = {match.group(1) for worker_id in ids if (match := re.search(r"-(\d+)-", worker_id))}
    return found


def test_second_owner_is_api_only(reference_app: ReferenceApp, tmp_path: Path) -> None:
    if reference_app.dialect != "postgresql":
        pytest.skip("the reference profile, and so a lease between processes, needs PostgreSQL")
    first = _process_server(reference_app, tmp_path, "first")
    second = _process_server(reference_app, tmp_path, "second")
    try:
        first.start()
        _wait_for_role(first, "owner", timeout=20)
        second.start()
        # The first owner holds the lease: the second serves requests (its readiness answers) and runs no loop.
        assert _role(second) == "secondary"
        time.sleep(LEASE_SECONDS)  # across a renewal: nothing changes hands while the owner lives
        assert (_role(first), _role(second)) == ("owner", "secondary")

        owner_id, token = _lease(reference_app)
        owner_pid = owner_id.split(":")[1]
        assert token == 1
        # The owner's workers heartbeat, and nobody else's do: exactly the lease holder's process, not merely
        # "at most one" (which an owner that never started its loops would also satisfy).
        deadline = time.monotonic() + 20
        processes = _heartbeat_processes(reference_app)
        while processes != {owner_pid} and time.monotonic() < deadline:
            time.sleep(0.5)
            processes = _heartbeat_processes(reference_app)
        assert processes == {owner_pid}, (
            f"owner-only workers heartbeat from the lease holder's process {owner_pid} only, saw {processes}"
        )

        # Kill the owner without a release: the lease lapses and the secondary takes it over.
        first.kill()
        took = _wait_for_role(second, "owner", timeout=TAKEOVER_BOUND_SECONDS + 5)
        assert took <= TAKEOVER_BOUND_SECONDS, (
            f"takeover took {took:.1f}s (bound {TAKEOVER_BOUND_SECONDS:.0f}s)"
        )
        new_owner, new_token = _lease(reference_app)
        assert new_owner != owner_id and new_token == 2, (
            "a new acquisition raises the fencing token"
        )
    finally:
        second.kill()
        first.kill()


# ======================================================================================================================
# The attack pack (B12.4c, #770)
# ======================================================================================================================

# -- hostile code through every public route -------------------------------------------------------------------------------


@pytest.fixture
def world(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    downstream: Downstream,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> World:
    return World(reference_app, downstream, tmp_path, monkeypatch)


def _refusal_in(response: Any) -> str:
    """The body text of a refused answer, which must carry the one code."""
    assert REFUSAL_CODE in response.text, response.text
    return str(response.text)


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_authoring_hostile_code_is_refused_by_validate_compile_and_publish(
    world: World, kind: str
) -> None:
    before = world.effects()
    workflow_id = world.new_workflow()
    version_id = world.draft(workflow_id, code_manifest(kind, workflow_id))

    validated = world.call("POST", f"/workflow-versions/{version_id}/validate")
    compiled = world.call("POST", f"/workflow-versions/{version_id}/compile")
    published = world.call("POST", f"/workflow-versions/{version_id}/publish")

    assert validated.status_code == 200, validated.text
    if world.reference:
        assert [(e["code"], e["path"]) for e in validated.json()["data"]["errors"]] == [
            (REFUSAL_CODE, "nodes.code")
        ]
        assert compiled.status_code == 400, compiled.text
        assert compiled.json()["report"]["errors"][0]["code"] == REFUSAL_CODE
        assert published.status_code == 400, published.text
        _refusal_in(published)
        assert world.version_status(version_id) == "draft"
    else:  # the control: the development profile authors, compiles and publishes the same node
        assert validated.json()["data"]["errors"] == []
        assert compiled.status_code == 200, compiled.text
        assert published.status_code == 200, published.text
        assert world.version_status(version_id) == "published"
    assert world.effects() == before, "authoring executes nothing, in either profile"


@pytest.fixture
def eval_dataset(reference_app: ReferenceApp) -> str:
    with reference_app.session_factory() as session:
        return seed_eval_dataset(session)


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_a_hostile_version_that_predates_the_profile_cannot_be_previewed_promoted_evaluated_imported_or_exported(
    world: World, kind: str, eval_dataset: str
) -> None:
    hostile = world.seeded_hostile(kind)
    manifest = code_manifest(kind, hostile.workflow_id)
    before = world.effects()

    preview = world.call(
        "POST", f"/workflow-versions/{hostile.version_id}/preview-run", {"input": "go"}
    )
    preview_override = world.call(
        "POST",
        f"/workflow-versions/{hostile.version_id}/preview-run",
        {"input": "go", "manifest": manifest},
    )
    sync_run = world.call(
        "POST", f"/workflow-versions/{hostile.version_id}/run", {"input": "go", "manifest": manifest}
    )
    promoted = world.call(
        "POST",
        f"/workflows/{hostile.workflow_id}/deployments/staging/promote",
        {"version_id": hostile.version_id},
    )
    evaluated = world.call(
        "POST",
        "/evaluations",
        {
            "dataset_id": eval_dataset,
            "predict_target": "workflow",
            "subject_ref": hostile.version_id,
            "scorers": [],
        },
    )
    import_preview = world.call("POST", "/workflows/import/preview", {"manifest": manifest})
    imported = world.call("POST", "/workflows/import", {"manifest": manifest})
    export_python = world.call("GET", f"/workflow-versions/{hostile.version_id}/export/python")
    export_bundle = world.call(
        "GET", f"/workflow-versions/{hostile.version_id}/export/deployment-bundle"
    )

    if world.reference:
        for refused in (preview, preview_override, promoted, evaluated, imported):
            assert refused.status_code == 400, refused.text
            _refusal_in(refused)
        assert preview_override.json()["detail"].startswith(f"cannot preview: {REFUSAL_CODE}: ")
        # A synchronous real run is not admitted at all in the reference profile (it would bypass admission).
        assert sync_run.status_code == 409, sync_run.text
        assert import_preview.status_code == 200, import_preview.text
        assert [e["code"] for e in import_preview.json()["data"]["validation"]["errors"]] == [
            REFUSAL_CODE
        ]
        assert import_preview.json()["data"]["ready_to_import"] is False
        for export in (export_python, export_bundle):
            assert export.status_code == 409, export.text
            assert export.json()["reason_code"] == REFUSAL_CODE
        with world.app.session_factory() as session:
            aliases = {d.alias for d in session.query(CaliberWorkflowDeployment).all()}
        assert aliases == {hostile.alias}, "promotion moved no alias"
        assert world.effects() == before
    else:  # the control: the development profile admits what the reference profile refused
        assert promoted.status_code == 200, promoted.text
        assert imported.status_code == 201, imported.text
        assert export_python.status_code == 200, export_python.text
        assert import_preview.json()["data"]["ready_to_import"] is True


@pytest.mark.parametrize("kind", CODE_KINDS)
@pytest.mark.parametrize("target", ["alias", "version"])
def test_a_queued_run_of_a_hostile_version_fails_by_the_code_and_the_node_never_runs(
    world: World, kind: str, target: str
) -> None:
    hostile = world.seeded_hostile(kind)
    before = world.effects()
    body = (
        {"workflow_id": hostile.workflow_id, "alias": hostile.alias}
        if target == "alias"
        else {"workflow_version_id": hostile.version_id}
    )

    accepted = world.queue_run(body)
    assert accepted.status_code == 202, accepted.text
    run_id = str(accepted.json()["data"]["workflow_run_id"])
    world.work()

    run = world.run_row(run_id)
    after = world.effects()
    if world.reference:
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), run.error_summary
        assert kind in str(run.error_summary)
        assert after == before, "the node never ran: no mutation, no import, no sandbox spawn"
    else:  # the control: the same hostile run executes in the development profile
        assert run.status == "completed", run.error_summary
        if kind == "external_app":
            assert (after.mutations, after.imported) == (before.mutations + 1, True)
        else:
            assert after.spawns > before.spawns, "the sandbox child ran the author's source"
            assert CODE_RAN_TEXT in str(world.call("GET", f"/workflow-runs/{run_id}").text)


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_the_refusal_survives_a_restart_and_a_second_process_over_the_same_database(
    world: World, kind: str
) -> None:
    """A fresh app over the same database (a restart, or a second API process) refuses the same hostile version."""
    hostile = world.seeded_hostile(kind)
    before = world.effects()
    restarted = create_app(CaliberConfig.load(environ=world.app.env))
    try:
        with TestClient(restarted) as client:
            headers = world.app.headers(EDITOR_A, PROJECT_A)
            token = client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
            promoted = client.post(
                f"{PREFIX}/workflows/{hostile.workflow_id}/deployments/staging/promote",
                json={"version_id": hostile.version_id},
                headers={**headers, "X-CALIBER-CSRF": token},
            )
    finally:
        restarted.state.session_factory.kw["bind"].dispose()
        bind_code_policy(CodePolicy.from_config(world.app.config))
    if world.reference:
        assert promoted.status_code == 400, promoted.text
        _refusal_in(promoted)
    else:
        assert promoted.status_code == 200, promoted.text
    assert world.effects() == before


# -- hostile tool bindings -------------------------------------------------------------------------------------------------

TOOL_FORMS = ("agent_tool", "tool_node")


@pytest.mark.parametrize("sandbox", ["sandbox_on", "sandbox_off"])
@pytest.mark.parametrize("form", TOOL_FORMS)
def test_an_administrators_python_tool_is_refused_in_every_binding_form_and_never_imported(
    world: World, form: str, sandbox: str
) -> None:
    """A registered Python tool is refused at author time, at publish and at run, whether it is an agent's tool or a tool
    node, and whether or not the sandbox is switched on: the platform's own configuration decides, not a label the tool carries."""
    world.config_update(registered_tool_sandbox_enabled=sandbox == "sandbox_on")
    ref = world.seed_tool(backend_config={"sandbox": True, "isolation": "container"}, owner="caliber")
    before = world.effects()

    def manifest_for(workflow_id: str) -> dict[str, Any]:
        return tool_manifest(form, workflow_id, ref)

    workflow_id = world.new_workflow()
    version_id = world.draft(workflow_id, manifest_for(workflow_id))
    validated = world.call("POST", f"/workflow-versions/{version_id}/validate")
    published = world.call("POST", f"/workflow-versions/{version_id}/publish")
    hostile = world.seeded_hostile("tool", manifest_for=manifest_for)
    accepted = world.queue_run({"workflow_id": hostile.workflow_id, "alias": hostile.alias})
    assert accepted.status_code == 202, accepted.text
    run_id = str(accepted.json()["data"]["workflow_run_id"])
    world.work()
    run = world.run_row(run_id)
    after = world.effects()

    if world.reference:
        errors = validated.json()["data"]["errors"]
        assert [e["code"] for e in errors] == [REFUSAL_CODE], errors
        assert errors[0]["path"].endswith(".tools.lookup") or errors[0]["path"].startswith("nodes.")
        assert published.status_code == 400, published.text
        _refusal_in(published)
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), run.error_summary
        assert after == before, "the tool's module was never imported and never called"
    elif sandbox == "sandbox_off":  # the control: in the development profile the unsandboxed tool is imported and runs
        assert validated.json()["data"]["errors"] == []
        assert published.status_code == 200, published.text
        assert run.status == "completed", run.error_summary
        assert (after.mutations, after.imported) == (before.mutations + 1, True)


@pytest.mark.parametrize(
    "spoof",
    [
        pytest.param({"name": "grok_parse", "owner": "caliber"}, id="a-shipped-name-and-owner"),
        pytest.param({"backend_config": {"sandbox": True}}, id="a-sandbox-label"),
        pytest.param({"execution_backend": "shell_exec"}, id="an-unknown-execution-backend"),
    ],
)
def test_a_tool_row_that_claims_to_be_shipped_or_sandboxed_or_something_new_is_refused(
    world: World, spoof: dict[str, Any]
) -> None:
    ref = world.seed_tool(**{"name": ATTACK_TOOL_NAME, **spoof})
    workflow_id = world.new_workflow()
    version_id = world.draft(workflow_id, tool_manifest("agent_tool", workflow_id, ref))
    before = world.effects()

    validated = world.call("POST", f"/workflow-versions/{version_id}/validate")

    if world.reference:
        assert [e["code"] for e in validated.json()["data"]["errors"]] == [REFUSAL_CODE]
    else:
        assert validated.json()["data"]["errors"] == []
    assert world.effects() == before


def test_an_administrators_tool_cannot_be_test_run_through_the_tools_routes(
    world: World,
) -> None:
    """``POST /tools/{id}/test-run`` runs a registered module in the sandbox child: refused in the reference profile."""
    world.seed_tool()
    with world.app.session_factory() as session:
        tool_id = session.query(CaliberToolRegistry).filter_by(name=ATTACK_TOOL_NAME).one().tool_id
    before = world.effects()

    response = world.call("POST", f"/tools/{tool_id}/test-run", {"input": {}})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    if world.reference:
        assert data["isolation"] == "refused"
        assert str(data["error"]).startswith(f"{REFUSAL_CODE}: "), data
        assert data["output"] is None
        assert world.effects() == before, "no sandbox child was started"
    else:  # the control: the development profile starts the sandbox child for the administrator's module
        assert world.effects().spawns > before.spawns


# -- the file root ---------------------------------------------------------------------------------------------------------

OUTSIDE_SECRET = "OUTSIDE-SECRET-4f1c9a"
INSIDE_TEXT = "INSIDE-OK-7be2"


@dataclass
class FileTree:
    """``root/`` is the configured workflow file root; ``outside/`` holds a secret the attacker wants."""

    root: Path
    outside: Path

    @property
    def secret_file(self) -> Path:
        return self.outside / "outside.txt"


@pytest.fixture
def file_tree(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FileTree:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.txt").write_text(INSIDE_TEXT, encoding="utf-8")
    (outside / "outside.txt").write_text(OUTSIDE_SECRET, encoding="utf-8")
    (root / "link.txt").symlink_to(outside / "outside.txt")
    (root / "dirlink").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("CALIBER_WORKFLOW_FILE_ROOT", str(root))
    return FileTree(root, outside)


def _host_node_manifest(workflow_id: str, node: dict[str, Any]) -> dict[str, Any]:
    data = make_manifest(workflow_id)
    data["nodes"]["host"] = {"id": "host", **node}
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "host", "map": {"msg": "path"}},
        {"id": "e2", "from": "host", "to": "agent", "map": {"text": "input"}},
        {"id": "e3", "from": "agent", "to": "final", "map": {"final_output": "response"}},
    ]
    return data


FILE_INPUT = {
    "type": "file_input",
    "inputs": {"path": {"type": "string"}},
    "outputs": {
        "text": {"type": "string"},
        "path": {"type": "string"},
        "metadata": {"type": "structured"},
    },
}


def _folder_input(pattern: str) -> dict[str, Any]:
    return {
        "type": "folder_input",
        "pattern": pattern,
        "recursive": False,
        "max_files": 10,
        "inputs": {"path": {"type": "string"}},
        "outputs": {
            "text": {"type": "string"},
            "files": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }


def _output_folder_manifest(workflow_id: str, path: str) -> dict[str, Any]:
    data = make_manifest(workflow_id)
    data["nodes"]["of"] = {
        "id": "of",
        "type": "output_folder",
        "path": path,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"files": {"type": "structured"}, "metadata": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e1", "from": "start", "to": "agent", "map": {"msg": "input"}},
        {"id": "e2", "from": "agent", "to": "final", "map": {"final_output": "response"}},
        {"id": "e3", "from": "agent", "to": "of", "map": {"final_output": "input"}},
    ]
    return data


def _run_host_node(world: World, node: dict[str, Any], run_input: str) -> tuple[Any, str]:
    """Publish a workflow with one host-path node (an inert kind: published through the public API as any author would), run
    it by ``run_input`` and return the run row and everything the API says about it."""
    workflow_id = world.new_workflow()
    manifest = (
        _output_folder_manifest(workflow_id, node["path"])
        if node["type"] == "output_folder"
        else _host_node_manifest(workflow_id, node)
    )
    version_id = world.draft(workflow_id, manifest)
    published = world.call("POST", f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    accepted = world.queue_run({"workflow_version_id": version_id, "input": run_input})
    assert accepted.status_code == 202, accepted.text
    run_id = str(accepted.json()["data"]["workflow_run_id"])
    world.work()
    said = "".join(
        world.call("GET", path).text for path in (f"/workflow-runs/{run_id}", f"/workflow-runs/{run_id}/events")
    )
    return world.run_row(run_id), said


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("../outside/outside.txt", id="dotdot"),
        pytest.param("{root}/../outside/outside.txt", id="dotdot-after-the-root"),
        pytest.param("{outside}/outside.txt", id="an-absolute-path-outside"),
        pytest.param("link.txt", id="a-symlink-inside-that-points-outside"),
        pytest.param("dirlink/outside.txt", id="a-symlinked-folder-that-points-outside"),
    ],
)
def test_a_file_input_node_cannot_read_outside_the_file_root_in_either_profile(
    world: World, file_tree: FileTree, path: str
) -> None:
    """``../../outside.txt`` and its cousins are refused with ``PathOutsideRoot`` (a refused run, nothing read): in the
    reference profile because the root is configured, and in the development profile too, because a configured root is a boundary
    in every profile."""
    target = path.format(root=file_tree.root, outside=file_tree.outside)
    if not target.startswith("/"):
        target = str(file_tree.root / target)

    run, said = _run_host_node(world, FILE_INPUT, target)

    assert run.status == "failed", run.error_summary
    assert "outside the configured workflow file root" in str(run.error_summary)
    assert OUTSIDE_SECRET not in str(run.error_summary) + said


def test_a_file_input_node_still_reads_inside_the_file_root(world: World, file_tree: FileTree) -> None:
    """The control: the root is a boundary, not a ban."""
    run, said = _run_host_node(world, FILE_INPUT, str(file_tree.root / "inside.txt"))
    assert run.status == "completed", run.error_summary
    assert INSIDE_TEXT in said


@pytest.mark.parametrize("pattern", ["../outside/*", "dirlink/*", "*"])
def test_a_folder_input_node_cannot_walk_or_link_out_of_the_file_root(
    world: World, file_tree: FileTree, pattern: str
) -> None:
    run, said = _run_host_node(world, _folder_input(pattern), str(file_tree.root))

    assert run.status == "failed", run.error_summary
    assert "outside" in str(run.error_summary)
    assert OUTSIDE_SECRET not in str(run.error_summary) + said


def test_with_no_file_root_every_host_path_node_is_refused(
    world: World, file_tree: FileTree, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CALIBER_WORKFLOW_FILE_ROOT")
    run, said = _run_host_node(world, FILE_INPUT, str(file_tree.root / "inside.txt"))
    assert run.status == "failed"
    assert "unmanaged host-filesystem nodes are disabled" in str(run.error_summary)
    assert INSIDE_TEXT not in said


def test_an_output_folder_node_writes_nothing_to_the_host_in_the_reference_profile(
    world: World, file_tree: FileTree
) -> None:
    """EP-014: the node that creates directories and files on the host is refused in the reference profile, whatever its path;
    the development profile writes inside the root (the control) and refuses a path outside it."""
    inside = file_tree.root / "written"
    escaping = file_tree.outside / "planted"
    node = {"type": "output_folder", "path": str(inside), "overwrite": True}

    run, _ = _run_host_node(world, node, "payload")
    escaped_run, _ = _run_host_node(world, {**node, "path": str(escaping)}, "payload")

    assert not escaping.exists(), "nothing was created outside the root"
    if world.reference:
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), run.error_summary
        assert (escaped_run.status, escaped_run.error_code) == ("failed", REFUSAL_CODE)
        assert not inside.exists()
    else:
        assert run.status == "completed", run.error_summary
        assert inside.exists()
        assert escaped_run.status == "failed"


# -- plugins: trusted-code admission ---------------------------------------------------------------------------------------

PLUGIN_DISTRIBUTION = "egi-attack-plugin"
PLUGIN_MODULE = "egi_attack_plugin"
PLUGIN_OPTIMIZER = "EgiAttack"


def _install_plugin(site: Path, marker: Path, *, distribution: str, entry_point: str = "attack") -> None:
    """A real installed distribution (``dist-info`` plus a module) whose module writes ``marker`` when it is imported."""
    site.mkdir(parents=True, exist_ok=True)
    (site / f"{PLUGIN_MODULE}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class _Declaration:\n"
        f"    name = {PLUGIN_OPTIMIZER!r}\n"
        "    summary = 'a hostile third-party optimizer'\n"
        "    artifact_types = frozenset({'prompt'})\n"
        "declaration = _Declaration()\n",
        encoding="utf-8",
    )
    dist_info = site / f"{distribution.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(
        f"[caliber.optimizers]\n{entry_point} = {PLUGIN_MODULE}:declaration\n", encoding="utf-8"
    )


def _plugin_server(world: World, **env: str) -> ServerProcess:
    """A process on the scenario's configuration, with the installed plugin on its ``PYTHONPATH``."""
    merged = {**world.app.env, **env}
    merged["PYTHONPATH"] = os.pathsep.join(
        [str(world.tmp_path / "plugin-site"), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    return ServerProcess(merged, app_dir=CALIBER_DIR, log_path=world.tmp_path / "plugin-server.log")


@pytest.mark.parametrize(
    "allowlist",
    [
        pytest.param(None, id="no-allowlist"),
        pytest.param("*", id="a-wildcard"),
        pytest.param("egi-attack", id="a-prefix-of-the-distribution"),
        pytest.param("egi-attack-plugin-2", id="a-near-miss"),
        pytest.param("trusted-optimizers", id="the-entry-point-name-of-an-untrusted-distribution"),
    ],
)
def test_forbidden_module_import(world: World, allowlist: str | None) -> None:
    """``forbidden_module_import`` (B12.4b, #763): a plugin the deployment was not told to trust is refused before one line of
    its module runs. The reference process exits at startup with ``plugin_not_allowlisted`` and binds no port; the development
    profile starts and lists the plugin without importing it. Either way the marker the plugin writes on import is never written."""
    _install_plugin(
        world.tmp_path / "plugin-site",
        world.marker,
        distribution=PLUGIN_DISTRIBUTION,
        entry_point="trusted-optimizers",
    )
    env = {} if allowlist is None else {"CALIBER_PLUGIN_ALLOWLIST": allowlist}
    server = _plugin_server(world, **env)

    if world.reference:
        with pytest.raises(ServerStartError):
            server.start()
        assert server.exit_code not in (None, 0), "the process must exit, not keep running"
        log = server.log_tail(80)
        (violation,) = [line for line in log.splitlines() if line.startswith("- ")]
        assert "[plugin_not_allowlisted]" in violation and PLUGIN_DISTRIBUTION in violation, log
        with pytest.raises(httpx.TransportError):
            httpx.get(server.base_url + HEALTH_PATH, timeout=2.0)
    else:
        with server:
            assert httpx.get(server.base_url + HEALTH_PATH, timeout=5.0).status_code == 200
            response = httpx.get(
                server.base_url + f"{PREFIX}/capabilities",
                headers=world.app.headers(EDITOR_A, PROJECT_A),
                timeout=10.0,
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["extensibility"]["optimizers"] != [], "built-ins only"
    assert not world.marker.exists(), "the plugin's module was imported"


def test_an_allowlisted_distribution_loads_on_first_use_in_both_profiles(world: World) -> None:
    """The control: naming the distribution (in any spelling the allowlist normalises) admits it, and only then is it imported."""
    _install_plugin(world.tmp_path / "plugin-site", world.marker, distribution=PLUGIN_DISTRIBUTION)
    server = _plugin_server(world, CALIBER_PLUGIN_ALLOWLIST="Egi_Attack.Plugin")

    with server:
        assert not world.marker.exists(), "starting does not import a plugin: the load is deferred"
        response = httpx.get(
            server.base_url + f"{PREFIX}/capabilities",
            headers=world.app.headers(EDITOR_A, PROJECT_A),
            timeout=10.0,
        )
        assert response.status_code == 200, response.text
        extensibility = response.json()["data"]["extensibility"]
        (plugin,) = extensibility["plugins"]
        assert plugin["allowlisted"] is True and plugin["error"] is None
        assert PLUGIN_OPTIMIZER in [o["name"] for o in extensibility["optimizers"]]
        assert world.marker.read_text() == "imported"


class _InstalledLater:
    """An entry point that appears after the process started, and that fails the test if anything imports it."""

    name = "late"
    value = "late_plugin:declaration"
    dist = type("_Dist", (), {"name": "late-plugin"})()

    def __init__(self, loads: list[str]) -> None:
        self._loads = loads

    def load(self) -> object:
        self._loads.append("imported")
        raise AssertionError("a refused plugin must not be imported")


def _optimization_request(app: ReferenceApp) -> httpx.Response:
    headers = app.headers(EDITOR_A, PROJECT_A)
    token = app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return app.client.post(
        f"{PREFIX}/prompts/optimization/runs",
        json={
            "agent_id": "no-such-agent",
            "eval_dataset_id": "EDS-NONE",
            "optimizer_type": "GEPA",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
        headers={**headers, "X-CALIBER-CSRF": token},
    )


def test_a_plugin_installed_after_the_process_started_is_refused_on_every_route_that_loads_plugins(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing into a running process (a ``pip install`` next to a live API) does not bypass the allowlist: the registry's
    own admission refuses, each route that loads plugins answers with the one code, and nothing is imported or written."""
    registry = optimizer_registry()
    registry.reset_for_tests()
    entries: list[object] = []
    loads: list[str] = []
    monkeypatch.setattr(entrypoints, "_entry_points", lambda: list(entries))
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    try:
        entries.append(_InstalledLater(loads))
        jobs_before = world.count(CaliberRefinementJob)

        capabilities = world.call("GET", "/capabilities")
        optimization = _optimization_request(world.app)
        pairs = world.call("GET", "/capabilities")  # a second load attempt: the refusal is remembered, not retried

        assert capabilities.status_code == pairs.status_code == 200
        if world.reference:
            (plugin,) = capabilities.json()["data"]["extensibility"]["plugins"]
            assert plugin["allowlisted"] is False
            assert plugin["error"].startswith(f"{REFUSAL_CODE}: ")
            assert optimization.status_code == 503, optimization.text
            assert optimization.json()["detail"].startswith(f"{REFUSAL_CODE}: ")
            assert "late-plugin" in optimization.json()["detail"]
            assert world.count(CaliberRefinementJob) == jobs_before
        else:  # the control: the development profile skips an unlisted plugin and serves the request
            assert optimization.status_code != 503, optimization.text
        assert loads == [], "no refused plugin was ever imported"

        # Recovery: the plugin is uninstalled (or allowlisted) and the registry rebuilt; the same request is served again.
        entries.clear()
        registry.reset_for_tests()
        assert _optimization_request(world.app).status_code != 503
    finally:
        registry.reset_for_tests()


def test_a_plugin_handed_straight_to_the_registry_is_admitted_on_the_same_terms(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OptimizerRegistry.register`` is a second way in; it applies the allowlist too, and the refusal carries the one code."""
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    registry = OptimizerRegistry()
    hostile = OptimizerSpec(
        name="Hostile",
        summary="registered directly",
        artifact_types=frozenset({"prompt"}),
        source="plugin",
        distribution="not-allowlisted",
    )

    if world.reference:
        with pytest.raises(PluginRefused) as refused:
            registry.register(hostile)
        assert refused.value.code == REFUSAL_CODE and refused.value.distribution == "not-allowlisted"
        assert not registry.has("Hostile")
    else:
        registry.register(hostile)
        assert registry.has("Hostile")


# -- identity and CSRF: the protections the attack pack preserves -----------------------------------------------------------


def _admit_request(world: World) -> dict[str, Any]:
    """A run request against an ordinary published workflow: what a spoofed caller would use to make the platform act."""
    workflow_id = _published_workflow(world.app)
    return {
        "workflow_id": workflow_id,
        "alias": "manual",
        "input": "hi",
        "idempotency_key": f"attack-{uuid.uuid4().hex}",
        **seed_release(world.app, PROJECT_A),
    }


def _stored(world: World) -> tuple[int, ...]:
    return tuple(
        world.count(model)
        for model in (CaliberWorkflowRun, CaliberRunGrant, CaliberRunManifest, CaliberPolicyDecision)
    )


def _csrf_token(world: World, headers: dict[str, str]) -> str:
    return str(world.app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"])


FORGED_IDENTITIES = [
    pytest.param({}, id="no-proxy-secret"),
    pytest.param({"X-CALIBER-Proxy-Secret": "not-the-secret"}, id="a-wrong-proxy-secret"),
    pytest.param({"X-CALIBER-Proxy-Secret": ""}, id="an-empty-proxy-secret"),
    pytest.param({"X-CALIBER-Proxy-Secret": "p" * 39}, id="a-truncated-proxy-secret"),
    pytest.param(
        {"X-CALIBER-Proxy-Secret": "p" * 40 + "x"}, id="the-proxy-secret-with-a-suffix"
    ),
]


@pytest.mark.parametrize("secret_header", FORGED_IDENTITIES)
def test_forged_identity_header(world: World, secret_header: dict[str, str]) -> None:
    """``forged_identity_header``: an ``X-CALIBER-User`` header that did not come through the trusted proxy is not an identity.

    Every request that claims to be ``editor_a`` without the proxy's secret is answered ``401`` and nothing is stored, built or
    dispatched; the same request through the proxy is accepted (the control)."""
    body = _admit_request(world)
    before_rows, before = _stored(world), world.effects()
    forged = {"X-CALIBER-User": EDITOR_A, "X-CALIBER-Project": PROJECT_A, **secret_header}

    # A CSRF token is public (any client may fetch one); the identity behind the request is what is refused.
    token = _csrf_token(world, forged)
    refused = [
        world.app.client.post(
            f"{PREFIX}/workflow-runs", json=body, headers={**forged, "X-CALIBER-CSRF": token}
        ),
        world.app.client.get(f"{PREFIX}/workflows", headers=forged),
        world.app.client.get(f"{PREFIX}/capabilities", headers=forged),
        world.app.client.post(
            f"{PREFIX}/workflows",
            json={"name": "forged"},
            headers={**forged, "X-CALIBER-CSRF": token},
        ),
    ]

    for response in refused:
        assert response.status_code == 401, (response.request.url.path, response.text)
        assert EDITOR_A not in response.text and PROJECT_A not in response.text
    assert _stored(world) == before_rows and world.effects() == before
    allowed = world.call("POST", "/workflow-runs", body)
    assert allowed.status_code == 202, allowed.text


@pytest.mark.parametrize(
    "claimed",
    [
        pytest.param("anonymous", id="the-anonymous-sentinel"),
        pytest.param("", id="an-empty-user"),
        pytest.param(f"{EDITOR_A}, {APPROVER_A}", id="a-list-of-users"),
    ],
)
def test_a_proxied_request_that_names_no_single_user_is_not_authenticated_as_anyone(
    world: World, claimed: str
) -> None:
    """Even through the proxy, a user header that is not exactly one user (an empty value, the ``anonymous`` sentinel, a list)
    never becomes an editor's identity."""
    body = _admit_request(world)
    before_rows, before = _stored(world), world.effects()
    headers = {**world.app.proxy_headers, "X-CALIBER-Project": PROJECT_A}
    headers["X-CALIBER-User"] = claimed
    token = _csrf_token(world, headers)
    response = world.app.client.post(
        f"{PREFIX}/workflow-runs", json=body, headers={**headers, "X-CALIBER-CSRF": token}
    )
    assert response.status_code in (401, 403), response.text
    assert _stored(world) == before_rows and world.effects() == before


def _csrf_attacks(world: World) -> dict[str, dict[str, str]]:
    """Header sets that carry a valid identity and a bad (or no) CSRF token."""
    headers = world.app.headers(EDITOR_A, PROJECT_A)
    token = _csrf_token(world, headers)
    manager = CSRFTokenManager(secret=CSRF_SECRET.encode(), ttl_seconds=3600)
    other_users = _csrf_token(world, world.app.headers(EDITOR_B, PROJECT_B))
    stamp, _, signature = token.partition(".")
    flipped = signature[:-1] + ("0" if signature[-1] != "0" else "1")
    return {
        "no-token": {},
        "an-empty-token": {"X-CALIBER-CSRF": ""},
        "garbage": {"X-CALIBER-CSRF": "not-a-token"},
        "no-separator": {"X-CALIBER-CSRF": stamp},
        "a-non-numeric-timestamp": {"X-CALIBER-CSRF": f"abc.{signature}"},
        "a-tampered-signature": {"X-CALIBER-CSRF": f"{stamp}.{flipped}"},
        "another-users-token": {"X-CALIBER-CSRF": other_users},
        "an-expired-token": {
            "X-CALIBER-CSRF": manager.issue(EDITOR_A, now=int(time.time()) - 7200)
        },
        "a-future-token": {
            "X-CALIBER-CSRF": manager.issue(EDITOR_A, now=int(time.time()) + 7200)
        },
    }


def test_csrf_without_token(world: World) -> None:
    """``csrf_without_token``: an identified caller whose state-changing request carries no valid CSRF token is answered ``403``
    and nothing is stored or run. The same request with a token minted for that caller is accepted (the control), and a safe
    method needs none."""
    body = _admit_request(world)
    before_rows, before = _stored(world), world.effects()
    headers = world.app.headers(EDITOR_A, PROJECT_A)

    for label, extra in _csrf_attacks(world).items():
        response = world.app.client.post(
            f"{PREFIX}/workflow-runs", json=body, headers={**headers, **extra}
        )
        assert response.status_code == 403, (label, response.text)
        assert "CSRF" in response.text, label
    for method, path in (("DELETE", "/workflows/WF-none"), ("PUT", "/secrets/nothing")):
        refused = world.app.client.request(method, f"{PREFIX}{path}", headers=headers)
        assert refused.status_code == 403, (method, refused.text)

    assert _stored(world) == before_rows and world.effects() == before
    assert world.app.client.get(f"{PREFIX}/workflows", headers=headers).status_code == 200
    accepted = world.call("POST", "/workflow-runs", body)
    assert accepted.status_code == 202, accepted.text


# -- egress: the metadata endpoint, and a name that changes its answer -----------------------------------------------------

OPENAPI = f"{PREFIX}/openapi-integrations"


@pytest.fixture
def egress_world(world: World, monkeypatch: pytest.MonkeyPatch) -> RecordingTransport:
    """The egress guard with the network taken away: whatever the guard lets through lands in the returned recorder.

    The guard's own transport is the real one (``caliber.egress.EgressGuardTransport``); only the transport *under* it, the one
    that would open a socket, is replaced, so a request that gets past the guard is observed instead of sent.
    """
    inner = RecordingTransport()
    real = egress.EgressGuardTransport

    class GuardOverRecorder(real):  # type: ignore[misc, valid-type]
        def __init__(self, policy: Any, inner_transport: Any = None) -> None:
            super().__init__(policy, inner=inner)

    monkeypatch.setattr(egress, "EgressGuardTransport", GuardOverRecorder)
    return inner


def _integration(world: World) -> str:
    # Integrations are off unless an operator turns the area on: the attacker's best case is that they did.
    world.config_update(feature_integrations_enabled=True)
    created = world.call(
        "POST", "/openapi-integrations", {"name": f"attack-{uuid.uuid4().hex[:6]}", "description": ""}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["data"]["integration_id"])


def _resolver(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[list[str]]]) -> dict[str, int]:
    """Scripted DNS: each host's answers in order (the last repeats), and how often each host was looked up."""
    looked_up: dict[str, int] = {}

    def fake(host: str) -> list[str]:
        scripted = answers[host]
        index = min(looked_up.get(host, 0), len(scripted) - 1)
        looked_up[host] = looked_up.get(host, 0) + 1
        return scripted[index]

    monkeypatch.setattr(egress, "_resolve_addresses", fake)
    return looked_up


def _probe_and_import(world: World, integration_id: str, url: str) -> tuple[Any, Any]:
    probe = world.call(
        "POST",
        f"/openapi-integrations/{integration_id}/validate-spec-source",
        {"source_kind": "url", "spec_url": url},
    )
    imported = world.call(
        "POST",
        f"/openapi-integrations/{integration_id}/import",
        {"source_kind": "url", "spec_url": url},
    )
    return probe, imported


@pytest.mark.parametrize(
    ("url", "resolves_to"),
    [
        pytest.param(f"http://{METADATA}/latest/meta-data/iam/security-credentials/", None, id="the-literal-address"),
        pytest.param("http://2852039166/latest/meta-data/", [METADATA], id="the-decimal-form"),
        pytest.param("http://0xa9fefea9/latest/meta-data/", [METADATA], id="the-hex-form"),
        pytest.param("http://[::ffff:169.254.169.254]/latest/meta-data/", None, id="an-ipv4-mapped-ipv6-literal"),
        pytest.param("http://[fd00:ec2::254]/latest/meta-data/", None, id="the-aws-ipv6-metadata-address"),
        pytest.param("http://metadata.attack.example/computeMetadata/v1/", [METADATA], id="a-name-that-resolves-to-it"),
        pytest.param("http://localhost:8080/", ["127.0.0.1"], id="loopback-by-name"),
        pytest.param("http://internal.attack.example/", ["10.0.0.7"], id="a-private-address-by-name"),
        pytest.param("http://mixed.attack.example/", [PUBLIC, METADATA], id="a-name-with-a-public-and-a-metadata-record"),
    ],
)
def test_ssrf_metadata_ip(
    world: World,
    egress_world: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    resolves_to: list[str] | None,
) -> None:
    """``ssrf_metadata_ip``: an operator-reachable route that fetches a URL refuses the cloud metadata endpoint, whatever way
    the URL spells it, with ``EgressBlockedError``'s reason, and no request leaves the process. The same in the development
    profile: the egress guard is not a reference-profile control, it is one the reference profile requires to stay on."""
    host = url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0]
    if resolves_to is not None:
        _resolver(monkeypatch, {host: [resolves_to]})
    integration_id = _integration(world)
    before = world.effects()

    probe, imported = _probe_and_import(world, integration_id, url)

    assert probe.status_code == 200, probe.text
    assert probe.json()["data"]["allowed"] is False and probe.json()["data"]["reachable"] is False
    assert imported.status_code == 400, imported.text
    assert "blocked" in imported.text
    assert egress_world.requests == [], "no request reached the connection layer"
    assert world.effects() == before


def test_dns_rebinding(
    world: World, egress_world: RecordingTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``dns_rebinding``: a name that answers a public address to the policy check and the metadata address to the connection
    is not connected to. The connection is pinned to the address the policy vetted (one lookup serves both), and when the answer
    changes between the check and the connect the request is blocked before the connection layer sees it. Control: a stable
    public name connects, to the vetted address, with the original ``Host`` and SNI."""
    host = "rebind.attack.example"
    lookups = _resolver(monkeypatch, {host: [[PUBLIC], [METADATA]], "stable.example": [[PUBLIC]]})
    integration_id = _integration(world)
    before = world.effects()

    probe, imported = _probe_and_import(world, integration_id, f"https://{host}/openapi.json")

    assert probe.json()["data"]["reachable"] is False
    assert imported.status_code == 400 and "blocked" in imported.text, imported.text
    assert egress_world.requests == [], "the metadata address was never connected to"
    assert lookups[host] >= 2, "the policy check and the connection each asked, and the second answer was the attack"
    assert world.effects() == before

    # The control: a name that does not change its answer is fetched, from the address the policy vetted.
    _probe_and_import(world, integration_id, "https://stable.example/openapi.json")
    assert egress_world.requests, "the guard lets a legitimate public name through"
    sent = egress_world.requests[0]
    assert sent.url.host == PUBLIC and sent.headers["Host"] == "stable.example"
    assert sent.extensions["sni_hostname"] == "stable.example"


# -- unsafe configuration --------------------------------------------------------------------------------------------------

#: The environment that breaks each rule of the reference profile (``caliber.profile.REFERENCE_REQUIREMENTS``) and nothing else.
#: ``plugin_not_allowlisted`` needs an installed plugin and is attacked by ``test_forbidden_module_import``.
UNSAFE_ENVIRONMENT: dict[str, dict[str, str]] = {
    "sqlite_database": {"CALIBER_DATABASE_URL": "sqlite:///./egi-attack.db"},
    "csrf_disabled": {"CALIBER_CSRF_ENABLED": "false"},
    "dev_fallback_enabled": {"CALIBER_AUTH_DEV_FALLBACK_ENABLED": "true"},
    "insecure_bootstrap": {"CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT": "true"},
    "trusted_header_without_proxy_secret": {"CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV": ""},
    "egress_policy_disabled": {"CALIBER_EGRESS_POLICY_ENABLED": "false"},
    "mcp_insecure_http": {"CALIBER_MCP_ALLOW_INSECURE_HTTP": "true"},
    "mcp_allowlist_not_in_profile": {
        "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST": "npx",
        "CALIBER_MCP_REMOTE_HOST_ALLOWLIST": "attacker.example",
    },
    "workflow_file_root_unconfined": {"CALIBER_WORKFLOW_FILE_ROOT": "unconfined"},
    "self_approval_allowed": {"CALIBER_APPROVAL_ALLOW_SELF_APPROVAL": "true"},
    "apply_distinct_approver_disabled": {"CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER": "false"},
    "memory_inference_enabled": {"CALIBER_MEMORY_ENABLED": "true", "CALIBER_MEMORY_INFER": "true"},
    "http_lookup_cleartext": {
        "CALIBER_EFFECT_HTTP_VERIFIERS": (
            '[{"project_id": "P", "resource": "https://hooks.example.test/a",'
            ' "lookup_url": "http://hooks.example.test/lookups"}]'
        )
    },
    "fault_barriers_enabled": {"CALIBER_FAULT_BARRIERS": "before_dispatch"},
}


def _unsafe_server(world: World, overrides: dict[str, str]) -> ServerProcess:
    return ServerProcess(
        {**world.app.env, **overrides}, app_dir=CALIBER_DIR, log_path=world.tmp_path / "unsafe.log"
    )


def _violations(log: str) -> list[str]:
    return [line for line in log.splitlines() if line.startswith("- ")]


def test_every_rule_of_the_reference_profile_has_an_attack() -> None:
    """A rule added to the profile without an attack here fails this test, so the pack cannot fall behind the profile."""
    attacked = set(UNSAFE_ENVIRONMENT) | {"plugin_not_allowlisted"}
    assert attacked == {requirement.code for requirement in REFERENCE_REQUIREMENTS}


@pytest.mark.parametrize("code", list(UNSAFE_ENVIRONMENT))
def test_unsafe_config_refused(world: World, code: str) -> None:
    """``unsafe_config_refused`` (#570): each unsafe setting alone stops the process before it serves: it exits, names the one
    violation by code and by the variable to change, binds no port and touches no downstream."""
    if not world.reference:
        pytest.skip("the reference profile refuses; the development control is the next test")
    server = _unsafe_server(world, UNSAFE_ENVIRONMENT[code])
    before = world.effects()

    with pytest.raises(ServerStartError):
        server.start()

    assert server.exit_code not in (None, 0), "the process must exit, not keep running"
    log = server.log_tail(80)
    assert "reference profile refused (1 violation)" in log, log
    (violation,) = _violations(log)
    assert violation.endswith(f"[{code}]"), violation
    named = [env_var for env_var in UNSAFE_ENVIRONMENT[code] if env_var in violation]
    assert named, f"the refusal names the variable to change: {violation}"
    with pytest.raises(httpx.TransportError):
        httpx.get(server.base_url + HEALTH_PATH, timeout=2.0)
    assert world.effects() == before


def test_the_development_profile_accepts_every_unsafe_setting(world: World) -> None:
    """The control: the profile, not the setting, is what refuses. The same settings, all at once, start a development process."""
    if world.reference:
        pytest.skip("the reference profile refuses them")
    combined = {key: value for env in UNSAFE_ENVIRONMENT.values() for key, value in env.items()}
    with _unsafe_server(world, combined) as server:
        assert httpx.get(server.base_url + HEALTH_PATH, timeout=5.0).status_code == 200


def test_every_unsafe_setting_at_once_is_listed_in_one_refusal(world: World) -> None:
    if not world.reference:
        pytest.skip("the development profile accepts them: see the control above")
    combined = {key: value for env in UNSAFE_ENVIRONMENT.values() for key, value in env.items()}
    server = _unsafe_server(world, combined)

    with pytest.raises(ServerStartError):
        server.start()

    log = server.log_tail(120)
    codes = [line.rsplit(" [", 1)[1].rstrip("]") for line in _violations(log)]
    assert codes == [
        requirement.code
        for requirement in REFERENCE_REQUIREMENTS
        if requirement.code in UNSAFE_ENVIRONMENT
    ]


# -- the secret canary -----------------------------------------------------------------------------------------------------

ADMIN = "egi_admin"


@pytest.fixture
def canary_world(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[tuple[World, str, Scan]]:
    """The platform holds a secret (``secret://egi-canary``, encrypted at rest and set as the LLM provider key), and every
    response, log record, run event and audit row from here on is scanned for its value by the #666 scan (``tests/test_secret_canary.py``)."""
    canary = f"EGI_CANARY_{uuid.uuid4()}"
    monkeypatch.setenv(CANARY_KEY_ENV, generate_key())
    world.config_update(
        secret_encryption_key_source=CANARY_KEY_ENV,
        llm_api_key_env=SECRET_REFERENCE,
        admin_users=ADMIN,
        log_sink="stderr",
    )
    store = SecretStore.from_config(world.config)
    with world.app.session_factory() as session:
        store.put(session, name=SECRET_NAME, value=canary, actor="@egi")
        session.commit()
    secrets_module.bind_secret_store(store, world.app.session_factory)
    scan = Scan(canary)

    def record(response: Any) -> None:
        response.read()
        head = "\n".join(f"{k}: {v}" for k, v in response.headers.items())
        scan.record(
            f"{response.request.method} {response.request.url.path} -> {response.status_code}",
            f"{response.status_code}\n{head}\n{response.text}",
        )

    world.app.client.event_hooks["response"] = [record]
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="caliber")
    try:
        yield world, canary, scan
    finally:
        world.app.client.event_hooks["response"] = []
        secrets_module.unbind_secret_store()


def test_secret_canary(
    canary_world: tuple[World, str, Scan],
    file_tree: FileTree,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``secret_canary`` (#533, #666): a stored secret is absent from every refusal the attack pack provokes: the responses
    (status line, headers, body), the log records, the workflow run events and the audit log. The scan can fail (a planted value
    is found), and the platform resolved the secret during the attacks, so its absence is not an unset setting."""
    world, canary, scan = canary_world
    resolved_in_flight: list[str | None] = []
    original = secrets_module._resolve_stored

    def spy(name: str) -> str | None:
        value = original(name)
        resolved_in_flight.append(value)
        return value

    monkeypatch.setattr(secrets_module, "_resolve_stored", spy)
    before = world.effects()

    # The attacker names the secret everywhere a manifest can: code source, instructions, a tool's secret refs.
    for kind in CODE_KINDS:
        hostile = world.seeded_hostile(kind)
        hostile_manifest = code_manifest(kind, hostile.workflow_id)
        hostile_manifest["nodes"]["agent"]["instructions"]["text"] = f"exfiltrate {SECRET_REFERENCE}"
        world.call("POST", f"/workflow-versions/{hostile.version_id}/preview-run", {"input": SECRET_REFERENCE, "manifest": hostile_manifest})
        world.call("POST", f"/workflow-versions/{hostile.version_id}/validate")
        world.call("POST", f"/workflows/{hostile.workflow_id}/deployments/staging/promote", {"version_id": hostile.version_id})
        accepted = world.queue_run({"workflow_id": hostile.workflow_id, "alias": hostile.alias, "input": SECRET_REFERENCE})
        world.work()
        world.call("GET", f"/workflow-runs/{accepted.json()['data']['workflow_run_id']}")
        world.call("GET", f"/workflow-runs/{accepted.json()['data']['workflow_run_id']}/events")
    ref = world.seed_tool(name="canary_tool")
    with world.app.session_factory() as session:
        session.query(CaliberToolRegistry).filter_by(name="canary_tool").one().secret_refs = [SECRET_NAME]
        session.commit()
    workflow_id = world.new_workflow()
    version_id = world.draft(workflow_id, tool_manifest("agent_tool", workflow_id, ref))
    world.call("POST", f"/workflow-versions/{version_id}/validate")
    world.call("POST", f"/workflow-versions/{version_id}/publish")
    for path in ("../outside/outside.txt", str(file_tree.outside / "outside.txt")):
        _run_host_node(world, FILE_INPUT, path)
    forged = {"X-CALIBER-User": EDITOR_A, "X-CALIBER-Proxy-Secret": SECRET_REFERENCE}
    world.app.client.get(f"{PREFIX}/workflows", headers=forged)
    world.app.client.post(f"{PREFIX}/workflows", json={"name": canary[:20]}, headers=world.app.headers(EDITOR_A, PROJECT_A))
    # ... and the platform reads the secret while it does (the provider status resolves the reference inside the request).
    status = world.app.client.get(f"{PREFIX}/settings/llm", headers=world.app.headers(ADMIN))
    assert status.status_code == 200, status.text
    assert status.json()["data"]["openai_key_present"] is True

    assert canary in resolved_in_flight, "nothing resolved the secret during the attacks, so its absence would prove nothing"
    assert len(scan.bodies) > 30, "the attacks produced the responses to scan"
    assert caplog.records, "DEBUG logging captured nothing"
    with world.app.session_factory() as session:
        assert session.query(CaliberWorkflowRunEvent).first() is not None
        assert session.query(CaliberAuditLog).first() is not None
    assert scan.hits(world.app.session_factory, caplog.records) == []
    if world.reference:
        assert world.effects() == before

    # The scan can fail: a value planted in a response body is found.
    planted = Scan(canary)
    planted.record("GET /leaky -> 200", f'{{"detail": "key was {canary}"}}')
    assert planted.hits(world.app.session_factory, []) == ["response body: GET /leaky -> 200"]


# -- the assistant's tool drafts and the tool sandbox -----------------------------------------------------------------------


def test_an_assistant_tool_draft_never_starts_the_sandbox_in_the_reference_profile(
    world: World,
) -> None:
    """EP-078/EP-079: the assistant authors a tool from source and tests it by running that source in the sandbox. In the
    reference profile the draft is created and its test gate reports the refusal (a failed report, the one code, no source and
    no test input echoed), and no sandbox child is ever started; the development profile runs the draft (the control)."""
    world.config_update(feature_aria_enabled=True)
    marker_text = "SOURCE-MARKER-3d1f"
    created = world.call("POST", "/assistant/sessions", {"title": "attack"})
    assert created.status_code in (200, 201), created.text
    session_id = created.json()["data"]["session_id"]
    before = world.effects()

    planned = world.call(
        "POST",
        f"/assistant/sessions/{session_id}/plans",
        {
            "intent_name": "create_tool",
            "slot_overrides": {
                "tool_name": "attack_tool",
                "source": f"def attack_tool(x: int) -> dict:\n    # {marker_text}\n    return {{'value': x * 2}}\n",
                "callable_name": "attack_tool",
                "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
                "tests": [{"name": "doubles two", "input": {"x": 2}, "expected": {"value": 4}}],
            },
        },
    )
    assert planned.status_code == 201, planned.text
    executed = world.call(
        "POST",
        f"/assistant/sessions/{session_id}/plans/execute",
        {"plan_id": planned.json()["data"]["plan_id"], "confirm": True},
    )
    assert executed.status_code == 201, executed.text
    report = executed.json()["data"]["result"]["test_report"]
    draft_id = executed.json()["data"]["result"]["ids"]["draft_id"]
    retested = world.call("POST", f"/assistant/drafts/{draft_id}/test")
    published = world.call("POST", f"/assistant/drafts/{draft_id}/publish")

    if world.reference:
        assert report["passed"] is False and str(report["error"]).startswith(f"{REFUSAL_CODE}: ")
        assert retested.json()["data"]["passed"] is False
        assert marker_text not in str(report) + retested.text
        assert published.status_code == 400, published.text
        assert world.effects() == before, "the draft's source was never run"
    else:
        assert report["passed"] is True, report
        assert world.effects().spawns > before.spawns


# -- MCP: a server definition is not code to run ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["test-connection", "discover-tools"])
def test_an_mcp_stdio_command_an_attacker_registers_is_never_spawned(
    world: World, route: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the integrations area on, an operator-scope caller can register an MCP server whose command is any program. The
    reference profile lists no MCP command (``CALIBER_MCP_STDIO_COMMAND_ALLOWLIST`` is empty), so naming the interpreter is
    refused at every route that would start it, and the program (which writes a marker) never runs. The caller is an administrator,
    the strongest caller the route allows. The development profile's shipped allowlist admits the interpreter, and the marker is
    written (the control)."""
    script = world.tmp_path / "attacker_mcp.py"
    script.write_text(
        f"import pathlib\npathlib.Path({str(world.marker)!r}).write_text('spawned')\n", encoding="utf-8"
    )
    world.config_update(feature_integrations_enabled=True, admin_users=ADMIN)
    if not world.reference:
        # The control: a development operator who allowlisted the interpreter and this script (the reference profile lists none).
        monkeypatch.setenv("CALIBER_MCP_STDIO_COMMAND_ALLOWLIST", "${PYTHON}")
        monkeypatch.setenv("CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST", str(script))
    created = world.call(
        "POST",
        "/mcp-servers",
        {
            "name": f"attack-{uuid.uuid4().hex[:6]}",
            "transport": "stdio",
            "command": "${PYTHON}",
            "args": [str(script)],
        },
        user=ADMIN,
        project=None,
    )
    before = world.effects()
    assert created.status_code in (200, 201), created.text
    server_id = str(created.json()["data"]["server_id"])

    response = world.call("POST", f"/mcp-servers/{server_id}/{route}", user=ADMIN, project=None)

    if world.reference:
        assert "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST" in response.text, response.text
        assert not world.marker.exists(), "the attacker's program was spawned"
        assert world.effects() == before
    else:
        assert world.marker.exists(), response.text


# -- code that starts itself: the cron scheduler and the event trigger --------------------------------------------------------


def _scheduled_manifest(kind: str, workflow_id: str, trigger: dict[str, Any]) -> dict[str, Any]:
    data = code_manifest(kind, workflow_id)
    data["nodes"]["start"]["trigger"] = trigger
    return data


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_a_cron_deployment_of_hostile_code_is_refused_when_the_scheduler_fires_it(
    world: World, kind: str
) -> None:
    """Nobody has to ask for a scheduled run: the scheduler fires an active deployment's cron trigger by itself. A hostile
    version deployed with a ``* * * * *`` trigger is enqueued (admission does not compile) and then fails by the one code at the
    worker with its node never run; the development profile runs it (the control)."""
    hostile = world.seeded_hostile(
        kind,
        alias="nightly",
        environment="development",
        manifest_for=lambda wf: _scheduled_manifest(
            kind, wf, {"mode": "cron", "cron": "* * * * *", "timezone": "UTC", "alias": "nightly"}
        ),
    )
    seed_release(world.app, PROJECT_A)  # the project has a current release: a scheduled run can name its lineage
    before = world.effects()

    scheduler = WorkflowSchedulerTask(
        session_factory=world.app.session_factory,
        admission=AdmissionSettings.from_config(world.config),
    )
    fired = scheduler._tick_inner(datetime.now(timezone.utc))
    world.work()

    with world.app.session_factory() as session:
        runs = list(
            session.execute(
                select(CaliberWorkflowRun).where(CaliberWorkflowRun.workflow_id == hostile.workflow_id)
            ).scalars()
        )
    if world.reference:
        assert fired == 1 and len(runs) == 1, "the scheduler enqueued the hostile deployment's run"
        assert all((r.status, r.error_code) == ("failed", REFUSAL_CODE) for r in runs), [
            (r.status, r.error_code, r.error_summary) for r in runs
        ]
        assert world.effects() == before
    else:
        assert fired == 1 and [r.status for r in runs] == ["completed"], [
            (r.status, r.error_summary) for r in runs
        ]
        assert world.effects() != before, "the control: the scheduled run executed the node"


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_an_event_trigger_of_a_hostile_deployment_fails_by_the_code_and_runs_nothing(
    world: World, kind: str
) -> None:
    """``POST /workflows/{id}/trigger``: an external caller starts a run of whatever the Start trigger's alias deploys."""
    hostile = world.seeded_hostile(
        kind,
        alias="events",
        manifest_for=lambda wf: _scheduled_manifest(
            kind, wf, {"mode": "event", "event_name": "go", "alias": "events"}
        ),
    )
    before = world.effects()

    triggered = world.call(
        "POST",
        f"/workflows/{hostile.workflow_id}/trigger",
        {
            "event_name": "go",
            "input": "go",
            "idempotency_key": f"attack-{uuid.uuid4().hex}",
            **seed_release(world.app, PROJECT_A),
        },
    )
    assert triggered.status_code in (200, 202), triggered.text
    run_id = str(triggered.json()["data"]["workflow_run_id"])
    world.work()

    run = world.run_row(run_id)
    if world.reference:
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), run.error_summary
        assert world.effects() == before
    else:
        assert run.status == "completed", run.error_summary
        assert world.effects() != before, "the control: the triggered run executed the node"


# -- the worker is a different process than the API ------------------------------------------------------------------------


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_the_execution_owner_process_refuses_a_hostile_run_the_api_process_queued(
    world: World, kind: str
) -> None:
    """The run is admitted by one process (the test's API) and executed by another (a real CALIBER process that holds the
    execution-owner lease and runs the worker loop), with the attacker's module on its ``PYTHONPATH`` and its entrypoint
    allowlisted. The owner fails the run by the one code and its node never runs; nothing is imported, spawned or mutated."""
    if not world.reference:
        pytest.skip("the owner-only worker loop is the reference profile's")
    hostile = world.seeded_hostile(kind)
    accepted = world.queue_run({"workflow_id": hostile.workflow_id, "alias": hostile.alias})
    assert accepted.status_code == 202, accepted.text
    run_id = str(accepted.json()["data"]["workflow_run_id"])
    before = world.effects()
    server = ServerProcess(
        {
            **world.app.env,
            "CALIBER_BACKGROUND_TASKS_ENABLED": "true",
            "CALIBER_EXECUTION_LEASE_SECONDS": "3",
            "CALIBER_WORKFLOW_RUN_WORKER_ENABLED": "true",
            "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED": "true",
            "CALIBER_EXTERNAL_APP_ENTRYPOINT_ALLOWLIST": ATTACK_ENTRYPOINT,
            "PYTHONPATH": os.pathsep.join(
                [str(world.tmp_path / "site"), *filter(None, [os.environ.get("PYTHONPATH")])]
            ),
        },
        app_dir=CALIBER_DIR,
        log_path=world.tmp_path / "owner.log",
    )

    with server:
        deadline = time.monotonic() + 60
        run = world.run_row(run_id)
        while run.status in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.5)
            run = world.run_row(run_id)
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), (
            run.status,
            run.error_summary,
            server.log_tail(30),
        )
    assert kind in str(run.error_summary)
    assert world.effects().mutations == before.mutations
    assert not world.marker.exists(), "the owner process never imported the attacker's module"


# -- races -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_a_draft_edited_into_hostile_code_after_it_validated_is_refused_at_publish(
    world: World, kind: str
) -> None:
    """Validate-then-edit: the draft was clean when the author validated it, and the edit makes it hostile. Publish checks the
    manifest it is about to publish, not the one that was validated, so the late edit is refused and the version stays a draft."""
    workflow_id = world.new_workflow()
    version_id = world.draft(workflow_id, make_manifest(workflow_id))
    assert world.call("POST", f"/workflow-versions/{version_id}/validate").json()["data"]["errors"] == []
    current_hash = world.call("GET", f"/workflow-versions/{version_id}").json()["data"]["manifest_hash"]
    before = world.effects()

    edited = world.call(
        "PATCH",
        f"/workflow-versions/{version_id}",
        {"manifest": code_manifest(kind, workflow_id), "manifest_hash": current_hash},
    )
    published = world.call("POST", f"/workflow-versions/{version_id}/publish")

    assert edited.status_code == 200, edited.text
    if world.reference:
        assert published.status_code == 400, published.text
        _refusal_in(published)
        assert world.version_status(version_id) == "draft"
    else:
        assert published.status_code == 200, published.text
    assert world.effects() == before


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_many_concurrent_publishes_and_promotions_of_hostile_code_all_refuse(
    world: World, kind: str
) -> None:
    """Eight callers publish the same hostile draft at once and eight promote the same published one: every call is refused
    (none wins a race to the alias) and nothing runs. PostgreSQL only: SQLite serialises its writers."""
    if not world.reference:
        pytest.skip("concurrent writers are proven on PostgreSQL; SQLite serializes every writer")
    workflow_id = world.new_workflow()
    draft_id = world.draft(workflow_id, code_manifest(kind, workflow_id))
    hostile = world.seeded_hostile(kind)
    before = world.effects()
    outcomes: list[tuple[str, int]] = []
    lock = threading.Lock()

    def attempt(label: str, method: str, path: str, body: Any = None) -> None:
        response = world.call(method, path, body)
        with lock:
            outcomes.append((label, response.status_code))

    threads = [
        threading.Thread(target=attempt, args=("publish", "POST", f"/workflow-versions/{draft_id}/publish"))
        for _ in range(8)
    ] + [
        threading.Thread(
            target=attempt,
            args=(
                "promote",
                "POST",
                f"/workflows/{hostile.workflow_id}/deployments/staging/promote",
                {"version_id": hostile.version_id},
            ),
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert len(outcomes) == 16
    assert {status for _, status in outcomes} == {400}, outcomes
    assert world.version_status(draft_id) == "draft"
    with world.app.session_factory() as session:
        aliases = {
            d.alias
            for d in session.query(CaliberWorkflowDeployment).filter_by(workflow_id=hostile.workflow_id)
        }
    assert aliases == {hostile.alias}
    assert world.effects() == before


# -- uploads, unknown kinds, dormant and nested code -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["../../egi-traversal-a.txt", "/tmp/egi-traversal-b.txt", "..\\..\\egi-traversal-c.txt", "a/../../egi-traversal-d.txt"],
)
def test_an_upload_named_with_a_traversal_is_never_written_outside_the_storage_root(
    world: World, filename: str
) -> None:
    """The staging upload takes the file's name from the client; whatever the platform does with it (refuse or sanitise), no
    file lands outside the configured workflow storage."""
    stem = filename.replace("\\", "/").rsplit("/", 1)[-1]
    storage = world.tmp_path / "workspaces"
    headers = world.app.headers(EDITOR_A, PROJECT_A)
    token = _csrf_token(world, headers)

    response = world.app.client.post(
        f"{PREFIX}/workflow-files",
        files={"file": (filename, b"payload", "text/plain")},
        data={"kind": "input"},
        headers={**headers, "X-CALIBER-CSRF": token},
    )

    assert response.status_code in (200, 201, 400, 422), response.text
    escaped = [
        path
        for root in (world.tmp_path, world.tmp_path.parent, Path("/tmp"))
        for path in root.glob(f"**/{stem}" if root == world.tmp_path else stem)
        if storage not in path.parents
    ]
    assert escaped == [], f"a file named {stem} was written outside the storage root: {escaped}"


@pytest.mark.parametrize("node_type", ["bash_exec", "shell", "python", "Python_Code", "python_code ", "eval"])
def test_a_node_kind_the_registry_does_not_list_is_never_stored_or_run(
    world: World, node_type: str
) -> None:
    """An unknown kind (a new name for code, a case or whitespace variant of a known one) fails closed at the manifest."""
    workflow_id = world.new_workflow()
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["code"] = {"id": "code", "type": node_type, "code": "return {}"}
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "code", "map": {"msg": "input"}},
        {"id": "e2", "from": "code", "to": "final", "map": {"text": "response"}},
    ]
    before = world.count(CaliberWorkflowVersion)

    response = world.call("POST", f"/workflows/{workflow_id}/versions", {"manifest": manifest})

    assert response.status_code == 400, response.text
    assert world.count(CaliberWorkflowVersion) == before, "nothing was stored"


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_a_code_node_that_nothing_connects_to_is_still_refused(world: World, kind: str) -> None:
    """The check walks the whole graph, not the nodes reachable from Start: an edit that wires a dormant code node in later
    must not be the first time the profile looks at it."""
    workflow_id = world.new_workflow()
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["code"] = code_manifest(kind, workflow_id)["nodes"]["code"]
    version_id = world.draft(workflow_id, manifest)  # start -> agent -> final, plus an orphan code node
    before = world.effects()

    validated = world.call("POST", f"/workflow-versions/{version_id}/validate")
    published = world.call("POST", f"/workflow-versions/{version_id}/publish")

    if world.reference:
        assert REFUSAL_CODE in [e["code"] for e in validated.json()["data"]["errors"]]
        assert published.status_code == 400, published.text
        _refusal_in(published)
    else:
        assert published.status_code == 200, published.text
    assert world.effects() == before


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_a_parent_workflow_that_calls_a_hostile_child_fails_by_the_same_code(
    world: World, kind: str
) -> None:
    """The parent is clean (a subworkflow node), the child it names holds code: the run of the parent fails by the one code
    and the child's node never runs."""
    child = world.seeded_hostile(kind, alias="child")
    workflow_id = world.new_workflow("EGI attack parent")
    manifest = make_manifest(workflow_id)
    manifest["nodes"]["child"] = {
        "id": "child",
        "type": "subworkflow",
        "workflow_id": child.workflow_id,
        "alias": child.alias,
        "inputs": {"input": {"type": "string"}},
        "outputs": {"text": {"type": "string"}},
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "child", "map": {"msg": "input"}},
        {"id": "e2", "from": "child", "to": "final", "map": {"text": "response"}},
    ]
    version_id = world.draft(workflow_id, manifest)
    published = world.call("POST", f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    before = world.effects()

    accepted = world.queue_run({"workflow_version_id": version_id})
    assert accepted.status_code == 202, accepted.text
    run_id = str(accepted.json()["data"]["workflow_run_id"])
    world.work()

    run = world.run_row(run_id)
    if world.reference:
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), run.error_summary
        assert world.effects() == before
    else:
        assert run.status == "completed", run.error_summary
        assert world.effects() != before


@pytest.mark.parametrize("kind", CODE_KINDS)
def test_a_published_service_over_a_hostile_deployment_runs_nothing_when_invoked(
    world: World, kind: str
) -> None:
    """``POST /services/{workflow_id}/invoke`` with a service token: the caller needs no editor role at all, only the token an
    operator issued. The run it admits fails by the one code at the worker and the node never runs."""
    hostile = world.seeded_hostile(kind, alias="service", environment="development")
    seed_release(world.app, PROJECT_A)
    published = world.call(
        "POST", f"/workflows/{hostile.workflow_id}/service", {"alias": hostile.alias}
    )
    assert published.status_code == 200, published.text
    issued = world.call("POST", f"/workflows/{hostile.workflow_id}/service/tokens", {"name": "attack"})
    assert issued.status_code in (200, 201), issued.text
    token = str(issued.json()["data"]["token"])
    before = world.effects()

    caller = {"Authorization": f"Bearer {token}", **world.app.proxy_headers}
    invoked = world.app.client.post(
        f"{PREFIX}/services/{hostile.workflow_id}/invoke",
        json={"input": {"msg": "go"}},
        headers={**caller, "X-CALIBER-CSRF": _csrf_token(world, caller)},
    )

    assert invoked.status_code in (200, 202), invoked.text
    run_id = str(invoked.json()["data"]["run_id"])
    world.work()
    run = world.run_row(run_id)
    if world.reference:
        assert (run.status, run.error_code) == ("failed", REFUSAL_CODE), run.error_summary
        assert world.effects() == before
    else:
        assert run.status == "completed", run.error_summary
        assert world.effects() != before, "the control: the service run executed the node"
