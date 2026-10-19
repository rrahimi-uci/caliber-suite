"""Cookbook 12: give Aria one sentence and get a judge and a test set, then fill the test set.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/12-aria-evaluation-harness/
README.md) and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001      # the server root, not the /ajax-api path
    export CALIBER_TOKEN=calpat_...                    # an ADMIN personal access token
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_12_aria_evaluation_harness

The server needs ``CALIBER_FEATURE_ARIA=true`` and ``CALIBER_OPERATOR_USERS=admin,olga`` (olga is
an operator who does not own the plan; the script also creates ``vera``, who is not an operator).
Nothing here needs a model: Aria's planner is deterministic. The one step that does, running the
judge in Evaluations, is refused on a simulated deployment, and the script verifies that refusal.

What it does, in order:

1. Checks Aria's capability list, and draws a plan for each of the 28 goals in
   ``assets/dataset/goals.jsonl``, comparing it with the dataset (nothing runs: every draft is
   cancelled again).
2. States the canonical intent, approves the plan, and answers its four questions: the judge's
   fields, an Approve, the test set's fields, an Approve. Checks the judge and the test set exist
   under the names typed, and that the test set is empty at version 1.
3. Adds the 30 rows of ``assets/dataset/faithfulness-cases.jsonl`` to the test set, and reads them
   back.
4. Runs the test set in Evaluations (refused on a simulated stack; checked structurally on a real
   one).
5. Provokes the refusals: a goal with no steps, a plan that was cancelled, answers the form rejects,
   a denied step, a judge with no evaluation variable, a duplicate name, an account without the
   operator scope, and an operator who does not own the plan.

``--cleanup`` archives what earlier runs created.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberPermissionError
from examples.cookbooks._aria_lab import (
    Checks,
    Report,
    SetupError,
    VerificationError,
    check_goals_report,
    ensure_accounts,
    env_password,
    form_values,
    load_jsonl,
    refused,
    require_admin_and_aria,
    require_scopes,
    run_plan,
    sign_in,
    start,
    step_keys,
    step_status,
    walk,
)
from examples.cookbooks._helpers import env_client

NAME_PREFIX = "cookbook-12-"
DEFAULT_PASSWORD = "cookbook-aria-lab-passphrase"
OPERATOR_USER = "olga"
VIEWER_USER = "vera"
INTENT = "Create a judge for answer faithfulness and an eval dataset to run it on."
#: What the planner is documented to draw for the 28 goals (see assets/dataset/goals.jsonl).
GOALS_EXPECTED = 28
ROWS_EXPECTED = 30
CAPABILITIES = {
    "eval_dataset.create": "mutate",
    "judge.create": "mutate",
    "judge.list": "read",
    "openapi_tool.invoke": "read",
    "openapi_tool.list": "read",
    "review_queue.add_items": "mutate",
    "review_queue.create": "mutate",
    "review_queue.list": "read",
    "workflow.calibrate": "mutate",
}


def default_assets_dir() -> Path:
    return Path(
        os.environ.get(
            "COOKBOOK_12_ASSETS",
            Path(__file__).resolve().parents[4]
            / "docs-site/cookbooks/12-aria-evaluation-harness/assets",
        )
    )


def run(
    caliber: CaliberClient,
    *,
    operator: CaliberClient,
    viewer: CaliberClient,
    suffix: str | None = None,
    assets: Path | None = None,
    report: Report = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the whole lab. ``caliber`` is the admin's client; ``operator`` and ``viewer`` are the
    two other accounts' clients."""
    assets = assets or default_assets_dir()
    suffix = suffix or time.strftime("%Y%m%d-%H%M%S")
    check = Checks(report)
    result: dict[str, Any] = {"suffix": suffix}
    goals = load_jsonl(assets / "dataset/goals.jsonl")
    cases = load_jsonl(assets / "dataset/faithfulness-cases.jsonl")
    judge_spec = form_values(assets / "judges/answer-faithfulness.judge.json")
    dataset_spec = form_values(assets / "datasets/support-faithfulness.dataset.json")
    judge_name = f"{NAME_PREFIX}{judge_spec['name']}-{suffix}"
    dataset_name = f"{NAME_PREFIX}{dataset_spec['name']}-{suffix}"
    judge_form = {**judge_spec, "name": judge_name}
    dataset_form = {**dataset_spec, "name": dataset_name}

    admin_id = require_admin_and_aria(caliber)
    require_scopes(
        {"olga": operator, "vera": viewer},
        has={"olga": True, "vera": False},
        hint=(
            "start CALIBER with CALIBER_OPERATOR_USERS=admin,olga (olga is an operator who does "
            "not own the plan, vera must not be one)"
        ),
    )

    # -- 1. What Aria can do, and what it plans for 28 goals -----------------------------------
    listed = {item["key"]: item["tier"] for item in caliber.aria.capabilities()["capabilities"]}
    check("Aria lists its nine capabilities with their tiers", listed == CAPABILITIES, len(listed))

    check_goals_report(caliber, goals, GOALS_EXPECTED, check)

    # -- 2. The canonical intent, through every question ----------------------------------------
    drawn = caliber.aria.create_plan(INTENT, autonomy="ask_each")
    plan_id = drawn.plan.plan_id
    check(
        "the intent draws judge.create then eval_dataset.create",
        step_keys(drawn) == ["judge.create", "eval_dataset.create"],
        plan_id,
    )
    check(
        "neither step has an input wired from the other (each asks for its own name)",
        all(not step.extra.get("inputs") and not step.depends_on for step in drawn.steps),
    )
    caliber.aria.approve_plan(plan_id)
    first = caliber.aria.execute_plan(plan_id)
    check(
        "execute pauses on the judge step, waiting for input",
        first.plan.status == "paused" and step_status(first)["judge.create"] == "waiting_input",
    )
    finished, trail = walk(
        caliber,
        plan_id,
        [
            ("input", judge_form),
            ("permission", "approve"),
            ("input", dataset_form),
            ("permission", "approve"),
        ],
    )
    check(
        "the plan asked for the judge's fields, an approval, the test set's fields and an approval",
        trail
        == [
            ("input", "judge.create"),
            ("permission", "judge.create"),
            ("input", "eval_dataset.create"),
            ("permission", "eval_dataset.create"),
        ],
    )
    check(
        "the plan completed with both steps done",
        finished.plan.status == "completed" and set(step_status(finished).values()) == {"done"},
        finished.plan.status,
    )
    judge_id = next(s for s in finished.steps if s.capability_key == "judge.create").result[
        "judge_id"
    ]
    dataset_id = next(
        s for s in finished.steps if s.capability_key == "eval_dataset.create"
    ).result["dataset_id"]
    judge = caliber.judges.get(judge_id)
    check(
        "the judge is active with the typed name, type and instructions",
        judge.status == "active"
        and judge.name == judge_name
        and judge.feedback_value_type == "bool"
        and judge.instructions == judge_spec["instructions"]
        and "{{ outputs }}" in (judge.instructions or ""),
        f"{judge_id} {judge.name}",
    )
    dataset = caliber.eval_datasets.get(dataset_id)
    check(
        "the test set has its own name (not the judge's), owner, tags, version 1 and no rows",
        dataset.name == dataset_name
        and dataset.name != judge.name
        and dataset.owner == admin_id
        and dataset.version == 1
        and sorted(dataset.tags) == sorted(dataset_spec["tags"])
        and not caliber.eval_datasets.examples(dataset_id),
        f"{dataset_id} {dataset.name}",
    )
    result.update(plan_id=plan_id, judge_id=judge_id, dataset_id=dataset_id)

    # -- 3. Fill the test set -----------------------------------------------------------------
    for case in cases:
        caliber.eval_datasets.add_example(
            dataset_id, input=case["input"], expected=case["expected"], tags=case["tags"]
        )
    rows = caliber.eval_datasets.examples(dataset_id)
    by_question = {row.input["question"]: row for row in rows}
    check(
        "the test set holds the 30 cases, one version per row",
        len(cases) == ROWS_EXPECTED
        and len(rows) == ROWS_EXPECTED
        and caliber.eval_datasets.get(dataset_id).version == 1 + ROWS_EXPECTED,
        f"{len(rows)} rows, version {caliber.eval_datasets.get(dataset_id).version}",
    )
    check(
        "every stored row is the file's row (input, expected answer, tags)",
        all(
            by_question[c["input"]["question"]].input == c["input"]
            and by_question[c["input"]["question"]].expected == c["expected"]
            and by_question[c["input"]["question"]].extra.get("tags") == c["tags"]
            for c in cases
        ),
    )
    categories = sorted({c["tags"][0] for c in cases})
    result["rows"] = {"count": len(rows), "categories": categories}

    # -- 4. Run it in Evaluations ---------------------------------------------------------------
    created: Any = None
    refusal: CaliberAPIError | None = None
    try:
        created = caliber.evaluations.create(dataset_id, scorers=["non_empty"])
    except CaliberAPIError as exc:
        refusal = exc
    if refusal is not None:
        check(
            "Evaluations are refused without a real model (400): the judge cannot run here",
            refusal.status_code == 400 and "real LLM provider" in str(refusal),
            "simulated stack",
        )
        result["evaluation"] = "refused (simulated provider)"
    else:
        evaluation = caliber.evaluations.wait(created.evaluation_id or created.run_id)
        check(
            "a real provider scored every row of the test set",
            evaluation.is_terminal and evaluation.n_examples == ROWS_EXPECTED,
            f"{evaluation.status}, {evaluation.n_examples} rows",
        )
        result["evaluation"] = evaluation.status

    # -- 5. Refusals --------------------------------------------------------------------------
    # 5a. A goal with no steps.
    nothing = caliber.aria.create_plan("Write me a poem about customer support.")
    approve_empty = refused(lambda: caliber.aria.approve_plan(nothing.plan.plan_id))
    check(
        "a goal with no steps is created with a warning and refused at approval (409)",
        nothing.plan.step_count == 0
        and bool(nothing.extra.get("warnings"))
        and approve_empty is not None
        and approve_empty.status_code == 409,
    )
    caliber.aria.update_plan(nothing.plan.plan_id, status="cancelled")

    # 5b. A cancelled draft cannot be approved or run.
    draft = caliber.aria.create_plan("Create a judge.")
    caliber.aria.update_plan(draft.plan.plan_id, status="cancelled")
    approve_cancelled = refused(lambda: caliber.aria.approve_plan(draft.plan.plan_id))
    execute_cancelled = refused(lambda: caliber.aria.execute_plan(draft.plan.plan_id))
    check(
        "a cancelled plan can be neither approved nor executed (409)",
        approve_cancelled is not None
        and approve_cancelled.status_code == 409
        and execute_cancelled is not None
        and execute_cancelled.status_code == 409,
    )

    # 5c. Answers the form refuses, then a step skipped.
    first = start(caliber, "Create a judge.", autonomy="approve_plan")
    form = next(i for i in caliber.aria.interactions(first.plan.plan_id) if i.status == "pending")
    bad = {
        "unknown field": {"name": "x", "instructions": "{{ outputs }}", "colour": "red"},
        "wrong type": {"name": 7, "instructions": "{{ outputs }}"},
        "empty name": {"name": "", "instructions": "{{ outputs }}"},
        "missing field": {"name": "x"},
    }
    refusals = {
        label: refused(partial(caliber.aria.answer, form.interaction_id, inputs=inputs))
        for label, inputs in bad.items()
    }
    check(
        "the form refuses an unknown field, a wrong type, an empty name and a missing field (409)",
        all(e is not None and e.status_code == 409 for e in refusals.values()),
        {label: str(e).split("] ")[1].split(" (POST")[0] for label, e in refusals.items() if e},
    )
    check(
        "after four refusals the question is still pending",
        [i.status for i in caliber.aria.interactions(first.plan.plan_id)] == ["pending"],
    )
    skipped, _ = walk(caliber, first.plan.plan_id, [("input", "skip")])
    check(
        "Skip step marks the step skipped and the plan completes",
        skipped.plan.status == "completed" and step_status(skipped) == {"judge.create": "skipped"},
    )
    answered_again = refused(lambda: caliber.aria.answer(form.interaction_id, approved=False))
    check(
        "an answered question cannot be answered again (409)",
        answered_again is not None and answered_again.status_code == 409,
    )

    # 5d. Deny one step, approve the other.
    denied_judge = f"{NAME_PREFIX}denied-{suffix}"
    denied_set = f"{NAME_PREFIX}kept-set-{suffix}"
    denied, _ = run_plan(
        caliber,
        INTENT,
        autonomy="ask_each",
        moves=[
            ("input", {**judge_form, "name": denied_judge}),
            ("permission", "deny"),
            ("input", {"name": denied_set}),
            ("permission", "approve"),
        ],
    )
    check(
        "denying the judge skips it; the test set step still runs and the plan completes",
        denied.plan.status == "completed"
        and step_status(denied) == {"judge.create": "skipped", "eval_dataset.create": "done"}
        and denied_judge not in {j.name for j in caliber.judges.list()}
        and denied_set in {d.name for d in caliber.eval_datasets.list()},
    )

    # 5e. A judge that uses no evaluation variable passes the form and fails the step.
    broken_name = f"{NAME_PREFIX}no-variable-{suffix}"
    broken, _ = run_plan(
        caliber,
        "Create a judge.",
        autonomy="approve_plan",
        moves=[
            (
                "input",
                {"name": broken_name, "instructions": "Is {{ evidence }} faithful to the policy?"},
            )
        ],
    )
    broken_error = str(broken.steps[0].error)
    check(
        "a judge with no evaluation variable fails its step and the plan, naming the field",
        broken.plan.status == "failed"
        and step_status(broken) == {"judge.create": "failed"}
        and broken_error.startswith("invalid input: instructions: instructions must reference")
        and broken_name not in {j.name for j in caliber.judges.list()},
        broken_error[:90],
    )
    rerun = refused(lambda: caliber.aria.execute_plan(broken.plan.plan_id))
    check(
        "a failed plan cannot be run again (409): state the goal again",
        rerun is not None and rerun.status_code == 409,
    )

    # 5f. A duplicate name fails the step.
    duplicate, _ = run_plan(
        caliber,
        "Create a judge.",
        autonomy="approve_plan",
        moves=[("input", judge_form)],
    )
    duplicate_error = str(duplicate.steps[0].error)
    check(
        "a judge name that is already taken fails its step",
        duplicate.plan.status == "failed" and "is already in use" in duplicate_error,
        duplicate_error,
    )

    # 5g. Roles.
    refused_judge = refused(
        lambda: viewer.judges.create(
            f"{NAME_PREFIX}viewer-{suffix}", instructions="Is {{ outputs }} fine?"
        )
    )
    check(
        "without the operator scope the judge route refuses the account (403)",
        isinstance(refused_judge, CaliberPermissionError),
    )
    by_viewer = start(viewer, "Create a judge.", autonomy="approve_plan")
    check(
        "a plan owned by an account without the operator scope fails at execute, before it asks "
        "for anything, and creates nothing",
        by_viewer.plan.status == "failed"
        and by_viewer.steps[0].error == "plan owner lacks required scope(s): operator"
        and f"{NAME_PREFIX}viewer-{suffix}" not in {j.name for j in caliber.judges.list()},
        by_viewer.steps[0].error,
    )
    own = start(caliber, "Create a judge.", autonomy="approve_plan")
    waiting = next(i for i in caliber.aria.interactions(own.plan.plan_id) if i.status == "pending")
    other_answer = refused(lambda: operator.aria.answer(waiting.interaction_id, approved=False))
    check(
        "an operator who does not own the plan cannot answer its question (403)",
        isinstance(other_answer, CaliberPermissionError),
        str(other_answer).split("] ")[1].split(" (POST")[0] if other_answer else "",
    )
    other_read = refused(lambda: operator.aria.get_plan(own.plan.plan_id))
    check(
        "nor can they see it (404): plans are private to their owner",
        other_read is not None and other_read.status_code == 404,
    )
    walk(caliber, own.plan.plan_id, [("input", "skip")])

    check.finish()
    return result


