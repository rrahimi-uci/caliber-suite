"""V14: the reference profile runs exactly one execution owner; every other process serves HTTP only (B13.3, #673).

Two real CALIBER processes share one PostgreSQL database in the reference profile. The first to start holds the
execution-owner lease and runs the owner-only loops; the second reports ``secondary``, starts no loop and never
heartbeats. Killing the owner (SIGKILL, no release) lets the secondary take the lease over when it expires.

The scenario needs the reference profile, which refuses SQLite, so it runs on PostgreSQL only (the "EGI scenarios
(PostgreSQL)" job; locally ``scripts/ci-local.sh egi-scenarios-postgres``).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tests.egi_scenarios.conftest import ReferenceApp
from tests.egi_scenarios.harness.process import ServerProcess

pytestmark = pytest.mark.xdist_group("egi-scenario-processes")

CALIBER_DIR = Path(__file__).resolve().parents[2]
READINESS = "/ajax-api/2.0/mlflow/caliber/readiness"
LEASE_SECONDS = 3.0
#: The issue's bound is 2 x the lease; polling over HTTP on a loaded runner needs a little slack on top.
TAKEOVER_BOUND_SECONDS = 2 * LEASE_SECONDS + 3.0


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
        processes = _heartbeat_processes(reference_app)
        assert len(processes) <= 1, (
            f"owner-only workers heartbeat from one process only, saw {processes}"
        )
        assert processes <= {owner_pid}, "and that process is the lease holder"

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
