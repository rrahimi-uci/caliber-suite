"""Sandbox execution backends for user-authored tools."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Protocol

from caliber.config import CaliberConfig
from caliber.tool_sandbox.models import (
    ToolSandboxRunRequest,
    ToolSandboxRunResult,
    ToolSandboxTestSuiteRequest,
    ToolSandboxTestSuiteResult,
)

_RUNNER_PATH = Path(__file__).with_name("_runner.py")


class ToolSandbox(Protocol):
    """Execution boundary for tool code and tool tests."""

    def run_tool(self, request: ToolSandboxRunRequest) -> ToolSandboxRunResult:
        """Run one tool invocation."""

    def run_tests(self, request: ToolSandboxTestSuiteRequest) -> ToolSandboxTestSuiteResult:
        """Run an example-based tool test suite."""


class LocalSubprocessToolSandbox:
    """Run tool code in a short-lived isolated Python subprocess.

    This backend is suitable for development, CI, and as the local fallback for
    the standalone sandbox service. It creates a fresh temp directory, starts
    Python with ``-I``, strips the environment, and enforces a hard timeout.
    Production deployments should put the sandbox service behind container,
    VM, or kernel-level isolation before allowing untrusted users to run code.
    """

    def __init__(
        self,
        *,
        default_timeout_seconds: float = 5.0,
        max_output_bytes: int = 65_536,
    ) -> None:
        self.default_timeout_seconds = default_timeout_seconds
        self.max_output_bytes = max_output_bytes

    @classmethod
    def from_config(cls, config: CaliberConfig) -> LocalSubprocessToolSandbox:
        return cls(
            default_timeout_seconds=config.tool_sandbox_timeout_seconds,
            max_output_bytes=config.tool_sandbox_max_output_bytes,
        )

    def run_tool(self, request: ToolSandboxRunRequest) -> ToolSandboxRunResult:
        started = time.monotonic()
        raw = self._run_payload(
            {
                "mode": "run",
                "source_code": request.source_code,
                "callable_name": request.callable_name,
                "input": request.input,
            },
            timeout_seconds=request.timeout_seconds,
        )
        raw["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return ToolSandboxRunResult.model_validate(raw)

    def run_tests(self, request: ToolSandboxTestSuiteRequest) -> ToolSandboxTestSuiteResult:
        started = time.monotonic()
        raw = self._run_payload(
            {
                "mode": "tests",
                "source_code": request.source_code,
                "callable_name": request.callable_name,
                "tests": [test.model_dump(mode="json") for test in request.tests],
            },
            timeout_seconds=request.timeout_seconds,
        )
        raw["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return ToolSandboxTestSuiteResult.model_validate(raw)

    def _run_payload(
        self,
        payload: dict[str, object],
        *,
        timeout_seconds: float | None,
    ) -> dict[str, object]:
        timeout = timeout_seconds or self.default_timeout_seconds
        with tempfile.TemporaryDirectory(prefix="caliber-tool-sandbox-") as cwd:
            try:
                completed = subprocess.run(  # noqa: S603 - fixed interpreter + runner path.
                    [sys.executable, "-I", str(_RUNNER_PATH)],
                    input=json.dumps(payload),
                    cwd=cwd,
                    env={},
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "status": "timed_out",
                    "error": f"tool sandbox timed out after {timeout:.2f}s",
                    "stdout": self._clip(exc.stdout),
                    "stderr": self._clip(exc.stderr),
                    "tests": [],
                    "duration_ms": round(timeout * 1000, 1),
                }

        stdout = self._clip(completed.stdout)
        stderr = self._clip(completed.stderr)
        if completed.returncode != 0:
            return {
                "status": "failed",
                "error": f"runner exited with status {completed.returncode}",
                "stdout": stdout,
                "stderr": stderr,
                "tests": [],
                "duration_ms": 0.0,
            }

        try:
            parsed = json.loads(stdout or "{}")
        except json.JSONDecodeError as exc:
            return {
                "status": "failed",
                "error": f"runner returned invalid JSON: {exc}",
                "stdout": stdout,
                "stderr": stderr,
                "tests": [],
                "duration_ms": 0.0,
            }
        if isinstance(parsed, dict):
            parsed["stdout"] = self._clip(str(parsed.get("stdout", "")))
            parsed["stderr"] = self._clip(str(parsed.get("stderr", stderr)))
            return parsed
        return {
            "status": "failed",
            "error": "runner returned a non-object payload",
            "stdout": stdout,
            "stderr": stderr,
            "tests": [],
            "duration_ms": 0.0,
        }

    def _clip(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        # Cap by BYTES, not characters: slicing a str by ``max_output_bytes``
        # counts code points, so multibyte output could blow past the byte
        # budget. Truncate the UTF-8 bytes and drop any split trailing sequence.
        raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
        return raw[: self.max_output_bytes].decode("utf-8", errors="ignore")


__all__ = ["LocalSubprocessToolSandbox", "ToolSandbox"]
