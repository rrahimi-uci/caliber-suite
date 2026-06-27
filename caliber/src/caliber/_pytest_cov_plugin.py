"""Early pytest plugin for repo-local coverage isolation."""

from __future__ import annotations

from typing import Any

from .pytest_coverage_bootstrap import (
    bootstrap_pytest_coverage_env,
    isolate_default_xml_coverage_report,
)

# The plugin is loaded via ``-p caliber._pytest_cov_plugin`` before pytest-cov
# starts, so running the bootstrap at import time is early enough to influence
# Coverage(...). The hook keeps the behavior idempotent if pytest reloads the
# plugin during its early startup sequence.
bootstrap_pytest_coverage_env()


def pytest_load_initial_conftests(
    early_config: Any,
    parser: Any,
    args: list[str],
) -> None:  # pragma: no cover - exercised via pytest startup
    del early_config, parser
    bootstrap_pytest_coverage_env(args)
    isolate_default_xml_coverage_report(args)
