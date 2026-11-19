"""Regression tests for the root container startup launcher."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "start.sh"


def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - CI and supported dev hosts have bash.
        pytest.skip("bash is required to exercise start.sh")
    return bash


def _fake_docker(bin_dir: Path) -> Path:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$DOCKER_LOG"

if [[ "${1:-}" == "compose" && "${2:-}" == "version" ]]; then
  printf 'v2.20.0\n'
  exit 0
fi
if [[ "${1:-}" == "info" ]]; then
  exit 0
fi
if [[ "${1:-}" == "inspect" ]]; then
  target="${@: -1}"
  if [[ "$target" == "caliber-minio" && "${FOREIGN_PROJECT:-}" != "" ]]; then
    if [[ "$*" == *"--format"* ]]; then
      printf '%s\n' "$FOREIGN_PROJECT"
    else
      printf '{}\n'
    fi
    exit 0
  fi
  exit 1
fi
if [[ "${1:-}" == "compose" || "${1:-}" == "port" ]]; then
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return docker


def test_start_script_rejects_foreign_fixed_name_container_before_compose_up(
    tmp_path: Path,
) -> None:
    """A standalone MinIO container must not cause a partial stack mutation."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir)

    log = tmp_path / "docker.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "DOCKER_LOG": str(log),
            "COMPOSE_PROJECT_NAME": "caliber-infra",
            "FOREIGN_PROJECT": "caliber-minio",
        }
    )

    result = subprocess.run(
        [_bash(), str(SCRIPT)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 1
    assert "caliber-minio" in result.stderr
    assert "fixed-name containers belong to another project" in result.stderr
    assert "docker rm -f caliber-minio" in result.stderr
    assert " up " not in log.read_text(encoding="utf-8")


def test_start_script_is_executable_and_parseable() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    result = subprocess.run(
        [_bash(), "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
