"""Cookbook 10: certify (or reject) an LLM judge by how well it agrees with human reviewers.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/10-judge-certification-human-
review-lab/README.md) and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001 CALIBER_TOKEN=calpat_...
    export MLFLOW_TRACKING_URI=http://127.0.0.1:5000      # where CALIBER's traces live
    PYTHONPATH=sdk/caliber-sdk python \
      sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py

What it does, in order:

1. Loads the 50-row test set from ``assets/dataset/aggregated-outputs.jsonl`` and checks its size.
2. Creates the ``FaithfulnessJudge`` from ``assets/judges/faithfulness-judge.judge.json``, and shows
   a judge with an unsupported template variable being refused.
3. Runs baseline and candidate Evaluations with the deterministic scorers and the judge, and reads
   the per-row results back. An evaluation of an empty test set is refused.
4. Writes the 50 recorded outputs as traces, has them "reviewed" (the human labels in the dataset
   file), then imports those labels and checks the judge against them (Judges > Human alignment).
5. Recomputes agreement, Cohen's kappa and the confusion counts independently from the per-row
   verdicts and compares them with what the server reported, then certifies or rejects the judge on
   the lab's thresholds (agreement >= 0.80 AND kappa >= 0.60 over >= 30 labelled rows).
6. Feeds every disagreement back into the test set as a hard case.

Two modes, chosen by asking the server. On a stack with a model behind it (a real provider, or the
stand-in model in the lab's ``assets/stand-in-model``) every step runs. On the default simulated
stack (``CALIBER_LLM_PROVIDER=fake``) Evaluations and the judge cannot run. The script then verifies
the refusals the product gives instead, plus everything that needs no model: the test set, the
review queue and label import, and a calibration job whose canned score is marked simulated.
``--expect-stand-in`` additionally pins the exact numbers the stand-in model produces, and
``--cleanup`` archives what the run created when it succeeds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, TypeVar

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from examples.cookbooks._helpers import env_client, get_recipe

ASSETS = Path(
    os.environ.get(
        "COOKBOOK_10_ASSETS",
        Path(__file__).resolve().parents[4]
        / "docs-site/cookbooks/10-judge-certification-human-review-lab/assets",
    )
)

#: The lab's certification policy. A judge is certified only when it clears all three.
AGREEMENT_MIN = 0.80
KAPPA_MIN = 0.60
MIN_LABELLED_ROWS = 30

#: The numbers the lab's stand-in model produces (``--expect-stand-in``). They are properties of its
#: mechanical rule on this dataset, not of any real judge: see the README.
STAND_IN_STRICT = {
    "agreement": 0.82,
    "kappa": 0.6365,
    "confusion": {"true_pos": 23, "false_pos": 5, "true_neg": 18, "false_neg": 4},
}
STAND_IN_LENIENT = {
    "agreement": 0.58,
    "kappa": 0.0933,
    "confusion": {"true_pos": 27, "false_pos": 21, "true_neg": 2, "false_neg": 0},
}
STAND_IN_LEAK = {"agreement": 0.66, "kappa": 0.2952}
STAND_IN_RUNS = {
    "baseline": {"non_empty": 1.0, "contains_expected": 0.0, "judge": 1.0},
    "candidate": {"non_empty": 1.0, "contains_expected": 0.66, "judge": 0.56},
}

SCORERS = ["non_empty", "contains_expected"]
EXAMPLES_EXPECTED = 50
DISAGREEMENTS_EXPECTED = 9


class VerificationError(AssertionError):
    """A documented outcome did not hold."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


_T = TypeVar("_T")


def present(value: _T | None, message: str) -> _T:
    """``value`` itself, so a field the server must return is narrowed from ``None`` for the type
    checker; a missing one is a failed verification like any other."""
    if value is None:
        raise VerificationError(message)
    return value


# --------------------------------------------------------------------------- #
# The metrics, recomputed from scratch (the server's numbers are checked against these)
# --------------------------------------------------------------------------- #
def agreement(judge: list[bool], human: list[bool]) -> float:
    return sum(a == b for a, b in zip(judge, human, strict=True)) / len(judge)


