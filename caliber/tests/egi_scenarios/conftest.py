"""Fixtures shared by the EGI scenario tests (``caliber/tests/egi_scenarios/``).

``downstream`` starts the disposable system of record (``downstream/app.py``) as a real process and
hands tests the handle they measure effects through. ``reference_app`` is a CALIBER app on the reference
profile (see its docstring for the one SQLite difference), ``two_workspaces`` seeds two projects and their
users, and ``scripted_model`` replays the two model turns of the document-request journey. Killing a
real CALIBER process at a fault barrier is ``harness/process.py``. The naming convention for scenario
tests is ``test_vNN_<slug>.py`` (see README.md).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import httpx
import pytest
from alembic import command
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.migrate import build_config
from caliber.db.models import CaliberProject, CaliberProjectMember
from caliber.server import create_app
from tests.dialect_helpers import DIALECTS, dialect_database_url
from tests.egi_scenarios.harness.process import free_port
from tests.workflow_helpers import seed_workspace_release

APP_PATH = Path(__file__).parent / "downstream" / "app.py"
#: How long ``start()`` waits for ``/health``. Raise it with the environment variable on a loaded CI runner.
START_TIMEOUT_SECONDS = float(os.environ.get("EGI_DOWNSTREAM_START_TIMEOUT", "5"))
STOP_TIMEOUT_SECONDS = 5.0


class Downstream:
    """A running downstream process and the helpers tests use to measure it.

    It runs ``downstream/app.py`` by path (``caliber.tests`` is not an importable package) on a free
    port with its state in ``db_path``, so :meth:`restart` keeps the state and :meth:`reset` clears it.
    """

    def __init__(self, workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.db_path = str(workdir / "downstream.db")
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._log_path = workdir / "downstream.log"
        self._log: IO[bytes] | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.client = httpx.Client(base_url=self.base_url, timeout=10.0)

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        assert self._proc is None, "already started"
        self._log = self._log_path.open("ab")
        self._proc = subprocess.Popen(
            [sys.executable, str(APP_PATH), "--port", str(self.port), "--db", self.db_path],
            env={**os.environ, "EGI_DOWNSTREAM_DB": self.db_path},
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"downstream exited at startup:\n{self._log_path.read_text()}")
            try:
                if self.client.get("/health").status_code == 200 and self._proc.poll() is None:
                    # Our process is alive and something answered: not a stray process on the port.
                    return
            except httpx.TransportError:
                time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"downstream did not answer /health in {START_TIMEOUT_SECONDS}s")

    def stop(self) -> None:
        """SIGTERM, wait, then SIGKILL; afterwards the process is gone."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc is not None:
            assert proc.poll() is not None, "downstream process survived stop()"
        if self._log is not None:
            self._log.close()
            self._log = None

    def restart(self) -> None:
        """Kill and start again on the same port and database file."""
        self.stop()
        self.start()

    def close(self) -> None:
        self.stop()
        self.client.close()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- what tests measure with ---------------------------------------------------------------

    def mutations(self) -> int:
        """Committed mutations, counted by the downstream itself (not by CALIBER's logs)."""
        return int(self.client.get("/_test/mutations").json()["count"])

    def query(self, idempotency_key: str) -> dict[str, Any] | None:
        response = self.client.get(
            "/document-requests", params={"idempotency_key": idempotency_key}
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    def reset(self) -> None:
        self.client.post("/_test/reset").raise_for_status()

    def set_fault(self, fault: str, seconds: float = 0) -> None:
        self.client.post(
            "/_test/faults", json={"fault": fault, "seconds": seconds}
        ).raise_for_status()


@pytest.fixture(scope="session")
def downstream(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Downstream]:
    handle = Downstream(tmp_path_factory.mktemp("downstream"))
    try:
        handle.start()
        yield handle
    finally:
        handle.close()


@pytest.fixture(autouse=True)
def _fresh_downstream(request: pytest.FixtureRequest) -> None:
    """A test that uses ``downstream`` starts from the seed: version 1, no requests, no armed fault.

    The process is shared for the session, so without this a test would see the previous test's
    mutation count, request ids and case version, and its assertions would depend on test order.
    """
    if "downstream" in request.fixturenames:
        request.getfixturevalue("downstream").reset()


# ------------------------------------------------------------------------------ reference app

PROXY_SECRET_ENV = "EGI_TEST_PROXY_SECRET"
CSRF_SECRET_ENV = "CALIBER_CSRF_SIGNING_SECRET"
PROXY_SECRET = "p" * 40
CSRF_SECRET = "c" * 48

PROJECT_A = "egi-project-a"
PROJECT_B = "egi-project-b"
EDITOR_A = "editor_a"
APPROVER_A = "approver_a"
EDITOR_B = "editor_b"


def reference_environment(database_url: str, profile: str, workdir: Path) -> dict[str, str]:
    """The environment of a safe reference deployment (every setting the profile checks is safe).

    It is the single source for the in-process app and for a :class:`ServerProcess` started from the same
    test, so both run the same configuration. Providers are the offline ones: scenarios measure effects at
    the downstream, not at a model or an MLflow registry.
    """
    return {
        "CALIBER_PROFILE": profile,
        "CALIBER_DATABASE_URL": database_url,
        "CALIBER_AUTH_MODE": "trusted_header",
        "CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV": PROXY_SECRET_ENV,
        PROXY_SECRET_ENV: PROXY_SECRET,
        "CALIBER_CSRF_ENABLED": "true",
        "CALIBER_CSRF_SIGNING_SECRET_ENV": CSRF_SECRET_ENV,
        CSRF_SECRET_ENV: CSRF_SECRET,
        "CALIBER_AUTH_DEV_FALLBACK_ENABLED": "false",
        "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT": "false",
        "CALIBER_EGRESS_POLICY_ENABLED": "true",
        "CALIBER_MCP_ALLOW_INSECURE_HTTP": "false",
        # The reference profile lists no MCP command or host (B12.3b, #746): empty, not the interpreter/loopback defaults.
        "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST": "",
        "CALIBER_MCP_REMOTE_HOST_ALLOWLIST": "",
        "CALIBER_APPROVAL_ALLOW_SELF_APPROVAL": "false",
        "CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER": "true",
        "CALIBER_OPERATOR_USERS": f"{EDITOR_A},{EDITOR_B}",
        "CALIBER_APPROVER_USERS": APPROVER_A,
        "CALIBER_LLM_PROVIDER": "fake",
        "CALIBER_EVAL_PROVIDER": "fake",
        "CALIBER_PROMOTER_PROVIDER": "fake",
        "CALIBER_ARTIFACT_STORE_PROVIDER": "fake",
        "CALIBER_ALLOW_SIMULATED_PROVIDERS": "true",
        "CALIBER_ASSISTANT_ENGINE": "fake",
        "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
        "CALIBER_WORKFLOW_STORAGE_BASE_URI": f"file://{workdir / 'workspaces'}",
    }


@dataclass
class ReferenceApp:
    """A migrated CALIBER app and the settings it runs on.

    ``env`` is what a :class:`ServerProcess` needs to run the same configuration in another process.
    """

    dialect: str
    profile: str
    database_url: str
    env: dict[str, str]
    config: CaliberConfig
    client: TestClient
    session_factory: sessionmaker[Session]
    proxy_headers: dict[str, str] = field(default_factory=dict)

    def headers(self, user: str, project: str | None = None) -> dict[str, str]:
        """Request headers that act as ``user`` (in ``project``), through the trusted proxy."""
        headers = {**self.proxy_headers, "X-CALIBER-User": user}
        if project:
            headers["X-CALIBER-Project"] = project
        return headers


@pytest.fixture(params=DIALECTS)
def reference_app(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ReferenceApp]:
    """A CALIBER app on a migrated database, once per dialect.

    On PostgreSQL it runs ``CALIBER_PROFILE=reference``. The reference profile refuses SQLite, so the
    SQLite instance runs the ``development`` profile with the very same safe settings: it exercises the
    scenario logic on a laptop, and the PostgreSQL instance (the "EGI scenarios (PostgreSQL)" CI job) is
    the one that proves the profile.
    """
    dialect: str = request.param
    profile = "reference" if dialect == "postgresql" else "development"
    with dialect_database_url(dialect, tmp_path, "egi_reference") as url:
        alembic = build_config()
        alembic.set_main_option("sqlalchemy.url", url)
        command.upgrade(alembic, "head")
        env = reference_environment(url, profile, tmp_path)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        config = CaliberConfig.load(environ=env)
        app = create_app(config)
        with TestClient(app) as client:
            yield ReferenceApp(
                dialect=dialect,
                profile=profile,
                database_url=url,
                env=env,
                config=config,
                client=client,
                session_factory=app.state.session_factory,
                proxy_headers={"X-CALIBER-Proxy-Secret": PROXY_SECRET},
            )


@contextmanager
def holding_sqlite_write_lock(app: ReferenceApp, *, busy_ms: int = 150) -> Iterator[None]:
    """Hold SQLite's one write lock on the app's database from a second connection, with the app's busy timeout shortened.

    The app's engine waits 30 s for the lock (``create_engine_from_config``); here its new connections wait ``busy_ms``, so a
    request that needs to write while the lock is held fails fast with ``database is locked``, which is exactly what a
    held lock does after 30 s. The lock is released, and the timeout restored, when the block ends.
    """
    assert app.dialect == "sqlite"
    engine = app.session_factory.kw["bind"]

    def shorten(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")

    event.listen(engine, "connect", shorten)
    engine.dispose()  # connections made from here on carry the short timeout
    holder = sqlite3.connect(app.database_url.removeprefix("sqlite:///"), isolation_level=None)
    try:
        holder.execute(
            "BEGIN IMMEDIATE"
        )  # the RESERVED lock: readers go on, any other writer is told the file is locked
        yield
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        event.remove(engine, "connect", shorten)
        engine.dispose()


@dataclass(frozen=True)
class Workspaces:
    project_a: str
    project_b: str
    editor_a: str
    approver_a: str
    editor_b: str


@pytest.fixture
def two_workspaces(reference_app: ReferenceApp) -> Workspaces:
    """Project A (``editor_a`` and ``approver_a``) and project B (``editor_b``), seeded in the database."""
    with reference_app.session_factory() as session:
        session.add_all(
            [
                CaliberProject(project_id=PROJECT_A, name="EGI project A", owner="@egi"),
                CaliberProject(project_id=PROJECT_B, name="EGI project B", owner="@egi"),
            ]
        )
        session.flush()
        for member, user, project in (
            ("egi-m-editor-a", EDITOR_A, PROJECT_A),
            ("egi-m-approver-a", APPROVER_A, PROJECT_A),
            ("egi-m-editor-b", EDITOR_B, PROJECT_B),
        ):
            session.add(
                CaliberProjectMember(
                    member_id=member,
                    project_id=project,
                    user_id=user,
                    role="editor",
                    created_by="@egi",
                )
            )
        session.commit()
    return Workspaces(PROJECT_A, PROJECT_B, EDITOR_A, APPROVER_A, EDITOR_B)


def seed_release(app: ReferenceApp, project_id: str, name: str = "egi") -> dict[str, str]:
    """An approved, current workspace release and its environment in ``project_id``: the runtime lineage coordinates
    a run names in the reference profile. Returns the two request fields (``workspace_release_id``, ``environment_id``)."""
    with app.session_factory() as session:
        coordinates = seed_workspace_release(session, project_id, name)
        session.commit()
        return coordinates


# ------------------------------------------------------------------------------ scripted model


@dataclass
class ScriptedModel:
    """The two model turns of the document-request journey, the same on every run.

    The first turn proposes the tool call ``request_missing_document(case_id, document_type)``; the
    second reports ``completed``. A scenario feeds these to whatever consumes model turns (the agent step
    arrives with #524), so no scenario depends on a live model and every proposal is reproducible.
    """

    case_id: str = "MORT-2026-000184"
    document_type: str = "pay_statement"
    calls: list[dict[str, Any]] = field(default_factory=list)

    def next_turn(self) -> dict[str, Any]:
        if not self.calls:
            turn: dict[str, Any] = {
                "type": "tool_call",
                "name": "request_missing_document",
                "arguments": {"case_id": self.case_id, "document_type": self.document_type},
            }
        else:
            turn = {"type": "completed", "summary": f"requested {self.document_type}"}
        self.calls.append(turn)
        return turn


@pytest.fixture
def scripted_model() -> ScriptedModel:
    return ScriptedModel()
