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
from typing import Any, Protocol, cast

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
#: Wall-clock added on top of the caller's timeout to cover interpreter start and module
#: import. Not a fudge factor: it is the cost of running out of process at all, which the
#: caller neither asked for nor can influence. Generous because it is only ever paid when
#: something is genuinely stuck — a healthy start costs well under a second.
_DEFAULT_STARTUP_GRACE_SECONDS = 15.0
_MIN_OUTPUT_READ_CAP = 262_144
_READ_CHUNK = 8192
# Must match the stdlib-only child runner. The child cannot emit JSON after a hard
# os._exit(), so this code is the protocol marker for an uncaught wall deadline.
_DEADLINE_EXIT_CODE = 124
_PARENT_READY_MARKER = "__CALIBER_SANDBOX_READY_V1__"
_PARENT_START_MARKER = "__CALIBER_SANDBOX_START_V1__"


class ToolSandbox(Protocol):
    """Execution boundary for tool code and tool tests."""

    def run_tool(self, request: ToolSandboxRunRequest) -> ToolSandboxRunResult:
        """Run one tool invocation."""

    def inspect_tool(self, request: ToolSandboxInspectRequest) -> ToolSandboxInspectResult:
        """Inspect one installed callable without importing it in the caller."""

    def run_tests(self, request: ToolSandboxTestSuiteRequest) -> ToolSandboxTestSuiteResult:
        """Run an example-based tool test suite."""


def _load_backend_factory(dotted: str) -> Any:
    """Resolve ``package.module:attribute`` to an operator-supplied sandbox factory.

    Imported in the control plane, and that is safe in a way user tool code is not: this
    path comes from an environment variable an operator set, exactly like the MCP stdio
    command allowlist. It is deployment configuration, not workflow content.
    """
    module_path, _, attribute = dotted.partition(":")
    if not module_path or not attribute:
        raise ValueError(
            f"CALIBER_TOOL_SANDBOX_BACKEND must look like 'package.module:Factory', got {dotted!r}"
        )
    import importlib  # noqa: PLC0415

    module = importlib.import_module(module_path)
    factory = getattr(module, attribute, None)
    if factory is None:
        raise ValueError(f"{module_path!r} has no attribute {attribute!r}")
    return factory


