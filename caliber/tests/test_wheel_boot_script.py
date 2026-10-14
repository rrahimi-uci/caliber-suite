"""The wheel boot gate fails when the built wheel loses the SPA or the migrations (alpha gap P1-17).

``scripts/verify-wheel-boot.sh`` is what CI's ``package`` job runs against the wheel it just
built. The install-and-boot half needs network access and a clean venv, so CI is where it
runs end to end. The half that decides whether a wheel is complete is a file-list check
that runs before anything is installed, and it is exercised here against synthetic wheels
so a regression in the gate itself is caught offline.

They read repository files, so they skip when the tests run from an sdist without them.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify-wheel-boot.sh"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
CI_LOCAL = REPO / "scripts" / "ci-local.sh"

pytestmark = pytest.mark.skipif(
    not (SCRIPT.exists() and CI_WORKFLOW.exists()), reason="repository scripts not present"
)

COMPLETE = (
    "caliber/ui/index.html",
    "caliber/ui/assets/index-abc.js",
    "caliber/db/migrations/env.py",
    "caliber/db/migrations/versions/0001_initial.py",
)


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash not available")
    return bash


def _wheel(tmp_path: Path, *, omit: tuple[str, ...] = ()) -> Path:
    path = tmp_path / "caliber_suite-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("caliber/__init__.py", "")
        for name in COMPLETE:
            if not any(name.startswith(prefix) for prefix in omit):
                archive.writestr(name, "x")
    return path


def _run(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_bash(), str(SCRIPT), "--python", sys.executable, *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_script_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("omit", "expected"),
    [
        (("caliber/ui/",), "caliber/ui/index.html is missing"),
        (("caliber/ui/index.html",), "caliber/ui/index.html is missing"),
        (("caliber/ui/assets/",), "caliber/ui/assets/ is empty"),
        (("caliber/db/migrations/",), "caliber/db/migrations/env.py is missing"),
        (("caliber/db/migrations/env.py",), "caliber/db/migrations/env.py is missing"),
        (
            ("caliber/db/migrations/versions/",),
            "caliber/db/migrations/versions/ has no revisions",
        ),
    ],
)
def test_a_wheel_without_the_spa_or_migrations_fails(
    tmp_path: Path, omit: tuple[str, ...], expected: str
) -> None:
    result = _run(_wheel(tmp_path, omit=omit))
    assert result.returncode == 1
    assert expected in result.stderr
    assert "the wheel is missing required content" in result.stderr
    # It stopped before installing anything.
    assert "2. Install the wheel" not in result.stdout


def test_every_missing_piece_is_reported_in_one_run(tmp_path: Path) -> None:
    result = _run(_wheel(tmp_path, omit=("caliber/ui/", "caliber/db/migrations/")))
    assert result.returncode == 1
    for fragment in ("index.html", "assets/", "env.py", "versions/"):
        assert fragment in result.stderr


def test_a_complete_wheel_passes_the_file_list_check(tmp_path: Path) -> None:
    """The gate is not a constant failure: a complete wheel gets past step 1.

    The synthetic wheel cannot actually install, so the script fails later, in step 2.
    """
    result = _run(_wheel(tmp_path))
    assert "ok: SPA and migrations are in the wheel" in result.stdout
    assert "missing required content" not in result.stderr


def test_usage_errors(tmp_path: Path) -> None:
    no_wheel = subprocess.run(
        [_bash(), str(SCRIPT)], capture_output=True, text=True, check=False, timeout=30
    )
    assert no_wheel.returncode == 2
    assert "usage:" in no_wheel.stderr
    missing = _run(tmp_path / "nope.whl")
    assert missing.returncode == 1
    assert "no such wheel" in missing.stderr


def test_the_package_job_runs_the_boot_gate_in_ci_and_locally() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    package = workflow.split("\n  package:\n", 1)[1].split("\n  alpha-journey:\n", 1)[0]
    assert "scripts/verify-wheel-boot.sh" in package
    assert "caliber/dist/" in package or "dist/*.whl" in package
    local = CI_LOCAL.read_text(encoding="utf-8")
    # The job body ends where the next section's comment starts (the gitleaks note).
    body = local.split("job_package()", 1)[1].split("# gitleaks is what CI uses", 1)[0]
    assert "scripts/verify-wheel-boot.sh" in body


STACK_SCRIPT = REPO / "scripts" / "verify-compose-stack.sh"
STACK_WORKFLOW = REPO / ".github" / "workflows" / "stack-smoke.yml"


@pytest.mark.skipif(
    not (STACK_SCRIPT.exists() and STACK_WORKFLOW.exists()), reason="stack smoke files not present"
)
class TestStackSmoke:
    def test_script_parses_and_is_executable(self) -> None:
        assert STACK_SCRIPT.stat().st_mode & stat.S_IXUSR
        result = subprocess.run(
            [_bash(), "-n", str(STACK_SCRIPT)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    def test_the_workflow_is_scheduled_and_runs_the_script(self) -> None:
        workflow = STACK_WORKFLOW.read_text(encoding="utf-8")
        assert "schedule:" in workflow
        assert "cron:" in workflow
        assert "workflow_dispatch:" in workflow
        assert "scripts/verify-compose-stack.sh" in workflow

    def test_the_script_builds_both_images_and_probes_them(self) -> None:
        script = STACK_SCRIPT.read_text(encoding="utf-8")
        # ``up -d --build`` builds every service in the app profile, including caliber and mlflow.
        assert "--profile app" in script
        assert "up -d --build" in script
        for needle in (
            "experiments/search",
            "/readiness",
            "/caliber/",
            "registered-models",
            "is_admin",
        ):
            assert needle in script, needle

    def test_the_script_never_reads_a_developers_own_env(self) -> None:
        script = STACK_SCRIPT.read_text(encoding="utf-8")
        assert "COMPOSE_DISABLE_ENV_FILE=1" in script
        assert "--env-file deploy/.env.example" in script
        assert "--env-file .env.example" in script

    def test_the_script_runs_as_its_own_project_and_refuses_to_share_a_stack(self) -> None:
        """``down -v`` at the end must never delete a developer's volumes."""
        script = STACK_SCRIPT.read_text(encoding="utf-8")
        assert 'PROJECT="caliber-stack-smoke"' in script
        assert 'docker compose -p "$PROJECT"' in script
        assert "another CALIBER stack owns these containers" in script
        # The refusal comes before anything is started.
        assert script.index("another CALIBER stack owns") < script.index("up -d --build")
