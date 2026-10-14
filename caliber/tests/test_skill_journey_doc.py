"""``docs/use/skills.md`` names real page actions and real SDK calls (alpha gap P1-20).

The page walks a user through author, preview, calibrate, review, apply and roll back, and
names each step twice: the control to press and the SDK call. A doc that names a button the
page no longer has is the kind of drift nobody notices until a tester follows it. This fails
when a bold control name in the journey table is missing from the UI sources, or an SDK call
is missing from the SDK.

They read repository files, so they skip when the tests run from an sdist without them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "use" / "skills.md"
UI = REPO / "caliber" / "caliber-ui" / "src"
SDK = REPO / "sdk" / "caliber-sdk" / "src" / "caliber_sdk"

pytestmark = pytest.mark.skipif(
    not (DOC.exists() and UI.exists() and SDK.exists()),
    reason="repository docs, UI and SDK sources not present",
)

_UI_FILES = (
    UI / "pages" / "Skills.tsx",
    UI / "pages" / "SkillWizard.tsx",
    UI / "pages" / "SkillDetail.tsx",
    UI / "components" / "SkillCalibrationJobs.tsx",
    UI / "components" / "versioning" / "VersionPanel.tsx",
)


def _journey_rows() -> list[list[str]]:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## The skill journey, step by step", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].isdigit():
            rows.append(cells)
    return rows


def test_the_journey_table_has_every_step() -> None:
    rows = _journey_rows()
    assert [row[1] for row in rows] == [
        "Author",
        "Preview",
        "Check selection",
        "Calibrate",
        "Review",
        "Apply",
        "Roll back",
    ]


def test_every_control_the_doc_names_exists_in_the_page() -> None:
    """Each **bold** control name in the "In the page" column is a string the UI renders."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in _UI_FILES)
    missing: list[str] = []
    for number, _step, page, _sdk, _result in _journey_rows():
        for control in re.findall(r"\*\*([^*]+)\*\*", page):
            # "Review & apply" is written with an entity in JSX.
            candidates = {control, control.replace("&", "&amp;")}
            if not any(candidate in source for candidate in candidates):
                missing.append(f"step {number}: {control!r}")
    assert not missing, "the doc names controls the page does not render: " + ", ".join(missing)


def test_every_sdk_call_the_doc_names_exists() -> None:
    sdk_source = "\n".join(path.read_text(encoding="utf-8") for path in SDK.rglob("*.py"))
    missing: list[str] = []
    for number, _step, _page, sdk, _result in _journey_rows():
        for resource, method in re.findall(r"`(\w+)\.(\w+)\(", sdk):
            if resource == "skills":
                found = re.search(rf"class SkillsAPI.*?def {method}\(", sdk_source, re.S)
            elif resource == "jobs":
                found = re.search(rf"class JobsAPI.*?def {method}\(", sdk_source, re.S)
            else:  # pragma: no cover - a new resource needs a new branch
                found = None
            if not found:
                missing.append(f"step {number}: {resource}.{method}()")
    assert not missing, "the doc names SDK calls that do not exist: " + ", ".join(missing)


def test_the_doc_states_the_three_honest_limits() -> None:
    # The doc wraps lines, so compare on collapsed whitespace.
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "Simulated evaluation — not evidence" in text
    assert "0 evaluation examples" in text
    assert "never rewrites history" in text
    assert "`editor` or `owner`" in text


def test_the_install_page_links_to_the_journey() -> None:
    install = (REPO / "docs" / "start" / "alpha-install.md").read_text(encoding="utf-8")
    assert "../use/skills.md#the-skill-journey-step-by-step" in install
