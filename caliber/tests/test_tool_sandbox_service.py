"""Tests for the standalone tool sandbox service."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import threading
import time
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.tool_sandbox import service as sandbox_service
from caliber.tool_sandbox.models import (
    ToolSandboxRunRequest,
    ToolSandboxRunResult,
    ToolSandboxTestCase,
    ToolSandboxTestSuiteRequest,
    ToolSandboxTestSuiteResult,
)
from caliber.tool_sandbox.server import HEALTH_PATH, RUN_PATH, TESTS_PATH, create_app
from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

pytestmark = pytest.mark.xdist_group("tool-sandbox-subprocess")


def _instant_timeout_sandbox() -> LocalSubprocessToolSandbox:
    """A sandbox with no startup grace, for the tests that *want* to hit the timeout.

    The shipped grace exists so a caller's ``timeout_seconds`` budgets their code rather
    than the interpreter start they neither asked for nor can influence. A test asserting
    the timeout mechanism must not wait for it, so it opts out explicitly — which also
    keeps the grace itself visible rather than something a reader has to infer.
    """
    return LocalSubprocessToolSandbox(
        default_timeout_seconds=30.0, max_output_bytes=4_000, startup_grace_seconds=0.0
    )


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


def test_zero_startup_grace_charges_startup_to_the_budget_instead_of_killing_immediately() -> None:
    """Zero disables the separate allowance; it does not disable the sandbox."""
    sandbox = LocalSubprocessToolSandbox(
        default_timeout_seconds=2.0,
        max_output_bytes=4_000,
        startup_grace_seconds=0.0,
    )
    result = sandbox.run_tool(
        ToolSandboxRunRequest(
            source_code="def ready():\n    return {'ready': True}\n",
            callable_name="ready",
        )
    )

    assert result.status == "completed", result
    assert result.output == {"ready": True}


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
    result = _instant_timeout_sandbox().run_tool(
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


def test_authored_repr_cannot_escape_the_hard_deadline_during_serialization() -> None:
    """Result conversion is part of authored execution, not trusted runner cleanup."""
    result = _instant_timeout_sandbox().run_tool(
        ToolSandboxRunRequest(
            source_code="""
class SlowResult:
    def __repr__(self):
        while True:
            pass

def build_result():
    return SlowResult()
""",
            callable_name="build_result",
            timeout_seconds=0.1,
        )
    )

    assert result.status == "timed_out"
    assert "timed out" in (result.error or "")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are unavailable")
def test_timeout_termination_kills_the_sandbox_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, signal.Signals]] = []
    # The direct runner has already exited with its watchdog marker. Descendants remain
    # addressable through the process-group id even though the leader is gone.
    process = SimpleNamespace(pid=4123, poll=lambda: 124)
    monkeypatch.setattr(
        sandbox_service.os,
        "killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )

    LocalSubprocessToolSandbox._terminate_process_tree(process)  # type: ignore[arg-type]

    assert calls == [(4123, signal.SIGKILL)]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are unavailable")
def test_timeout_termination_falls_back_when_group_signal_is_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[bool] = []
    process = SimpleNamespace(pid=4123, poll=lambda: None, kill=lambda: killed.append(True))

    def _forbidden(_pid: int, _sig: signal.Signals) -> None:
        raise PermissionError("process-group signalling is blocked")

    monkeypatch.setattr(sandbox_service.os, "killpg", _forbidden)

    LocalSubprocessToolSandbox._terminate_process_tree(process)  # type: ignore[arg-type]

    assert killed == [True]


def test_deadline_exit_is_treated_as_a_tree_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status 124 is emitted by the child watchdog, not the parent wait timeout."""
    sandbox = _instant_timeout_sandbox()
    process = SimpleNamespace(
        pid=4123,
        stdin=None,
        stdout=io.StringIO(""),
        stderr=io.StringIO(""),
        returncode=124,
        wait=lambda timeout: 124,
        poll=lambda: 124,
    )
    terminated: list[object] = []
    monkeypatch.setattr(sandbox, "_terminate_process_tree", terminated.append)

    _stdout, _stderr, timed_out = sandbox._communicate_bounded(
        process,  # type: ignore[arg-type]
        None,
        timeout=1.0,
    )

    assert timed_out is True
    assert terminated == [process]


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


