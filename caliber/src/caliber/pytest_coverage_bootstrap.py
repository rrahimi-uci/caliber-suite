"""Helpers for isolating pytest coverage artifacts per process.

`pytest-cov` already writes suffixed data files while tests execute, but each
pytest process still combines those suffix files back into one base
``data_file`` at shutdown. When multiple pytest commands run in parallel from
the same checkout, those combine steps can contend on the shared SQLite file.

We avoid that by assigning a unique ``COVERAGE_FILE`` early in pytest startup
whenever the caller has not already set one explicitly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PYTHON_MODULE_PYTEST_ARGC = 3
_PYTHON_SCRIPT_PYTEST_ARGC = 2
_COVERAGE_CACHE_SUBDIR = Path(".pytest_cache") / "coverage"
_DEFAULT_XML_REPORT = "xml"


def looks_like_pytest(argv: list[str] | None = None) -> bool:
    """Return True when the current process appears to be a pytest entrypoint."""
    effective_argv = sys.argv if argv is None else argv
    if not effective_argv:
        return False
    executable = Path(effective_argv[0]).name
    if executable.startswith("pytest") or executable == "py.test":
        return True
    if executable.startswith("python"):
        if (
            len(effective_argv) >= _PYTHON_MODULE_PYTEST_ARGC
            and effective_argv[1] == "-m"
            and effective_argv[2] == "pytest"
        ):
            return True
        if len(effective_argv) >= _PYTHON_SCRIPT_PYTEST_ARGC and Path(
            effective_argv[1]
        ).name.startswith("pytest"):
            return True
    return False


def _coverage_cache_dir(base_dir: Path | None = None) -> Path | None:
    root_dir = Path.cwd() if base_dir is None else base_dir
    cache_dir = root_dir / _COVERAGE_CACHE_SUBDIR
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return cache_dir


def assign_unique_coverage_file(base_dir: Path | None = None) -> Path | None:
    """Point coverage at a process-unique data file unless one is already set."""
    configured = os.environ.get("COVERAGE_FILE")
    if configured:
        return Path(configured)
    cache_dir = _coverage_cache_dir(base_dir)
    if cache_dir is None:
        return None
    target = cache_dir / f".coverage.{os.getpid()}"
    os.environ["COVERAGE_FILE"] = os.fspath(target)
    return target


def isolate_default_xml_coverage_report(
    args: list[str],
    *,
    base_dir: Path | None = None,
    pid: int | None = None,
) -> Path | None:
    """Rewrite default XML coverage reports to a process-unique file."""
    cache_dir = _coverage_cache_dir(base_dir)
    if cache_dir is None:
        return None
    effective_pid = os.getpid() if pid is None else pid
    target = cache_dir / f"coverage.{effective_pid}.xml"
    xml_value = f"{_DEFAULT_XML_REPORT}:{target}"
    replaced = False
    index = 0
    while index < len(args):
        arg = args[index]
        if (
            arg == "--cov-report"
            and index + 1 < len(args)
            and args[index + 1] == _DEFAULT_XML_REPORT
        ):
            args[index + 1] = xml_value
            replaced = True
            index += 2
            continue
        if arg == f"--cov-report={_DEFAULT_XML_REPORT}":
            args[index] = f"--cov-report={xml_value}"
            replaced = True
        index += 1
    return target if replaced else None


def bootstrap_pytest_coverage_env(argv: list[str] | None = None) -> Path | None:
    """Assign a unique coverage data file for pytest-like processes."""
    if os.environ.get("CALIBER_PYTEST_UNIQUE_COVERAGE", "1") == "0":
        return None
    if not looks_like_pytest(argv):
        return None
    return assign_unique_coverage_file()
