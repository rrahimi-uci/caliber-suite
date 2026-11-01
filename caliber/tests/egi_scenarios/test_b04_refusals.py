"""B04.7 (#774), the public path: paths the inventory refuses are refused in the reference profile, before anything runs.

Two public entry points, driven the way an operator reaches them. The reference app's HTTP routes: the runnable-code
exports answer ``409`` with ``reason_code: unsupported_in_reference_profile`` and, because the refusal comes before the version
is looked up, the answer reveals nothing about which versions exist (the same request in the development profile is a plain
``404``). And the standalone tool-sandbox service, a real process: the unauthenticated service exits at startup in the
reference profile, binding no port, and starts and answers in the development profile.

The reference profile refuses SQLite, so on SQLite the app runs the development profile with the same safe settings (see
``reference_app``): the scenario asserts the refusal on PostgreSQL and the unchanged behaviour on SQLite, so both halves of the
control are exercised by the one test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.egi_scenarios.conftest import EDITOR_A, ReferenceApp
from tests.egi_scenarios.harness.process import free_port
from tests.workflow_helpers import PREFIX

CODE = "unsupported_in_reference_profile"
CALIBER_DIR = Path(__file__).resolve().parents[2]
START_TIMEOUT_SECONDS = 30.0


@pytest.mark.parametrize("route", ["export/python", "export/deployment-bundle"])
def test_runnable_code_exports_are_refused_in_the_reference_profile(
    reference_app: ReferenceApp, route: str
) -> None:
    response = reference_app.client.get(
        f"{PREFIX}/workflow-versions/WFV-does-not-exist/{route}",
        headers=reference_app.headers(EDITOR_A),
    )

    if reference_app.profile == "reference":
        assert response.status_code == 409
        body = response.json()
        assert body["reason_code"] == CODE
        assert body["detail"].startswith(f"{CODE}: EP-04")
        assert "CALIBER_PROFILE=development" in body["detail"]
        # Nothing of the request, the version or the export is echoed.
        assert "WFV-does-not-exist" not in response.text
    else:
        assert response.status_code == 404, "development: the export routes are unchanged"


def _sandbox_process(profile: str, port: int) -> subprocess.Popen[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CALIBER_")}
    env["CALIBER_PROFILE"] = profile
    return subprocess.Popen(
        [sys.executable, "-m", "caliber.tool_sandbox", "--port", str(port)],
        cwd=CALIBER_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _answers(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200
    except httpx.TransportError:
        return False


def test_the_unauthenticated_sandbox_service_does_not_start_in_the_reference_profile() -> None:
    port = free_port()
    process = _sandbox_process("reference", port)
    try:
        output, _ = process.communicate(timeout=START_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode not in (None, 0), "the process must exit, not keep running"
    assert f"{CODE}: EP-029" in output, output
    assert "CALIBER_PROFILE=development" in output, output
    assert not _answers(port), (
        "nothing may be listening: the refusal comes before the port is bound"
    )


def test_the_sandbox_service_still_starts_and_runs_a_tool_in_the_development_profile() -> None:
    port = free_port()
    process = _sandbox_process("development", port)
    try:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not _answers(port):
            assert process.poll() is None, "the development service exited at startup"
            time.sleep(0.1)
        assert _answers(port)
        response = httpx.post(
            f"http://127.0.0.1:{port}/v1/tool-sandbox/run",
            json={"source_code": "def f(): return 7", "callable_name": "f"},
            timeout=30.0,
        )
        assert response.status_code == 200
        assert response.json()["data"]["output"] == 7
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()