class _BlockingHttpSandbox:
    """Synchronous backend used to prove the ASGI loop remains responsive."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def _block(self) -> None:
        self.started.set()
        self.release.wait()

    def run_tool(self, _request: object) -> ToolSandboxRunResult:
        self._block()
        return ToolSandboxRunResult(status="completed", output={"ok": True}, duration_ms=0)

    def run_tests(self, _request: object) -> ToolSandboxTestSuiteResult:
        self._block()
        return ToolSandboxTestSuiteResult(status="passed", tests=[], duration_ms=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            RUN_PATH,
            {"source_code": "def ok(): return 1", "callable_name": "ok"},
        ),
        (
            TESTS_PATH,
            {
                "source_code": "def ok(): return 1",
                "callable_name": "ok",
                "tests": [{"name": "ok", "expected": 1}],
            },
        ),
    ],
)
async def test_sandbox_http_execution_does_not_block_health(
    path: str,
    body: dict[str, object],
) -> None:
    sandbox = _BlockingHttpSandbox()
    app = create_app(config=CaliberConfig.load(environ={}), sandbox=sandbox)  # type: ignore[arg-type]
    # A backstop makes the regression fail instead of hanging if synchronous work is
    # accidentally moved back onto the event loop.
    backstop = threading.Timer(3.0, sandbox.release.set)
    backstop.start()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://sandbox.test"
        ) as client:
            execution = asyncio.create_task(client.post(path, json=body))
            observed = await asyncio.to_thread(sandbox.started.wait, 1.0)
            assert observed, "sandbox backend was not invoked"
            assert not sandbox.release.is_set(), (
                "event loop stayed blocked until the backstop fired"
            )

            response = await asyncio.wait_for(client.get(HEALTH_PATH), timeout=1.0)
            assert response.status_code == 200
            assert not sandbox.release.is_set(), "health only responded after execution completed"

            sandbox.release.set()
            assert (await execution).status_code == 200
    finally:
        sandbox.release.set()
        backstop.cancel()


def test_clip_caps_output_by_bytes_not_characters() -> None:
    """Regression (#25): max_output_bytes is a BYTE budget; clipping by character
    count let multibyte output exceed it."""
    sb = LocalSubprocessToolSandbox(default_timeout_seconds=30.0, max_output_bytes=8)
    clipped = sb._clip("£" * 100)  # '£' is 2 bytes in UTF-8
    assert len(clipped.encode("utf-8")) <= 8


# ---------------------------------------------------------------------------
# Bounded output capture and configured limits (C8)
# ---------------------------------------------------------------------------


def test_sandbox_output_is_bounded_during_capture_not_after() -> None:
    """Regression (C8): the review found that ``Popen.communicate()`` accumulated
    the child's entire output in memory and the caller clipped it only afterwards,
    so "output memory is not actually bounded". A child printing far more than the
    cap must not be buffered in full.
    """
    from caliber.tool_sandbox.models import ToolSandboxRunRequest
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    sandbox = LocalSubprocessToolSandbox(max_output_bytes=1024, default_timeout_seconds=20.0)
    # ~8 MB of stdout, thousands of times the 1 KB cap.
    source = (
        "def noisy():\n    for _ in range(8192):\n        print('x' * 1024)\n    return 'done'\n"
    )
    result = sandbox.run_tool(
        ToolSandboxRunRequest(source_code=source, callable_name="noisy", input={})
    )
    # The captured stdout is bounded by the read cap, not by the child's volume.
    assert len(result.stdout) < 8 * 1024 * 1024
    from caliber.tool_sandbox.service import _MIN_OUTPUT_READ_CAP, _OUTPUT_READ_HEADROOM

    assert len(result.stdout) <= max(1024 * _OUTPUT_READ_HEADROOM, _MIN_OUTPUT_READ_CAP)


def test_a_noisy_tool_still_completes_rather_than_being_killed_by_epipe() -> None:
    """Past the cap, output is drained and dropped rather than the pipe being
    closed: closing it would kill the child with EPIPE and turn a chatty tool into a
    crash, which is a different bug than the one being fixed."""
    from caliber.tool_sandbox.models import ToolSandboxRunRequest
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    sandbox = LocalSubprocessToolSandbox(max_output_bytes=256, default_timeout_seconds=20.0)
    source = "def chatty():\n    for _ in range(2000):\n        print('y' * 512)\n    return {'ok': True}\n"
    result = sandbox.run_tool(
        ToolSandboxRunRequest(source_code=source, callable_name="chatty", input={})
    )
    assert result.status == "completed", result.error
    assert result.output == {"ok": True}


def test_a_hung_tool_still_times_out_with_bounded_reads() -> None:
    """The bounded reader owns the timeout, so a child that never exits is still
    killed and the request still returns."""
    from caliber.tool_sandbox.models import ToolSandboxRunRequest
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    # No startup grace: this asserts the timeout mechanism, so waiting out the
    # platform's interpreter-start allowance would only make the test slow.
    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=0.2, startup_grace_seconds=0.0)
    # Imports are blocked by design, so a busy loop stands in for time.sleep.
    source = "def hang():\n    while True:\n        pass\n"
    result = sandbox.run_tool(
        ToolSandboxRunRequest(source_code=source, callable_name="hang", input={})
    )
    assert result.status == "timed_out"


def test_repeated_runs_do_not_leak_descriptors() -> None:
    """``communicate()`` used to close the pipes; the bounded reader must too, or the
    parent leaks a descriptor per run and eventually trips its own open-file limit."""
    import gc
    import warnings

    from caliber.tool_sandbox.models import ToolSandboxRunRequest
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=10.0)
    request = ToolSandboxRunRequest(
        source_code="def ok():\n    return 1\n", callable_name="ok", input={}
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        for _ in range(5):
            assert sandbox.run_tool(request).status == "completed"
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]


def test_configured_limits_reach_an_in_process_sandbox() -> None:
    """Regression (C8): ``from_config`` was the only reader of
    ``tool_sandbox_max_memory_bytes`` / ``max_file_bytes`` / ``max_open_files``, and
    its only caller was the standalone sandbox app — so every in-process construction
    silently used the class defaults instead of the operator's values."""
    from caliber.config import CaliberConfig
    from caliber.tool_sandbox.service import sandbox_from_optional_config

    config = CaliberConfig(
        tool_sandbox_timeout_seconds=7.0,
        tool_sandbox_max_output_bytes=4096,
        tool_sandbox_max_memory_bytes=123_456_789,
        tool_sandbox_max_file_bytes=2048,
        tool_sandbox_max_open_files=11,
    )
    sandbox = sandbox_from_optional_config(config)
    assert sandbox.default_timeout_seconds == 7.0
    assert sandbox.max_output_bytes == 4096
    assert sandbox.max_memory_bytes == 123_456_789
    assert sandbox.max_file_bytes == 2048
    assert sandbox.max_open_files == 11

    # A per-call timeout overrides the configured default; the resource limits stay
    # the operator's.
    scoped = sandbox_from_optional_config(config, default_timeout_seconds=1.5)
    assert scoped.default_timeout_seconds == 1.5
    assert scoped.max_open_files == 11

    # No config still yields a bounded sandbox rather than an exception.
    assert sandbox_from_optional_config(None).max_open_files > 0


def test_build_plan_threads_every_configured_sandbox_limit() -> None:
    """The dominant workflow path previously passed only the timeout and the optional
    output cap."""
    from caliber.config import CaliberConfig
    from caliber.workflows.runtime import RuntimePlan

    config = CaliberConfig(
        tool_sandbox_max_memory_bytes=99_000_000,
        tool_sandbox_max_file_bytes=4096,
        tool_sandbox_max_open_files=9,
    )
    plan = RuntimePlan(
        ir=None,
        resolver=None,  # type: ignore[arg-type]
        sandbox_max_memory_bytes=config.tool_sandbox_max_memory_bytes,
        sandbox_max_file_bytes=config.tool_sandbox_max_file_bytes,
        sandbox_max_open_files=config.tool_sandbox_max_open_files,
    )
    assert plan.sandbox_max_memory_bytes == 99_000_000
    assert plan.sandbox_max_file_bytes == 4096
    assert plan.sandbox_max_open_files == 9


def test_an_operator_can_supply_a_container_backed_sandbox() -> None:
    """OS-enforced isolation is a deployment concern, so the boundary is pluggable.

    The shipped ``LocalSubprocessToolSandbox`` is a *process* boundary — separate
    interpreter, empty environment, private working directory, POSIX rlimits — and not a
    container, VM, or seccomp boundary: the child keeps ambient filesystem and network
    authority on the host. Portable Python cannot close that (namespaces are Linux-only and
    privileged, seccomp needs a native binding, containers are infrastructure), so rather
    than claim isolation it does not have, a deployment points
    ``CALIBER_TOOL_SANDBOX_BACKEND`` at its own factory.
    """
    from types import SimpleNamespace

    from caliber.tool_sandbox.service import sandbox_from_optional_config

    config = SimpleNamespace(
        tool_sandbox_backend="tests.test_tool_sandbox_service:_FakeContainerSandbox",
    )
    sandbox = sandbox_from_optional_config(config)

    assert isinstance(sandbox, _FakeContainerSandbox)
    assert sandbox.received_config is config


def test_a_backend_missing_a_protocol_method_is_refused_at_construction() -> None:
    """A partial backend must fail where an operator can see it, not on the first tool
    call in production — the whole point is that this path executes untrusted code."""
    from types import SimpleNamespace

    import pytest as _pytest

    from caliber.tool_sandbox.service import sandbox_from_optional_config

    config = SimpleNamespace(
        tool_sandbox_backend="tests.test_tool_sandbox_service:_IncompleteSandbox",
    )
    with _pytest.raises(TypeError, match="does not implement"):
        sandbox_from_optional_config(config)


def test_the_default_backend_is_the_built_in_subprocess_one() -> None:
    """Unset must keep the shipped behaviour: an operator who configures nothing still
    gets the process boundary rather than nothing at all."""
    from caliber.tool_sandbox.service import (
        LocalSubprocessToolSandbox,
        sandbox_from_optional_config,
    )

    assert isinstance(sandbox_from_optional_config(None), LocalSubprocessToolSandbox)


def test_standalone_service_uses_the_configured_backend_factory() -> None:
    """The standalone boundary must not bypass the operator's hardened backend."""
    config = CaliberConfig.load(
        environ={
            "CALIBER_TOOL_SANDBOX_BACKEND": (
                "tests.test_tool_sandbox_service:_FakeContainerSandbox"
            )
        }
    )

    app = create_app(config=config)

    assert isinstance(app.state.sandbox, _FakeContainerSandbox)
    assert app.state.sandbox.received_config is config


class _FakeContainerSandbox:
    """Stands in for a Docker/gVisor-backed implementation."""

    def __init__(self, config: object) -> None:
        self.received_config = config

    def run_tool(self, request: object) -> object:  # pragma: no cover - not invoked
        raise NotImplementedError

    def inspect_tool(self, request: object) -> object:  # pragma: no cover - not invoked
        raise NotImplementedError

    def run_tests(self, request: object) -> object:  # pragma: no cover - not invoked
        raise NotImplementedError


class _IncompleteSandbox:
    """Implements only part of the protocol, which must be refused."""

    def __init__(self, config: object) -> None:
        self.received_config = config

    def run_tool(self, request: object) -> object:  # pragma: no cover - not invoked
        raise NotImplementedError


def test_interpreter_startup_is_not_charged_to_the_callers_timeout() -> None:
    """A caller's ``timeout_seconds`` budgets their code, not the cold start.

    This is the fix for an intermittent that reported a working node as a product failure.
    Three `test_workflow_runtime` tests failed together on one xdist worker in a local CI
    run: the python_code node builds its sandbox with the manifest's `timeout_seconds: 5`,
    and under a suite-wide `python -I` spawn storm the interpreter start alone consumed
    that budget. Measured idle, startup is ~0.05s for a trivial module and ~0.55s for one
    importing the caliber package — small, but not small compared to a saturated host.

    So the wall-clock wait is the caller's timeout *plus* a startup allowance, while the
    CPU rlimit stays derived from the caller's timeout alone: the rlimit is what stops a
    runaway loop and should measure the work, not the overhead.
    """
    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=5.0)

    assert sandbox.startup_grace_seconds > 0, "startup must not be charged to the caller"

    # A tool that sleeps just past a tiny budget still times out: the grace covers startup,
    # it does not extend how long the caller's code may run.
    result = LocalSubprocessToolSandbox(
        default_timeout_seconds=0.2, startup_grace_seconds=0.0
    ).run_tool(
        ToolSandboxRunRequest(
            # A busy loop rather than a sleep: source mode restricts imports, so
            # ``import time`` fails as an ImportError long before any timeout.
            source_code="def slow():\n    while True:\n        pass\n",
            callable_name="slow",
            timeout_seconds=0.2,
        )
    )
    assert result.status == "timed_out"
    # The message reports the configured budget, not the internal allowance.
    assert "0.20s" in (result.error or "")


