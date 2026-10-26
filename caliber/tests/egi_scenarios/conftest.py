"""Fixtures shared by the EGI scenario tests (``caliber/tests/egi_scenarios/``).

``downstream`` starts the disposable system of record (``downstream/app.py``) as a real process and
hands tests the handle they measure effects through. #541 extends this file with ``reference_app``,
``two_workspaces`` and the fault barriers; the naming convention for scenario tests is
``test_vNN_<slug>.py``.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import httpx
import pytest

APP_PATH = Path(__file__).parent / "downstream" / "app.py"
#: How long ``start()`` waits for ``/health``. Raise it with the environment variable on a loaded CI runner.
START_TIMEOUT_SECONDS = float(os.environ.get("EGI_DOWNSTREAM_START_TIMEOUT", "5"))
STOP_TIMEOUT_SECONDS = 5.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


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
