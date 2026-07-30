"""Sandbox execution backends for user-authored tools."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from caliber.config import CaliberConfig
from caliber.tool_sandbox.models import (
    ToolSandboxInspectRequest,
    ToolSandboxInspectResult,
    ToolSandboxRunRequest,
    ToolSandboxRunResult,
    ToolSandboxTestSuiteRequest,
    ToolSandboxTestSuiteResult,
)

_RUNNER_PATH = Path(__file__).with_name("_runner.py")


#: Read headroom over ``max_output_bytes`` before a stream is drained-and-dropped.
#: Enough that a legitimate result plus its JSON envelope is never cut mid-parse.
_OUTPUT_READ_HEADROOM = 4
_MIN_OUTPUT_READ_CAP = 262_144
_READ_CHUNK = 8192


class ToolSandbox(Protocol):
    """Execution boundary for tool code and tool tests."""

    def run_tool(self, request: ToolSandboxRunRequest) -> ToolSandboxRunResult:
        """Run one tool invocation."""

    def inspect_tool(self, request: ToolSandboxInspectRequest) -> ToolSandboxInspectResult:
        """Inspect one installed callable without importing it in the caller."""

    def run_tests(self, request: ToolSandboxTestSuiteRequest) -> ToolSandboxTestSuiteResult:
        """Run an example-based tool test suite."""


def sandbox_from_optional_config(
    config: Any | None, *, default_timeout_seconds: float | None = None
) -> LocalSubprocessToolSandbox:
    """Build a sandbox honouring the operator's configured limits when available.

    ``LocalSubprocessToolSandbox.from_config`` existed but its only caller was the
    standalone sandbox app, so every in-process construction used the *class*
    defaults — meaning ``tool_sandbox_max_memory_bytes``,
    ``tool_sandbox_max_file_bytes``, and ``tool_sandbox_max_open_files`` were
    effectively unwired for the paths that actually run tool code.

    ``config=None`` falls back to the class defaults deliberately: callers that
    genuinely have no config (a few test helpers) should still get a bounded sandbox
    rather than an exception.
    """
    if config is None:
        kwargs: dict[str, Any] = {}
        if default_timeout_seconds is not None:
            kwargs["default_timeout_seconds"] = default_timeout_seconds
        return LocalSubprocessToolSandbox(**kwargs)
    sandbox = LocalSubprocessToolSandbox.from_config(config)
    if default_timeout_seconds is not None:
        # A per-call timeout (e.g. an Aria run budget) overrides the configured
        # default; the resource limits stay the operator's.
        sandbox.default_timeout_seconds = default_timeout_seconds
    return sandbox


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
                # Empty in source mode; the runner picks the branch off this.
                "module_path": request.module_path,
                "args": request.args,
                "callable_name": request.callable_name,
                "input": request.input,
                # Forwarded, and this omission was a real defect: the runtime sent
                # candidate call shapes and this method dropped them, so the child fell
                # back to ``fn(*args, **input)`` with both empty. A model tool call for
                # ``lookup_policy(query="refund policy")`` executed as ``lookup_policy()``
                # and returned an answer computed from an empty query — silent argument
                # loss, which is worse than an error because the caller sees a plausible
                # result. Serialized by ``mode="json"`` so the payload stays JSON-safe.
                "shapes": [s.model_dump(mode="json") for s in request.shapes],
            },
            timeout_seconds=request.timeout_seconds,
        )
        raw["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return ToolSandboxRunResult.model_validate(raw)

    def inspect_tool(self, request: ToolSandboxInspectRequest) -> ToolSandboxInspectResult:
        """Return signature/doc/source from the child process.

        Source inspection used to import the registered module in the API process. That
        made a supposedly read-only metadata endpoint execute module-level Python with
        the control plane's credentials. Inspection belongs behind the same boundary as
        invocation because import is execution.
        """
        started = time.monotonic()
        raw = self._run_payload(
            {
                "mode": "inspect",
                "module_path": request.module_path,
                "callable_name": request.callable_name,
            },
            timeout_seconds=request.timeout_seconds,
        )
        raw["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
        return ToolSandboxInspectResult.model_validate(raw)

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
            # The child bounds its own capture, so the JSON envelope this process
            # parses is bounded too. Without it, the parent's read cap could truncate
            # a valid response mid-string.
            "output_chars": self.max_output_bytes,
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
            raw_stdout, raw_stderr, timed_out = self._communicate_bounded(
                process, json.dumps(payload), timeout
            )
            if timed_out:
                return {
                    "status": "timed_out",
                    "error": f"tool sandbox timed out after {timeout:.2f}s",
                    "stdout": self._clip(raw_stdout),
                    "stderr": self._clip(raw_stderr),
                    "tests": [],
                    "duration_ms": round(timeout * 1000, 1),
                }

        # ``raw_stdout`` is the runner's JSON *envelope*, not tool output: clipping it
        # before parsing corrupted the response whenever ``max_output_bytes`` was
        # smaller than the envelope. The envelope is already bounded — the child caps
        # its own capture and the parent caps its reads — so parse it whole and clip
        # only the tool-facing fields below.
        stderr = self._clip(raw_stderr)
        if process.returncode != 0:
            return {
                "status": "failed",
                "error": f"runner exited with status {process.returncode}",
                "stdout": self._clip(raw_stdout),
                "stderr": stderr,
                "tests": [],
                "duration_ms": 0.0,
            }

        try:
            parsed = json.loads(raw_stdout or "{}")
        except json.JSONDecodeError as exc:
            return {
                "status": "failed",
                "error": f"runner returned invalid JSON: {exc}",
                "stdout": self._clip(raw_stdout),
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
            "stdout": self._clip(raw_stdout),
            "stderr": stderr,
            "tests": [],
            "duration_ms": 0.0,
        }

    def _communicate_bounded(
        self,
        process: subprocess.Popen[str],
        stdin_payload: str | None,
        timeout: float,
    ) -> tuple[str, str, bool]:
        """Write stdin and read stdout/stderr with a **bounded** buffer.

        ``Popen.communicate()`` accumulates the child's entire output in memory and
        only then does the caller clip it, so a child printing gigabytes exhausted the
        parent before any limit applied — the review recorded that "output memory is
        not actually bounded".

        Reading is capped at a small multiple of ``max_output_bytes``: enough headroom
        that a legitimate result plus a JSON envelope is never truncated mid-parse,
        while still bounding the worst case. Once a stream passes the cap it is
        drained and discarded rather than buffered, so the child still makes progress
        to exit (silently closing the pipe would kill it with EPIPE and turn a noisy
        tool into a crash).

        Returns ``(stdout, stderr, timed_out)``. The timeout is handled here rather
        than raised, because this method owns the stream lifetime: a caller that
        caught the timeout and re-read would be reading descriptors this method has
        already closed.
        """
        cap = max(self.max_output_bytes * _OUTPUT_READ_HEADROOM, _MIN_OUTPUT_READ_CAP)
        collected: dict[str, list[str]] = {"stdout": [], "stderr": []}
        sizes: dict[str, int] = {"stdout": 0, "stderr": 0}

        def _drain(name: str, stream: Any) -> None:
            if stream is None:
                return
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    return
                if sizes[name] < cap:
                    collected[name].append(chunk)
                    sizes[name] += len(chunk)
                # Past the cap the chunk is dropped, not buffered. The loop keeps
                # reading so the child is never blocked on a full pipe.

        if stdin_payload is not None and process.stdin is not None:
            try:
                process.stdin.write(stdin_payload)
                process.stdin.flush()
            except (BrokenPipeError, ValueError):
                # The child exited before reading its input; the exit status and
                # whatever it wrote are the useful signal.
                pass
            finally:
                with contextlib.suppress(Exception):
                    process.stdin.close()

        threads = [
            threading.Thread(target=_drain, args=(name, getattr(process, name)), daemon=True)
            for name in ("stdout", "stderr")
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the whole group: a tool that spawned children must not outlive the
            # request that started it.
            self._terminate_process_tree(process)
            with contextlib.suppress(Exception):
                process.wait(timeout=1.0)
        finally:
            # Bounded join: a reader blocked on a pipe the child never closes must not
            # hang the request. The process-group kill in the timeout path closes them.
            for thread in threads:
                thread.join(timeout=1.0)
            # ``communicate()`` used to own closing these. Doing it here keeps the
            # parent from leaking a descriptor per sandbox run, which would eventually
            # trip the very open-file limit this sandbox sets on its child.
            for name in ("stdout", "stderr"):
                stream = getattr(process, name, None)
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()
        return "".join(collected["stdout"]), "".join(collected["stderr"]), timed_out

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
