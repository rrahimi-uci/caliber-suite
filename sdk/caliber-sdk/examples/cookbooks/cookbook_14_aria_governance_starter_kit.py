"""Cookbook 14: give Aria one sentence and get a judge, a test set and a review queue.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/14-aria-governance-starter-kit/
README.md) and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001      # the server root, not the /ajax-api path
    export CALIBER_TOKEN=calpat_...                    # an ADMIN personal access token
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_14_aria_governance_starter_kit

The server needs ``CALIBER_FEATURE_ARIA=true`` and ``CALIBER_OPERATOR_USERS=admin,olga``; the script
also creates ``vera``, who is not an operator. Nothing in the planning needs a model: Aria's
planner is deterministic. The step that does, running the judge in Evaluations, has two modes, told
apart by ``GET /readiness``. On a simulated stack (the default) it is refused with a 400 and the
script verifies the refusal. On a stack started with
``scripts/cookbook-env.sh up NAME --flags cookbooks,aria --real-llm`` the script scores the 30
policy cases with the judge for real (an answerer prompt that sees the policy text), asserts
structure, ranges and the ``high-risk`` slice (never exact scores), and then checks the Ask Aria
chat on both sides of its permission boundary. That takes a few minutes and costs cents;
``COOKBOOK_ARIA_HTTP_TIMEOUT`` (seconds, default 900) bounds one request.

What it does, in order:

1. Draws a plan for each of the 28 goals in ``assets/dataset/goals.jsonl`` and compares it with the
   dataset (nothing runs).
2. States the canonical intent under *Stop before each change* and answers the seven questions the
   plan asks (three forms with an Approve each, then the enqueue form, which it skips because there
   are no traces yet). Checks the judge, the review queue and the test set exist under three
   different names.
3. Adds the 30 cases of ``assets/dataset/release-candidate-cases.jsonl`` to the test set and runs
   it in Evaluations (refused on a simulated stack; judge-scored and checked by invariants on a
   real one).
4. Runs the same sentence under *Run everything it safely can*, after changing the autonomy of the
   draft, and shows that only the forms are asked.
5. Provokes the refusals: a denied step that takes its dependent with it, a failure half way (what
   was made stays made), a plan with no steps, an account without the operator scope.
6. Real stack only: Ask Aria's chat creates a review queue in a ``build`` / ``auto_all`` session and
   is not offered the tool in the default session.

``--cleanup`` archives what earlier runs created (and deletes the prompt of a real run).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberPermissionError
from examples.cookbooks._aria_lab import (
    GROUNDED_PROMPT,
    Checks,
    Report,
    SetupError,
    VerificationError,
    check_chat_boundary,
    check_goals_report,
    check_judged_evaluation,
    create_prompt,
    ensure_accounts,
    env_password,
    form_values,
    http_timeout,
    is_real,
    judge_rate,
    load_jsonl,
    pending,
    refused,
    require_admin_and_aria,
    require_scopes,
    run_judged_evaluation,
    run_plan,
    sign_in,
    start,
    step_keys,
    step_status,
    walk,
)
from examples.cookbooks._helpers import env_client

NAME_PREFIX = "cookbook-14-"
DEFAULT_PASSWORD = "cookbook-aria-lab-passphrase"
OPERATOR_USER = "olga"
VIEWER_USER = "vera"
INTENT = (
    "Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to "
    "score against, and a review queue for human checks."
)
KIT = ["judge.create", "review_queue.create", "eval_dataset.create", "review_queue.add_items"]
GOALS_EXPECTED = 28
CASES_EXPECTED = 30
HIGH_RISK_EXPECTED = 5
#: A real run: the share of the 30 policy cases the judge finds faithful must reach this. A grounded
#: answerer typically scores far higher (README); the bar only rules out a judge or an answerer
#: that does not work.
MIN_FAITHFUL_RATE = float(os.environ.get("COOKBOOK_14_MIN_RATE", "0.6"))


def default_assets_dir() -> Path:
    return Path(
        os.environ.get(
            "COOKBOOK_14_ASSETS",
            Path(__file__).resolve().parents[4]
            / "docs-site/cookbooks/14-aria-governance-starter-kit/assets",
        )
    )


def _reason(error: CaliberAPIError | None) -> str:
    return str(error).split("] ", 1)[1].split(" (", 1)[0] if error else ""


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
    cases = load_jsonl(assets / "dataset/release-candidate-cases.jsonl")
    judge_spec = form_values(assets / "judges/answer-faithfulness.judge.json")
    queue_spec = form_values(assets / "review-queues/governance-review.queue.json")
    dataset_spec = form_values(assets / "datasets/release-candidates.dataset.json")

    def forms(tag: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """The three forms with names that cannot collide with another run of the kit."""
        stem = f"{NAME_PREFIX}{{}}-{tag}-{suffix}"
        return (
            {**judge_spec, "name": stem.format(judge_spec["name"])},
            {**queue_spec, "name": stem.format(queue_spec["name"])},
            {**dataset_spec, "name": stem.format(dataset_spec["name"])},
        )

    def names() -> dict[str, set[str]]:
        return {
            "judges": {j.name for j in caliber.judges.list()},
            "queues": {q.name for q in caliber.review_queues.list()},
            "test_sets": {d.name for d in caliber.eval_datasets.list()},
        }

    require_admin_and_aria(caliber)
    real = is_real(caliber)
    result["mode"] = "real" if real else "simulated"
    prompts: list[str] = []
    result["prompts"] = prompts
    require_scopes(
        {"olga": operator, "vera": viewer},
        has={"olga": True, "vera": False},
        hint=(
            "start CALIBER with CALIBER_OPERATOR_USERS=admin,olga (olga is an operator who does "
            "not own the plan, vera must not be one)"
        ),
    )

    # -- 1. What Aria plans for 28 goals ------------------------------------------------------
    check_goals_report(caliber, goals, GOALS_EXPECTED, check)

    # -- 2. The canonical intent under "Stop before each change" ------------------------------
    judge_form, queue_form, dataset_form = forms("kit")
    drawn = caliber.aria.create_plan(INTENT, autonomy="ask_each")
    plan_id = drawn.plan.plan_id
    check(
        "the intent draws judge, review queue, test set, then the enqueue step (registry order)",
        step_keys(drawn) == KIT,
        plan_id,
    )
    queue_step = drawn.steps[1]
    wired = {s.capability_key: s.extra.get("inputs") for s in drawn.steps}
    check(
        "only the enqueue step has a wired input (the queue's id); the three names are asked for",
        wired
        == {
            "judge.create": {},
            "review_queue.create": {},
            "eval_dataset.create": {},
            "review_queue.add_items": {
                "queue_id": {"$from_step": queue_step.step_id, "path": "queue_id"}
            },
        },
    )
    caliber.aria.approve_plan(plan_id)
    caliber.aria.execute_plan(plan_id)
    finished, trail = walk(
        caliber,
        plan_id,
        [
            ("input", judge_form),
            ("permission", "approve"),
            ("input", queue_form),
            ("permission", "approve"),
            ("input", dataset_form),
            ("permission", "approve"),
            ("input", "skip"),
        ],
    )
    check(
        "the plan asked 7 questions: a form and an approval for each of the three, then the "
        "enqueue form",
        trail
        == [
            ("input", "judge.create"),
            ("permission", "judge.create"),
            ("input", "review_queue.create"),
            ("permission", "review_queue.create"),
            ("input", "eval_dataset.create"),
            ("permission", "eval_dataset.create"),
            ("input", "review_queue.add_items"),
        ],
    )
    check(
        "the plan completed: three steps done, the enqueue step skipped (no traces yet)",
        finished.plan.status == "completed"
        and step_status(finished)
        == {
            "judge.create": "done",
            "review_queue.create": "done",
            "eval_dataset.create": "done",
            "review_queue.add_items": "skipped",
        },
        finished.plan.status,
    )
    results = {s.capability_key: s.result for s in finished.steps}
    judge = caliber.judges.get(results["judge.create"]["judge_id"])
    queue = caliber.review_queues.get(results["review_queue.create"]["queue_id"])
    dataset = caliber.eval_datasets.get(results["eval_dataset.create"]["dataset_id"])
    check(
        "the judge, the review queue and the test set exist under three different names",
        len({judge.name, queue.name, dataset.name}) == 3
        and (judge.name, queue.name, dataset.name)
        == (judge_form["name"], queue_form["name"], dataset_form["name"]),
        f"{judge.judge_id} {queue.queue_id} {dataset.dataset_id}",
    )
    check(
        "the judge is active and boolean, the queue has its four questions, the test set is empty "
        "at version 1",
        judge.status == "active"
        and judge.feedback_value_type == "bool"
        and queue.status == "active"
        and [q["key"] for q in queue.questions] == ["faithful", "citation_ok", "tone", "notes"]
        and queue.item_count == 0
        and dataset.version == 1
        and not caliber.eval_datasets.examples(dataset.dataset_id),
    )
    result.update(
        plan_id=plan_id,
        judge_id=judge.judge_id,
        queue_id=queue.queue_id,
        dataset_id=dataset.dataset_id,
    )

    # -- 3. Fill the test set -----------------------------------------------------------------
    for case in cases:
        caliber.eval_datasets.add_example(
            dataset.dataset_id, input=case["input"], expected=case["expected"], tags=case["tags"]
        )
    rows = caliber.eval_datasets.examples(dataset.dataset_id)
    by_question = {row.input["question"]: row for row in rows}
    check(
        "the test set holds the 30 cases, one version per row",
        len(cases) == CASES_EXPECTED
        and len(rows) == CASES_EXPECTED
        and caliber.eval_datasets.get(dataset.dataset_id).version == 1 + CASES_EXPECTED,
        f"{len(rows)} rows, version {caliber.eval_datasets.get(dataset.dataset_id).version}",
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
    if real:
        result["evaluation"] = _evaluate_for_real(
            caliber, check, dataset.dataset_id, judge.judge_id, suffix=suffix, prompts=prompts
        )
    else:
        refusal = refused(
            lambda: caliber.evaluations.create(dataset.dataset_id, scorers=["non_empty"])
        )
        check(
            "Evaluations are refused without a real model (400): the judge cannot run here",
            refusal is not None
            and refusal.status_code == 400
            and "real LLM provider" in str(refusal),
            "simulated stack",
        )
        result["evaluation"] = "refused (simulated provider)"
    result["cases"] = {
        "count": len(cases),
        "categories": sorted({c["tags"][0] for c in cases}),
        "high_risk": sum(1 for c in cases if "high-risk" in c["tags"]),
    }

    # -- 4. The same sentence, asking less ----------------------------------------------------
    auto_judge, auto_queue, auto_dataset = forms("auto")
    draft = caliber.aria.create_plan(INTENT)
    check(
        "a plan is drawn under the default autonomy ('Review the plan, then run')",
        draft.plan.autonomy == "approve_plan",
    )
    switched = caliber.aria.update_plan(draft.plan.plan_id, autonomy="auto_guarded")
    check(
        "a draft's autonomy can be changed before it is approved",
        switched.plan.autonomy == "auto_guarded" and switched.plan.status == "draft",
    )
    caliber.aria.approve_plan(draft.plan.plan_id)
    edit_after_approval = refused(
        lambda: caliber.aria.update_plan(draft.plan.plan_id, autonomy="ask_each")
    )
    check(
        "after approval it can no longer be changed (409)",
        edit_after_approval is not None and edit_after_approval.status_code == 409,
        _reason(edit_after_approval),
    )
    caliber.aria.execute_plan(draft.plan.plan_id)
    second, _ = walk(
        caliber,
        draft.plan.plan_id,
        [
            ("input", auto_judge),
            ("input", auto_queue),
            ("input", auto_dataset),
            ("input", "skip"),
        ],
    )
    asked = Counter(i.kind for i in caliber.aria.interactions(draft.plan.plan_id))
    first_asked = Counter(i.kind for i in caliber.aria.interactions(plan_id))
    check(
        "under 'Run everything it safely can' only the four forms are asked, no approvals; the "
        "same kit is built",
        second.plan.status == "completed"
        and step_status(second) == step_status(finished)
        and asked == Counter({"input": 4})
        and first_asked == Counter({"input": 4, "permission": 3}),
        f"ask_each: {dict(first_asked)}; auto_guarded: {dict(asked)}",
    )
    check(
        "both kits are in the deployment, six artifacts under six names",
        {auto_judge["name"], judge_form["name"]} <= names()["judges"]
        and {auto_queue["name"], queue_form["name"]} <= names()["queues"]
        and {auto_dataset["name"], dataset_form["name"]} <= names()["test_sets"],
    )

    # -- 5. Refusals --------------------------------------------------------------------------
    # 5a. Deny the queue: the enqueue goes with it, the test set still gets made.
    d_judge, d_queue, d_dataset = forms("denied")
    denied, _ = run_plan(
        caliber,
        INTENT,
        autonomy="ask_each",
        moves=[
            ("input", d_judge),
            ("permission", "approve"),
            ("input", d_queue),
            ("permission", "deny"),
            ("input", d_dataset),
            ("permission", "approve"),
        ],
    )
    now = names()
    check(
        "denying the queue skips it and the enqueue step with it (never asked); the judge and the "
        "test set are made",
        denied.plan.status == "completed"
        and step_status(denied)
        == {
            "judge.create": "done",
            "review_queue.create": "skipped",
            "eval_dataset.create": "done",
            "review_queue.add_items": "skipped",
        }
        and d_judge["name"] in now["judges"]
        and d_dataset["name"] in now["test_sets"]
        and d_queue["name"] not in now["queues"],
        str(denied.steps[3].error),
    )

    # 5b. A failure half way stops the plan; what was already made stays made.
    f_judge, _unused_queue, f_dataset = forms("halfway")
    halfway, _ = run_plan(
        caliber,
        INTENT,
        autonomy="approve_plan",
        moves=[("input", f_judge), ("input", {**queue_form, "name": queue_form["name"]})],
    )
    now = names()
    check(
        "a queue name that is taken fails the plan after the judge was made: the judge stays, "
        "the steps after it never run",
        halfway.plan.status == "failed"
        and step_status(halfway)
        == {
            "judge.create": "done",
            "review_queue.create": "failed",
            "eval_dataset.create": "pending",
            "review_queue.add_items": "pending",
        }
        and "is already in use" in str(halfway.steps[1].error)
        and f_judge["name"] in now["judges"]
        and f_dataset["name"] not in now["test_sets"],
        halfway.steps[1].error,
    )
    broken_judge, _, broken_dataset = forms("broken")
    broken, _ = run_plan(
        caliber,
        INTENT,
        autonomy="approve_plan",
        moves=[
            (
                "input",
                {**broken_judge, "instructions": "Is the answer {{ faithful }} to the evidence?"},
            )
        ],
    )
    check(
        "a judge with no evaluation variable fails the plan before anything else is made",
        broken.plan.status == "failed"
        and str(broken.steps[0].error).startswith(
            "invalid input: instructions: instructions must reference"
        )
        and set(step_status(broken).values()) == {"failed", "pending"}
        and broken_judge["name"] not in names()["judges"]
        and broken_dataset["name"] not in names()["test_sets"],
        str(broken.steps[0].error)[:80],
    )

    # 5c. A goal with no steps.
    nothing = caliber.aria.create_plan("Create the governance kit.")
    approve_empty = refused(lambda: caliber.aria.approve_plan(nothing.plan.plan_id))
    check(
        "a goal with no steps (governance is not a domain word) is refused at approval (409)",
        nothing.plan.step_count == 0
        and bool(nothing.extra.get("warnings"))
        and approve_empty is not None
        and approve_empty.status_code == 409,
    )
    caliber.aria.update_plan(nothing.plan.plan_id, status="cancelled")

    # 5d. Roles.
    by_viewer = start(viewer, INTENT, autonomy="approve_plan")
    check(
        "a plan owned by an account without the operator scope fails at execute and creates "
        "nothing",
        by_viewer.plan.status == "failed"
        and by_viewer.steps[0].error == "plan owner lacks required scope(s): operator"
        and not any(s.status == "done" for s in by_viewer.steps),
        by_viewer.steps[0].error,
    )
    own = start(caliber, INTENT, autonomy="approve_plan")
    waiting = pending(caliber, own.plan.plan_id)[0]
    other_answer = refused(lambda: operator.aria.answer(waiting.interaction_id, approved=False))
    other_read = refused(lambda: operator.aria.get_plan(own.plan.plan_id))
    check(
        "an operator who does not own the plan cannot answer its question (403) or read it (404)",
        isinstance(other_answer, CaliberPermissionError)
        and other_read is not None
        and other_read.status_code == 404,
        _reason(other_answer),
    )
    skipped_all, skipped_trail = walk(
        caliber, own.plan.plan_id, [("input", "skip"), ("input", "skip"), ("input", "skip")]
    )
    check(
        "skipping the three forms skips everything: the enqueue step is never asked (its queue was "
        "skipped), and the plan completes with nothing made",
        skipped_all.plan.status == "completed"
        and set(step_status(skipped_all).values()) == {"skipped"}
        and [capability for _, capability in skipped_trail]
        == ["judge.create", "review_queue.create", "eval_dataset.create"],
        step_status(skipped_all),
    )

    # -- 6. Ask Aria's chat, on a real stack ------------------------------------------------------
    if real:
        result["chat"] = _chat_for_real(caliber, check, suffix)

    check.finish()
    return result


def _evaluate_for_real(
    caliber: CaliberClient,
    check: Checks,
    dataset_id: str,
    judge_id: str,
    *,
    suffix: str,
    prompts: list[str],
) -> dict[str, Any]:
    """Score the 30 policy cases with the judge, the answers coming from a grounded prompt."""
    prompt_ref = create_prompt(caliber, f"{NAME_PREFIX}grounded-answer-{suffix}", GROUNDED_PROMPT)
    prompts.append(prompt_ref.split("@")[0])
    run = run_judged_evaluation(
        caliber, dataset_id, judge_id, prompt_ref=prompt_ref, label=f"release candidate {suffix}"
    )
    summary = check_judged_evaluation(
        check, "release candidate", run, judge_id, rows=CASES_EXPECTED, prompt_ref=prompt_ref
    )
    rate = judge_rate(run, judge_id) or 0.0
    check(
        f"the judge finds at least {MIN_FAITHFUL_RATE:.0%} of the grounded answers faithful",
        rate >= MIN_FAITHFUL_RATE,
        f"{rate:.0%}",
    )
    slices = (run.evidence or {}).get("slices") or {}
    check(
        f"the evidence slices the run by tag, with the {HIGH_RISK_EXPECTED} high-risk cases apart",
        (slices.get("high-risk") or {}).get("n_examples") == HIGH_RISK_EXPECTED,
        {tag: (v or {}).get("n_examples") for tag, v in sorted(slices.items())},
    )
    summary["high_risk_rows"] = (slices.get("high-risk") or {}).get("n_examples")
    return summary


def _chat_for_real(caliber: CaliberClient, check: Checks, suffix: str) -> dict[str, Any]:
    """Ask Aria (the chat, not the Plans page) acts in build / auto_all and only reads otherwise."""
    acting = f"{NAME_PREFIX}chat-queue-{suffix}"
    reading = f"{NAME_PREFIX}chat-manual-{suffix}"
    spec = (
        "with one pass_fail question: key faithful, title 'Is the answer faithful to the policy?'. "
        "Then tell me its id."
    )

    def queue_names() -> set[str]:
        return {q.name for q in caliber.review_queues.list()}

    summary = check_chat_boundary(
        caliber,
        check,
        tool="review_queue_create",
        ask=f"Create a review queue named {acting} {spec}",
        exists=lambda: acting in queue_names(),
        absent_ask=f"Create a review queue named {reading} {spec}",
        absent_exists=lambda: reading in queue_names(),
    )
    made = next((q for q in caliber.review_queues.list() if q.name == acting), None)
    check(
        "the chat's queue is active with the question that was asked for",
        made is not None
        and made.status == "active"
        and [q["key"] for q in caliber.review_queues.get(made.queue_id).questions] == ["faithful"],
        made.queue_id if made else "",
    )
    return summary


def cleanup(caliber: CaliberClient) -> dict[str, int]:
    """Archive the judges, queues and test sets earlier runs created (names ``cookbook-14-``)."""
    archived = {"judges": 0, "review_queues": 0, "test_sets": 0, "prompts": 0}
    for judge in caliber.judges.list():
        if judge.name.startswith(NAME_PREFIX) and judge.status != "archived":
            caliber.judges.update(judge.judge_id, status="archived")
            archived["judges"] += 1
    for queue in caliber.review_queues.list():
        if queue.name.startswith(NAME_PREFIX):
            caliber.review_queues.update(queue.queue_id, status="archived")
            archived["review_queues"] += 1
    for dataset in caliber.eval_datasets.list():
        if dataset.name.startswith(NAME_PREFIX) and dataset.status != "archived":
            caliber.eval_datasets.update(dataset.dataset_id, status="archived")
            archived["test_sets"] += 1
    for prompt in caliber.prompts.list():
        if prompt.prompt_name.startswith(NAME_PREFIX):
            caliber.prompts.delete(prompt.prompt_name)
            archived["prompts"] += 1
    return archived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cleanup", action="store_true", help="archive what earlier runs created")
    parser.add_argument("--suffix", help="name suffix (default: a timestamp)")
    args = parser.parse_args(argv)
    base_url = os.environ["CALIBER_BASE_URL"]
    password = env_password("COOKBOOK_ARIA_PASSWORD", DEFAULT_PASSWORD)
    opened: list[CaliberClient] = []
    with env_client(timeout=http_timeout()) as caliber:
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
