"""``docs/use/workflows.md`` and the install page state what a workflow needs (alpha gap P1-22).

The node-prerequisite table must say what the catalog says, and the install page must name the
settings that decide whether a run can pause. They read repository files, so they skip when the
tests run from an sdist without them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from caliber.workflows.component_catalog import _COMPONENT_REQUIRES

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "use" / "workflows.md"
INSTALL = REPO / "docs" / "start" / "alpha-install.md"
CONFIG = REPO / "caliber" / "src" / "caliber" / "config.py"

pytestmark = pytest.mark.skipif(
    not (DOC.exists() and INSTALL.exists()), reason="repository docs not present"
)


def _table() -> dict[str, str]:
    """``node type -> the Needs cell`` from the "What each node needs" table."""
    section = DOC.read_text(encoding="utf-8").split("## What each node needs", 1)[1]
    section = section.split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("`"):
            for node in re.findall(r"`([a-z_]+)`", cells[0]):
                rows[node] = cells[1]
    return rows


#: The words each requirement id is expected to be described with in the table.
_WORDS = {
    "real_llm": "real LLM",
    "object_store": "object store",
    "mcp_server": "MCP server",
    "entrypoint_allowlist": "CALIBER_EXTERNAL_APP_ENTRYPOINT_ALLOWLIST",
    "run_queue": "run queue",
}


def test_the_table_lists_exactly_the_nodes_the_catalog_says_need_something() -> None:
    needing = {node for node, needs in _COMPONENT_REQUIRES.items() if needs}
    assert set(_table()) == needing, (
        "docs/use/workflows.md and the component catalog disagree about which nodes have "
        f"prerequisites: {sorted(set(_table()) ^ needing)}"
    )


def test_each_row_describes_the_requirement_the_catalog_declares() -> None:
    table = _table()
    for node, needs in _COMPONENT_REQUIRES.items():
        for requirement in needs:
            assert _WORDS[requirement] in table[node], (node, requirement, table[node])


def test_the_doc_states_the_python_code_trust_and_how_calibration_is_scored() -> None:
    text = " ".join(DOC.read_text(encoding="utf-8").split())
    assert "containment, not isolation" in text
    assert "the author is trusted" in text
    assert "../use/tools.md#who-is-trusted-to-write-tools" in text
    assert "Simulated evaluation" in text
    assert "`eval_results.simulated`" in text
    assert "`eval_results.scorer`" in text
    assert "structural" in text


def test_the_install_page_names_the_three_settings_and_what_changes_when_they_are_off() -> None:
    text = " ".join(INSTALL.read_text(encoding="utf-8").split())
    config = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    for setting in (
        "CALIBER_WORKFLOW_RUN_QUEUE_ENABLED",
        "CALIBER_WORKFLOW_RUN_CHECKPOINTING_ENABLED",
        "CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED",
        "CALIBER_APPROVAL_ALLOW_SELF_APPROVAL",
    ):
        assert setting in text, setting
        if config:
            assert setting in config, f"{setting} is documented but not a real setting"
    assert "approval_readiness.status" in text
    assert "configuration_required" in text
    assert "required_role" in text
    assert "#waits-approvals-and-queued-runs" in DOC.read_text(encoding="utf-8")


def test_the_marker_names_the_doc_uses_are_the_ones_the_stage_writes() -> None:
    stages = (
        REPO / "caliber" / "src" / "caliber" / "orchestrator" / "workflow_stages.py"
    ).read_text(encoding="utf-8")
    for marker in ('"simulated"', '"scorer"', '"provider"'):
        assert marker in stages, marker