def test_a_blocking_tool_is_bounded_by_its_budget_not_by_budget_plus_grace() -> None:
    """The startup allowance must not become extra runtime.

    Adding the allowance to the parent's wait fixed cold starts but made the *effective*
    deadline budget + grace for anything that blocks: `RLIMIT_CPU` does not tick while a
    process sleeps or waits on I/O, so a 1s budget with a 4s grace actually waited 5s. An
    independent review caught it.

    The child now announces readiness and waits for the parent to arm the authored-code
    clock. The child watchdog remains a second boundary, but the startup allowance is a
    separate phase rather than extra runtime.
    """
    import time

    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=1.0, startup_grace_seconds=15.0)
    started = time.monotonic()
    result = sandbox.run_tool(
        ToolSandboxRunRequest(
            module_path="time",
            callable_name="sleep",
            shapes=[{"args": [30], "kwargs": {}}],
        )
    )
    elapsed = time.monotonic() - started

    assert result.status == "timed_out", result.error
    # Comfortably under budget + grace (16s); the point is the grace is not runtime.
    assert elapsed < 8.0, f"a blocking tool ran for {elapsed:.1f}s on a 1s budget"


def test_authored_code_cannot_catch_and_continue_after_the_wall_deadline() -> None:
    """The former SIGALRM raised Exception, so a tool could catch the boundary itself."""
    import time

    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=0.2, startup_grace_seconds=2.0)
    started = time.monotonic()
    result = sandbox.run_tool(
        ToolSandboxRunRequest(
            source_code=(
                "def evade():\n"
                "    try:\n"
                "        while True:\n"
                "            pass\n"
                "    except Exception:\n"
                "        return {'escaped': True}\n"
            ),
            callable_name="evade",
            timeout_seconds=0.2,
        )
    )

    assert result.status == "timed_out", result
    assert result.output is None
    assert time.monotonic() - started < 1.5


