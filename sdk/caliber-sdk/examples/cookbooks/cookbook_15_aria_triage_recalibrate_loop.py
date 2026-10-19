"""Cookbook 15: give Aria one sentence about weak runs; it queues them for review and calibrates.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/15-aria-triage-recalibrate-loop/
README.md) and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001      # the server root, not the /ajax-api path
    export CALIBER_TOKEN=calpat_...                    # an ADMIN personal access token
    export MLFLOW_TRACKING_URI=http://127.0.0.1:5000   # where CALIBER's traces live
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_15_aria_triage_recalibrate_loop

The server needs ``CALIBER_FEATURE_ARIA=true``, ``CALIBER_OPERATOR_USERS=admin,olga``, and its
background workers on (the plan worker resumes the parked plan, the refinement worker runs the
calibration); the script also creates ``vera``, who is not an operator. Nothing here needs a model:
Aria's planner is deterministic and the calibration replays the test set through a simulated
executor. That is also why the calibration finds nothing to apply on a simulated deployment (see
below).

What it does, in order:

1. Registers the two tools, imports and publishes the support workflow with its test set as the
   deploy gate, and registers an agent: the *existing* things Aria remediates (it creates none of
   them). Writes the 12 weak runs of ``assets/dataset/support-runs.jsonl`` to MLflow as traces, and
   draws a plan for each of the 28 goals in ``assets/dataset/goals.jsonl`` (nothing runs).
2. States the canonical intent and answers six questions: the queue's fields, an Approve, the 12
   trace ids, an Approve, the workflow and agent ids, an Approve. The last Approve enqueues a real
   calibration job and the plan *parks*: ``running``, the step ``waiting_job``. It then waits for
   the plan worker to resume it (falling back to ``POST /aria/plans/{id}/poll``) and checks that the
   plan ends where the job ended.
3. Has a reviewer triage three of the flagged runs, with feedback and an expectation.
4. Provokes the refusals: a step that fails at once (an unknown workflow, an unregistered agent, a
   workflow with no test set), a denied calibration, a parked plan that cannot be edited, a plan
   with no steps, an account without the operator scope.

On a simulated deployment the calibration job ends ``rejected``: the fake executor gives a candidate
the same outputs as the baseline, so the quality gain is 0.000, below the 0.02 it must clear, and
the plan ends ``failed`` with that reason. On a stack with a real model the job can reach
``candidate_ready`` and the plan completes; the script accepts either, and asserts that the plan
mirrors the job.

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
    check_chat_boundary,
    check_goals_report,
    ensure_accounts,
    env_password,
    form_values,
    http_timeout,
    is_real,
    load_json,
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

NAME_PREFIX = "cookbook-15-"
DEFAULT_PASSWORD = "cookbook-aria-lab-passphrase"
OPERATOR_USER = "olga"
VIEWER_USER = "vera"
INTENT = (
    "Our workflow's recent runs look weak - set up a review queue for the flagged traces and "
    "kick off a workflow calibration."
)
GOALS_EXPECTED = 28
RUNS_EXPECTED = 30
WEAK_EXPECTED = 12
TRIAGED = ("S19", "S21", "S24")  # the flagged runs a reviewer triages by hand
#: How long the plan worker gets to resume the parked plan before the script calls ``poll``.
WORKER_PATIENCE_SECONDS = 75.0
#: The same on a stack with a real model, where the calibration job takes minutes.
REAL_PATIENCE_SECONDS = 1800.0
TERMINAL = {"completed", "failed", "cancelled"}


def default_assets_dir() -> Path:
    return Path(
        os.environ.get(
            "COOKBOOK_15_ASSETS",
            Path(__file__).resolve().parents[4]
            / "docs-site/cookbooks/15-aria-triage-recalibrate-loop/assets",
        )
    )


def seed_traces(assets: Path) -> dict[str, str]:
    """Write the 12 weak runs as traces (needs ``mlflow`` and ``MLFLOW_TRACKING_URI``)."""
    spec = importlib.util.spec_from_file_location(
        "cookbook15_seed_traces", assets / "seed_traces.py"
    )
    if spec is None or spec.loader is None:
        raise SetupError(f"{assets / 'seed_traces.py'} not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.seed(assets / "dataset" / "support-runs.jsonl"))


def _reason(error: CaliberAPIError | None) -> str:
    return str(error).split("] ", 1)[1].split(" (", 1)[0] if error else ""


def load_subject_setup(assets: Path) -> Any:
    """The lab's ``assets/setup_subject.py`` as a module (registers the tools, imports the workflow
    and its test set, registers an agent)."""
    spec = importlib.util.spec_from_file_location(
        "cookbook15_setup_subject", assets / "setup_subject.py"
    )
    if spec is None or spec.loader is None:
        raise SetupError(f"{assets / 'setup_subject.py'} not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_for_resume(
    caliber: CaliberClient,
    plan_id: str,
    *,
    patience: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float] = time.monotonic,
) -> tuple[Any, str]:
    """Wait for a parked plan to reach a terminal state.

    Gives the plan worker ``patience`` seconds to do it on its own; after that the script advances
    the plan itself with ``POST /aria/plans/{id}/poll``, which is the same thing on demand. Returns
    the plan and who advanced it (``"worker"`` or ``"poll"``).
    """
    started = clock()
    detail = caliber.aria.get_plan(plan_id)
    while detail.plan.status not in TERMINAL:
        if clock() - started > patience:
            for _ in range(60):
                detail = caliber.aria.poll_plan(plan_id)
                if detail.plan.status in TERMINAL:
                    return detail, "poll"
                sleep(2)
            return detail, "poll"
        sleep(3)
        detail = caliber.aria.get_plan(plan_id)
    return detail, "worker"


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
    patience: float | None = None,
) -> dict[str, Any]:
    """Run the whole lab. ``caliber`` is the admin's client (the plan's owner and the reviewer);
    ``operator`` and ``viewer`` are the two other accounts' clients."""
    assets = assets or default_assets_dir()
    suffix = suffix or time.strftime("%Y%m%d-%H%M%S")
    check = Checks(report)
    result: dict[str, Any] = {"suffix": suffix}
    goals = load_jsonl(assets / "dataset/goals.jsonl")
    runs = load_jsonl(assets / "dataset/support-runs.jsonl")
    weak = [row for row in runs if row["weak"]]
    queue_spec = form_values(assets / "review-queues/triage-review.queue.json")
    queue_form = {**queue_spec, "name": f"{NAME_PREFIX}{queue_spec['name']}-{suffix}"}

    admin_id = require_admin_and_aria(caliber)
    real = is_real(caliber)
    result["mode"] = "real" if real else "simulated"
    if patience is None:
        # A simulated calibration is over in seconds; a real one replays the test set through a
        # model (about 80 workflow runs, so minutes). The worker resumes the plan either way.
        patience = float(
            os.environ.get(
                "COOKBOOK_15_CALIBRATION_TIMEOUT",
                REAL_PATIENCE_SECONDS if real else WORKER_PATIENCE_SECONDS,
            )
        )
    require_scopes(
        {"olga": operator, "vera": viewer},
        has={"olga": True, "vera": False},
        hint=(
            "start CALIBER with CALIBER_OPERATOR_USERS=admin,olga (olga is an operator who does "
            "not own the plan, vera must not be one)"
        ),
    )

    # -- 1. The things Aria remediates (it creates none of them) ------------------------------
    subject = load_subject_setup(assets).setup(caliber, suffix, assets)
    workflow_id, agent_id = subject["workflow_id"], subject["agent_id"]
    gate_name, gate_id = subject["gate_name"], subject["dataset_id"]
    stored = caliber.workflows.versions.get(subject["version_id"]).manifest
    gate_ref = next(iter(stored["deploy_gates"].values()))["dataset_ref"]
    check(
        "the two tools are registered, the workflow is imported and published with the 30-case "
        "test set as its deploy gate, and an agent is registered",
        subject["tools"] == ["get_order", "lookup_policy"]
        and caliber.workflows.versions.get(subject["version_id"]).status == "published"
        and stored["artifacts"]["eval_datasets"][gate_ref]["dataset_name"] == gate_name
        and len(caliber.eval_datasets.examples(gate_id)) == RUNS_EXPECTED,
        f"{workflow_id}, gate '{gate_name}'",
    )
    result.update(workflow_id=workflow_id, agent_id=agent_id)

    trace_of = traces if traces is not None else seed_traces(assets)
    check(
        "the recorded runs are consistent: a run is weak exactly when it skipped a tool its "
        "question needs",
        len(runs) == RUNS_EXPECTED
        and len(weak) == WEAK_EXPECTED
        and all(
            row["weak"] == (not set(row["required_tools"]) <= set(row["tools_called"]))
            for row in runs
        ),
        f"{len(weak)} weak of {len(runs)}",
    )
    check(
        "the 12 weak runs are in MLflow as traces, one per run",
        set(trace_of) == {row["id"] for row in weak}
        and len(set(trace_of.values())) == WEAK_EXPECTED,
        f"{len(trace_of)} traces",
    )
    check_goals_report(caliber, goals, GOALS_EXPECTED, check)

    # -- 2. The canonical intent: forms, approvals, and a plan that parks on a job ------------
    drawn = caliber.aria.create_plan(INTENT, autonomy="ask_each")
    plan_id = drawn.plan.plan_id
    create_step = drawn.steps[0]
    check(
        "the intent draws review_queue.create, review_queue.add_items, workflow.calibrate",
        step_keys(drawn) == ["review_queue.create", "review_queue.add_items", "workflow.calibrate"],
        plan_id,
    )
    check(
        "only the enqueue step's queue_id is wired (from the create step); the calibrate step "
        "depends on nothing",
        [s.extra.get("inputs") for s in drawn.steps]
        == [{}, {"queue_id": {"$from_step": create_step.step_id, "path": "queue_id"}}, {}]
        and drawn.steps[2].depends_on == [],
    )
    caliber.aria.approve_plan(plan_id)
    caliber.aria.execute_plan(plan_id)
    flagged_ids = [trace_of[row["id"]] for row in weak]
    _, first_trail = walk(
        caliber,
        plan_id,
        [
            ("input", queue_form),
            ("permission", "approve"),
            ("input", {"trace_ids": flagged_ids, "assigned_to": admin_id}),
            ("permission", "approve"),
            (
                "input",
                {"workflow_id": workflow_id, "agent_id": agent_id},
            ),
        ],
    )
    parked_before = caliber.aria.get_plan(plan_id)
    check(
        "before the last approval the plan has made the queue and queued the 12 traces, and the "
        "calibration has not started",
        step_status(parked_before)
        == {
            "review_queue.create": "done",
            "review_queue.add_items": "done",
            "workflow.calibrate": "waiting_input",
        }
        and parked_before.steps[1].result["added"] == WEAK_EXPECTED,
        parked_before.steps[1].result,
    )
    jobs_before = {job.job_id for job in caliber.jobs.list()}
    parked, _ = walk(caliber, plan_id, [("permission", "approve")])
    calibrate = parked.steps[2]
    check(
        "approving the calibration enqueues a job and PARKS the plan: the plan stays 'running' and "
        "the step reads 'waiting_job' with the job's id",
        parked.plan.status == "running"
        and calibrate.status == "waiting_job"
        and str(calibrate.job_id).startswith("RFN-")
        and not pending(caliber, plan_id)
        and calibrate.job_id not in jobs_before,
        f"{parked.plan.status}; {calibrate.status}; {calibrate.job_id}",
    )
    edit_parked = refused(lambda: caliber.aria.update_plan(plan_id, status="cancelled"))
    check(
        "a parked plan cannot be cancelled or edited (409): only drafts can",
        edit_parked is not None and edit_parked.status_code == 409,
        _reason(edit_parked),
    )
    check(
        "the plan asked six questions in all: a form and an approval for each step",
        [kind for kind, _ in first_trail] + ["permission"]
        == ["input", "permission", "input", "permission", "input", "permission"],
    )

    final, advanced_by = wait_for_resume(caliber, plan_id, patience=patience, sleep=sleep)
    job = caliber.jobs.get(str(calibrate.job_id))
    final_step = final.steps[2]
    if job.status in {"candidate_ready", "applied"}:
        outcome_ok = final.plan.status == "completed" and final_step.status == "done"
        outcome = "the calibration found a candidate: the plan completed"
    else:
        outcome_ok = (
            job.status == "rejected"
            and final.plan.status == "failed"
            and final_step.status == "failed"
            and str(final_step.error) == f"rejected: {job.error_message}"
            and "no candidate met calibration gates" in str(job.error_message)
        )
        outcome = "the calibration was rejected: the plan failed with the job's reason"
    check(
        f"the plan resumed by itself ({advanced_by}) and ends where the job ended: {outcome}",
        outcome_ok,
        f"job {job.status}; plan {final.plan.status}; step: {str(final_step.error)[:110]}",
    )
    again = caliber.aria.poll_plan(plan_id)
    check(
        "polling a finished plan changes nothing",
        again.plan.status == final.plan.status and step_status(again) == step_status(final),
    )
    evaluation = job.eval_results or {}
    check(
        "the job replayed 20 of the gate's 30 cases (the default budget) and reports its baseline",
        bool(evaluation.get("calibration"))
        and evaluation.get("n_examples") == 20
        and "baseline" in evaluation,
        f"{evaluation.get('n_examples')} examples; "
        f"gate: {(evaluation.get('gate') or {}).get('reasons')}",
    )
    queue_id = final.steps[0].result["queue_id"]
    detail = caliber.raw.get(f"/review-queues/{queue_id}")
    queue, items = detail["queue"], detail["items"]
    check(
        "the review queue exists with its four triage questions and the 12 flagged traces "
        "pending, assigned to the reviewer",
        queue["name"] == queue_form["name"]
        and [q["key"] for q in queue["questions"]]
        == ["resolution_correct", "failure_mode", "severity", "expected_answer"]
        and queue["item_count"] == WEAK_EXPECTED
        and {i["trace_id"] for i in items} == set(flagged_ids)
        and all(i["status"] == "pending" and i["assigned_to"] == admin_id for i in items),
        f"{queue_id}: {queue['item_count']} items",
    )
    result.update(
        plan_id=plan_id,
        queue_id=queue_id,
        calibration={
            "job_id": str(calibrate.job_id),
            "job_status": job.status,
            "plan_status": final.plan.status,
            "advanced_by": advanced_by,
        },
    )

    # -- 3. A reviewer triages three of the flagged runs --------------------------------------
    item_of = {item["trace_id"]: item["item_id"] for item in items}
    modes = {r["id"]: r["reason"].split(":", 1)[0] for r in weak}
    for row_id in TRIAGED:
        row = next(r for r in weak if r["id"] == row_id)
        caliber.review_queues.submit(
            queue_id,
            item_of[trace_of[row_id]],
            resolution_correct=False,
            failure_mode=modes[row_id],
            severity=4 if modes[row_id] == "hallucinated_claim" else 3,
            expected_answer=row["reference"],
        )
    stored = {
        row_id: {
            a["name"]: a["value"]
            for a in caliber.observability.trace(trace_of[row_id])["assessments"]
        }
        for row_id in TRIAGED
    }
    check(
        "three triaged runs carry the reviewer's verdict, failure mode and the expected answer "
        "on their traces",
        all(
            stored[row_id].get("resolution_correct") is False
            and stored[row_id].get("failure_mode") == modes[row_id]
            and stored[row_id].get("expected_answer")
            == next(r for r in weak if r["id"] == row_id)["reference"]
            for row_id in TRIAGED
        ),
        {row_id: modes[row_id] for row_id in TRIAGED},
    )
    after = caliber.raw.get(f"/review-queues/{queue_id}")["queue"]
    check(
        "the queue shows 3 of 12 reviewed",
        after["item_count"] == WEAK_EXPECTED and after["pending_count"] == WEAK_EXPECTED - 3,
        f"{WEAK_EXPECTED - after['pending_count']}/{after['item_count']} reviewed",
    )
    result["triage"] = {"reviewed": 3, "pending": WEAK_EXPECTED - 3}

    # -- 4. Refusals --------------------------------------------------------------------------
    calibrate_only = "Calibrate the support workflow."

    def failed_calibration(workflow_id: str, agent: str) -> Any:
        detail, _ = run_plan(
            caliber,
            calibrate_only,
            autonomy="approve_plan",
            moves=[("input", {"workflow_id": workflow_id, "agent_id": agent})],
        )
        return detail

    unknown_workflow = failed_calibration("WF-does-not-exist", agent_id)
    check(
        "an unknown workflow fails the step at once (404), without parking",
        unknown_workflow.plan.status == "failed"
        and unknown_workflow.steps[0].status == "failed"
        and "not found" in str(unknown_workflow.steps[0].error)
        and unknown_workflow.steps[0].job_id is None,
        unknown_workflow.steps[0].error,
    )
    unregistered = failed_calibration(workflow_id, "agent-that-is-not-registered")
    check(
        "an agent that is not registered fails the step at once (400)",
        unregistered.plan.status == "failed"
        and "is not registered" in str(unregistered.steps[0].error),
        unregistered.steps[0].error,
    )
    no_gate = dict(load_json(assets / "workflows/support-policy-agent.workflow.json"))
    no_gate["deploy_gates"] = {}
    no_gate["artifacts"] = {**no_gate["artifacts"], "eval_datasets": {}}
    ungated = caliber.workflows.import_workflow(
        manifest=no_gate, name=f"{NAME_PREFIX}no-gate-{suffix}"
    )
    caliber.workflows.versions.publish(
        caliber.workflows.versions.list(ungated.workflow_id)[0].version_id
    )
    no_dataset = failed_calibration(ungated.workflow_id, agent_id)
    check(
        "a workflow with no deploy-gate test set fails the step at once (400): calibration needs "
        "one",
        no_dataset.plan.status == "failed"
        and "deploy-gate eval dataset" in str(no_dataset.steps[0].error),
        no_dataset.steps[0].error,
    )
    calibration_jobs = {j.job_id for j in caliber.jobs.list() if j.agent_id == agent_id}
    check(
        "none of the three refusals queued a job",
        calibration_jobs == {str(calibrate.job_id)},
        len(calibration_jobs),
    )

    # A form the schema refuses stays open; a denied calibration queues nothing.
    first = start(caliber, calibrate_only, autonomy="ask_each")
    form = pending(caliber, first.plan.plan_id)[0]
    bad = {
        "an empty workflow id": {"workflow_id": "", "agent_id": agent_id},
        "a missing agent id": {"workflow_id": workflow_id},
        "an unknown field": {"workflow_id": "WF-1", "agent_id": "a", "objective": "quality"},
    }
    form_refusals = {
        label: refused(partial(caliber.aria.answer, form.interaction_id, inputs=inputs))
        for label, inputs in bad.items()
    }
    check(
        "the form refuses an empty workflow id, a missing agent id and a field it does not have "
        "(409) and stays open",
        all(e is not None and e.status_code == 409 for e in form_refusals.values())
        and [i.status for i in caliber.aria.interactions(first.plan.plan_id)] == ["pending"],
        {label: _reason(e) for label, e in form_refusals.items()},
    )
    denied, _ = walk(
        caliber,
        first.plan.plan_id,
        [
            ("input", {"workflow_id": workflow_id, "agent_id": agent_id}),
            ("permission", "deny"),
        ],
    )
    check(
        "denying the calibration skips the step and queues no job; the plan completes",
        denied.plan.status == "completed"
        and step_status(denied) == {"workflow.calibrate": "skipped"}
        and {j.job_id for j in caliber.jobs.list() if j.agent_id == agent_id}
        == {str(calibrate.job_id)},
    )

    # Skip the queue: its enqueue goes with it, the calibration still asks.
    skipped, trail = walk(
        caliber,
        start(caliber, INTENT, autonomy="approve_plan").plan.plan_id,
        [("input", "skip"), ("input", "skip")],
    )
    check(
        "skipping the queue skips the enqueue step with it (never asked); the calibration's form "
        "is still asked, and skipping it ends the plan",
        skipped.plan.status == "completed"
        and set(step_status(skipped).values()) == {"skipped"}
        and [capability for _, capability in trail] == ["review_queue.create", "workflow.calibrate"]
        and "step 1 (review_queue.create) was skipped" in str(skipped.steps[1].error),
        step_status(skipped),
    )

    nothing = caliber.aria.create_plan("Tune the pipeline and get a human to look at the bad runs.")
    approve_empty = refused(lambda: caliber.aria.approve_plan(nothing.plan.plan_id))
    check(
        "a goal with no domain word is created with a warning and refused at approval (409)",
        nothing.plan.step_count == 0
        and bool(nothing.extra.get("warnings"))
        and approve_empty is not None
        and approve_empty.status_code == 409,
    )
    caliber.aria.update_plan(nothing.plan.plan_id, status="cancelled")

    by_viewer = start(viewer, calibrate_only, autonomy="approve_plan")
    check(
        "a plan owned by an account without the operator scope fails at execute and queues nothing",
        by_viewer.plan.status == "failed"
        and by_viewer.steps[0].error == "plan owner lacks required scope(s): operator",
        by_viewer.steps[0].error,
    )
    own = start(caliber, calibrate_only, autonomy="approve_plan")
    waiting = pending(caliber, own.plan.plan_id)[0]
    other_answer = refused(lambda: operator.aria.answer(waiting.interaction_id, approved=False))
    other_poll = refused(lambda: operator.aria.poll_plan(own.plan.plan_id))
    check(
        "an operator who does not own the plan cannot answer its question (403) or poll it (404)",
        isinstance(other_answer, CaliberPermissionError)
        and other_poll is not None
        and other_poll.status_code == 404,
        _reason(other_answer),
    )
    walk(caliber, own.plan.plan_id, [("input", "skip")])

    # -- 5. Ask Aria's chat, on a real stack ------------------------------------------------------
    if real:
        result["chat"] = _chat_for_real(caliber, check, suffix)

    check.finish()
    return result


