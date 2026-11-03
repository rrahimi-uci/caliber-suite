"""Exact source and registration contracts for the Beta Lab 22 cookbook.

Lab 22 keeps the cookbook package and its disposable reference-profile runner
explicitly wired. The browser journey remains owned by B15.3b; this contract
ensures the API runner cannot silently fall back to an unsafe SQLite setup.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAB = REPO_ROOT / "docs-site/cookbooks/22-governed-evidence-review-lab"
RUNNER = REPO_ROOT / "scripts/cookbook-run-all.sh"
BOOTSTRAP = REPO_ROOT / "caliber/scripts/cookbook_22_reference_bootstrap.py"
BUILDER = REPO_ROOT / "docs-site/build-docs.mjs"
APP = REPO_ROOT / "caliber/caliber-ui/src/App.tsx"


def test_lab22_has_the_same_package_contract_as_lab21() -> None:
    assert {path.name for path in LAB.iterdir() if path.is_file()} == {
        "README.md",
        "build.yaml",
        "scenario.yaml",
        "test-data.yaml",
        "training-steps.json",
        "verification.yaml",
    }
    assert {path.name for path in (LAB / "assets").iterdir() if path.is_file()} == {"README.md"}


def test_lab22_structured_contracts_are_valid_and_exact() -> None:
    yaml = pytest.importorskip("yaml")
    scenario = yaml.safe_load((LAB / "scenario.yaml").read_text(encoding="utf-8"))
    test_data = yaml.safe_load((LAB / "test-data.yaml").read_text(encoding="utf-8"))
    build = yaml.safe_load((LAB / "build.yaml").read_text(encoding="utf-8"))
    verification = yaml.safe_load((LAB / "verification.yaml").read_text(encoding="utf-8"))
    training = json.loads((LAB / "training-steps.json").read_text(encoding="utf-8"))

    assert scenario["id"] == "SCN-22"
    assert scenario["slug"] == "governed-evidence-review-lab"
    assert scenario["inputs"]["case_id"] == "MORT-2026-000184"
    assert scenario["outputs"] == {
        "run_status": "completed",
        "acceptance_status": "accepted",
        "downstream_case_version": 2,
        "downstream_mutations": 1,
        "replay_status": "matched",
    }
    assert test_data["fixture"]["path"] == "caliber/tests/fixtures/egi/case_000184.json"
    assert test_data["fixture"]["synthetic_only"] is True
    assert test_data["fixture"]["forbidden_use"] == "credit_decisions"
    assert test_data["proposal"]["resource"] == "cases/MORT-2026-000184/document-requests"
    assert test_data["downstream_contract"] == {
        "first_commit": {"status": 201, "mutation_count": 1},
        "same_key_replay": {"status": 200, "mutation_count": 1},
        "stale_if_match": {"status": 412, "mutation_count": 1},
    }
    assert [step["id"] for step in training["steps"]] == [f"22.{n}" for n in range(1, 9)]
    assert len(training["steps"]) == 8
    assert {
        "admission",
        "protected proposal",
        "distinct approval",
        "verified effect",
        "downstream boundaries",
        "evidence",
    } <= {gate["gate"].lower() for gate in verification["quality_gates"]}
    assert "waiting_approval" in " ".join(build["feasibility"])
    assert "201" in " ".join(build["feasibility"])
    assert "412" in " ".join(build["feasibility"])


def test_lab22_readme_pins_beta_install_and_observable_refusal_contracts() -> None:
    text = (LAB / "README.md").read_text(encoding="utf-8")
    for required in (
        "docs/start/beta-scope.md",
        "waiting_approval",
        "project_role_required",
        "MORT-2026-000184",
        "201",
        "200",
        "412",
        "no_authoritative_verifier",
        "connector_unbound",
        "export",
        "replay",
        "scripts/cookbook-run-all.sh --labs 22 --api-only",
        "cookbook_22_governed_evidence_review",
        "not a credit decision",
    ):
        assert required in text, required
    assert "## Run it through the UI" in text
    assert "## Run it through the SDK" in text
    assert "case_000184.json" in text
    assert "does not create another route" in text


def test_lab22_runner_registration_is_selectable_with_the_beta_ui_journey() -> None:
    text = RUNNER.read_text(encoding="utf-8")
    assert re.search(
        r'ALL_LABS="01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 18 19 20 21 22"',
        text,
    )
    lab22 = text[text.index("    22)") :]
    assert 'LAB_TITLE="governed evidence review"; LAB_FLAGS="release_candidates,cookbooks"' in lab22
    assert 'LAB_HELPERS="harborlight22 scriptedllm22"' in lab22
    assert "LAB_REAL=1" in lab22
    assert "LAB_API_ARGV=(-m examples.cookbooks.cookbook_22_governed_evidence_review)" in text
    assert 'LAB_UI_ENV=(CALIBER_LAB22_ADMIN_PASSWORD="$LAB_ADMIN_PASSWORD")' in lab22
    assert 'CALIBER_DATABASE_URL="$LAB22_DATABASE_URL"' in text
    assert (
        'CALIBER_COOKBOOK_PRESTART="$REPO_ROOT/caliber/scripts/cookbook_22_reference_bootstrap.py"'
        in text
    )
    assert "CALIBER_EFFECT_CALLBACK_CONNECTORS=" in text
    assert "CALIBER_AGENT_STEP_TOOL_BACKENDS=" in text
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'TOOL_NAME = "request_missing_document"' in bootstrap
    assert 'TOOL_VERSION = "1.0"' in bootstrap
    assert "project_id=PROJECT_ID" in bootstrap
    assert 'side_effect_level="external_action"' in bootstrap
    assert "requires_approval=True" in bootstrap
    assert "harborlight22) start_helper harborlight-22 8810" in text
    assert "scriptedllm22) start_helper scripted-llm-22 8792" in text
    assert BOOTSTRAP.is_file()
    assert 'LAB_UI="22-governed-evidence-review.spec.ts"' in lab22


def test_lab22_is_registered_in_docs_builder_and_generated_nav() -> None:
    builder = BUILDER.read_text(encoding="utf-8")
    assert "REQUIRED_BETA_COOKBOOK_PAGES" in builder
    assert "m-cookbook-22-governed-evidence-review.html" in builder
    assert "22 · Governed Evidence Review" in builder

    nav = json.loads((REPO_ROOT / "docs-site/cookbooks-nav.json").read_text(encoding="utf-8"))
    assert {
        "href": "m-cookbook-22-governed-evidence-review.html",
        "label": "22 · Governed Evidence Review",
    } in nav
    assert (REPO_ROOT / "docs-site/m-cookbook-22-governed-evidence-review.html").is_file()


def test_lab22_training_routes_are_registered_by_the_spa() -> None:
    registered = set(re.findall(r'path="([^"]+)"', APP.read_text(encoding="utf-8")))
    for step in json.loads((LAB / "training-steps.json").read_text(encoding="utf-8"))["steps"]:
        route = step["route"]
        route = route.replace("{run_id}", ":runId")
        assert route in registered, (step["id"], route)
