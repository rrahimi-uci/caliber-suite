"""Contract tests: cookbook docs must stay in sync with the shipped code.

These guard the doc/code drift corrected after the ``ui-complete-report.md``
UI-completeness audit. Each test pins a *code* fact and asserts the cookbook
docs describe it correctly, so that either

  * a future code change that invalidates a doc claim, or
  * a doc edit that reintroduces one of the stale claims we just removed,

fails here instead of shipping. This is the CI backstop the report recommended:
"validate route names, field names, UI labels, referenced assets ... in CI".
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SITE = REPO_ROOT / "docs-site"
COOKBOOKS = DOCS_SITE / "cookbooks"


def _read(rel: str) -> str:
    path = COOKBOOKS / rel
    assert path.is_file(), f"expected cookbook doc missing (renamed?): {path}"
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Evaluations can score prompt / skill / workflow (cookbooks 01, 08)
# --------------------------------------------------------------------------- #
def test_eval_predict_targets_support_prompt_skill_workflow() -> None:
    from caliber.schemas import EvalPredictTarget

    targets = set(typing.get_args(EvalPredictTarget))
    assert {"llm", "prompt", "skill", "workflow"} <= targets, targets


def test_cookbook01_does_not_claim_evaluations_cannot_score_a_prompt() -> None:
    # Code supports predict_target=prompt, so the old "can't score a prompt"
    # limitation is obsolete and must not reappear in the recipe.
    for rel in (
        "01-prompt-regression-lab/README.md",
        "01-prompt-regression-lab/verification.yaml",
    ):
        text = _read(rel).lower()
        assert "can't score a prompt" not in text, rel
        assert "cannot score a prompt" not in text, rel


def test_cookbook08_does_not_claim_evaluations_cannot_run_the_workflow() -> None:
    from caliber.routes.evaluations import (
        _DEFAULT_MAX_EXAMPLES,
        _WORKFLOW_MAX_EXAMPLES,
    )

    # Workflow eval runs synchronously with a tighter cap than the default.
    assert _WORKFLOW_MAX_EXAMPLES < _DEFAULT_MAX_EXAMPLES

    text = _read("08-incident-response-commander/training-steps.json")
    assert "does <strong>not</strong> run the <code>incident-copilot" not in text
    # The recipe should teach the workflow-scoring path and its cap.
    assert "Workflow version" in text
    assert str(_WORKFLOW_MAX_EXAMPLES) in text


# --------------------------------------------------------------------------- #
# Tool registration field name: side_effect_level (cookbook 03)
# --------------------------------------------------------------------------- #
def test_tool_register_request_uses_side_effect_level() -> None:
    from caliber.schemas import ToolRegisterRequest

    fields = ToolRegisterRequest.model_fields
    assert "side_effect_level" in fields
    assert "side_effect" not in fields  # legacy name gone
    assert ToolRegisterRequest.model_config.get("extra") == "forbid"


def test_cookbook_tool_json_assets_use_side_effect_level() -> None:
    tool_files = sorted(COOKBOOKS.rglob("*.tool.json"))
    assert tool_files, "no *.tool.json assets found — layout changed?"
    allowed = {"read", "write", "external_action"}
    for path in tool_files:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "side_effect" not in data, f"legacy 'side_effect' key in {path}"
        if "side_effect_level" in data:
            assert data["side_effect_level"] in allowed, path


def test_cookbook03_readme_drops_bare_side_effect_assignment() -> None:
    text = _read("03-tool-hardening-contract-lab/README.md")
    assert "side_effect=read" not in text
    assert "side_effect=write" not in text
    assert "side_effect_level" in text


# --------------------------------------------------------------------------- #
# Prompt Playground is a live chat, not render-only (README + FEASIBILITY)
# --------------------------------------------------------------------------- #
def test_playground_docs_reflect_live_chat() -> None:
    readme = _read("README.md")
    assert "Playground renders only" not in readme
    feas = _read("FEASIBILITY.md")
    assert "Render-only" not in feas


# --------------------------------------------------------------------------- #
# Aria: honest planner + interaction schema (cookbooks 12-15, FEASIBILITY)
# --------------------------------------------------------------------------- #
def test_aria_default_planner_is_deterministic_heuristic() -> None:
    from caliber.assistant.plans import HeuristicPlanner, build_default_planner

    assert isinstance(build_default_planner(), HeuristicPlanner)
    # FEASIBILITY must not sell plan decomposition as LLM-backed.
    feas = _read("FEASIBILITY.md")
    assert "Real LLM-backed (OpenAI/Anthropic)" not in feas


def test_aria_interaction_answer_schema_is_narrow() -> None:
    from caliber.schemas import AriaInteractionAnswerRequest

    assert AriaInteractionAnswerRequest.model_config.get("extra") == "forbid"
    assert set(AriaInteractionAnswerRequest.model_fields) == {
        "approved",
        "choice",
        "value",
    }


# --------------------------------------------------------------------------- #
# Judge human-alignment is computed in-product (cookbook 10)
# --------------------------------------------------------------------------- #
def test_judge_alignment_metrics_exist_and_are_documented() -> None:
    from caliber.eval.alignment import (
        cohen_kappa,
        confusion_counts,
        observed_agreement,
    )

    judge = [True, False, True, False]
    human = [True, True, False, False]
    assert 0.0 <= observed_agreement(judge, human) <= 1.0
    assert -1.0 <= cohen_kappa(judge, human) <= 1.0
    assert {"true_pos", "false_pos", "true_neg", "false_neg"} <= set(confusion_counts(judge, human))

    readme = _read("10-judge-certification-human-review-lab/README.md")
    assert "Human alignment" in readme


# --------------------------------------------------------------------------- #
# Training guide link + generated index (README)
# --------------------------------------------------------------------------- #
def test_readme_links_to_existing_training_index() -> None:
    readme = _read("README.md")
    assert "training/index.html" not in readme  # never existed
    assert "m-16-cookbooks.html" in readme
    assert (DOCS_SITE / "m-16-cookbooks.html").is_file()


# --------------------------------------------------------------------------- #
# All cookbook data files still parse
# --------------------------------------------------------------------------- #
def test_cookbook_json_and_jsonl_parse() -> None:
    for path in sorted(COOKBOOKS.rglob("*.json")):
        json.loads(path.read_text(encoding="utf-8"))
    for path in sorted(COOKBOOKS.rglob("*.jsonl")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                json.loads(line)  # raises with the offending file/line in context


def test_cookbook_yaml_parses() -> None:
    yaml = pytest.importorskip("yaml")
    for path in sorted(COOKBOOKS.rglob("*.yaml")):
        list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
