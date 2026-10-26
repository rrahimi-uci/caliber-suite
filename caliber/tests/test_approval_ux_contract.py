"""The approval UX contract has its five sections, the eight denial codes, and an example that validates.

``docs/architecture/approval-ux-contract.md`` is what #523 builds the approver's panel from. The checks keep it
complete: every section the issue lists, every denial code with a message and a UI state, every allowed response
with an API call, and a fixture that is a real ``approval_request`` offering exactly those responses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from caliber.contracts import load_schema, validate_record

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "docs" / "architecture" / "approval-ux-contract.md"
EXAMPLE = (
    Path(__file__).parent / "fixtures" / "contracts" / "approval_request" / "valid_ux_example.json"
)

pytestmark = pytest.mark.skipif(not CONTRACT.exists(), reason="repository docs not present")

SECTIONS = (
    "## Fields shown",
    "## Allowed responses",
    "## Denial reasons",
    "## Accessibility",
    "## Wireframe",
)
DENIAL_CODES = (
    "stale_version",
    "expired",
    "superseded",
    "self_approval",
    "duplicate_approver",
    "role_revoked",
    "fenced_owner",
    "intent_mismatch",
)
RESPONSES = ("approve", "reject", "request_changes")


def section(text: str, heading: str) -> str:
    body = text.split(f"{heading}\n", 1)[1]
    return re.split(r"^## ", body, maxsplit=1, flags=re.M)[0]


def table_rows(body: str) -> list[list[str]]:
    rows = [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in body.splitlines()
        if line.startswith("|")
    ]
    return rows[2:]  # the header and the separator


def contract() -> str:
    return CONTRACT.read_text(encoding="utf-8")


def test_the_five_sections_are_present_in_order() -> None:
    headings = [line for line in contract().splitlines() if line.startswith("## ")]
    assert [h for h in headings if h in SECTIONS] == list(SECTIONS)


def test_every_denial_code_has_a_status_a_message_and_a_ui_state() -> None:
    rows = table_rows(section(contract(), "## Denial reasons"))
    by_code = {row[0].strip("`"): row for row in rows}
    assert tuple(by_code) == DENIAL_CODES
    for code, (_, status, message, state) in by_code.items():
        assert status in {"403", "409"}, code
        assert len(message) > 40 and message.startswith('"') and message.endswith('"'), code
        assert len(state) > 20, f"{code} has no UI state"


def test_every_allowed_response_has_an_api_call_a_body_and_a_resulting_status() -> None:
    rows = table_rows(section(contract(), "## Allowed responses"))
    assert tuple(row[0].strip("`") for row in rows) == RESPONSES
    for response, call, body, result in ((r[0], r[1], r[2], r[3]) for r in rows):
        assert call.startswith("`POST /workflow-runs/{run_id}/approvals/{approval_id}/"), response
        assert body.startswith("`{") and result, response


def test_every_displayed_field_names_its_schema_source() -> None:
    schema_fields = set(load_schema("approval_request", "v1")["properties"])
    rows = table_rows(section(contract(), "## Fields shown"))
    assert len(rows) >= 9
    cited = {name for row in rows for name in re.findall(r"`approval_request\.(\w+)`", row[1])}
    assert cited <= schema_fields, f"fields the schema does not have: {cited - schema_fields}"
    for needed in (
        "proposal_id",
        "intent_hash",
        "expected_resource_version",
        "evidence_hash",
        "policy_digest",
        "risk_class",
        "requested_by",
        "expires_at",
    ):
        assert needed in cited, needed


def test_the_accessibility_section_covers_focus_live_region_and_labels() -> None:
    body = section(contract(), "## Accessibility").lower()
    for needed in ("keyboard order", "focus on open", "aria-live", "label"):
        assert needed in body, needed


def test_the_example_is_an_approval_request_for_the_case_with_every_response() -> None:
    record = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert validate_record("approval_request", record) is None
    assert record["risk_class"] == "reversible_write"
    assert record["allowed_responses"] == list(RESPONSES)
    assert record["intent_hash"].startswith("sha256:") and record["proposal_id"] == "pr-0001"