def cleanup(caliber: CaliberClient) -> dict[str, int]:
    """Archive the judges and test sets earlier runs created (names start ``cookbook-12-``)."""
    archived = {"judges": 0, "test_sets": 0}
    for judge in caliber.judges.list():
        if judge.name.startswith(NAME_PREFIX) and judge.status != "archived":
            caliber.judges.update(judge.judge_id, status="archived")
            archived["judges"] += 1
    for dataset in caliber.eval_datasets.list():
        if dataset.name.startswith(NAME_PREFIX) and dataset.status != "archived":
            caliber.eval_datasets.update(dataset.dataset_id, status="archived")
            archived["test_sets"] += 1
    return archived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cleanup", action="store_true", help="archive what earlier runs created")
    parser.add_argument("--suffix", help="name suffix (default: a timestamp)")
    args = parser.parse_args(argv)
    base_url = os.environ["CALIBER_BASE_URL"]
    password = env_password("COOKBOOK_ARIA_PASSWORD", DEFAULT_PASSWORD)
    opened: list[CaliberClient] = []
    with env_client() as caliber:
        try:
            if args.cleanup:
                print(json.dumps(cleanup(caliber), indent=2))
                return 0
            ensure_accounts(caliber, (OPERATOR_USER, VIEWER_USER), password)
            clients = {}
            for user in (OPERATOR_USER, VIEWER_USER):
                clients[user] = CaliberClient(base_url, token=sign_in(base_url, user, password))
                opened.append(clients[user])
            result = run(
                caliber,
                operator=clients[OPERATOR_USER],
                viewer=clients[VIEWER_USER],
                suffix=args.suffix,
            )
        except (VerificationError, SetupError, CaliberAPIError) as exc:
            print(f"\nVERIFICATION FAILED: {exc}", file=sys.stderr)
            return 1
        finally:
            for client in opened:
                client.close()
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