def _chat_for_real(caliber: CaliberClient, check: Checks, suffix: str) -> dict[str, Any]:
    """Ask Aria (the chat, not the Plans page) acts in build / auto_all and only reads otherwise."""
    acting = f"{NAME_PREFIX}chat-queue-{suffix}"
    reading = f"{NAME_PREFIX}chat-manual-{suffix}"
    spec = (
        "with one pass_fail question: key resolved, title 'Did the agent resolve the request?'. "
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
        and [q["key"] for q in caliber.review_queues.get(made.queue_id).questions] == ["resolved"],
        made.queue_id if made else "",
    )
    return summary


def cleanup(caliber: CaliberClient) -> dict[str, int]:
    """Archive what earlier runs created (names start ``cookbook-15-``)."""
    archived = {"review_queues": 0, "test_sets": 0, "workflows": 0}
    for queue in caliber.review_queues.list():
        if queue.name.startswith(NAME_PREFIX):
            caliber.review_queues.update(queue.queue_id, status="archived")
            archived["review_queues"] += 1
    for dataset in caliber.eval_datasets.list():
        if dataset.name.startswith(NAME_PREFIX) and dataset.status != "archived":
            caliber.eval_datasets.update(dataset.dataset_id, status="archived")
            archived["test_sets"] += 1
    for workflow in caliber.workflows.list():
        if workflow.name.startswith(NAME_PREFIX) and workflow.status != "archived":
            caliber.workflows.update(workflow.workflow_id, status="archived")
            archived["workflows"] += 1
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
