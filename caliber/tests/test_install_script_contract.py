"""``scripts/install.sh`` fails safely and its ``caliber`` command behaves as documented.

The installer's happy path downloads a release and resolves dependencies from PyPI, so it is
exercised end to end by ``scripts/verify-installer.sh`` in the "Alpha install" CI job. These
tests cover what must hold offline: argument and interpreter refusals, checksum verification
before anything is installed, the guard against reinstalling under a running server, and the
generated ``caliber`` command's status/help/error behavior.

They read repository files, so they skip when the tests run from an sdist without them.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "install.sh"
VERIFY = REPO / "scripts" / "verify-installer.sh"
PAGE = REPO / "docs" / "start" / "alpha-install.md"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="repository scripts not present")

BASH = shutil.which("bash")


def _supported_python() -> str | None:
    """An interpreter the installer accepts (3.10-3.12), or None."""
    candidates = [sys.executable, "python3.12", "python3.11", "python3.10", "python3"]
    for candidate in candidates:
        path = shutil.which(candidate) or (candidate if Path(candidate).is_absolute() else None)
        if not path:
            continue
        probe = subprocess.run(
            [
                path,
                "-c",
                "import sys; print(sys.version_info[:2] >= (3, 10) and sys.version_info[:2] <= (3, 12))",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() == "True":
            return path
    return None


def _run(
    *args: str, home: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if BASH is None:  # pragma: no cover
        pytest.skip("bash not available")
    full_env = {**os.environ, "HOME": str(home), **(env or {})}
    full_env.pop("CALIBER_HOME", None)
    full_env.pop("CALIBER_VERSION", None)
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
        timeout=120,
    )


def test_installer_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    if BASH is None:  # pragma: no cover
        pytest.skip("bash not available")
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


OPTIONS = {
    "--version",
    "--prefix",
    "--bin-dir",
    "--python",
    "--port",
    "--mlflow-port",
    "--with-llm",
    "--no-start",
    "--wheel-dir",
    "--base-url",
}


def test_help_documents_exactly_the_options_the_parser_accepts(tmp_path: Path) -> None:
    result = _run("--help", home=tmp_path)
    assert result.returncode == 0
    for option in OPTIONS:
        assert option in result.stdout, option
    script = SCRIPT.read_text(encoding="utf-8")
    parsed = set(re.findall(r"^\s+(--[a-z-]+)\)", script, re.MULTILINE))
    assert parsed == OPTIONS


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "unknown argument"),
        (["--port", "abc"], "not a port number"),
        (["--mlflow-port", "5x"], "not a port number"),
        (["--version", "1.0; rm -rf /"], "not a version"),
        (["--prefix"], "needs a value"),
    ],
)
def test_usage_errors_exit_2_before_doing_anything(
    tmp_path: Path, args: list[str], message: str
) -> None:
    result = _run(*args, home=tmp_path)
    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / ".caliber").exists()


def test_an_unsupported_python_is_refused(tmp_path: Path) -> None:
    shim = tmp_path / "python3.13"
    shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    shim.chmod(0o755)
    result = _run("--python", str(shim), "--prefix", str(tmp_path / "home"), home=tmp_path)
    assert result.returncode == 1
    assert "not Python 3.10-3.12" in result.stderr
    assert not (tmp_path / "home" / "venv").exists()


def _mirror(
    tmp_path: Path, version: str, *, tamper: str | None = None, omit: str | None = None
) -> str:
    """A file:// release mirror with three fake wheels and a SHA256SUMS listing them."""
    release = tmp_path / "mirror" / f"caliber-v{version}"
    release.mkdir(parents=True)
    lines = []
    for name in ("caliber_sdk", "caliber_cli", "caliber_suite"):
        wheel = f"{name}-{version}-py3-none-any.whl"
        content = f"fake {wheel}".encode()
        (release / wheel).write_bytes(content)
        digest = hashlib.sha256(
            b"something else" if wheel.startswith(tamper or "\0") else content
        ).hexdigest()
        if not wheel.startswith(omit or "\0"):
            lines.append(f"{digest}  {wheel}")
    (release / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"file://{tmp_path / 'mirror'}"


def test_a_tampered_wheel_is_rejected_and_removed_before_install(tmp_path: Path) -> None:
    python = _supported_python()
    if python is None:  # pragma: no cover
        pytest.skip("no Python 3.10-3.12 on this machine")
    url = _mirror(tmp_path, "9.9.9", tamper="caliber_cli")
    prefix = tmp_path / "home"
    result = _run(
        "--version",
        "9.9.9",
        "--base-url",
        url,
        "--prefix",
        str(prefix),
        "--python",
        python,
        "--no-start",
        home=tmp_path,
    )
    assert result.returncode == 1
    assert "checksum mismatch for caliber_cli-9.9.9-py3-none-any.whl" in result.stderr
    assert not (prefix / "downloads" / "9.9.9" / "caliber_cli-9.9.9-py3-none-any.whl").exists()
    assert not (prefix / "venv").exists(), "nothing may be installed after a failed verification"


def test_a_wheel_missing_from_the_checksum_list_is_rejected(tmp_path: Path) -> None:
    python = _supported_python()
    if python is None:  # pragma: no cover
        pytest.skip("no Python 3.10-3.12 on this machine")
    url = _mirror(tmp_path, "9.9.9", omit="caliber_suite")
    result = _run(
        "--version",
        "9.9.9",
        "--base-url",
        url,
        "--prefix",
        str(tmp_path / "home"),
        "--python",
        python,
        "--no-start",
        home=tmp_path,
    )
    assert result.returncode == 1
    assert "does not list caliber_suite-9.9.9-py3-none-any.whl" in result.stderr


def test_an_unknown_version_fails_with_a_pointer_to_the_releases(tmp_path: Path) -> None:
    python = _supported_python()
    if python is None:  # pragma: no cover
        pytest.skip("no Python 3.10-3.12 on this machine")
    (tmp_path / "mirror").mkdir()
    result = _run(
        "--version",
        "0.0.0",
        "--base-url",
        f"file://{tmp_path / 'mirror'}",
        "--prefix",
        str(tmp_path / "home"),
        "--python",
        python,
        home=tmp_path,
    )
    assert result.returncode == 1
    assert "could not download" in result.stderr
    assert "releases" in result.stderr


def test_the_installer_refuses_to_replace_a_running_install(tmp_path: Path) -> None:
    prefix = tmp_path / "home"
    (prefix / "bin").mkdir(parents=True)
    launcher = prefix / "bin" / "caliber"
    launcher.write_text('#!/bin/sh\n[ "$1" = status ] && exit 0\nexit 1\n', encoding="utf-8")
    launcher.chmod(0o755)
    python = _supported_python()
    if python is None:  # pragma: no cover
        pytest.skip("no Python 3.10-3.12 on this machine")
    result = _run("--prefix", str(prefix), "--python", python, home=tmp_path)
    assert result.returncode == 1
    assert "caliber down" in result.stderr


# ---------------------------------------------------------------- the generated command


def _launcher(tmp_path: Path, *, with_env: bool = True) -> Path:
    """Write the ``caliber`` command exactly as the installer does, for a fake install."""
    script = SCRIPT.read_text(encoding="utf-8")
    body = script.split("cat <<'LAUNCHER'\n", 1)[1].split("\nLAUNCHER\n", 1)[0]
    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    (home / "logs").mkdir()
    if with_env:
        (home / "caliber.env").write_text(
            "CALIBER_PORT=5999\nMLFLOW_PORT=5998\nCALIBER_DATABASE_URL=sqlite:///x.db\n",
            encoding="utf-8",
        )
    (home / "admin-password").write_text("generated-password-value", encoding="utf-8")
    path = home / "caliber"
    path.write_text(f"#!/usr/bin/env bash\nCALIBER_HOME={home}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _call(launcher: Path, *args: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:  # pragma: no cover
        pytest.skip("bash not available")
    return subprocess.run(
        [BASH, str(launcher), *args], capture_output=True, text=True, check=False, timeout=60
    )


def test_the_command_documents_its_subcommands(tmp_path: Path) -> None:
    result = _call(_launcher(tmp_path), "help")
    assert result.returncode == 0
    for sub in (
        "caliber up",
        "caliber down",
        "caliber status",
        "caliber logs",
        "caliber password",
        "caliber open",
    ):
        assert sub in result.stdout


def test_status_reports_stopped_and_exits_1(tmp_path: Path) -> None:
    result = _call(_launcher(tmp_path), "status")
    assert result.returncode == 1
    assert "mlflow: stopped" in result.stdout
    assert "caliber: stopped" in result.stdout


def test_status_ignores_a_stale_pid_file(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (launcher.parent / "run" / "caliber.pid").write_text(str(dead.pid), encoding="utf-8")
    result = _call(launcher, "status")
    assert result.returncode == 1
    assert "caliber: stopped" in result.stdout


def test_down_on_a_stopped_install_is_a_no_op(tmp_path: Path) -> None:
    result = _call(_launcher(tmp_path), "down")
    assert result.returncode == 0


def test_password_prints_the_generated_password(tmp_path: Path) -> None:
    result = _call(_launcher(tmp_path), "password")
    assert result.returncode == 0
    assert result.stdout.strip() == "generated-password-value"


def test_an_unknown_subcommand_exits_2(tmp_path: Path) -> None:
    result = _call(_launcher(tmp_path), "launch")
    assert result.returncode == 2
    assert "unknown command" in result.stderr


def test_a_missing_configuration_is_reported(tmp_path: Path) -> None:
    result = _call(_launcher(tmp_path, with_env=False), "up")
    assert result.returncode == 1
    assert "caliber.env is missing" in result.stderr


# A gunicorn-shaped server: the parent restarts its worker whenever it dies, and on SIGTERM
# takes a moment to shut down gracefully. The original `down` sent signals and returned at
# once, so the Linux CI run (where MLflow's gunicorn behaves like this) found processes still
# alive; `down` must wait for the whole process group and force-kill what is left.
_RESPAWNING_SERVER = """
import os, signal, sys, time

stopping = False
def on_term(*_):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, on_term)

def spawn():
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        while True:
            time.sleep(0.1)
    return pid

children = [spawn()]
with open(sys.argv[1], "w") as handle:
    handle.write("ready")
while not stopping:
    done, _ = os.waitpid(-1, os.WNOHANG)
    if done:
        children.append(spawn())
        with open(sys.argv[1] + ".respawned", "w") as handle:
            handle.write(str(children[-1]))
    time.sleep(0.05)
time.sleep(1.5)  # graceful shutdown, as gunicorn's master takes on Linux
for child in children:
    try:
        os.kill(child, signal.SIGTERM)
    except ProcessLookupError:
        pass
"""


def test_down_stops_a_server_whose_parent_restarts_its_workers(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    ready = tmp_path / "ready"
    server = subprocess.Popen(
        [sys.executable, "-c", _RESPAWNING_SERVER, str(ready)], start_new_session=True
    )
    # Reap it as soon as it exits, as init does for the launcher's orphaned services; an
    # unreaped zombie would keep its process group "alive" for `down`.
    reaper = threading.Thread(target=server.wait, daemon=True)
    reaper.start()
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "the stand-in server did not start"
        (launcher.parent / "run" / "mlflow.pid").write_text(str(server.pid), encoding="utf-8")

        result = _call(launcher, "down")

        # Checked the moment `down` returns, as verify-installer.sh does: a `down` that only
        # sends signals returns while the server is still shutting down.
        survivors = subprocess.run(
            ["pgrep", "-g", str(server.pid)], capture_output=True, text=True, check=False
        )
        assert survivors.stdout.strip() == "", f"processes survived: {survivors.stdout}"
        assert result.returncode == 0
        assert "stopped mlflow" in result.stdout
        assert not (launcher.parent / "run" / "mlflow.pid").exists()
        reaper.join(timeout=20)
        assert not reaper.is_alive()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(server.pid, signal.SIGKILL)


# ---------------------------------------------------------------- docs and CI wiring


def test_the_install_page_leads_with_the_one_command_install() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "scripts/install.sh" in page
    assert page.index("scripts/install.sh") < page.index("pip install --find-links")


def test_ci_runs_the_installer_end_to_end() -> None:
    assert VERIFY.exists() and VERIFY.stat().st_mode & stat.S_IXUSR
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/verify-installer.sh" in workflow