def sandbox_from_optional_config(
    config: Any | None, *, default_timeout_seconds: float | None = None
) -> ToolSandbox:
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
    # An operator-supplied backend takes precedence, because the shipped one cannot be
    # made into what some deployments need. ``LocalSubprocessToolSandbox`` is a *process*
    # boundary: separate interpreter, empty environment, private working directory, POSIX
    # rlimits. It is not a container, VM, or seccomp boundary, so the child keeps ambient
    # filesystem and network authority on the host — which is fine for trusted authors and
    # is not isolation for untrusted ones.
    #
    # Portable Python cannot close that: namespaces are Linux-only and privileged, seccomp
    # needs a native binding, and containers are infrastructure. So rather than pretend, the
    # boundary is pluggable — a deployment that needs OS-enforced isolation points
    # ``CALIBER_TOOL_SANDBOX_BACKEND`` at a factory implementing :class:`ToolSandbox`
    # (Docker, gVisor, Firecracker) without forking the product.
    backend = str(getattr(config, "tool_sandbox_backend", "") or "").strip() if config else ""
    if backend:
        factory = _load_backend_factory(backend)
        sandbox_obj: Any = factory(config)
        for method in ("run_tool", "inspect_tool", "run_tests"):
            if not callable(getattr(sandbox_obj, method, None)):
                raise TypeError(
                    f"tool sandbox backend {backend!r} does not implement {method}(); "
                    "it must satisfy caliber.tool_sandbox.service.ToolSandbox"
                )
        if default_timeout_seconds is not None:
            with contextlib.suppress(AttributeError):
                sandbox_obj.default_timeout_seconds = default_timeout_seconds
        return cast("ToolSandbox", sandbox_obj)
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
        startup_grace_seconds: float = _DEFAULT_STARTUP_GRACE_SECONDS,
    ) -> None:
        self.default_timeout_seconds = default_timeout_seconds
        self.startup_grace_seconds = max(0.0, float(startup_grace_seconds))
        self.max_output_bytes = max_output_bytes
        self.max_memory_bytes = max_memory_bytes
        self.max_file_bytes = max_file_bytes
        self.max_open_files = max_open_files

    @classmethod
    def from_config(cls, config: CaliberConfig) -> LocalSubprocessToolSandbox:
        """Build from operator configuration, tolerating a partial object.

        ``getattr`` with the class defaults rather than attribute access, for the same
        reason ``sandbox_from_optional_config`` accepts ``config=None``: a caller that
        cannot supply every field should still get a *bounded* sandbox rather than an
        ``AttributeError``. Direct access made any partially-populated config a crash — and
        once the ``python_code`` node started resolving through this factory, a config
        carrying only the sandbox on/off flag took 27 worker tests down with it.
        """
        defaults = cls()
        return cls(
            default_timeout_seconds=getattr(
                config, "tool_sandbox_timeout_seconds", defaults.default_timeout_seconds
            ),
            max_output_bytes=getattr(
                config, "tool_sandbox_max_output_bytes", defaults.max_output_bytes
            ),
            max_memory_bytes=getattr(
                config, "tool_sandbox_max_memory_bytes", defaults.max_memory_bytes
            ),
            max_file_bytes=getattr(config, "tool_sandbox_max_file_bytes", defaults.max_file_bytes),
            max_open_files=getattr(config, "tool_sandbox_max_open_files", defaults.max_open_files),
            startup_grace_seconds=getattr(
                config, "tool_sandbox_startup_grace_seconds", defaults.startup_grace_seconds
            ),
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
        # The caller's timeout is a budget for *their code*, so interpreter startup is not
        # charged to it. A manifest saying ``timeout_seconds: 5`` means "my node may take
        # five seconds", not "five seconds including a cold Python start" — and the two are
        # not close under load. Measured idle: ~0.05s for a trivial module, ~0.55s for one
        # importing the caliber package. Under a suite-wide spawn storm a legitimate node
        # exceeded a 5s budget on startup alone and was reported as a product failure.
        #
        # ``cpu_seconds`` stays derived from the caller's timeout, not the extended wall
        # clock: the rlimit is what stops a runaway loop, and it should still measure the
        # work rather than the overhead.
        # The child watchdog remains defense in depth, but the parent owns the authored
        # wall deadline. A child thread can be denied the GIL, and registered module code
        # can mutate process-global modules; neither can stop a separate parent process.
        # An explicit ready/start handshake separates the startup allowance from this
        # budget so authored work can consume exactly ``timeout`` and never the grace.
        payload["deadline_seconds"] = timeout
        payload["_parent_deadline_handshake"] = True
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
                process,
                json.dumps(payload),
                timeout,
                startup_timeout=self.startup_grace_seconds,
            )
            if timed_out:
                return {
                    "status": "timed_out",
                    # Reports the caller's budget, which is the number they configured;
                    # the grace is overhead the platform added, not part of their contract.
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
        if process.returncode == _DEADLINE_EXIT_CODE:
            return {
                "status": "timed_out",
                "error": f"tool sandbox timed out after {timeout:.2f}s",
                "stdout": self._clip(raw_stdout),
                "stderr": stderr,
                "tests": [],
                "duration_ms": round(timeout * 1000, 1),
            }
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

    def _communicate_bounded(  # noqa: PLR0912, PLR0915 - bounded two-phase process protocol
        self,
        process: subprocess.Popen[str],
        stdin_payload: str | None,
        timeout: float,
        *,
        startup_timeout: float | None = None,
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

        def _write_stdin(value: str, *, close: bool) -> None:
            if process.stdin is None:
                return
            try:
                process.stdin.write(value)
                process.stdin.flush()
            except (BrokenPipeError, ValueError):
                # The child exited before reading its input; the exit status and
                # whatever it wrote are the useful signal.
                pass
            if close:
                with contextlib.suppress(Exception):
                    process.stdin.close()

        threads: list[threading.Thread] = []

        def _start_drain(name: str) -> None:
            thread = threading.Thread(
                target=_drain,
                args=(name, getattr(process, name)),
                daemon=True,
            )
            threads.append(thread)
            thread.start()

        timed_out = False
        try:
            if startup_timeout is None:
                # Private compatibility path for callers/tests exercising this helper
                # directly. Production calls always use the ready/start protocol below.
                if stdin_payload is not None:
                    _write_stdin(stdin_payload, close=True)
                _start_drain("stdout")
                _start_drain("stderr")
            else:
                # stderr has no protocol data and can be drained throughout startup.
                _start_drain("stderr")
                startup_started = time.monotonic()
                # ``0`` historically meant "no *extra* startup grace", not "kill the
                # child before it can execute one instruction". In that mode startup
                # consumes the caller's total budget; a positive grace remains a separate
                # phase and preserves the full authored-code budget after readiness.
                startup_budget = startup_timeout if startup_timeout > 0 else timeout
                ready: dict[str, str] = {"line": ""}
                ready_event = threading.Event()

                def _read_ready() -> None:
                    stream = process.stdout
                    if stream is not None:
                        # Before this marker the child has not run authored code, and it
                        # waits for our start acknowledgement, so this bounded line is the
                        # only possible stdout protocol message.
                        ready["line"] = stream.readline(len(_PARENT_READY_MARKER) + 2)
                    ready_event.set()

                ready_thread = threading.Thread(target=_read_ready, daemon=True)
                threads.append(ready_thread)
                ready_thread.start()
                if stdin_payload is not None:
                    _write_stdin(stdin_payload + "\n", close=False)

                if not ready_event.wait(timeout=startup_budget):
                    timed_out = True
                    self._terminate_process_tree(process)
                    with contextlib.suppress(Exception):
                        process.wait(timeout=1.0)
                elif ready["line"].rstrip("\r\n") != _PARENT_READY_MARKER:
                    # Preserve the unexpected bytes for diagnostics, then stop a child
                    # that cannot participate in the deadline protocol.
                    collected["stdout"].append(ready["line"])
                    self._terminate_process_tree(process)
                    with contextlib.suppress(Exception):
                        process.wait(timeout=1.0)
                else:
                    # The authored clock starts before permission is sent. Starting the
                    # normal reader first prevents a fast/noisy child from filling its
                    # pipe before the parent reaches wait().
                    _start_drain("stdout")
                    if startup_timeout > 0:
                        execution_budget = timeout
                    else:
                        execution_budget = max(
                            0.0,
                            timeout - (time.monotonic() - startup_started),
                        )
                    deadline = time.monotonic() + execution_budget
                    _write_stdin(_PARENT_START_MARKER + "\n", close=True)
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))

            if not timed_out and startup_timeout is None:
                process.wait(timeout=timeout)

            if not timed_out and process.returncode == _DEADLINE_EXIT_CODE:
                # The runner's uncatchable watchdog exits only the direct child. A
                # registered tool may have spawned descendants in the sandbox session;
                # those descendants can keep running (and keep our pipes open) after the
                # leader exits with 124. Treat the protocol exit exactly like the parent
                # backstop timeout and reap the whole POSIX process group immediately.
                timed_out = True
                self._terminate_process_tree(process)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Kill the whole group: a tool that spawned children must not outlive the
            # request that started it.
            self._terminate_process_tree(process)
            with contextlib.suppress(Exception):
                process.wait(timeout=1.0)
        finally:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
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

        if os.name == "posix":
            # Deliberately attempt killpg even when the process-group leader has already
            # exited. That is the child-watchdog (status 124) case: descendants remain in
            # the group and are precisely what this boundary must terminate.
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                # Some constrained hosts allow signalling a direct child but deny
                # process-group signals. Preserve the stronger tree kill whenever the
                # host permits it; otherwise reap the child rather than turning a
                # correctly detected timeout into an unrelated 500/runner exception.
                # A hardened deployment still needs a container/VM (and Windows a Job
                # Object) to guarantee descendant cleanup outside this POSIX primitive.
                if process.poll() is None:
                    process.kill()
                return
        if process.poll() is None:
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
