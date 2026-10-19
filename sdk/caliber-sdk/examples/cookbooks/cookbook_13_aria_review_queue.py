"""Cookbook 13: give Aria one sentence and get a review queue with 30 flagged replies to label.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/13-aria-review-governance-queue/
README.md) and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001      # the server root, not the /ajax-api path
    export CALIBER_TOKEN=calpat_...                    # an ADMIN personal access token
    export MLFLOW_TRACKING_URI=http://127.0.0.1:5000   # where CALIBER's traces live
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_13_aria_review_queue

The server needs ``CALIBER_FEATURE_ARIA=true`` and ``CALIBER_OPERATOR_USERS=admin,olga``; the script
also creates ``vera``, who is not an operator. Nothing here needs a model: Aria's planner is
deterministic and a review queue is people's work.

What it does, in order:

1. Writes the 30 flagged replies of ``assets/dataset/agent-replies.jsonl`` to MLflow as traces
   (``assets/seed_traces.py``), and draws a plan for each of the 26 goals in
   ``assets/dataset/goals.jsonl``, comparing it with the dataset (nothing runs).
2. States the canonical intent and answers four questions: the queue's fields, an Approve, the
   trace ids (the queue's id is wired from the first step), an Approve. Checks the queue and its 30
   pending items.
3. Reviews 12 of the 30 (``REVIEWED_IDS``) as the person who owns the queue, with the human
   labels in the dataset, and
   checks that every answer landed on its trace as an assessment and became a labelled example for
   judge alignment.
4. Provokes the refusals: answers the queue's questions do not fit, a trace id that does not exist,
   a skipped producer step, a denied step, a malformed question schema, a taken name, an account
   without the operator scope, and an operator who cannot see a queue they do not own.

``--cleanup`` archives what earlier runs created.
"""

from __future__ import annotations

import argparse
import importlib.util
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
    pending,
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

NAME_PREFIX = "cookbook-13-"
DEFAULT_PASSWORD = "cookbook-aria-lab-passphrase"
OPERATOR_USER = "olga"
VIEWER_USER = "vera"
INTENT = "Set up a review queue for human labeling of agent replies for safety, citation, and tone."
GOALS_EXPECTED = 26
REPLIES_EXPECTED = 30
#: The rows a person reviews by hand: clean replies, unsafe ones, wrong citations and bad tone.
REVIEWED_IDS = ("R01", "R03", "R05", "R09", "R11", "R12", "R13", "R15", "R18", "R22", "R23", "R27")
REVIEWED = len(REVIEWED_IDS)
FLAGGED_REASONS = {
    "thumbs_down",
    "latency_over_8s",
    "low_judge_score",
    "keyword:medication",
    "keyword:lawsuit",
    "keyword:refund",
    "pii_detected",
    "jailbreak_pattern",
    "escalation_ignored",
}


def default_assets_dir() -> Path:
    return Path(
        os.environ.get(
            "COOKBOOK_13_ASSETS",
            Path(__file__).resolve().parents[4]
            / "docs-site/cookbooks/13-aria-review-governance-queue/assets",
        )
    )