def cohen_kappa(judge: list[bool], human: list[bool]) -> float:
    n = len(judge)
    observed = agreement(judge, human)
    expected = sum((judge.count(label) / n) * (human.count(label) / n) for label in (True, False))
    return 1.0 if expected >= 1.0 else (observed - expected) / (1 - expected)


def confusion(judge: list[bool], human: list[bool]) -> dict[str, int]:
    pairs = Counter(zip(judge, human, strict=True))
    return {
        "true_pos": pairs[(True, True)],
        "false_pos": pairs[(True, False)],
        "true_neg": pairs[(False, False)],
        "false_neg": pairs[(False, True)],
    }


def decide(scored: int, n: int, agreement_rate: float, kappa: float) -> str:
    """The lab's certification rule."""
    if scored != n or scored < MIN_LABELLED_ROWS:
        return "NOT_CERTIFIABLE"
    return "CERTIFIED" if agreement_rate >= AGREEMENT_MIN and kappa >= KAPPA_MIN else "REJECTED"


# --------------------------------------------------------------------------- #
# Dataset files
# --------------------------------------------------------------------------- #
def load_rows(assets: Path) -> list[dict[str, Any]]:
    path = assets / "dataset" / "aggregated-outputs.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def output_text(row: dict[str, Any]) -> str:
    return row["outputs"].get("reply") or row["outputs"].get("summary") or ""


def seed_traces(assets: Path) -> dict[str, str]:
    """Write the 50 recorded outputs as traces (needs ``mlflow`` and ``MLFLOW_TRACKING_URI``)."""
    spec = importlib.util.spec_from_file_location(
        "cookbook10_seed_traces", assets / "seed_traces.py"
    )
    check(spec is not None and spec.loader is not None, "seed_traces.py not found")
    assert spec is not None and spec.loader is not None  # narrowed for the type checker
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seeded: dict[str, str] = module.seed(assets / "dataset" / "aggregated-outputs.jsonl")
    return seeded


def refused(call: Any) -> CaliberAPIError:
    """Run ``call`` and return the error the server refused it with."""
    try:
        call()
    except CaliberAPIError as error:
        return error
    raise VerificationError("the server accepted a request it should have refused")


