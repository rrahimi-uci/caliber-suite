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


# ---------------------------------------------------------------------------
# The real-provider tier
# ---------------------------------------------------------------------------


def failed_checks(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL")]


def test_lab_07_on_a_real_stack_runs_the_model_variant_and_asserts_invariants() -> None:
    fake = FakeCaliber(approval_class="staging", real=True)
    result, lines = run_lab(lab07, fake, model_variant=True)

    assert not failed_checks(lines)
    assert result["mode"] == "real"
    variant = result["variant"]
    assert variant["tickets"] == 12
    assert 0.5 <= variant["label_agreement"] <= 1.0
    # nine of the twelve escalate or are routed to approval in the variant (every escalation waits)
    assert variant["paused_at_gate"] >= 1
    assert variant["calibration"]["simulated"] is False
    assert variant["calibration"]["scorer"] == "structural"
    assert any("cannot score the deterministic workflow" in line for line in lines)
    assert any("no escalation is released without a person" in line for line in lines)


def test_lab_07_reports_where_the_model_disagrees_with_the_rules() -> None:
    """The model is told by the stand-in to obey the injection; the guard still decides."""
    fake = FakeCaliber(real=True, model_overrides={"Ignore your instructions": "escalate_bug"})
    result, lines = run_lab(lab07, fake, model_variant=True)

    assert not failed_checks(lines)
    assert result["variant"]["disagreements"], "the injection ticket must be reported"
    # even then the escalation is not released: it paused at the approval gate
    assert result["variant"]["paused_at_gate"] >= 1


def test_lab_07_fails_when_the_model_variant_routes_most_tickets_wrongly() -> None:
    fake = FakeCaliber(real=True, model_overrides={"": "reply"})
    with pytest.raises(lab07.LabVerificationError, match="Evaluations scores the model variant"):
        run_lab(lab07, fake, model_variant=True)


def test_lab_07_fails_when_an_escalation_is_released_without_a_person(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCaliber(real=True)
    original = fake.execute_model_triage

    def releasing(nodes: dict[str, Any], text: str, mode: str) -> dict[str, Any]:
        # a broken guard: the queue releases the action instead of pausing for a person
        return original(nodes, text, "preview" if mode == "queue" else mode)

    monkeypatch.setattr(fake, "execute_model_triage", releasing)
    with pytest.raises(lab07.LabVerificationError, match="no escalation is released"):
        run_lab(lab07, fake, model_variant=True)


def test_lab_07_the_model_variant_needs_a_real_provider() -> None:
    with pytest.raises(lab07.SetupError, match="real provider"):
        run_lab(lab07, FakeCaliber(real=False), model_variant=True)


def test_lab_07_a_real_stack_that_echoes_the_simulated_reply_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCaliber(real=True)
    original = fake.execute_triage

    def echo(nodes: dict[str, Any], text: str) -> dict[str, Any]:
        outcome = original(nodes, text)
        if outcome["status"] == "completed" and "draft_reply" in outcome["path"]:
            outcome["output"] = "[support-reply] processed: x"
        return outcome

    monkeypatch.setattr(fake, "execute_triage", echo)
    with pytest.raises(lab07.LabVerificationError, match="check"):
        run_lab(lab07, fake)


def test_lab_08_on_a_real_stack_scores_the_workflow_and_runs_the_model_variant() -> None:
    fake = FakeCaliber(approval_class="production", real=True)
    result, lines = run_lab(lab08, fake, model_variant=True)

    assert not failed_checks(lines)
    assert result["mode"] == "real"
    assert result["evaluation"]["n_examples"] == lab08.EVAL_MAX_EXAMPLES
    assert result["evaluation"]["available_examples"] == 41
    assert result["evaluation"]["contains_expected"] >= 0.9
    assert result["calibration"] == {
        "status": "rejected",
        "n_examples": 20,
        "baseline_quality": 0.95,
        "candidates": 0,
        "provider": "openai",
        "simulated": False,
        "scorer": "structural",
    }
    variant = result["variant"]
    assert variant["verdicts_equal_tables"] == variant["incidents"] == 10
    assert variant["model_agrees_action"] == 10
    assert any("executor is a real model" in line for line in lines)


def test_lab_08_the_model_cannot_authorise_a_rollback() -> None:
    """A model that proposes a rollback for a conflicting-evidence incident changes nothing."""
    fake = FakeCaliber(
        approval_class="production",
        real=True,
        proposal_overrides={"IN05": {"severity": "sev1", "action": "rollback"}},
    )
    result, lines = run_lab(lab08, fake, model_variant=True)

    assert not failed_checks(lines)
    assert result["variant"]["verdicts_equal_tables"] == 10
    assert result["variant"]["model_disagreements"] == ["IN05"]


def test_lab_08_fails_when_a_real_stack_reports_the_simulated_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCaliber(approval_class="production", real=True)
    real_gate = fake.gate

    def says_fake(version: dict[str, Any]) -> dict[str, Any] | None:
        gate = real_gate(version)
        if gate is not None:
            gate["executor"] = {"provider": "fake", "deterministic": True}
        return gate

    monkeypatch.setattr(fake, "gate", says_fake)
    with pytest.raises(lab08.LabVerificationError, match="executor is a real model"):
        run_lab(lab08, fake)


def test_lab_08_fails_when_the_calibration_does_not_say_what_measured_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeCaliber(approval_class="production")
    original = fake.job

    def unmarked(request: Any, body: dict[str, Any], job_id: str) -> Any:
        response = original(request, body, job_id)
        payload = json.loads(response.content)
        for key in ("provider", "simulated", "scorer"):
            payload["data"]["eval_results"].pop(key)
        return type(response)(200, json=payload)

    monkeypatch.setattr(fake, "job", unmarked)
    with pytest.raises(lab08.LabVerificationError, match="what measured its baseline"):
        run_lab(lab08, fake)


def test_lab_08_the_model_variant_needs_a_real_provider() -> None:
    with pytest.raises(lab08.SetupError, match="real provider"):
        run_lab(lab08, FakeCaliber(approval_class="production"), model_variant=True)


def test_the_variant_manifests_and_judges_are_valid_json_with_the_documented_shape() -> None:
    support = json.loads((ASSETS_07 / "workflows/support-triage-model.workflow.json").read_text())
    incident = json.loads(
        (ASSETS_08 / "workflows/incident-commander-model.workflow.json").read_text()
    )
    # the model variants add agents and no Python Code node: Preview and Evaluations can run them
    for manifest in (support, incident):
        types = {node["type"] for node in manifest["nodes"].values()}
        assert "agent" in types and "python_code" not in types
    assert {"classify_model", "guard", "review_bug", "review_support"} <= set(support["nodes"])
    assert "propose_triage" in incident["nodes"]
    for judge in (
        ASSETS_07 / "judges/support-triage-routing-and-reply.judge.json",
        ASSETS_08 / "judges/stakeholder-update-honesty.judge.json",
    ):
        spec = json.loads(judge.read_text())
        assert spec["feedback_value_type"] == "bool"
        assert "{{ outputs }}" in spec["instructions"] and "{{ inputs }}" in spec["instructions"]


def test_lab_07_resubmits_runs_a_dropped_provider_connection_failed() -> None:
    fake = FakeCaliber(real=True, flaky=("export button throws a 500",))
    result, lines = run_lab(lab07, fake)

    assert not failed_checks(lines)
    assert result["resubmitted_after_provider_errors"] == ["TK29"]
    assert result["batch"]["waiting_approval"] == 15


def test_lab_08_resubmits_runs_a_rate_limit_failed() -> None:
    fake = FakeCaliber(
        approval_class="production",
        real=True,
        flaky=("gateway 5xx rate 18%",),
        flaky_error="RateLimitError: Error code: 429",
    )
    result, lines = run_lab(lab08, fake)

    assert not failed_checks(lines)
    assert result["resubmitted_after_provider_errors"] == ["IN01"]


def test_lab_07_does_not_hide_a_failure_that_is_not_the_providers() -> None:
    fake = FakeCaliber(
        real=True, flaky=("export button throws a 500",), flaky_error="KeyError: 'x'"
    )
    with pytest.raises(lab07.LabVerificationError, match="every ticket routed as expected"):
        run_lab(lab07, fake)