def seed_traces(assets: Path) -> dict[str, str]:
    """Write the 30 replies as traces (needs ``mlflow`` and ``MLFLOW_TRACKING_URI``)."""
    spec = importlib.util.spec_from_file_location(
        "cookbook13_seed_traces", assets / "seed_traces.py"
    )
    if spec is None or spec.loader is None:
        raise SetupError(f"{assets / 'seed_traces.py'} not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.seed(assets / "dataset" / "agent-replies.jsonl"))


def _reason(error: CaliberAPIError | None) -> str:
    return str(error).split("] ", 1)[1].split(" (", 1)[0] if error else ""


def run(
    caliber: CaliberClient,
    *,
    operator: CaliberClient,
    viewer: CaliberClient,
    suffix: str | None = None,
    assets: Path | None = None,
    traces: dict[str, str] | None = None,
    report: Report = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the whole lab. ``caliber`` is the admin's client (the plan's owner and the reviewer);
    ``operator`` and ``viewer`` are the two other accounts' clients."""
    assets = assets or default_assets_dir()
    suffix = suffix or time.strftime("%Y%m%d-%H%M%S")
    check = Checks(report)
    result: dict[str, Any] = {"suffix": suffix}
    goals = load_jsonl(assets / "dataset/goals.jsonl")
    replies = load_jsonl(assets / "dataset/agent-replies.jsonl")
    queue_spec = form_values(assets / "review-queues/agent-reply-review.queue.json")
    queue_name = f"{NAME_PREFIX}{queue_spec['name']}-{suffix}"
    queue_form = {**queue_spec, "name": queue_name}

    require_admin_and_aria(caliber)
    require_scopes(
        {"olga": operator, "vera": viewer},
        has={"olga": True, "vera": False},
        hint=(
            "start CALIBER with CALIBER_OPERATOR_USERS=admin,olga (olga is an operator who does "
            "not own the queue, vera must not be one)"
        ),
    )

    # -- 1. The traces to review, and what Aria plans for 26 goals -----------------------------
    trace_of = traces if traces is not None else seed_traces(assets)
    check(
        "the 30 flagged replies are in MLflow as traces, one per row",
        len(replies) == REPLIES_EXPECTED
        and set(trace_of) == {row["id"] for row in replies}
        and len(set(trace_of.values())) == REPLIES_EXPECTED,
        f"{len(trace_of)} traces",
    )
    check(
        "every reply says why it was flagged",
        {row["flag"] for row in replies} <= FLAGGED_REASONS,
        sorted({row["flag"] for row in replies}),
    )
    check_goals_report(caliber, goals, GOALS_EXPECTED, check)

    # -- 2. The canonical intent, through every question ----------------------------------------
    drawn = caliber.aria.create_plan(INTENT, autonomy="ask_each")
    plan_id = drawn.plan.plan_id
    create_step, add_step = drawn.steps[0], drawn.steps[-1]
    check(
        "the intent draws review_queue.create then review_queue.add_items",
        step_keys(drawn) == ["review_queue.create", "review_queue.add_items"],
        plan_id,
    )
    check(
        "the enqueue step's queue_id is wired from the create step, nothing else is",
        add_step.extra.get("inputs")
        == {"queue_id": {"$from_step": create_step.step_id, "path": "queue_id"}}
        and add_step.depends_on == [create_step.step_id]
        and not create_step.extra.get("inputs"),
    )
    caliber.aria.approve_plan(plan_id)
    caliber.aria.execute_plan(plan_id)
    walk(caliber, plan_id, [("input", queue_form), ("permission", "approve")])
    enqueue_form = pending(caliber, plan_id)[0]
    check(
        "the enqueue form asks for trace ids only: the queue's id is already known",
        enqueue_form.kind == "input" and enqueue_form.evidence["missing"] == ["trace_ids"],
        enqueue_form.evidence["missing"],
    )
    trace_ids = [trace_of[row["id"]] for row in replies]
    finished, _ = walk(
        caliber,
        plan_id,
        [
            ("input", {"trace_ids": trace_ids, "assigned_to": "admin"}),
            ("permission", "approve"),
        ],
    )
    check(
        "the plan completed with both steps done",
        finished.plan.status == "completed" and set(step_status(finished).values()) == {"done"},
        finished.plan.status,
    )
    queue_id = finished.steps[0].result["queue_id"]
    check(
        "the enqueue step reports 30 traces added to that queue",
        finished.steps[1].result == {"queue_id": queue_id, "added": REPLIES_EXPECTED},
        finished.steps[1].result,
    )
    detail = caliber.raw.get(f"/review-queues/{queue_id}")
    queue, items = detail["queue"], detail["items"]
    check(
        "the queue is active with the typed name, five questions and its reviewers",
        queue["status"] == "active"
        and queue["name"] == queue_name
        and [q["key"] for q in queue["questions"]]
        == ["safety", "severity", "citation_ok", "tone_ok", "reviewer_notes"]
        and queue["reviewers"] == queue_spec["reviewers"]
        and queue["questions"][1]["options"] == ["low", "medium", "high"],
        f"{queue_id} {queue['name']}",
    )
    check(
        "it holds the 30 traces, all pending and assigned",
        queue["item_count"] == REPLIES_EXPECTED
        and queue["pending_count"] == REPLIES_EXPECTED
        and {item["trace_id"] for item in items} == set(trace_ids)
        and all(item["status"] == "pending" and item["assigned_to"] == "admin" for item in items),
        f"{queue['item_count']} items, {queue['pending_count']} pending",
    )
    result.update(plan_id=plan_id, queue_id=queue_id)

    # -- 3. Review twelve of them -------------------------------------------------------------
    item_of = {item["trace_id"]: item["item_id"] for item in items}
    reviewed_rows = [row for row in replies if row["id"] in REVIEWED_IDS]
    for row in reviewed_rows:
        caliber.review_queues.submit(queue_id, item_of[trace_of[row["id"]]], **row["review"])
    after = caliber.raw.get(f"/review-queues/{queue_id}")
    done = [item for item in after["items"] if item["status"] == "completed"]
    check(
        "twelve reviews are in: 12 completed, 18 still pending",
        len(done) == REVIEWED and after["queue"]["pending_count"] == REPLIES_EXPECTED - REVIEWED,
        f"{len(done)} completed, {after['queue']['pending_count']} pending",
    )
    wrong: list[str] = []
    for row in reviewed_rows:
        stored = {
            a["name"]: a["value"]
            for a in caliber.observability.trace(trace_of[row["id"]])["assessments"]
        }
        if stored != row["review"]:
            wrong.append(f"{row['id']}: {stored}")
    check(
        "every answer is on its trace as an assessment, with the reviewer's value",
        not wrong,
        f"{REVIEWED} traces" if not wrong else wrong[:2],
    )
    labels = caliber.review_queues.alignment_examples(queue_id, question_key="safety")["examples"]
    by_trace = {ex["provenance"]["trace_id"]: ex["label"] for ex in labels}
    check(
        "the safety answers are 12 labelled examples a judge can be checked against (lab 10)",
        len(labels) == REVIEWED
        and all(by_trace[trace_of[row["id"]]] is row["review"]["safety"] for row in reviewed_rows),
        f"{sum(1 for v in by_trace.values() if v is False)} unsafe of {len(labels)}",
    )
    unsafe_seen = sum(1 for row in reviewed_rows if not row["review"]["safety"])
    result["review"] = {
        "reviewed": REVIEWED,
        "pending": REPLIES_EXPECTED - REVIEWED,
        "unsafe_in_reviewed": unsafe_seen,
    }

    # -- 4. What a review refuses -------------------------------------------------------------
    spare = next(row for row in replies if row["id"] not in REVIEWED_IDS)
    target = item_of[trace_of[spare["id"]]]
    good = dict(spare["review"])
    attempts = {
        "a missing required answer": {k: v for k, v in good.items() if k != "citation_ok"},
        "an answer of the wrong type": {**good, "safety": "yes"},
        "an option the question does not have": {**good, "severity": "catastrophic"},
        "an answer to a question that does not exist": {**good, "mood": "calm"},
    }
    refusals = {
        label: refused(partial(caliber.review_queues.submit, queue_id, target, **answers))
        for label, answers in attempts.items()
    }
    check(
        "a review that does not fit the questions is refused (400) and names why",
        all(e is not None and e.status_code == 400 for e in refusals.values()),
        {label: _reason(e) for label, e in refusals.items()},
    )
    item_now = next(
        i for i in caliber.raw.get(f"/review-queues/{queue_id}")["items"] if i["item_id"] == target
    )
    check("after four refusals the item is still pending", item_now["status"] == "pending")
    resubmit = refused(
        lambda: caliber.review_queues.submit(
            queue_id, item_of[trace_of[reviewed_rows[0]["id"]]], **reviewed_rows[0]["review"]
        )
    )
    check(
        "a reviewed item cannot be reviewed again (409)",
        resubmit is not None and resubmit.status_code == 409,
        _reason(resubmit),
    )
    check(
        "an account without the operator scope cannot review (403)",
        isinstance(
            refused(lambda: viewer.review_queues.submit(queue_id, target, **good)),
            CaliberPermissionError,
        ),
    )
    olga_read = refused(lambda: operator.review_queues.get(queue_id))
    olga_submit = refused(lambda: operator.review_queues.submit(queue_id, target, **good))
    check(
        "an operator the queue lists as a reviewer still cannot open or review it (404): "
        "a personal queue belongs to its owner",
        olga_read is not None
        and olga_read.status_code == 404
        and olga_submit is not None
        and olga_submit.status_code == 404
        and "olga" in queue["reviewers"],
        "reviewers: " + ", ".join(queue["reviewers"]),
    )

    # A trace id that does not exist is queued; the reviewer finds out when they submit.
    ghost = "tr-this-trace-does-not-exist"
    caliber.review_queues.enqueue(queue_id, trace_ids=[ghost])
    ghost_item = next(
        i for i in caliber.raw.get(f"/review-queues/{queue_id}")["items"] if i["trace_id"] == ghost
    )
    ghost_submit = refused(
        lambda: caliber.review_queues.submit(queue_id, ghost_item["item_id"], **good)
    )
    ghost_after = next(
        i
        for i in caliber.raw.get(f"/review-queues/{queue_id}")["items"]
        if i["item_id"] == ghost_item["item_id"]
    )
    check(
        "a trace id that does not exist is queued without complaint, and fails when reviewed "
        "(502); the item stays pending",
        ghost_item["status"] == "pending"
        and ghost_submit is not None
        and ghost_submit.status_code == 502
        and "not found" in str(ghost_submit)
        and ghost_after["status"] == "pending",
        _reason(ghost_submit)[:70],
    )

    # -- 5. Plan refusals ---------------------------------------------------------------------
    # 5a. Skip the queue: the enqueue step has nothing to act on and is skipped too.
    skipped, _ = run_plan(caliber, INTENT, autonomy="approve_plan", moves=[("input", "skip")])
    check(
        "skipping the queue skips the enqueue step with it, and the plan completes",
        skipped.plan.status == "completed"
        and step_status(skipped)
        == {"review_queue.create": "skipped", "review_queue.add_items": "skipped"}
        and "step 1 (review_queue.create) was skipped" in str(skipped.steps[1].error),
        str(skipped.steps[1].error),
    )

    # 5b. Create the queue, deny the enqueue: the queue is kept.
    kept_name = f"{NAME_PREFIX}kept-{suffix}"
    kept, _ = run_plan(
        caliber,
        INTENT,
        autonomy="ask_each",
        moves=[
            ("input", {**queue_form, "name": kept_name}),
            ("permission", "approve"),
            ("input", {"trace_ids": trace_ids[:2]}),
            ("permission", "deny"),
        ],
    )
    kept_queue = next(q for q in caliber.review_queues.list() if q.name == kept_name)
    check(
        "denying the enqueue leaves the queue created and empty; the plan completes",
        kept.plan.status == "completed"
        and step_status(kept)
        == {"review_queue.create": "done", "review_queue.add_items": "skipped"}
        and kept_queue.item_count == 0,
    )

    # 5c. Forms the schema refuses stay open; schemas the queue refuses fail the step.
    first = start(caliber, INTENT, autonomy="approve_plan")
    form = pending(caliber, first.plan.plan_id)[0]
    bad_forms = {
        "no questions": {"name": "x", "questions": []},
        "an unknown field": {"name": "x", "questions": [{"key": "a", "title": "A?"}], "colour": 1},
        "a question without a title": {"name": "x", "questions": [{"key": "a"}]},
        "a question type that does not exist": {
            "name": "x",
            "questions": [{"key": "a", "title": "A?", "type": "likert"}],
        },
    }
    form_refusals = {
        label: refused(partial(caliber.aria.answer, form.interaction_id, inputs=inputs))
        for label, inputs in bad_forms.items()
    }
    check(
        "the form refuses no questions, an unknown field, an untitled question and a made-up "
        "question type (409) and stays open",
        all(e is not None and e.status_code == 409 for e in form_refusals.values())
        and [i.status for i in caliber.aria.interactions(first.plan.plan_id)] == ["pending"],
        {label: _reason(e) for label, e in form_refusals.items()},
    )
    walk(caliber, first.plan.plan_id, [("input", "skip")])

    categorical, _ = run_plan(
        caliber,
        INTENT,
        autonomy="approve_plan",
        moves=[
            (
                "input",
                {
                    "name": f"{NAME_PREFIX}no-options-{suffix}",
                    "questions": [{"key": "tone", "title": "Tone?", "type": "categorical"}],
                },
            )
        ],
    )
    check(
        "a categorical question with no options passes the form and fails the step, naming it",
        categorical.plan.status == "failed"
        and str(categorical.steps[0].error)
        == "invalid input: questions.0: categorical questions require a non-empty 'options' list",
        categorical.steps[0].error,
    )
    taken, _ = run_plan(caliber, INTENT, autonomy="approve_plan", moves=[("input", queue_form)])
    check(
        "a queue name that is already taken fails its step",
        taken.plan.status == "failed" and "is already in use" in str(taken.steps[0].error),
        taken.steps[0].error,
    )
    rerun = refused(lambda: caliber.aria.execute_plan(taken.plan.plan_id))
    check(
        "a failed plan leaves the steps after it pending, and cannot be run again (409)",
        step_status(taken)["review_queue.add_items"] == "pending"
        and rerun is not None
        and rerun.status_code == 409,
    )

    # 5d. A goal with no steps; a cancelled plan.
    nothing = caliber.aria.create_plan("Write a poem about review season.")
    approve_empty = refused(lambda: caliber.aria.approve_plan(nothing.plan.plan_id))
    check(
        "a goal with no steps is created with a warning and refused at approval (409)",
        nothing.plan.step_count == 0
        and bool(nothing.extra.get("warnings"))
        and approve_empty is not None
        and approve_empty.status_code == 409,
    )
    caliber.aria.update_plan(nothing.plan.plan_id, status="cancelled")

    # 5e. Roles.
    by_viewer = start(viewer, INTENT, autonomy="approve_plan")
    check(
        "a plan owned by an account without the operator scope fails at execute and creates "
        "nothing",
        by_viewer.plan.status == "failed"
        and by_viewer.steps[0].error == "plan owner lacks required scope(s): operator",
        by_viewer.steps[0].error,
    )
    own = start(caliber, INTENT, autonomy="approve_plan")
    waiting = pending(caliber, own.plan.plan_id)[0]
    other_answer = refused(lambda: operator.aria.answer(waiting.interaction_id, approved=False))
    check(
        "an operator who does not own the plan cannot answer its question (403)",
        isinstance(other_answer, CaliberPermissionError),
        _reason(other_answer),
    )
    walk(caliber, own.plan.plan_id, [("input", "skip")])

    check.finish()
    return result


def cleanup(caliber: CaliberClient) -> dict[str, int]:
    """Archive the review queues earlier runs created (names start ``cookbook-13-``)."""
    archived = 0
    for queue in caliber.review_queues.list():
        if queue.name.startswith(NAME_PREFIX):
            caliber.review_queues.update(queue.queue_id, status="archived")
            archived += 1
    return {"review_queues": archived}


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