# --------------------------------------------------------------------------- #
# The lab
# --------------------------------------------------------------------------- #
def run(
    caliber: CaliberClient,
    *,
    assets: Path = ASSETS,
    trace_ids: dict[str, str] | None = None,
    suffix: str | None = None,
    expect_stand_in: bool = False,
    cleanup: bool = False,
) -> dict[str, Any]:
    suffix = suffix or uuid.uuid4().hex[:6]
    rows = load_rows(assets)
    check(
        len(rows) == EXAMPLES_EXPECTED,
        f"expected {EXAMPLES_EXPECTED} dataset rows, got {len(rows)}",
    )
    result: dict[str, Any] = {"suffix": suffix}
    created: dict[str, list[str]] = {"datasets": [], "judges": [], "queues": [], "prompts": []}

    recipe = get_recipe(caliber, "10")
    result["cookbook"] = {"id": recipe.id, "title": recipe.title, "steps": len(recipe.steps)}

    # ---- 1. The test set ------------------------------------------------------------------
    owner = caliber.me.get().user_id
    dataset = caliber.eval_datasets.create(
        f"judge-certification-set-{suffix}",
        owner=owner,
        description="Support replies (SCN-07) and incident summaries (SCN-08) to certify on.",
    )
    created["datasets"].append(dataset.dataset_id)
    for row in rows:
        caliber.eval_datasets.add_example(
            dataset.dataset_id, input=row["input"], expected=row["expected"], tags=row["tags"]
        )
    stored = caliber.eval_datasets.get(dataset.dataset_id)
    check(
        stored.version == len(rows) + 1,
        f"dataset version {stored.version}, expected {len(rows) + 1}",
    )
    check(
        len(caliber.eval_datasets.examples(dataset.dataset_id)) == len(rows),
        "the test set does not hold every row",
    )
    result["dataset"] = {"id": dataset.dataset_id, "examples": len(rows), "version": stored.version}

    # ---- 2. The judge, and a template it refuses ------------------------------------------
    spec = json.loads((assets / "judges" / "faithfulness-judge.judge.json").read_text("utf-8"))
    judge = caliber.judges.create(
        f"FaithfulnessJudge-{suffix}",
        instructions=spec["instructions"],
        feedback_value_type=spec["feedback_value_type"],
    )
    lenient = caliber.judges.create(
        f"LenientJudge-{suffix}",
        instructions=(
            "Return true when {{ outputs }} is a complete, well-formed answer to {{ inputs }}. "
            "Return false only if it is empty or cut off."
        ),
        feedback_value_type="bool",
    )
    created["judges"] += [judge.judge_id, lenient.judge_id]
    bad_template = refused(
        lambda: caliber.judges.create(
            f"BadTemplateJudge-{suffix}",
            instructions="Return true when {{ outputs }} is faithful to {{ evidence }}.",
            feedback_value_type="bool",
        )
    )
    check(bad_template.status_code == 400, "an unsupported template variable was not a 400")
    check("evidence" in str(bad_template), "the refusal does not name the unsupported variable")
    result["judge"] = {"id": judge.judge_id, "lenient_id": lenient.judge_id}
    result["invalid_template"] = {"status": bad_template.status_code, "variable": "evidence"}

    # ---- 3. Evaluations --------------------------------------------------------------------
    empty = caliber.eval_datasets.create(f"empty-set-{suffix}", owner=owner)
    created["datasets"].append(empty.dataset_id)
    empty_error = refused(
        lambda: caliber.evaluations.create(empty.dataset_id, scorers=["non_empty"])
    )
    check(empty_error.status_code == 400, "an empty test set was not a 400")
    check("no examples" in str(empty_error), "the empty-set refusal does not say why")
    result["empty_dataset_evaluation"] = {"status": empty_error.status_code}

    judge_scorer = f"Judge.{judge.judge_id}"
    prompt = caliber.prompts.create(
        f"grounded-reply-{suffix}",
        "You answer support tickets and incident alerts using ONLY the evidence you are given. "
        "State what the evidence says and nothing more; if it does not cover the question, "
        "say so and escalate to a person.",
    )
    created["prompts"].append(prompt["name"])
    modelled = True
    try:
        baseline = caliber.evaluations.create(
            dataset.dataset_id, scorers=[*SCORERS, judge_scorer], label=f"baseline {suffix}"
        )
    except CaliberAPIError as error:
        check(
            error.status_code == 400 and "real LLM provider" in str(error),
            f"the first evaluation failed for an unexpected reason: {error}",
        )
        modelled = False
        result["evaluation_refused"] = {"status": error.status_code, "detail": error.detail}
    result["mode"] = "model" if modelled else "simulated"

    if modelled:
        candidate = caliber.evaluations.create(
            dataset.dataset_id,
            scorers=[*SCORERS, judge_scorer],
            label=f"candidate {suffix}",
            predict_target="prompt",
            subject_ref=f"{prompt['name']}@{prompt['version']}",
        )
        result["evaluations"] = _read_runs(
            caliber, baseline, candidate, judge_scorer, expect_stand_in
        )

    # ---- 4. Human review and label import ---------------------------------------------------
    trace_ids = trace_ids or seed_traces(assets)
    check(set(trace_ids) == {row["id"] for row in rows}, "need one trace per dataset row")
    queue = caliber.review_queues.create(
        f"faithfulness-certification-{suffix}",
        questions=json.loads((assets / "review" / "review-questions.json").read_text("utf-8"))[
            "questions"
        ],
    )
    created["queues"].append(queue.queue_id)
    items = caliber.review_queues.enqueue(
        queue.queue_id, trace_ids=[trace_ids[r["id"]] for r in rows]
    )
    item_for_trace = {item["trace_id"]: item["item_id"] for item in items}
    check(len(item_for_trace) == len(rows), "the queue did not take every trace")
    for row in rows:
        answers: dict[str, Any] = {
            "faithful": row["human_label"] == "pass",
            "evidence": json.dumps(row["expected"], ensure_ascii=False),
        }
        caliber.review_queues.submit(
            queue.queue_id, item_for_trace[trace_ids[row["id"]]], **answers
        )

    imported = caliber.review_queues.alignment_examples(queue.queue_id, question_key="faithful")
    check(imported["skipped"] == [], f"label import skipped rows: {imported['skipped']}")
    examples = imported["examples"]
    check(len(examples) == len(rows), f"imported {len(examples)} labels, expected {len(rows)}")
    row_for_trace = {trace_ids[row["id"]]: row for row in rows}
    for example in examples:  # the import carries the trace, the reply, the label and the evidence
        row = row_for_trace[example["provenance"]["trace_id"]]
        check(example["outputs"] == output_text(row), f"imported output differs for {row['id']}")
        check(
            example["label"] == (row["human_label"] == "pass"),
            f"imported label differs for {row['id']}",
        )
    sendable = [
        {key: example[key] for key in ("inputs", "outputs", "expectations", "label")}
        for example in examples
    ]
    result["review"] = {"queue_id": queue.queue_id, "labels_imported": len(examples)}

    if modelled:
        result["alignment"] = _certify(
            caliber,
            judge.judge_id,
            lenient.judge_id,
            sendable,
            examples,
            row_for_trace,
            expect_stand_in,
        )
        result["feedback"] = _feed_back(
            caliber, dataset.dataset_id, result["alignment"], examples, row_for_trace, trace_ids
        )
    else:
        result["alignment"] = _alignment_without_a_model(caliber, judge.judge_id, sendable)
        result["calibration"] = _calibrate_on_canned_scores(
            caliber, prompt["name"], dataset.dataset_id
        )

    result["result"] = "verified"
    if cleanup:
        _archive(caliber, created)
        result["archived"] = {kind: len(ids) for kind, ids in created.items()}
    return result