def test_tool_test_suite_uses_the_same_uncatchable_wall_deadline() -> None:
    """Test mode previously installed no wall timer and waited for the CPU rlimit."""
    import time

    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=0.2, startup_grace_seconds=2.0)
    started = time.monotonic()
    result = sandbox.run_tests(
        ToolSandboxTestSuiteRequest(
            source_code="def hang():\n    while True:\n        pass\n",
            callable_name="hang",
            tests=[ToolSandboxTestCase(name="bounded", input={}, expected=None)],
            timeout_seconds=0.2,
        )
    )

    assert result.status == "timed_out", result
    assert result.tests == []
    assert time.monotonic() - started < 1.5


def test_top_level_authored_source_is_inside_the_wall_deadline() -> None:
    """A hostile module body must not receive the parent's full startup grace."""
    import time

    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=0.2, startup_grace_seconds=2.0)
    started = time.monotonic()
    result = sandbox.run_tool(
        ToolSandboxRunRequest(
            source_code="while True:\n    pass\n\ndef never_runs():\n    return 1\n",
            callable_name="never_runs",
            timeout_seconds=0.2,
        )
    )

    assert result.status == "timed_out", result
    assert time.monotonic() - started < 1.5


def test_mutating_os_exit_cannot_consume_the_startup_allowance() -> None:
    """Registered module code cannot disarm both hard wall-clock owners.

    ``builtins.exec`` runs with normal imports, replaces the child process's shared
    ``os._exit`` attribute, then loops forever. The captured child primitive and the
    independent parent clock must still enforce the authored budget rather than the
    much larger startup grace.
    """
    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=0.2, startup_grace_seconds=3.0)
    started = time.monotonic()
    result = sandbox.run_tool(
        ToolSandboxRunRequest(
            module_path="builtins",
            callable_name="exec",
            shapes=[{"args": ["import os\nos._exit = lambda code: None\nwhile True:\n    pass\n"]}],
            timeout_seconds=0.2,
        )
    )

    assert result.status == "timed_out", result
    assert time.monotonic() - started < 1.5


