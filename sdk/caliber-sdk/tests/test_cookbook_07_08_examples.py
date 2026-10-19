"""Cookbooks 07 and 08: the scripts, their asset files, and what they refuse to pass.

The scripts run against ``cookbook_07_08_fake_server``, which runs the real ``classify`` code and
the real decision tables from the asset manifests. Running them against a live stack is how the
queue, the approval rules and the deploy gate are verified (see the lab READMEs).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from cookbook_07_08_fake_server import FakeCaliber

from examples.cookbooks import cookbook_07_support_triage_copilot as lab07
from examples.cookbooks import cookbook_08_incident_response_copilot as lab08

COOKBOOKS = Path(__file__).resolve().parents[3] / "docs-site/cookbooks"
ASSETS_07 = COOKBOOKS / "07-support-triage-resolution-loop/assets"
ASSETS_08 = COOKBOOKS / "08-incident-response-commander/assets"


def run_lab(module: Any, fake: FakeCaliber, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
    lines: list[str] = []
    result = module.run(
        fake.client_for("admin"),
        project_client=fake.client_for,
        suffix="t",
        report=lines.append,
        **kwargs,
    )
    return result, lines


# ---------------------------------------------------------------------------
# The asset files
# ---------------------------------------------------------------------------


def test_ticket_dataset_has_the_documented_shape() -> None:
    rows = lab07.load_cases(ASSETS_07 / "dataset/ticket-cases.jsonl")
    assert len(rows) == 44
    assert len({row["id"] for row in rows}) == 44
    assert sum(row["expected"]["requires_approval"] for row in rows) == 15
    assert {row["expected"]["decision"] for row in rows} == {
        "reply",
        "clarify",
        "escalate_support",
        "escalate_bug",
    }


def test_incident_dataset_has_the_documented_shape() -> None:
    rows = lab08.load_cases(ASSETS_08 / "dataset/incident-cases.jsonl")
    assert len(rows) == 41
    assert [row["id"] for row in rows if row["expected"]["requires_approval"]] == [
        "IN01",
        "IN11",
        "IN12",
        "IN15",
        "IN23",
    ]
    for row in rows:
        assert (
            row["expected"]["answer"]
            == f"{row['expected']['severity']} {row['expected']['action']}"
        )


def test_the_manifest_code_is_the_shipped_file() -> None:
    manifest = lab07.load_manifest(ASSETS_07)
    assert (
        manifest["nodes"]["classify"]["code"]
        == (ASSETS_07 / "workflows/classify-ticket.py").read_text()
    )


def test_a_manifest_that_drifted_from_its_code_file_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "assets"
    shutil.copytree(ASSETS_07, copy)
    (copy / "workflows/classify-ticket.py").write_text(
        "# edited\n" + (ASSETS_07 / "workflows/classify-ticket.py").read_text()
    )
    with pytest.raises(lab07.LabVerificationError, match="no longer matches"):
        lab07.load_manifest(copy)


# ---------------------------------------------------------------------------
# Lab 07
# ---------------------------------------------------------------------------


def test_lab_07_verifies_every_documented_outcome() -> None:
    fake = FakeCaliber(approval_class="staging")
    result, lines = run_lab(lab07, fake)
    assert not [line for line in lines if line.startswith("FAIL")]
    assert result["batch"]["decisions"] == {
        "clarify": 10,
        "escalate_bug": 11,
        "escalate_support": 12,
        "reply": 11,
    }
    assert result["batch"]["waiting_approval"] == 15
    assert result["regression"] == {"caught": ["TK29", "TK30", "TK35"], "after_rollback": 0}
    assert result["promotion"] == "approved"


def test_lab_07_without_a_staging_approval_policy_the_promotion_rotates() -> None:
    fake = FakeCaliber(approval_class="none")
    result, lines = run_lab(lab07, fake)
    assert result["promotion"] == "rotated"
    assert not [line for line in lines if line.startswith("FAIL")]


def test_lab_07_fails_when_the_server_routes_a_ticket_wrongly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCaliber()
    original = fake.execute_triage

    def wrong(nodes: dict[str, Any], text: str) -> dict[str, Any]:
        outcome = original(nodes, text)
        if "refund" in text and "charged twice for October" in text:
            outcome["status"] = "completed"
            outcome["path"] = ["start", "classify", "triage_router", "draft_reply", "final"]
        return outcome

    monkeypatch.setattr(fake, "execute_triage", wrong)
    with pytest.raises(lab07.LabVerificationError, match="check"):
        run_lab(lab07, fake)


def test_lab_07_stops_with_a_setup_error_when_the_approver_lacks_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cookbook_07_08_fake_server import TOKENS

    monkeypatch.setitem(TOKENS, "tok-lena", ("lena", ["caliber.operator"], False))
    with pytest.raises(lab07.SetupError, match="CALIBER_APPROVER_USERS"):
        run_lab(lab07, FakeCaliber())


# ---------------------------------------------------------------------------
# Lab 08
# ---------------------------------------------------------------------------


def test_lab_08_verifies_every_documented_outcome() -> None:
    fake = FakeCaliber(approval_class="production")
    result, lines = run_lab(lab08, fake)
    assert not [line for line in lines if line.startswith("FAIL")]
    assert result["gate"]["good"] == 1.0
    assert result["gate"]["bad"] < 1.0
    assert result["batch"]["waiting_approval"] == ["IN01", "IN11", "IN12", "IN15", "IN23"]
    assert result["batch"]["actions"]["rollback"] == 5


def test_lab_08_fails_when_the_gate_lets_a_bad_version_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCaliber(approval_class="production")
    real_gate = fake.gate

    def lenient(version: dict[str, Any]) -> dict[str, Any] | None:
        gate = real_gate(version)
        if gate is not None:
            gate["passed"] = True
            gate["runs"][0]["passed"] = True
        return gate

    monkeypatch.setattr(fake, "gate", lenient)
    with pytest.raises(lab08.LabVerificationError, match="check"):
        run_lab(lab08, fake)


def test_lab_08_the_verdict_comes_from_the_dataset_tables() -> None:
    fake = FakeCaliber()
    manifest = json.loads((ASSETS_08 / "workflows/incident-commander.workflow.json").read_text())
    for row in lab08.load_cases(ASSETS_08 / "dataset/incident-cases.jsonl"):
        outcome = fake.execute_incident(manifest["nodes"], json.dumps(row["input"]), "queue")
        verdict = outcome["steps"][0]["output_by_port"]["result"]
        assert verdict["verdict"] == row["expected"]["answer"], row["id"]