def _archive(caliber: CaliberClient, created: dict[str, list[str]]) -> None:
    """Archive what the run created (names stay reserved, so a rerun uses a new suffix)."""
    for judge_id in created["judges"]:
        caliber.judges.update(judge_id, status="archived")
    for dataset_id in created["datasets"]:
        caliber.eval_datasets.update(dataset_id, status="archived")
    for queue_id in created["queues"]:
        caliber.review_queues.update(queue_id, status="archived")
    for name in created["prompts"]:
        caliber.prompts.delete(name)


def _read_runs(
    caliber: CaliberClient,
    baseline: Any,
    candidate: Any,
    judge_scorer: str,
    expect_stand_in: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, run in (("baseline", baseline), ("candidate", candidate)):
        stored = caliber.evaluations.get(run.run_id)
        check(
            stored.status == "completed",
            f"{label} run is {stored.status}"
            + (f": {stored.error_message}" if stored.error_message else ""),
        )
        check(
            stored.n_examples == EXAMPLES_EXPECTED, f"{label} scored {stored.n_examples} examples"
        )
        rows = stored.results or []
        check(len(rows) == EXAMPLES_EXPECTED, f"{label} stored {len(rows)} per-row results")
        check(
            all(
                set(row["scores"]) == {*SCORERS, judge_scorer} and row["error"] is None
                for row in rows
            ),
            f"a {label} row is missing a score or carries an error",
        )
        out[label] = {
            "run_id": run.run_id,
            "overall": stored.overall_score,
            "pass_rate": stored.pass_rate,
            "non_empty": stored.aggregate["non_empty"],
            "contains_expected": stored.aggregate["contains_expected"],
            "judge": stored.aggregate[judge_scorer],
        }
    out["delta_overall"] = round(out["candidate"]["overall"] - out["baseline"]["overall"], 4)
    if expect_stand_in:
        for label, expected in STAND_IN_RUNS.items():
            for scorer, value in expected.items():
                check(
                    abs(out[label][scorer] - value) < 1e-9,
                    f"{label} {scorer} is {out[label][scorer]}, expected {value}",
                )
    return out


def _certify(
    caliber: CaliberClient,
    judge_id: str,
    lenient_id: str,
    sendable: list[dict[str, Any]],
    examples: list[dict[str, Any]],
    row_for_trace: dict[str, dict[str, Any]],
    expect_stand_in: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, jid, pinned in (
        ("faithfulness", judge_id, STAND_IN_STRICT),
        ("lenient", lenient_id, STAND_IN_LENIENT),
    ):
        report = caliber.judges.alignment(jid, examples=sendable)
        check(
            report.n == len(sendable) and report.scored == report.n,
            f"{label}: judge errored on a row",
        )
        per = report.per_example
        n = present(report.n, f"{label}: the report has no row count")
        scored = present(report.scored, f"{label}: the report has no scored count")
        agreement_rate = present(report.agreement_rate, f"{label}: the report has no agreement")
        kappa = present(report.cohen_kappa, f"{label}: the report has no kappa")
        judge_labels = [bool(r["judge_label"]) for r in per]
        human_labels = [bool(r["human_label"]) for r in per]
        # The server's numbers are arithmetic over the supplied labels; recompute them here.
        check(
            abs(agreement(judge_labels, human_labels) - agreement_rate) < 5e-5,
            f"{label} agreement",
        )
        check(
            abs(cohen_kappa(judge_labels, human_labels) - kappa) < 5e-5,
            f"{label} kappa",
        )
        check(
            confusion(judge_labels, human_labels) == report.confusion, f"{label} confusion counts"
        )
        disagreements = [
            row_for_trace[examples[i]["provenance"]["trace_id"]]["id"]
            for i, r in enumerate(per)
            if not r["agree"]
        ]
        out[label] = {
            "n": report.n,
            "scored": report.scored,
            "agreement": report.agreement_rate,
            "kappa": report.cohen_kappa,
            "confusion": report.confusion,
            "decision": decide(scored, n, agreement_rate, kappa),
            "disagreements": disagreements,
        }
        if expect_stand_in:
            check(
                report.agreement_rate == pinned["agreement"],
                f"{label} agreement {report.agreement_rate}",
            )
            check(report.cohen_kappa == pinned["kappa"], f"{label} kappa {report.cohen_kappa}")
            check(report.confusion == pinned["confusion"], f"{label} confusion {report.confusion}")
    if expect_stand_in:
        check(
            out["faithfulness"]["decision"] == "CERTIFIED", "the strict judge should be certified"
        )
        check(out["lenient"]["decision"] == "REJECTED", "the lenient judge should be rejected")
        check(
            len(out["faithfulness"]["disagreements"]) == DISAGREEMENTS_EXPECTED,
            "expected nine disagreements",
        )

    # Import labels hands the judge every OTHER answer on a queue item as {{ expectations }}. A
    # reviewer's explanation that quotes the unsupported claim therefore becomes "evidence", and the
    # judge starts agreeing with the claim it should be catching.
    leaky = []
    for example, send in zip(examples, sendable, strict=True):
        row = row_for_trace[example["provenance"]["trace_id"]]
        reason = (
            {"unsupported_claims": row["human_label_reason"]}
            if row["human_label"] == "fail"
            else {}
        )
        leaky.append({**send, "expectations": {**send["expectations"], **reason}})
    polluted = caliber.judges.alignment(judge_id, examples=leaky)
    out["rationale_leak"] = {
        "agreement": polluted.agreement_rate,
        "kappa": polluted.cohen_kappa,
        "confusion": polluted.confusion,
    }
    if expect_stand_in:
        check(polluted.agreement_rate == STAND_IN_LEAK["agreement"], "rationale-leak agreement")
        check(polluted.cohen_kappa == STAND_IN_LEAK["kappa"], "rationale-leak kappa")

    # Too few labelled rows: the product still returns numbers, the lab's policy refuses them.
    few = caliber.judges.alignment(judge_id, examples=sendable[:5])
    few_scored = present(few.scored, "the five-row report has no scored count")
    few_n = present(few.n, "the five-row report has no row count")
    out["too_few_rows"] = {
        "n": few.n,
        "agreement": few.agreement_rate,
        "kappa": few.cohen_kappa,
        "decision": decide(few_scored, few_n, few.agreement_rate or 0.0, few.cohen_kappa or 0.0),
    }
    check(out["too_few_rows"]["decision"] == "NOT_CERTIFIABLE", "five rows must not be certifiable")
    return out


def _feed_back(
    caliber: CaliberClient,
    dataset_id: str,
    alignment: dict[str, Any],
    examples: list[dict[str, Any]],
    row_for_trace: dict[str, dict[str, Any]],
    trace_ids: dict[str, str],
) -> dict[str, Any]:
    """Every disagreement becomes a permanent hard case in the test set."""
    disagreements = alignment["faithfulness"]["disagreements"]
    before = present(caliber.eval_datasets.get(dataset_id).version, "the dataset has no version")
    for row_id in disagreements:
        row = next(r for r in row_for_trace.values() if r["id"] == row_id)
        caliber.eval_datasets.add_from_trace(
            dataset_id,
            trace_ids[row_id],
            input=row["input"],
            expected=row["expected"],
            tags=[*row["tags"], "judge-disagreement"],
        )
    after = present(
        caliber.eval_datasets.get(dataset_id).version, "the dataset has no version afterwards"
    )
    check(after == before + len(disagreements), f"dataset version {before} -> {after}")
    return {"hard_cases_added": len(disagreements), "dataset_version": after}


def _alignment_without_a_model(
    caliber: CaliberClient, judge_id: str, sendable: list[dict[str, Any]]
) -> dict[str, Any]:
    """With no model the judge cannot run: every row comes back as an error and is left out."""
    report = caliber.judges.alignment(judge_id, examples=sendable)
    check(report.n == len(sendable), "alignment did not echo the number of rows")
    check(report.scored == 0, f"{report.scored} rows were scored without a model")
    check(
        all(r["judge_label"] is None and r["error"] for r in report.per_example),
        "a row came back without an error",
    )
    return {
        "n": report.n,
        "scored": report.scored,
        "agreement": report.agreement_rate,
        "kappa": report.cohen_kappa,
        "decision": decide(
            present(report.scored, "the report has no scored count"),
            present(report.n, "the report has no row count"),
            report.agreement_rate or 0.0,
            0.0,
        ),
        "first_row_error": report.per_example[0]["error"],
    }


def _calibrate_on_canned_scores(
    caliber: CaliberClient, prompt_name: str, dataset_id: str
) -> dict[str, Any]:
    """The test set also feeds calibration; on the simulated stack its score is canned."""
    run = caliber.prompts.create_calibration_run(
        agent_id=prompt_name,
        eval_dataset_id=dataset_id,
        optimizer_type="MetaPrompt",
        scorers=[{"name": "Correctness"}],
    )
    job = caliber.jobs.wait(run["job"]["job_id"], timeout=120)
    check(job.status == "candidate_ready", f"calibration job ended {job.status}")
    evidence = job.eval_results or {}
    check(evidence.get("simulated") is True, "a canned score was not marked simulated")
    check(
        evidence.get("n_examples") == EXAMPLES_EXPECTED,
        "calibration did not use the whole test set",
    )
    return {
        "job_id": job.job_id,
        "status": job.status,
        "n_examples": evidence["n_examples"],
        "dataset_version": evidence["eval_dataset_version"],
        "provider": evidence.get("provider"),
        "simulated": evidence["simulated"],
        "gate_passed_on_a_canned_score": evidence["gate"]["passed"],
    }


def main() -> None:
    expect_stand_in = "--expect-stand-in" in sys.argv
    cleanup = "--cleanup" in sys.argv
    try:
        with env_client() as caliber:
            result = run(caliber, expect_stand_in=expect_stand_in, cleanup=cleanup)
    except VerificationError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
