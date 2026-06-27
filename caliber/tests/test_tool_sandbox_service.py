"""Tests for the standalone tool sandbox service."""

from __future__ import annotations

from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.tool_sandbox.models import (
    ToolSandboxRunRequest,
    ToolSandboxTestCase,
    ToolSandboxTestSuiteRequest,
)
from caliber.tool_sandbox.server import RUN_PATH, TESTS_PATH, create_app
from caliber.tool_sandbox.service import LocalSubprocessToolSandbox


def _sandbox() -> LocalSubprocessToolSandbox:
    # A generous default so a cold ``python -I`` subprocess start doesn't flake
    # under full-suite load (the machine is busy spawning/importing across ~1800
    # tests, and the interpreter still loads the venv site/.pth on startup). The
    # dedicated timeout test overrides this per-request with ``timeout_seconds=0.1``,
    # so this only affects the success-path tests.
    return LocalSubprocessToolSandbox(default_timeout_seconds=30.0, max_output_bytes=4_000)


def test_local_subprocess_sandbox_runs_tool() -> None:
    result = _sandbox().run_tool(
        ToolSandboxRunRequest(
            source_code="""
def add(x, y=0):
    print("running add")
    return {"sum": x + y}
""",
            callable_name="add",
            input={"x": 2, "y": 3},
        )
    )

    assert result.status == "completed"
    assert result.output == {"sum": 5}
    assert "running add" in result.stdout


def test_local_subprocess_sandbox_blocks_imports_by_default() -> None:
    result = _sandbox().run_tool(
        ToolSandboxRunRequest(
            source_code="""
import os

def cwd():
    return {"cwd": os.getcwd()}
""",
            callable_name="cwd",
        )
    )

    assert result.status == "failed"
    assert result.error is not None
    assert "__import__" in result.error


def test_local_subprocess_sandbox_times_out() -> None:
    result = _sandbox().run_tool(
        ToolSandboxRunRequest(
            source_code="""
def loop():
    while True:
        pass
""",
            callable_name="loop",
            timeout_seconds=0.1,
        )
    )

    assert result.status == "timed_out"
    assert "timed out" in (result.error or "")


def test_local_subprocess_sandbox_runs_tool_tests() -> None:
    result = _sandbox().run_tests(
        ToolSandboxTestSuiteRequest(
            source_code="""
def double(x):
    return {"value": x * 2}
""",
            callable_name="double",
            tests=[
                ToolSandboxTestCase(name="doubles two", input={"x": 2}, expected={"value": 4}),
                ToolSandboxTestCase(name="catches mismatch", input={"x": 3}, expected={"value": 9}),
            ],
        )
    )

    assert result.status == "failed"
    assert [test.status for test in result.tests] == ["passed", "failed"]
    assert result.tests[1].error == "output did not match expected value"


def test_sandbox_http_service_runs_tool() -> None:
    app = create_app(
        config=CaliberConfig.load(environ={}),
        sandbox=_sandbox(),
    )
    with TestClient(app) as client:
        response = client.post(
            RUN_PATH,
            json={
                "source_code": "def greet(name):\n    return {'message': 'hi ' + name}",
                "callable_name": "greet",
                "input": {"name": "Reza"},
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "completed"
    assert response.json()["data"]["output"] == {"message": "hi Reza"}


def test_sandbox_http_service_runs_tests() -> None:
    app = create_app(
        config=CaliberConfig.load(environ={}),
        sandbox=_sandbox(),
    )
    with TestClient(app) as client:
        response = client.post(
            TESTS_PATH,
            json={
                "source_code": "def normalize(text):\n    return {'text': text.strip().lower()}",
                "callable_name": "normalize",
                "tests": [
                    {
                        "name": "trims and lowercases",
                        "input": {"text": "  HELLO  "},
                        "expected": {"text": "hello"},
                    }
                ],
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "passed"
    assert response.json()["data"]["tests"][0]["status"] == "passed"


def test_sandbox_http_service_rejects_invalid_body() -> None:
    app = create_app(config=CaliberConfig.load(environ={}), sandbox=_sandbox())
    with TestClient(app) as client:
        response = client.post(RUN_PATH, json={"source_code": "def f(): return 1"})

    assert response.status_code == 400


def test_clip_caps_output_by_bytes_not_characters() -> None:
    """Regression (#25): max_output_bytes is a BYTE budget; clipping by character
    count let multibyte output exceed it."""
    sb = LocalSubprocessToolSandbox(default_timeout_seconds=30.0, max_output_bytes=8)
    clipped = sb._clip("£" * 100)  # '£' is 2 bytes in UTF-8
    assert len(clipped.encode("utf-8")) <= 8
