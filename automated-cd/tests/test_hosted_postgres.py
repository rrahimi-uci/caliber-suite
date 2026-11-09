"""Hosted task QA reaches a real PostgreSQL without giving the worker Docker access."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from autocd.config import load

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/automated-cd-run.yml"
SCRIPT = ROOT / "scripts/ci-local.sh"
URL = "postgresql+psycopg://caliber:caliber@localhost:5432/caliber_autocd_admin"


@pytest.fixture(scope="module")
def job():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["execute"]


def test_workflow_provides_a_postgres_service_the_worker_reaches_over_localhost(job):
    service = job["services"]["postgres"]
    assert service["image"] == "pgvector/pgvector:pg16"  # migration 0060 needs pgvector
    assert service["ports"] == ["5432:5432"]
    assert service["env"]["POSTGRES_USER"] == "caliber"
    # Probe the database that exists: without -d, pg_isready connects to a database named
    # after the user and floods the server log with `database "caliber" does not exist`.
    assert f'pg_isready -U caliber -d {service["env"]["POSTGRES_DB"]}"' in service["options"]
    assert job["env"]["CALIBER_CI_LOCAL_POSTGRES_URL"] == URL
    # The URL names the service's own user, password and database.
    assert f"caliber:{service['env']['POSTGRES_PASSWORD']}@localhost:5432/" in URL
    assert URL.endswith("/" + service["env"]["POSTGRES_DB"])


def test_workflow_never_enables_postgres_cases_for_every_qa_command(job):
    """CALIBER_TEST_POSTGRES_URL would turn on PostgreSQL cases in the full coverage suite."""
    assert "CALIBER_TEST_POSTGRES_URL" not in job["env"]
    for step in job["steps"]:
        assert "CALIBER_TEST_POSTGRES_URL" not in step.get("env", {})


def test_the_audit_task_runs_the_postgres_scenarios_it_requires():
    commands = load().issue_checks[1144]
    assert commands == ("scripts/ci-local.sh egi-scenarios-postgres migration-parity-postgres",)


def _run_job(tmp_path: Path, job_name: str, url: str | None):
    """Run the real ci-local.sh with a fake Python venv and a recording fake docker."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(SCRIPT, scripts / "ci-local.sh")
    python = tmp_path / "caliber/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\n"
        'python3 -c \'import json,os,sys;open(sys.argv[1],"a").write(json.dumps('
        '{"argv":sys.argv[2:],"url":os.environ.get("CALIBER_TEST_POSTGRES_URL")})+"\\n")\' '
        f'{tmp_path / "python.log"} "$@"\n'
    )
    python.chmod(0o755)
    docker = tmp_path / "bin/docker"
    docker.parent.mkdir()
    docker.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "docker.log"}\nexit 1\n')
    docker.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CALIBER_TEST_POSTGRES_URL", "CALIBER_CI_LOCAL_POSTGRES_URL"}
    }
    environment["PATH"] = f"{docker.parent}{os.pathsep}{environment['PATH']}"
    # The runner refuses to start beside another pytest; this test must not depend on that.
    environment["CI_LOCAL_ALLOW_CONCURRENT"] = "1"
    if url:
        environment["CALIBER_CI_LOCAL_POSTGRES_URL"] = url
    result = subprocess.run(
        ["bash", str(scripts / "ci-local.sh"), job_name],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    log = tmp_path / "python.log"
    recorded = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    # The runner's own preflight also calls the interpreter; only pytest runs matter here.
    calls = [call for call in recorded if call["argv"][:2] == ["-m", "pytest"]]
    return result, calls, (tmp_path / "docker.log").exists()


def test_egi_scenarios_use_the_external_server_and_never_touch_docker(tmp_path: Path):
    result, calls, used_docker = _run_job(tmp_path, "egi-scenarios-postgres", URL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not used_docker
    assert calls == [
        {"argv": ["-m", "pytest", "tests/egi_scenarios", "--no-cov", "-q"], "url": URL}
    ]


def test_migration_parity_uses_the_external_server_and_never_touches_docker(tmp_path: Path):
    result, calls, used_docker = _run_job(tmp_path, "migration-parity-postgres", URL)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not used_docker
    assert len(calls) == 1 and calls[0]["url"] == URL
    argv = calls[0]["argv"]
    assert argv[:2] == ["-m", "pytest"] and argv[-4:] == ["-k", "postgresql", "--no-cov", "-q"]
    # CI's list, including the file that had drifted out of the local runner.
    assert "tests/test_routes_verification_queue.py" in argv
    assert "tests/test_upgrade_from_alpha.py" in argv


@pytest.mark.parametrize("job_name", ["egi-scenarios-postgres", "migration-parity-postgres"])
def test_without_an_external_server_the_docker_path_is_unchanged(tmp_path: Path, job_name: str):
    result, calls, used_docker = _run_job(tmp_path, job_name, None)
    assert used_docker and calls == []
    assert result.returncode == 1, result.stdout + result.stderr  # fake docker refuses to run
