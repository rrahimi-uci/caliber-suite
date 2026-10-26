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


def test_every_denial_code_has_a_status_a_detection_point_a_message_and_a_ui_state() -> None:
    rows = table_rows(section(contract(), "## Denial reasons"))
    by_code = {row[0].strip("`"): row for row in rows}
    assert tuple(by_code) == DENIAL_CODES
    for code, cells in by_code.items():
        assert len(cells) == 5, f"{code}: expected code | HTTP | detected | message | UI state"
        _, status, detected, message, state = cells
        assert re.match(r"^(403|409)\b", status), code
        assert "decision" in detected or "commit" in detected, (
            f"{code} does not say where it is detected"
        )
        assert len(message) > 40 and message.startswith('"') and message.count('"') >= 2, code
        assert len(state) > 20, f"{code} has no UI state"
    assert "commit only" in by_code["fenced_owner"][2], (
        "a fence is never an answer to the decision call"
    )


def test_every_allowed_response_has_an_api_call_a_body_and_a_resulting_state() -> None:
    schema = load_schema("approval_request", "v1")
    assert tuple(schema["properties"]["allowed_responses"]["items"]["enum"]) == RESPONSES
    rows = table_rows(section(contract(), "## Allowed responses"))
    assert tuple(row[0].strip("`") for row in rows) == RESPONSES
    for response, call, body, result in ((r[0], r[1], r[2], r[3]) for r in rows):
        assert call.startswith("`POST /workflow-runs/{run_id}/approvals/{approval_id}/"), response
        assert body.startswith("`{") and result, response


def test_every_displayed_field_names_a_real_schema_source() -> None:
    schemas = {
        kind: set(load_schema(kind, "v1")["properties"])
        for kind in ("approval_request", "proposal", "policy_decision")
    }
    rows = table_rows(section(contract(), "## Fields shown"))
    assert len(rows) >= 10
    cited: dict[str, set[str]] = {kind: set() for kind in schemas}
    for row in rows:
        for kind, field in re.findall(
            r"`(approval_request|proposal|policy_decision)\.([\w]+)`", row[1]
        ):
            assert field in schemas[kind], f"{kind}.{field} is not a schema field"
            cited[kind].add(field)
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
        assert needed in cited["approval_request"], needed
    assert {"connector", "resource"} <= {
        f for f in ("connector", "resource") if "proposal.target." + f in contract()
    }


def test_view_fields_are_declared_as_not_stored() -> None:
    body = contract()
    for view_field in ("preview", "current_state", "requested_by_type", "evidence_url"):
        assert f"`{view_field}`" in body or f'"{view_field}"' in body, view_field
    assert "view field" in body and "record field" in body


def test_the_wireframe_shows_the_panel() -> None:
    wireframe = section(contract(), "## Wireframe")
    drawing = wireframe.split("```")[1]
    assert len(drawing.strip().splitlines()) >= 15
    for needed in (
        "Approve",
        "Request changes",
        "Reject",
        "Reason",
        "banner",
        "Before and after",
        "Expires",
    ):
        assert needed in drawing, needed
    assert drawing.index("Reason") < drawing.index("[ Approve ]"), (
        "the reason comes before the buttons"
    )


def test_the_accessibility_section_covers_focus_live_region_labels_and_disabled_actions() -> None:
    body = section(contract(), "## Accessibility").lower()
    for needed in (
        "keyboard order = visual order",
        "focus on open",
        'aria-live="polite"',
        'role="alert"',
        "aria-disabled",
        "aria-describedby",
        "not a dialog",
        "confirmation",
        "<dt>",
    ):
        assert needed in body, needed


def test_the_example_is_an_approval_request_for_the_case_with_every_response() -> None:
    record = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert validate_record("approval_request", record) is None
    assert record["risk_class"] == "reversible_write"
    assert record["allowed_responses"] == list(RESPONSES)
    assert record["intent_hash"].startswith("sha256:")
    assert record["proposal_id"] == "pr-ux-0001" and record["approval_request_id"] == "ar-ux-0001"
    assert record["intent_hash"] != record["policy_digest"], (
        "two different digests, not one value twice"
    )
    text = contract()
    assert record["proposal_id"] in text and record["approval_request_id"] in text, (
        "the wireframe uses the example"
    )
    assert "MORT-2026-000184" in text
