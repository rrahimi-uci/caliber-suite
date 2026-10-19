"""Cookbook 10's dataset, stand-in model and documented numbers stay consistent.

The lab certifies (or rejects) a judge by how well it agrees with human labels. Its README quotes
exact agreement and kappa values, and those only mean something if they can be reproduced from the
files the lab ships. These tests pin that: the dataset is what the README says it is, the stand-in
model's mechanical rule produces the documented confusion table, the product's own alignment math
turns that table into the documented numbers, and the stand-in model is compatible with the real
judge code path (``make_judge`` through to ``score_with_judge``) that the lab runs it through.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import threading
from collections import Counter
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from caliber.eval.alignment import cohen_kappa, confusion_counts, observed_agreement
from caliber.eval.judge_scorer import build_judge, score_with_judge
from caliber.schemas import JudgeAlignmentRequest, JudgeCreateRequest

LAB = (
    Path(__file__).resolve().parents[2]
    / "docs-site"
    / "cookbooks"
    / "10-judge-certification-human-review-lab"
)
ASSETS = LAB / "assets"


def _load_stand_in() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cookbook10_stand_in", ASSETS / "stand-in-model" / "stand_in_model_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAND_IN = _load_stand_in()
ROWS: list[dict[str, Any]] = [
    json.loads(line)
    for line in (ASSETS / "dataset" / "aggregated-outputs.jsonl").read_text("utf-8").splitlines()
    if line.strip()
]


def _text(row: dict[str, Any]) -> str:
    return row["outputs"].get("reply") or row["outputs"].get("summary") or ""


def _verdicts(rule: Any) -> tuple[list[bool], list[bool]]:
    judge = [bool(rule(_text(r), r["expected"])[0]) for r in ROWS]
    human = [r["human_label"] == "pass" for r in ROWS]
    return judge, human


# --------------------------------------------------------------------------- #
# The dataset
# --------------------------------------------------------------------------- #
def test_dataset_is_fifty_rows_that_import_through_the_ui_upload_shape() -> None:
    """The UI upload (Prompts > Calibration) reads ``input``, ``expected`` and ``tags`` per row."""
    assert len(ROWS) == 50
    assert [r["id"] for r in ROWS] == [f"J{n:02d}" for n in range(1, 51)]
    for row in ROWS:
        assert isinstance(row["input"], dict) and row["input"]
        assert isinstance(row["expected"], dict) and row["expected"]
        assert isinstance(row["expected"]["expected"], str) and row["expected"]["expected"]
        assert row["human_label"] in {"pass", "fail"} and row["human_label_reason"]
        assert row["tags"] and _text(row)


def test_dataset_inputs_are_unique_so_a_replaying_model_can_tell_rows_apart() -> None:
    inputs = [json.dumps(r["input"], sort_keys=True) for r in ROWS]
    assert len(set(inputs)) == len(inputs)


def test_dataset_spans_support_and_incident_rows_and_clear_and_borderline_cases() -> None:
    domains = Counter(r["tags"][0] for r in ROWS)
    assert domains == {"scn-07": 22, "scn-08": 28}
    tags = Counter(tag for r in ROWS for tag in r["tags"])
    assert (tags["clear_pass"], tags["clear_fail"], tags["borderline"]) == (21, 17, 12)
    # The 12 borderline rows are the 9 judge/human disagreements and 3 that both sides agree on.
    borderline = {r["id"] for r in ROWS if "borderline" in r["tags"]}
    assert borderline - {"J07", "J25", "J26"} == {
        r["id"] for r in ROWS if "expected_disagreement" in r["tags"]
    }
    labels = Counter(r["human_label"] for r in ROWS)
    assert labels == {"pass": 27, "fail": 23}


def test_the_expected_disagreement_tag_marks_exactly_the_rows_the_strict_rule_gets_wrong() -> None:
    judge, human = _verdicts(STAND_IN.strict_faithfulness)
    wrong = {r["id"] for r, j, h in zip(ROWS, judge, human, strict=True) if j != h}
    tagged = {r["id"] for r in ROWS if "expected_disagreement" in r["tags"]}
    assert wrong == tagged == {"J03", "J12", "J14", "J17", "J19", "J22", "J24", "J30", "J33"}


def test_expected_alignment_csv_matches_the_rules() -> None:
    with (ASSETS / "alignment" / "expected-alignment.csv").open(encoding="utf-8") as handle:
        table = list(csv.DictReader(handle))
    strict, human = _verdicts(STAND_IN.strict_faithfulness)
    lenient, _ = _verdicts(STAND_IN.lenient_faithfulness)
    assert [t["id"] for t in table] == [r["id"] for r in ROWS]
    assert [t["human_label"] == "pass" for t in table] == human
    assert [t["stand_in_strict_judge"] == "pass" for t in table] == strict
    assert [t["stand_in_lenient_judge"] == "pass" for t in table] == lenient
    assert [t["agree"] == "yes" for t in table] == [s == h for s, h in zip(strict, human)]


# --------------------------------------------------------------------------- #
# The numbers the README documents, through the product's own alignment math
# --------------------------------------------------------------------------- #
def test_strict_judge_alignment_is_the_documented_certification_case() -> None:
    judge, human = _verdicts(STAND_IN.strict_faithfulness)
    assert confusion_counts(judge, human) == {
        "true_pos": 23,
        "false_pos": 5,
        "true_neg": 18,
        "false_neg": 4,
    }
    assert observed_agreement(judge, human) == 0.82
    assert round(cohen_kappa(judge, human), 4) == 0.6365


def test_lenient_judge_alignment_is_the_documented_rejection_case() -> None:
    judge, human = _verdicts(STAND_IN.lenient_faithfulness)
    assert confusion_counts(judge, human) == {
        "true_pos": 27,
        "false_pos": 21,
        "true_neg": 2,
        "false_neg": 0,
    }
    assert observed_agreement(judge, human) == 0.58
    assert round(cohen_kappa(judge, human), 4) == 0.0933


def test_kappa_worked_by_hand_matches_the_worksheet() -> None:
    """p_o 0.82; p_e = 0.56 * 0.54 + 0.44 * 0.46 = 0.5048; kappa = 0.3152 / 0.4952."""
    judge, human = _verdicts(STAND_IN.strict_faithfulness)
    p_e = 0.56 * 0.54 + 0.44 * 0.46
    assert p_e == pytest.approx(0.5048)
    assert (0.82 - p_e) / (1 - p_e) == pytest.approx(cohen_kappa(judge, human))


def test_five_rows_clear_both_thresholds_which_is_why_the_lab_requires_thirty() -> None:
    judge, human = _verdicts(STAND_IN.strict_faithfulness)
    assert observed_agreement(judge[:5], human[:5]) == 0.8
    assert round(cohen_kappa(judge[:5], human[:5]), 4) == 0.6154


def test_the_ten_rows_of_the_ui_path() -> None:
    reviewed = ["J01", "J02", "J03", "J04", "J05", "J08", "J09", "J11", "J14", "J26"]
    chosen = [r for r in ROWS if r["id"] in reviewed]
    strict = [STAND_IN.strict_faithfulness(_text(r), r["expected"])[0] for r in chosen]
    lenient = [STAND_IN.lenient_faithfulness(_text(r), r["expected"])[0] for r in chosen]
    human = [r["human_label"] == "pass" for r in chosen]
    assert confusion_counts(strict, human) == {
        "true_pos": 4,
        "false_pos": 1,
        "true_neg": 4,
        "false_neg": 1,
    }
    assert observed_agreement(strict, human) == 0.8
    assert round(cohen_kappa(strict, human), 2) == 0.6
    assert observed_agreement(lenient, human) == 0.6
    assert round(cohen_kappa(lenient, human), 2) == 0.2
    assert confusion_counts(lenient, human)["false_pos"] == 4


def test_a_reviewers_rationale_in_expectations_leaks_the_claim_into_the_evidence() -> None:
    """Import labels passes every non-label answer to the judge as ``{{ expectations }}``."""
    judge, human = [], []
    for row in ROWS:
        leaked = dict(row["expected"])
        if row["human_label"] == "fail":
            leaked["unsupported_claims"] = row["human_label_reason"]
        judge.append(STAND_IN.strict_faithfulness(_text(row), leaked)[0])
        human.append(row["human_label"] == "pass")
    assert observed_agreement(judge, human) == 0.66
    assert round(cohen_kappa(judge, human), 4) == 0.2952
    assert confusion_counts(judge, human)["false_pos"] == 13


# --------------------------------------------------------------------------- #
# The stand-in model's rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("output", "verdict"),
    [
        ("The refund is $49 and arrives in 5-7 business days.", True),
        ("The refund is $50 and arrives in 5-7 business days.", False),  # figure not in evidence
        ("The refund is $49 and arrives in 5-7 business days", False),  # cut off mid-sentence
        ("", False),
        ('{"reply": "The refund is $49', False),  # truncated JSON
        ('{"reply": "The refund is $49."}', True),  # complete JSON
        ("We have already emailed you a copy.", True),  # no figures: a claim the rule cannot see
    ],
)
def test_strict_rule_checks_figures_and_completeness_only(output: str, verdict: bool) -> None:
    evidence = {"amount": "$49", "eta": "5-7 business days"}
    assert STAND_IN.strict_faithfulness(output, evidence)[0] is verdict


def test_lenient_rule_only_rejects_empty_or_truncated_output() -> None:
    assert STAND_IN.lenient_faithfulness("Anything complete.", {})[0] is True
    assert STAND_IN.lenient_faithfulness("Anything cut off", {})[0] is False


def test_fact_tokens_cover_money_percentages_versions_times_and_identifiers() -> None:
    tokens = STAND_IN.fact_tokens(
        "v2.9.1 at 14:32 UTC: 6.1% of $1,200 on acct_1432 (SEV2), 5-7 days"
    )
    assert {"v2.9.1", "14:32", "6.1%", "$1,200", "acct_1432", "sev2", "5-7"} <= tokens


# --------------------------------------------------------------------------- #
# The stand-in model against the real judge path
# --------------------------------------------------------------------------- #
@pytest.fixture
def stand_in_url(monkeypatch: pytest.MonkeyPatch) -> Any:
    handler = STAND_IN.make_handler(
        STAND_IN.load_recorded_outputs(ASSETS / "dataset" / "aggregated-outputs.jsonl")
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    monkeypatch.setenv("OPENAI_API_KEY", "stand-in")
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    monkeypatch.setenv("OPENAI_API_BASE", url)
    yield url
    server.shutdown()
    server.server_close()


def test_the_real_judge_path_gets_the_documented_verdicts_from_the_stand_in(
    stand_in_url: str,
) -> None:
    """``build_judge`` (MLflow ``make_judge``) -> request -> structured reply -> ``score_with_judge``."""
    spec = json.loads((ASSETS / "judges" / "faithfulness-judge.judge.json").read_text("utf-8"))
    judge = build_judge("FaithfulnessJudge", spec["instructions"], feedback_value_type="bool")
    by_id = {r["id"]: r for r in ROWS}

    def verdict(row_id: str) -> bool:
        row = by_id[row_id]
        outcome = score_with_judge(
            judge, inputs=row["input"], outputs=_text(row), expectations=row["expected"]
        )
        return outcome.value is True

    assert verdict("J01") is True  # grounded
    assert verdict("J02") is False  # invents a figure
    assert verdict("J04") is False  # truncated
    assert verdict("J03") is True  # unsupported cause with no figures: the rule cannot see it
    assert verdict("J14") is False  # a derived total that is not literally in the evidence


def test_the_candidate_prompt_gets_the_recorded_output_and_the_baseline_a_vague_reply() -> None:
    recorded = STAND_IN.load_recorded_outputs(ASSETS / "dataset" / "aggregated-outputs.jsonl")
    row = ROWS[0]
    user = json.dumps(row["input"], sort_keys=True)
    candidate = STAND_IN.answer_predict("Answer using ONLY the evidence.", user, recorded)
    baseline = STAND_IN.answer_predict("Answer the question.", user, recorded)
    unknown = STAND_IN.answer_predict("Answer using ONLY the evidence.", "{}", recorded)
    assert candidate == _text(row)
    assert baseline == STAND_IN.BASELINE_REPLY
    assert unknown.startswith("I do not have enough information")


# --------------------------------------------------------------------------- #
# The files the README tells you to load
# --------------------------------------------------------------------------- #
def test_judge_and_review_files_are_accepted_by_the_product_schemas() -> None:
    judge = json.loads((ASSETS / "judges" / "faithfulness-judge.judge.json").read_text("utf-8"))
    JudgeCreateRequest.model_validate(
        {
            "name": judge["name"],
            "instructions": judge["instructions"],
            "feedback_value_type": judge["feedback_value_type"],
        }
    )
    queue = json.loads((ASSETS / "review" / "review-questions.json").read_text("utf-8"))
    assert [q["key"] for q in queue["questions"]] == ["faithful", "evidence"]
    assert queue["questions"][0]["type"] == "pass_fail"
    JudgeAlignmentRequest.model_validate(
        {
            "examples": [
                {
                    "inputs": r["input"],
                    "outputs": _text(r),
                    "expectations": r["expected"],
                    "label": r["human_label"] == "pass",
                }
                for r in ROWS
            ]
        }
    )


def test_readme_quotes_the_numbers_the_files_produce() -> None:
    readme = (LAB / "README.md").read_text("utf-8")
    for fragment in ("0.82", "0.6365", "0.0933", "TP 23", "FP 5", "TN 18", "FN 4", "J03", "J33"):
        assert fragment in readme, f"README no longer mentions {fragment}"
