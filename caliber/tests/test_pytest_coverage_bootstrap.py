from __future__ import annotations

import os
import sys
from importlib import import_module
from pathlib import Path

from caliber import pytest_coverage_bootstrap as bootstrap


def test_looks_like_pytest_recognizes_common_entrypoints() -> None:
    assert bootstrap.looks_like_pytest(["pytest"])
    assert bootstrap.looks_like_pytest(["/tmp/.venv/bin/pytest", "-q"])
    assert bootstrap.looks_like_pytest(["python", "-m", "pytest", "-q"])
    assert not bootstrap.looks_like_pytest(["python", "app.py"])


def test_assign_unique_coverage_file_uses_pytest_cache(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COVERAGE_FILE", raising=False)

    target = bootstrap.assign_unique_coverage_file()

    configured = Path(os.environ["COVERAGE_FILE"])
    assert target == configured
    assert configured.parent == tmp_path / ".pytest_cache" / "coverage"
    assert configured.name.startswith(".coverage.")
    assert configured.parent.is_dir()


def test_assign_unique_coverage_file_preserves_explicit_override(
    monkeypatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "custom.coverage"
    monkeypatch.setenv("COVERAGE_FILE", str(explicit))
    monkeypatch.chdir(tmp_path)

    target = bootstrap.assign_unique_coverage_file()

    assert target == explicit
    assert os.environ["COVERAGE_FILE"] == str(explicit)


def test_isolate_default_xml_coverage_report_rewrites_inline_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    args = ["--cov-report=xml", "-q"]

    target = bootstrap.isolate_default_xml_coverage_report(args, pid=12345)

    assert target == tmp_path / ".pytest_cache" / "coverage" / "coverage.12345.xml"
    assert args[0] == f"--cov-report=xml:{target}"


def test_isolate_default_xml_coverage_report_rewrites_split_flag(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    args = ["--cov-report", "xml", "-q"]

    target = bootstrap.isolate_default_xml_coverage_report(args, pid=67890)

    assert target == tmp_path / ".pytest_cache" / "coverage" / "coverage.67890.xml"
    assert args[1] == f"xml:{target}"


def test_isolate_default_xml_coverage_report_preserves_explicit_destination(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    args = ["--cov-report=xml:custom.xml", "-q"]

    target = bootstrap.isolate_default_xml_coverage_report(args, pid=777)

    assert target is None
    assert args[0] == "--cov-report=xml:custom.xml"


def test_bootstrap_plugin_sets_coverage_file_on_import(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COVERAGE_FILE", raising=False)
    monkeypatch.delenv("CALIBER_PYTEST_UNIQUE_COVERAGE", raising=False)
    monkeypatch.setattr(sys, "argv", ["pytest", "-q"])
    sys.modules.pop("caliber._pytest_cov_plugin", None)

    import_module("caliber._pytest_cov_plugin")

    configured = Path(os.environ["COVERAGE_FILE"])
    assert configured.parent == tmp_path / ".pytest_cache" / "coverage"
    assert configured.name.startswith(".coverage.")


def test_bootstrap_plugin_rewrites_default_xml_report(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CALIBER_PYTEST_UNIQUE_COVERAGE", raising=False)
    sys.modules.pop("caliber._pytest_cov_plugin", None)
    plugin = import_module("caliber._pytest_cov_plugin")
    args = ["--cov-report=xml", "-q"]

    plugin.pytest_load_initial_conftests(None, None, args)

    assert args[0].startswith("--cov-report=xml:")
    assert args[0].endswith(".xml")
