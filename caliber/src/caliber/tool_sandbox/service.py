"""Sandbox execution backends for user-authored tools."""

from __future__ import annotations

import json
import os
import signal
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
        max_memory_bytes: int = 268_435_456,
        max_file_bytes: int = 1_048_576,
        max_open_files: int = 32,
    ) -> None:
        self.default_timeout_seconds = default_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.max_memory_bytes = max_memory_bytes
        self.max_file_bytes = max_file_bytes
        self.max_open_files = max_open_files

    @classmethod
    def from_config(cls, config: CaliberConfig) -> LocalSubprocessToolSandbox:
        return cls(
            default_timeout_seconds=config.tool_sandbox_timeout_seconds,
            max_output_bytes=config.tool_sandbox_max_output_bytes,
            max_memory_bytes=config.tool_sandbox_max_memory_bytes,
            max_file_bytes=config.tool_sandbox_max_file_bytes,
            max_open_files=config.tool_sandbox_max_open_files,
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
        payload["_limits"] = {
            "cpu_seconds": max(1, int(timeout) + 1),
            "memory_bytes": self.max_memory_bytes,
            "file_bytes": self.max_file_bytes,
            "open_files": self.max_open_files,
        }
        with tempfile.TemporaryDirectory(prefix="caliber-tool-sandbox-") as cwd:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter + runner path.
                [sys.executable, "-I", str(_RUNNER_PATH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env={},
                text=True,
                start_new_session=os.name == "posix",
            )
            try:
                raw_stdout, raw_stderr = process.communicate(json.dumps(payload), timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(process)
                raw_stdout, raw_stderr = process.communicate()
                return {
                    "status": "timed_out",
                    "error": f"tool sandbox timed out after {timeout:.2f}s",
                    "stdout": self._clip(raw_stdout),
                    "stderr": self._clip(raw_stderr),
                    "tests": [],
                    "duration_ms": round(timeout * 1000, 1),
                }

        stdout = self._clip(raw_stdout)
        stderr = self._clip(raw_stderr)
        if process.returncode != 0:
            return {
                "status": "failed",
                "error": f"runner exited with status {process.returncode}",
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

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
        """Kill the whole sandbox process group on POSIX after a timeout.

        ``Popen.kill`` only targets the direct child.  Starting a fresh session
        and killing its process group prevents an escaped grandchild from
        surviving the request.  Windows falls back to terminating the child;
        production-grade Windows isolation still requires a Job Object or an
        external container/VM boundary.
        """

        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
        process.kill()

    def _clip(self, value: str | bytes | None) -> str:
        if value is None:
            return ""
        # Cap by BYTES, not characters: slicing a str by ``max_output_bytes``
        # counts code points, so multibyte output could blow past the byte
        # budget. Truncate the UTF-8 bytes and drop any split trailing sequence.
        raw = value if isinstance(value, bytes) else value.encode("utf-8", errors="replace")
        return raw[: self.max_output_bytes].decode("utf-8", errors="ignore")


__all__ = ["LocalSubprocessToolSandbox", "ToolSandbox"]
