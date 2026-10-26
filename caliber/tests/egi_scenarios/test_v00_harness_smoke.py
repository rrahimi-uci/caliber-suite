"""The scenario harness works: fixtures boot, a barrier kills a real process, the process restarts.

This file owns no scenario claim. It proves the infrastructure the V01–V35 scenarios stand on, so a
failure in a scenario can be told apart from a failure of the harness.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from caliber.db.models import CaliberProjectMember
from caliber.testing import fault_barriers
from caliber.testing.fault_barriers import BARRIERS, ENV_VAR, EXIT_CODE, armed_barriers, barrier
from tests.egi_scenarios import conftest as scenario_conftest
from tests.egi_scenarios.conftest import (
    APPROVER_A,
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    PROJECT_B,
    Downstream,
    ReferenceApp,
    ScriptedModel,
    Workspaces,
)
from tests.egi_scenarios.harness import process as process_module
from tests.egi_scenarios.harness.process import ServerProcess, ServerStartError

CALIBER_DIR = Path(__file__).resolve().parents[2]
BARRIER_APP = "tests.egi_scenarios.harness.barrier_app:app"
CASE = "MORT-2026-000184"


def barrier_server(armed: str, tmp_path: Path) -> ServerProcess:
    return ServerProcess(
        {ENV_VAR: armed},
        app=BARRIER_APP,
        factory=False,
        health_path="/health",
        app_dir=CALIBER_DIR,
        log_path=tmp_path / "barrier-server.log",
    )


# ---------------------------------------------------------------- the fixtures boot


def test_fixtures_boot(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    scripted_model: ScriptedModel,
    downstream: Downstream,
) -> None:
    health = reference_app.client.get("/ajax-api/2.0/mlflow/caliber/health")
    assert health.status_code == 200
    assert reference_app.profile == (
        "reference" if reference_app.dialect == "postgresql" else "development"
    )
    assert reference_app.config.profile == reference_app.profile

    assert (two_workspaces.project_a, two_workspaces.project_b) == (PROJECT_A, PROJECT_B)
    with reference_app.session_factory() as session:
        members = dict(
            session.execute(
                select(CaliberProjectMember.user_id, CaliberProjectMember.project_id)
            ).all()
        )
    assert members == {EDITOR_A: PROJECT_A, APPROVER_A: PROJECT_A, EDITOR_B: PROJECT_B}

    first = scripted_model.next_turn()
    assert first["name"] == "request_missing_document"
    assert first["arguments"] == {"case_id": CASE, "document_type": "pay_statement"}
    assert scripted_model.next_turn()["type"] == "completed"

    assert downstream.mutations() == 0


def test_the_reference_app_acts_as_a_user_of_a_project(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    """The trusted proxy secret and the user header are what the app needs to know who is calling."""
    assert reference_app.headers(EDITOR_A) == {
        "X-CALIBER-Proxy-Secret": scenario_conftest.PROXY_SECRET,
        "X-CALIBER-User": EDITOR_A,
    }
    assert reference_app.headers(EDITOR_A, PROJECT_A)["X-CALIBER-Project"] == PROJECT_A
    with reference_app.session_factory() as session:
        count = session.execute(select(func.count()).select_from(CaliberProjectMember)).scalar_one()
    assert count == 3


# ---------------------------------------------------------------- barriers


def test_barrier_exits_only_when_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(fault_barriers.os, "_exit", exits.append)

    monkeypatch.delenv(ENV_VAR, raising=False)
    barrier("before_dispatch")
    assert exits == []

    monkeypatch.setenv(ENV_VAR, " after_outbox_commit , before_dispatch ,, ")
    assert armed_barriers() == {"after_outbox_commit", "before_dispatch"}
    barrier("before_receipt_persist")
    assert exits == [], "a barrier that is not in the list stays inert"
    barrier("before_dispatch")
    assert exits == [EXIT_CODE] == [137]

    monkeypatch.setenv(ENV_VAR, "   ")
    assert armed_barriers() == frozenset()


def test_the_documented_barriers_are_the_five_the_plan_names() -> None:
    assert BARRIERS == (
        "before_dispatch",
        "after_downstream_commit",
        "before_receipt_persist",
        "after_outbox_commit",
        "after_evaluation_claim",
    )


@pytest.mark.parametrize(
    ("armed", "expected_status", "expected_output"),
    [("", 0, "alive"), ("other", 0, "alive"), ("x", EXIT_CODE, "")],
)
def test_barrier_in_a_subprocess(armed: str, expected_status: int, expected_output: str) -> None:
    """A real process: unarmed it carries on, armed it dies at the call and prints nothing after it."""
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\nfrom caliber.testing.fault_barriers import barrier\n"
            "barrier('x')\nsys.stdout.write('alive')\nsys.stdout.flush()",
        ],
        env={**__import__("os").environ, ENV_VAR: armed},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == expected_status
    assert done.stdout == expected_output


# ---------------------------------------------------------------- the process harness


def test_barrier_kills_and_restarts(downstream: Downstream, tmp_path: Path) -> None:
    created = downstream.client.post(
        f"/cases/{CASE}/document-requests",
        json={"document_type": "pay_statement"},
        headers={"Idempotency-Key": "harness-1"},
    )
    assert created.status_code == 201
    assert downstream.mutations() == 1

    with barrier_server("before_dispatch", tmp_path) as server:
        assert server.running and server.exit_code is None
        unarmed = httpx.get(f"{server.base_url}/barrier/after_outbox_commit", timeout=5)
        assert unarmed.status_code == 200, "only the armed barrier kills the process"

        with pytest.raises(httpx.TransportError):
            httpx.get(f"{server.base_url}/barrier/before_dispatch", timeout=5)
        assert server.wait_for_exit() == EXIT_CODE
        assert not server.running

        server.restart()
        assert server.running and server.exit_code is None
        assert httpx.get(f"{server.base_url}/health", timeout=5).status_code == 200
        assert ENV_VAR not in server.env, "the restarted process is not armed"
        # The same barrier no longer kills it, and the downstream outlived the whole episode.
        assert httpx.get(f"{server.base_url}/barrier/before_dispatch", timeout=5).status_code == 200
    assert downstream.mutations() == 1
    assert downstream.query("harness-1") is not None


def test_restart_can_keep_the_barrier_armed(tmp_path: Path) -> None:
    server = barrier_server("before_dispatch", tmp_path)
    try:
        server.start()
        server.restart(disarm=False)
        assert server.env[ENV_VAR] == "before_dispatch"
        with pytest.raises(httpx.TransportError):
            httpx.get(f"{server.base_url}/barrier/before_dispatch", timeout=5)
        assert server.wait_for_exit() == EXIT_CODE
    finally:
        server.kill()


def test_a_server_that_exits_while_starting_reports_why(tmp_path: Path) -> None:
    server = ServerProcess(
        {},
        app="no_such_module_for_egi:app",
        factory=False,
        health_path="/health",
        app_dir=CALIBER_DIR,
        log_path=tmp_path / "broken.log",
    )
    with pytest.raises(ServerStartError, match="exited with") as failure:
        server.start()
    assert "no_such_module_for_egi" in str(failure.value)
    assert server.exit_code not in (None, 0)
    assert not server.running


def test_a_server_that_never_answers_health_is_killed_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process_module, "START_TIMEOUT_SECONDS", 1.0)
    server = ServerProcess(
        {},
        app=BARRIER_APP,
        factory=False,
        health_path="/no-such-route",
        app_dir=CALIBER_DIR,
        log_path=tmp_path / "silent.log",
    )
    with pytest.raises(ServerStartError, match="did not answer /no-such-route"):
        server.start()
    assert not server.running


def test_the_lifecycle_calls_are_safe_out_of_order(tmp_path: Path) -> None:
    server = ServerProcess(
        {}, app=BARRIER_APP, factory=False, health_path="/health", app_dir=CALIBER_DIR
    )
    assert not server.running
    assert server.exit_code is None
    assert server.log_tail() == "(no log file)"
    server.kill()  # nothing to kill is fine
    with pytest.raises(AssertionError, match="never started"):
        server.wait_for_exit()
    try:
        server.start()
        with pytest.raises(RuntimeError, match="already running"):
            server.start()
        with pytest.raises(TimeoutError):
            server.wait_for_exit(timeout=0.2)
    finally:
        server.kill()
    assert server.exit_code == -9, "kill() is a SIGKILL"
    server.kill()  # twice is fine


def test_log_tail_reads_the_end_of_the_log(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("\n".join(f"line {n}" for n in range(100)))
    server = ServerProcess({}, log_path=log)
    assert server.log_tail(lines=2) == "line 98\nline 99"
    assert ServerProcess({}, log_path=tmp_path / "missing.log").log_tail() == "(no log file)"


# ---------------------------------------------------------------- the real app, in its own process


def test_reference_app_environment_starts_a_real_process(
    reference_app: ReferenceApp, tmp_path: Path
) -> None:
    """The environment the fixture builds is a complete, bootable configuration, not only in-process."""
    with ServerProcess(
        reference_app.env, app_dir=CALIBER_DIR, log_path=tmp_path / "caliber.log"
    ) as server:
        assert httpx.get(server.base_url + process_module.HEALTH_PATH, timeout=5).status_code == 200


def test_an_armed_process_refuses_to_start_on_the_reference_profile(
    reference_app: ReferenceApp, tmp_path: Path
) -> None:
    env = {**reference_app.env, "CALIBER_PROFILE": "reference", ENV_VAR: "before_dispatch"}
    server = ServerProcess(env, app_dir=CALIBER_DIR, log_path=tmp_path / "refused.log")
    with pytest.raises(ServerStartError) as failure:
        server.start()
    assert "fault barriers are armed (before_dispatch)" in str(failure.value)
    assert server.exit_code not in (None, 0)
