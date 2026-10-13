"""The alpha install path stays documented, scripted and consistent (alpha gap P0-5).

``docs/start/alpha-install.md`` is only useful if it is the same procedure that
``scripts/verify-alpha-install.sh`` executes in CI. These tests keep the two aligned and
keep every onboarding page honest about the steps whose omission broke the first install
(migration before the first start, and the role variables).

They read repository files, so they skip when the tests run from an sdist without them.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "docs" / "start" / "alpha-install.md"
SCRIPT = REPO / "scripts" / "verify-alpha-install.sh"
MAKEFILE = REPO / "Makefile"

pytestmark = pytest.mark.skipif(
    not (PAGE.exists() and SCRIPT.exists()), reason="repository docs and scripts not present"
)

_EXPORT = re.compile(r"^export\s+((?:CALIBER|MLFLOW)_[A-Z0-9_]+)=", re.MULTILINE)


def _exports(text: str) -> set[str]:
    return set(_EXPORT.findall(text))


def test_script_is_executable_and_parses() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash not available")
    result = subprocess.run([bash, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_page_exports_every_variable_the_script_exports() -> None:
    """If a variable is needed to make the install work, the page must say so."""
    script_vars = _exports(SCRIPT.read_text(encoding="utf-8"))
    page_vars = _exports(PAGE.read_text(encoding="utf-8"))
    assert script_vars, "the script sets no CALIBER_/MLFLOW_ variables?"
    missing = script_vars - page_vars
    assert not missing, f"alpha-install.md is missing exports the verified script uses: {missing}"


def test_the_page_and_script_agree_on_the_commands() -> None:
    page = PAGE.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    for fragment in (
        "caliber-migrate",
        "mlflow server",
        "--backend-store-uri",
        "uvicorn caliber.server:create_app --factory",
        "--find-links",
        "caliber-sdk caliber-cli caliber-suite",
    ):
        assert fragment in page, f"page lost: {fragment}"
        assert fragment in script, f"script lost: {fragment}"


def test_migration_comes_before_the_server_starts_on_the_page() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert page.index("caliber-migrate") < page.index("uvicorn caliber.server:create_app")


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "caliber/README.md",
        "docs/operate/local-bring-up.md",
    ],
)
def test_native_startup_pages_run_the_migration_and_set_roles(relative: str) -> None:
    text = (REPO / relative).read_text(encoding="utf-8")
    assert "caliber-migrate" in text, f"{relative} has no migration step"
    assert text.index("caliber-migrate") < text.index("uvicorn caliber.server:create_app"), relative
    assert "CALIBER_ADMIN_USERS" in text, f"{relative} does not set the role variables"


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "caliber/README.md",
        "docs/operate/local-bring-up.md",
        "docs/start/five-minute-quickstart.md",
    ],
)
def test_onboarding_pages_link_the_alpha_install_page(relative: str) -> None:
    assert "alpha-install" in (REPO / relative).read_text(encoding="utf-8"), relative


def test_the_page_is_registered_in_the_docs_site() -> None:
    registry = (REPO / "docs-site" / "build-docs.mjs").read_text(encoding="utf-8")
    assert "start/alpha-install.md" in registry


def test_the_script_exercises_the_acceptance_checks() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    for needle in ("is_admin", "/projects", "/prompts", "readiness", "registered-models"):
        assert needle in script, needle


def test_make_check_refuses_an_unsupported_python(tmp_path: Path) -> None:
    """``make setup`` must stop with an actionable message before creating a venv."""
    make = shutil.which("make")
    if make is None or not MAKEFILE.exists():  # pragma: no cover
        pytest.skip("make or the root Makefile is not available")
    shim = tmp_path / "python3.14"
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "Python 3.14.1"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    result = subprocess.run(
        [make, "-C", str(REPO), "check", f"PYTHON={shim}"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "unsupported Python" in output
    assert "3.10-3.12" in output
    assert "PYTHON=python3.12 make setup" in output
    assert not (REPO / ".venv-guard-probe").exists()


def test_make_setup_does_not_create_a_venv_on_an_unsupported_python(tmp_path: Path) -> None:
    make = shutil.which("make")
    if make is None or not MAKEFILE.exists():  # pragma: no cover
        pytest.skip("make or the root Makefile is not available")
    shim = tmp_path / "python3.14"
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "Python 3.14.1"; exit 0; fi\nexit 1\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)

    result = subprocess.run(
        [make, "-C", str(REPO), "-n", "setup", f"PYTHON={shim}"],
        capture_output=True,
        text=True,
        check=False,
    )

    # ``-n`` prints the recipe without running it. The guard is a prerequisite of
    # ``setup`` (``check``), so a dry run still shows ``check`` first, and the venv
    # command is only reachable after it succeeds.
    recipe = result.stdout
    assert recipe.index("sys.version_info") < recipe.index("-m venv")