def test_child_gil_starvation_cannot_consume_the_startup_allowance() -> None:
    """A separate parent clock remains runnable when the child watchdog is not.

    CPython's catastrophic regex path holds the child GIL, so its watchdog thread
    cannot be the sole wall-clock authority. The parent must kill the process group at
    the authored deadline, not after adding the startup grace.
    """
    sandbox = LocalSubprocessToolSandbox(default_timeout_seconds=0.2, startup_grace_seconds=3.0)
    started = time.monotonic()
    result = sandbox.run_tool(
        ToolSandboxRunRequest(
            module_path="re",
            callable_name="fullmatch",
            shapes=[{"args": ["(a+)+$", "a" * 50_000 + "!"]}],
            timeout_seconds=0.2,
        )
    )

    assert result.status == "timed_out", result
    assert time.monotonic() - started < 1.5


def test_a_partial_config_still_yields_a_bounded_sandbox() -> None:
    """A config missing fields must degrade to defaults, not raise.

    ``from_config`` read every ``tool_sandbox_*`` attribute directly, so any partially
    populated config object was an ``AttributeError``. That stayed hidden while only the
    standalone sandbox app called it; the moment the ``python_code`` node started resolving
    through the same factory, a config carrying just the sandbox on/off flag took 27 worker
    tests down with it.

    The rule matches ``sandbox_from_optional_config(None)``: a caller that cannot supply
    every field should still get a *bounded* sandbox rather than a crash.
    """
    from types import SimpleNamespace

    from caliber.tool_sandbox.service import LocalSubprocessToolSandbox

    sandbox = LocalSubprocessToolSandbox.from_config(
        SimpleNamespace(registered_tool_sandbox_enabled=False)  # type: ignore[arg-type]
    )
    defaults = LocalSubprocessToolSandbox()

    assert sandbox.default_timeout_seconds == defaults.default_timeout_seconds
    assert sandbox.max_memory_bytes == defaults.max_memory_bytes
    assert sandbox.max_open_files == defaults.max_open_files
    # Still bounded — the point is a safe fallback, not an unlimited one.
    assert sandbox.max_output_bytes > 0
