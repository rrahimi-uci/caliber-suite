"""Run CALIBER as a real server process that a scenario can kill and start again.

A recovery scenario ("kill the worker after the downstream commit, restart it, the effect is not
repeated") needs an operating-system process that really dies, with its database and the downstream
system of record staying up. :class:`ServerProcess` starts ``uvicorn`` on a free local port with the
environment it is given, waits for the health endpoint, and can be killed and restarted on the same port.

The default target is ``caliber.server:create_app``; a test that needs a process to hit a fault barrier
before the platform places one (the dispatcher in #522 does) can pass a tiny ASGI app of its own.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import IO

import httpx

from caliber.testing.fault_barriers import ENV_VAR as FAULT_BARRIERS_ENV

#: The route CALIBER answers while it is up (``routes/health.py``).
HEALTH_PATH = "/ajax-api/2.0/mlflow/caliber/health"
#: Raise it with the variable on a slow CI runner; starting the full app imports a lot.
START_TIMEOUT_SECONDS = float(os.environ.get("EGI_SERVER_START_TIMEOUT", "30"))
EXIT_TIMEOUT_SECONDS = 10.0
KILL_TIMEOUT_SECONDS = 5.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ServerStartError(RuntimeError):
    """The server exited, or never answered its health endpoint, while starting."""


class ServerProcess:
    """``uvicorn <app> --factory`` as a subprocess on ``127.0.0.1:<port>``.

    The child runs with ``env`` and the test process's environment **minus every ``CALIBER_*`` variable**, so
    it is configured by ``env`` alone, the same as an in-process app built from that dict, and a variable in
    a developer's shell (an ambient ``CALIBER_FAULT_BARRIERS``, say) cannot change the run.
    ``app_dir`` is where ``app`` is imported from when it is not an installed module. ``log_path`` receives stdout and stderr of every start, appended, so a failed start can be read.
    """

    def __init__(
        self,
        env: dict[str, str],
        port: int | None = None,
        *,
        app: str = "caliber.server:create_app",
        factory: bool = True,
        health_path: str = HEALTH_PATH,
        app_dir: Path | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.env = dict(env)
        self.port = port if port is not None else free_port()
        self.app = app
        self.factory = factory
        self.health_path = health_path
        self.app_dir = app_dir
        self.log_path = log_path
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._proc: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None

    # -- lifecycle -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start the process and return once ``health_path`` answers 200; raise otherwise."""
        if self.running:
            raise RuntimeError("server already running")
        self._proc = None
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            self.app,
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
        ]
        if self.factory:
            command.append("--factory")
        if self.app_dir is not None:
            command += ["--app-dir", str(self.app_dir)]
        stream: IO[bytes] | int = subprocess.DEVNULL
        if self.log_path is not None:
            self._close_log()
            self._log = self.log_path.open("ab")
            stream = self._log
        self._proc = subprocess.Popen(
            command, env=self._child_environment(), stdout=stream, stderr=subprocess.STDOUT
        )
        try:
            self._wait_until_healthy()
        except ServerStartError:
            self._close_log()
            raise

    def _child_environment(self) -> dict[str, str]:
        inherited = {k: v for k, v in os.environ.items() if not k.startswith("CALIBER_")}
        return {**inherited, **self.env}

    def _wait_until_healthy(self) -> None:
        assert self._proc is not None
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise ServerStartError(
                    f"server exited with {self._proc.returncode} while starting\n{self.log_tail()}"
                )
            try:
                # The process is ours and alive, so an answer is not a stray listener on the port.
                if httpx.get(self.base_url + self.health_path, timeout=2.0).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        self.kill()
        raise ServerStartError(
            f"server did not answer {self.health_path} in {START_TIMEOUT_SECONDS}s\n{self.log_tail()}"
        )

    def kill(self) -> None:
        """SIGKILL the process and wait for it; harmless when it is already gone."""
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=KILL_TIMEOUT_SECONDS)
        self._close_log()

    def restart(self, *, disarm: bool = True) -> None:
        """Kill whatever is running and start a fresh process on the same port.

        The restarted process is not armed (``CALIBER_FAULT_BARRIERS`` is dropped from its environment):
        the usual scenario kills a process at a barrier and then needs a healthy one to recover with. Pass
        ``disarm=False`` to restart it armed again.
        """
        self.kill()
        if disarm:
            self.env.pop(FAULT_BARRIERS_ENV, None)
        self.start()

    def wait_for_exit(self, timeout: float = EXIT_TIMEOUT_SECONDS) -> int:
        """Wait for the process to end on its own (for example at a fault barrier); return its status."""
        assert self._proc is not None, "server was never started"
        try:
            status = self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"server still running after {timeout}s") from exc
        self._close_log()
        return status

    # -- state ---------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        """The status the process ended with, or ``None`` while it runs (or was never started)."""
        return None if self._proc is None else self._proc.poll()

    def log_tail(self, lines: int = 40) -> str:
        if self.log_path is None or not self.log_path.exists():
            return "(no log file)"
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    # -- context manager -----------------------------------------------------------------------

    def __enter__(self) -> ServerProcess:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.kill()
