"""``docs/use/tools.md`` states the trust boundary and names real controls (alpha gap P1-21).

A supported tool registry that executes code must say who it trusts. These tests keep that
statement, and the journey table next to it, from drifting away from what the page and the
configuration really are.

They read repository files, so they skip when the tests run from an sdist without them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "use" / "tools.md"
INSTALL = REPO / "docs" / "start" / "alpha-install.md"
UI = REPO / "caliber" / "caliber-ui" / "src"
CONFIG = REPO / "caliber" / "src" / "caliber" / "config.py"

pytestmark = pytest.mark.skipif(
    not (DOC.exists() and UI.exists() and CONFIG.exists()),
    reason="repository docs, UI and config sources not present",
)


def _text() -> str:
    # The doc wraps lines, so compare on collapsed whitespace.
    return " ".join(DOC.read_text(encoding="utf-8").split())


def _journey_rows() -> list[list[str]]:
    section = DOC.read_text(encoding="utf-8").split("## The tool journey, step by step", 1)[1]
    section = section.split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_the_doc_states_the_trust_boundary() -> None:
    text = _text()
    assert "## Who is trusted to write tools" in text
    assert "Tool authors are trusted." in text
    assert "a process boundary, not a container" in text
    assert "not** a container, virtual machine or seccomp boundary" in text
    assert "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST" in text
    assert "registered tool modules are unrestricted" in text
    assert "`python_code`" in text


def test_the_documented_settings_exist() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    for setting in (
        "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST",
        "CALIBER_MEMORY_ENABLED",
        "CALIBER_TOOL_SANDBOX_BACKEND",
    ):
        assert setting in config, setting


def test_the_readiness_text_the_doc_quotes_is_the_real_text() -> None:
    readiness = (REPO / "caliber" / "src" / "caliber" / "observability" / "readiness.py").read_text(
        encoding="utf-8"
    )
    assert "registered tool modules are unrestricted" in readiness


def test_the_install_page_explains_the_allowlist_and_links_the_tools_page() -> None:
    install = INSTALL.read_text(encoding="utf-8")
    assert "CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST" in install
    assert "../use/tools.md#who-is-trusted-to-write-tools" in install
    assert "../use/tools.md#the-tool-journey-step-by-step" in install


def test_the_journey_table_has_every_step() -> None:
    assert [row[1] for row in _journey_rows()] == [
        "Register",
        "Run it",
        "Save fixtures",
        "Calibrate",
        "Pin a baseline",
        "Use it in a workflow",
    ]


def test_every_control_the_journey_names_exists_in_the_page() -> None:
    """Each **bold** control name in the "In the page" column is a string the UI renders."""
    files = [
        UI / "pages" / "ToolRegistry.tsx",
        UI / "pages" / "ToolDetail.tsx",
        UI / "pages" / "ToolWizard.tsx",
        UI / "components" / "CalibrationPanel.tsx",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in files if path.exists())
    missing: list[str] = []
    for number, _step, page, _result in _journey_rows():
        for control in re.findall(r"\*\*([^*]+)\*\*", page):
            if control not in source:
                missing.append(f"step {number}: {control!r}")
    assert not missing, "the doc names controls the page does not render: " + ", ".join(missing)


def test_the_labels_the_doc_describes_are_rendered() -> None:
    badges = (UI / "components" / "ToolOriginBadges.tsx").read_text(encoding="utf-8")
    assert "Built-in" in badges
    assert "Unavailable" in badges
