"""Cookbook 08: an incident commander workflow, gated, queued and approved, for real.

This is the executable form of ``docs-site/cookbooks/08-incident-response-commander/README.md``.
It drives a real CALIBER server and exits 0 only when every documented outcome was verified:

1. install the Cookbooks starter (``08``) and run it through the queue; Preview refuses it because
   it holds API Request nodes (they cannot be isolated);
2. import ``assets/dataset/incident-cases.jsonl`` (41 incidents) as a test set, then import the lab
   workflow (``assets/workflows/incident-commander.workflow.json``). The workflow names that test
   set in its deploy gate, so the import is refused until the test set exists;
3. validate it and Preview it: every node is declarative, so Preview runs it, and the dataset's
   verdicts hold in Preview as well (severity and action for every incident);
4. publish it and move the aliases: ``staging`` rotates at once; ``prod`` replays the dataset
   through the deploy gate (the executor is the simulated one, and the gate says so), then waits
   for a second person to approve the promotion;
5. publish a version with a bad rule and watch the gate refuse it (``prod`` does not move);
6. run every incident through the queue against ``prod``: check severity, action and path, and
   answer the rollback approval gate (self-approval 403, approve, reject, cancel the rest);
7. the failure drills: an Error Boundary turns a failing lookup into a fallback, a workflow
   naming an unregistered tool is refused at import, an API Request that reaches a blocked host
   fails the run, and a Wait For Event run pauses until its event arrives (its checkpoint is
   readable);
8. publish a good version, promote it, and roll ``prod`` back; ask for a workflow calibration and
   read its outcome; and confirm the Evaluations route refuses workflow scoring without a model.

Two modes, detected from ``GET /readiness`` (``simulated`` empty means a real provider):

* Simulated stack (the default): the deploy gate names the fake executor, the workflow calibration
  reports a ``simulated`` baseline, and Evaluations refuses workflow scoring (400).
* Real provider (``scripts/cookbook-env.sh up NAME --flags cookbooks --real-llm``): the gate replays
  the incidents through a real model (and says so), the calibration baseline is measured and not
  marked simulated, and Evaluations scores the workflow for real with a deterministic scorer and a
  model-graded judge. ``--model-variant`` also imports the optional model-based variant
  (``incident-commander-model.workflow.json``), where an agent proposes a severity and action and
  the decision tables still decide. The script asserts invariants (markers, counts, "the model
  cannot authorise a rollback"), never generated text or an exact model score.

Environment (see the lab README): ``CALIBER_BASE_URL`` (the server root without the API path) and
``CALIBER_TOKEN`` (an ADMIN personal access token). The server must have been started as the README
shows (``CALIBER_OPERATOR_USERS``/``CALIBER_APPROVER_USERS`` naming ``lena``, release policy for
``production``). Optional: ``COOKBOOK_08_ASSETS``, ``COOKBOOK_08_PASSWORD``.

    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_08_incident_response_copilot
    ... --cleanup      # archive what earlier runs created
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

from caliber_sdk import CaliberClient, wait_for
from caliber_sdk.errors import CaliberAPIError, CaliberPermissionError
from examples.cookbooks._helpers import PAUSED_OR_TERMINAL_RUN_STATES, env_client
from examples.cookbooks.cookbook_07_support_triage_copilot import (
    APPROVER_USER,
    MEMBER_USER,
    ensure_accounts,
    is_real_stack,
    refused,
    resubmit_transient,
    sign_in,
)
from examples.cookbooks.cookbook_07_support_triage_copilot import (
    LabVerificationError as LabVerificationError,  # re-exported: the lab's test imports it here
)
from examples.cookbooks.cookbook_07_support_triage_copilot import (
    SetupError as SetupError,  # re-exported: the lab's test imports it here
)

NAME_PREFIX = "cookbook-08-"
DEFAULT_PASSWORD = "cookbook08-lab-passphrase"
GATE_DATASET_REF = "incident-cases"
APPROVAL_NODE = "review"
RELEASED_TEXT = "ROLLBACK_APPROVED"
#: The node each action ends at when no approval is needed.
TERMINAL_NODE = {
    "escalate_human": "handoff_note",
    "gather_more_evidence": "handoff_note",
    "disable_feature_flag": "recommendation",
    "scale_out": "recommendation",
    "monitor": "recommendation",
}
#: The regression the gate must catch: rollbacks that need the error rate to reach 50%.
BAD_RULE = ('"value": 0.05}]', '"value": 0.5}]')

#: The optional model-based variant of the workflow (real provider only).
VARIANT_FILE = "workflows/incident-commander-model.workflow.json"
JUDGE_FILE = "judges/stakeholder-update-honesty.judge.json"
#: Incidents the variant is run over (Preview): one or two per action, plus the vague and the
#: conflicting-evidence cases where a model is most likely to disagree with the tables.
VARIANT_INCIDENTS = ("IN01", "IN02", "IN03", "IN04", "IN05", "IN07", "IN09", "IN12", "IN36", "IN38")
#: How many incidents a real Evaluations run scores (the route caps a workflow run at 20).
EVAL_MAX_EXAMPLES = 8

#: Seconds a single request may take. A real provider answers a gate replay or an evaluation inside
#: one request, which takes minutes (the SDK default is 30 s).
REQUEST_TIMEOUT = 900.0

Report = Callable[[str], None]


def default_assets_dir() -> Path:
    override = os.environ.get("COOKBOOK_08_ASSETS")
    if override:
        return Path(override)
    return (
        Path(__file__).resolve().parents[4]
        / "docs-site/cookbooks/08-incident-response-commander/assets"
    )


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for row in rows:
        if not {"id", "input", "expected"} <= set(row):
            raise LabVerificationError(f"{path.name}: row {row.get('id')} lacks id/input/expected")
    return rows


def load_json(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text(encoding="utf-8")))


def summary_of(run: Any) -> dict[str, Any]:
    return dict((run.extra or {}).get("summary") or {})


def node_path(run: Any) -> list[str]:
    return list(summary_of(run).get("node_path") or [])


def verdict_of(run: Any) -> dict[str, Any] | None:
    """The verdict the ``summarize`` node produced, from the run's own step trace."""
    for step in summary_of(run).get("steps") or []:
        if step.get("node_id") == "summarize":
            result = (step.get("output_by_port") or {}).get("result")
            return dict(result) if isinstance(result, dict) else None
    return None


def run_output(run: Any) -> str:
    return str(summary_of(run).get("output") or "")


def terminal_node(action: str) -> str:
    return APPROVAL_NODE if action == "rollback" else TERMINAL_NODE[action]


def problems_for(case: dict[str, Any], run: Any) -> list[str]:
    expected = case["expected"]
    verdict = verdict_of(run)
    if verdict is None:
        return [f"no verdict (status {run.status}, error {run.error})"]
    found = [
        f"{key}: expected {expected[key]!r}, got {verdict.get(key)!r}"
        for key in ("severity", "action")
        if verdict.get(key) != expected[key]
    ]
    path = node_path(run)
    if expected["requires_approval"]:
        if run.status != "waiting_approval" or not path or path[-1] != APPROVAL_NODE:
            found.append(f"expected a pause at {APPROVAL_NODE!r}, got {run.status} via {path}")
    else:
        terminal = terminal_node(expected["action"])
        if run.status != "completed" or terminal not in path or APPROVAL_NODE in path:
            found.append(f"expected completion via {terminal!r}, got {run.status} via {path}")
    return found


def wait_settled(caliber: CaliberClient, run_id: str, *, timeout: float = 240.0) -> Any:
    return wait_for(
        lambda: caliber.workflows.runs.get(run_id),
        is_done=lambda item: item.status in PAUSED_OR_TERMINAL_RUN_STATES,
        interval=0.5,
        max_interval=2.0,
        timeout=timeout,
    )


def submit_cases(
    caliber: CaliberClient,
    cases: list[dict[str, Any]],
    *,
    workflow_id: str,
    alias: str,
    key: str,
    retried: list[str] | None = None,
) -> dict[str, Any]:
    inputs = {case["id"]: json.dumps(case["input"]) for case in cases}
    queued = {
        case["id"]: caliber.workflows.runs.submit(
            workflow_id=workflow_id,
            alias=alias,
            input=inputs[case["id"]],
            idempotency_key=f"{key}-{case['id']}",
        )
        for case in cases
    }
    runs = {case_id: wait_settled(caliber, run.workflow_run_id) for case_id, run in queued.items()}
    # A real provider can drop a connection or push back: resubmit those runs, only those.
    return resubmit_transient(
        caliber, runs, inputs, workflow_id=workflow_id, alias=alias, key=key, retried=retried
    )


def grade(cases: list[dict[str, Any]], runs: dict[str, Any]) -> dict[str, list[str]]:
    return {case["id"]: found for case in cases if (found := problems_for(case, runs[case["id"]]))}


def run(
    caliber: CaliberClient,
    *,
    project_client: Callable[[str, str], CaliberClient],
    suffix: str | None = None,
    assets: Path | None = None,
    report: Report = print,
    model_variant: bool = False,
) -> dict[str, Any]:
    """Run the whole lab. ``project_client(user_id, project_id)`` returns that person's client.

    ``model_variant`` adds the optional model-based variant after the main lab (real provider only).
    """
    assets = assets or default_assets_dir()
    suffix = suffix or time.strftime("%Y%m%d-%H%M%S")
    cases = load_cases(assets / "dataset/incident-cases.jsonl")
    manifest = load_json(assets / "workflows/incident-commander.workflow.json")
    failures: list[str] = []
    retried: list[str] = []
    result: dict[str, Any] = {"suffix": suffix}

    def check(label: str, ok: bool, detail: Any = "") -> None:
        report(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail != "" else ""))
        if not ok:
            failures.append(label)

    me = caliber.me.get()
    if not me.is_admin:
        raise SetupError("CALIBER_TOKEN must be an admin personal access token")
    if "08" not in {recipe.id for recipe in caliber.cookbooks.list()}:
        raise SetupError("Cookbooks are off: start the server with CALIBER_FEATURE_COOKBOOKS=true")
    real = is_real_stack(caliber)
    result["mode"] = "real" if real else "simulated"
    if model_variant and not real:
        raise SetupError("--model-variant needs a real provider (start with --real-llm)")

    # -- 0. The project and the people ------------------------------------------------------
    project = caliber.projects.create(
        f"{NAME_PREFIX}incident-ops-{suffix}", description="Cookbook 08 incident response"
    )
    for user in (APPROVER_USER, MEMBER_USER):
        caliber.projects.add_member(project.project_id, user, role="reviewer")
    admin = project_client(me.user_id, project.project_id)
    approver = project_client(APPROVER_USER, project.project_id)
    scopes = set(approver.me.get().scopes)
    if "caliber.approver" not in scopes:
        raise SetupError(
            "start CALIBER with CALIBER_OPERATOR_USERS=admin,lena,sam and "
            "CALIBER_APPROVER_USERS=admin,lena (lena approves)"
        )
    result["project_id"] = project.project_id

    # -- 1. The starter: it runs, Preview refuses it ----------------------------------------
    installed = admin.cookbooks.install(
        "08", name=f"{NAME_PREFIX}starter-{suffix}", acknowledge_prerequisites=True
    )
    admin.workflows.update(installed["workflow"]["workflow_id"], status="active")
    starter = admin.workflows.versions.publish(installed["version"]["version_id"])
    queued = admin.workflows.runs.submit(
        workflow_version_id=starter.version_id,
        input="gateway 5xx after deploy a1b9f3c",
        idempotency_key=f"{suffix}-starter",
    )
    starter_run = wait_settled(admin, queued.workflow_run_id)
    check(
        "the installed starter runs through the queue",
        starter_run.status == "completed",
        starter_run.workflow_run_id,
    )
    refusal = admin.workflows.versions.preview_run(starter.version_id, input="x")
    check(
        "Preview refuses the starter (its API Request nodes cannot be isolated)",
        refusal["status"] == "error" and "(api_request)" in refusal["error"],
        refusal["error"][-70:],
    )

    # -- 2. The test set first, then the workflow that names it ------------------------------
    dataset_name = f"{NAME_PREFIX}incident-cases-{suffix}"
    manifest["name"] = f"{NAME_PREFIX}incident-commander-{suffix}"
    manifest["artifacts"]["eval_datasets"][GATE_DATASET_REF]["dataset_name"] = dataset_name
    early = admin.workflows.preview_import(manifest=manifest)
    codes = {error["code"] for error in early["validation"]["errors"]}
    check(
        "the workflow cannot be imported before its test set exists",
        not early["ready_to_import"] and "unresolved_import_dependency" in codes,
        sorted(codes),
    )
    dataset = admin.eval_datasets.create(
        dataset_name, owner=me.user_id, description="Cookbook 08 incidents"
    )
    for row in cases:
        admin.eval_datasets.add_example(
            dataset.dataset_id, input=row["input"], expected=row["expected"], tags=row["tags"]
        )
    stored = admin.eval_datasets.examples(dataset.dataset_id)
    canonical = Counter(json.dumps(row["input"], sort_keys=True) for row in cases)
    returned = Counter(json.dumps(row.input, sort_keys=True) for row in stored)
    check(
        "the test set holds every incident, unchanged",
        len(stored) == len(cases) and canonical == returned,
        f"{len(stored)} rows",
    )
    imported = admin.workflows.import_workflow(manifest=manifest)
    workflow_id = imported.workflow_id
    v1 = admin.workflows.versions.list(workflow_id)[0]
    validation = admin.workflows.versions.validate(v1.version_id)
    check("the imported manifest validates", validation["valid"] and not validation["errors"])
    result["workflow_id"] = workflow_id

    # -- 3. Preview works: every node is declarative ----------------------------------------
    previews = {
        case["id"]: admin.workflows.versions.preview_run(
            v1.version_id, input=json.dumps(case["input"])
        )
        for case in cases[:6]
    }
    ok_previews = [
        case_id
        for case_id, preview in previews.items()
        if preview["status"] == "completed" and "Verdict: " in preview["output"]
    ]
    check("Preview runs the workflow (no node needs isolating)", len(ok_previews) == len(previews))

    # -- 4. Publish; staging rotates, prod is gated and approved ----------------------------
    v1 = admin.workflows.versions.publish(v1.version_id)

    def deployed(alias: str) -> str | None:
        rows = {row["alias"]: row["version_id"] for row in admin.workflows.deployments(workflow_id)}
        return rows.get(alias)

    def promote(version_id: str, alias: str = "prod") -> dict[str, Any]:
        try:
            moved = admin.workflows.promote_deployment(workflow_id, alias, version_id=version_id)
        except CaliberAPIError as exc:
            if "graded" in str(exc):
                raise SetupError(
                    "the server wants a real model for production gates: start it with "
                    "CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES= (empty), "
                    "as the README shows"
                ) from exc
            raise
        if not moved["rotated"]:
            pending = moved["promotion"]
            approver.workflows.promotions.approve(pending["promotion_id"], reason="Gate passed.")
        return dict(moved)

    admin.workflows.promote_deployment(workflow_id, "staging", version_id=v1.version_id)
    check("staging rotates at once (it is not gated)", deployed("staging") == v1.version_id)
    moved = promote(v1.version_id)
    gate = moved["gate"]
    gate_run = gate["runs"][0]
    check(
        "the prod gate replayed every incident and passed",
        gate["has_gate"]
        and gate["passed"]
        and gate_run["n_examples"] == len(cases)
        and gate_run["pass_rate"] == 1.0,
        f"{gate_run['n_examples']} replays, pass rate {gate_run['pass_rate']}",
    )
    executor = gate["executor"]
    if real:
        check(
            "the gate says its executor is a real model, not the simulated one",
            executor["provider"] != "fake" and executor["deterministic"] is False,
            executor,
        )
    else:
        check(
            "the gate says its executor is the simulated one",
            executor["provider"] == "fake" and executor["deterministic"] is True,
            executor,
        )
    check(
        "prod waits for a second person, then serves version 1",
        moved["rotated"] is False and deployed("prod") == v1.version_id,
        "promotion approved by " + APPROVER_USER,
    )

    # -- 5. A bad version: the gate refuses it ----------------------------------------------
    # Derive new versions from the stored manifest: it carries the workflow's real id.
    stored_manifest = admin.workflows.versions.get(v1.version_id).manifest
    bad = json.loads(json.dumps(stored_manifest))
    bad_text = json.dumps(bad["nodes"]["action_table"])
    if BAD_RULE[0] not in bad_text:
        raise LabVerificationError("the action table no longer holds the rule the lab breaks")
    bad["nodes"]["action_table"] = json.loads(bad_text.replace(*BAD_RULE, 1))
    v2 = admin.workflows.versions.create(workflow_id, bad)
    v2 = admin.workflows.versions.publish(v2.version_id)
    blocked = refused(
        lambda: admin.workflows.promote_deployment(workflow_id, "prod", version_id=v2.version_id)
    )
    body = (blocked.payload or {}) if blocked else {}
    failing = (body.get("gate") or {}).get("runs") or [{}]
    check(
        "the gate refuses the bad version (400) and says why",
        blocked is not None
        and blocked.status_code == 400
        and failing[0].get("passed") is False
        and failing[0].get("n_examples") == len(cases)
        and failing[0].get("pass_rate", 1.0) < 1.0,
        f"pass rate {failing[0].get('pass_rate')} over {failing[0].get('n_examples')} replays",
    )
    check("prod still serves version 1", deployed("prod") == v1.version_id)
    result["gate"] = {"good": gate_run["pass_rate"], "bad": failing[0].get("pass_rate")}

    # -- 6. Every incident through the queue, against prod -----------------------------------
    started = time.monotonic()
    runs = submit_cases(
        admin, cases, workflow_id=workflow_id, alias="prod", key=f"{suffix}-prod", retried=retried
    )
    mismatches = grade(cases, runs)
    seconds = round(time.monotonic() - started, 1)
    check(
        "every incident got the expected severity, action and path",
        not mismatches,
        f"{len(cases) - len(mismatches)}/{len(cases)} in {seconds}s"
        + (f", {mismatches}" if mismatches else ""),
    )
    waiting = sorted(case_id for case_id, item in runs.items() if item.status == "waiting_approval")
    rollbacks = sorted(c["id"] for c in cases if c["expected"]["action"] == "rollback")
    check("exactly the rollback incidents are paused at the gate", waiting == rollbacks, waiting)
    result["batch"] = {
        "cases": len(cases),
        "severities": dict(sorted(Counter(c["expected"]["severity"] for c in cases).items())),
        "actions": dict(sorted(Counter(c["expected"]["action"] for c in cases).items())),
        "waiting_approval": waiting,
    }

    approve_id, reject_id = waiting[0], waiting[1]
    own = refused(lambda: admin.workflows.runs.approve(runs[approve_id].workflow_run_id))
    check(
        "the person who started the run cannot approve the rollback (403)",
        isinstance(own, CaliberPermissionError),
    )
    approver.workflows.runs.approve(runs[approve_id].workflow_run_id, reason="Deploy is the cause.")
    approver.workflows.runs.resume(runs[approve_id].workflow_run_id)
    approved = wait_settled(admin, runs[approve_id].workflow_run_id)
    check(
        f"{approve_id}: approved, resumed, rollback released",
        approved.status == "completed" and run_output(approved).startswith(RELEASED_TEXT),
        run_output(approved)[:40],
    )
    approver.workflows.runs.reject(runs[reject_id].workflow_run_id, reason="Not the deploy.")
    rejected = wait_settled(admin, runs[reject_id].workflow_run_id)
    check(
        f"{reject_id}: rejected, nothing released",
        rejected.status == "failed" and RELEASED_TEXT not in run_output(rejected),
    )
    for case_id in waiting[2:]:
        admin.workflows.runs.cancel(runs[case_id].workflow_run_id)
    left = {admin.workflows.runs.get(runs[i].workflow_run_id).status for i in waiting[2:]}
    check("the remaining paused runs cancel cleanly", left == {"cancelled"}, f"{len(waiting) - 2}")

    # -- 7. Failure drills -------------------------------------------------------------------
    def drill(file: str) -> dict[str, Any]:
        spec = load_json(assets / "workflows" / file)
        spec["name"] = f"{NAME_PREFIX}{spec['name']}-{suffix}"
        return spec

    def published_drill(file: str) -> Any:
        created = admin.workflows.import_workflow(manifest=drill(file))
        version = admin.workflows.versions.list(created.workflow_id)[0]
        return admin.workflows.versions.publish(version.version_id)

    def run_once(version: Any, text: str, key: str) -> Any:
        queued_run = admin.workflows.runs.submit(
            workflow_version_id=version.version_id,
            input=text,
            idempotency_key=f"{suffix}-{key}",
        )
        return wait_settled(admin, queued_run.workflow_run_id)

    lookup = published_drill("evidence-lookup-drill.workflow.json")
    good = run_once(lookup, '{"service": "gateway", "environment": "prod"}', "lookup-ok")
    bad_lookup = run_once(lookup, "not json at all", "lookup-bad")
    check(
        "Error Boundary: a good request returns the deployments",
        good.status == "completed" and "a1b9f3c" in run_output(good),
    )
    check(
        "Error Boundary: a failing lookup degrades to the fallback and the run completes",
        bad_lookup.status == "completed" and run_output(bad_lookup).startswith("NO_EVIDENCE"),
        run_output(bad_lookup)[:30],
    )
    preview = admin.workflows.versions.preview_run(lookup.version_id, input="{}")
    check(
        "Preview refuses a Python Code node",
        preview["status"] == "error" and "(python_code)" in preview["error"],
    )

    missing = refused(
        lambda: admin.workflows.import_workflow(manifest=drill("missing-tool-drill.workflow.json"))
    )
    check(
        "a workflow naming an unregistered tool is refused at import (400)",
        missing is not None and missing.status_code == 400,
        "preflight failed",
    )

    status_page = run_once(published_drill("status-page-drill.workflow.json"), "INC-9", "status")
    check(
        "an API Request to a blocked host fails the run with the egress reason",
        status_page.status == "failed" and "blocked" in str(status_page.extra.get("error_summary")),
        str(status_page.extra.get("error_summary"))[:70],
    )

    wait_version = published_drill("mitigation-wait-drill.workflow.json")
    waiting_run = run_once(
        wait_version,
        json.dumps({"incident_id": "INC-9"}),
        "wait",
    )
    checkpoints = admin.workflows.runs.checkpoints(waiting_run.workflow_run_id)
    check(
        "Wait For Event: the run pauses with a readable checkpoint",
        waiting_run.status == "waiting_event"
        and checkpoints
        and checkpoints[0]["state_blob"]["expected_event_name"] == "incident.mitigation_confirmed",
        waiting_run.status,
    )
    admin.workflows.runs.resume_by_event(
        event_name="incident.mitigation_confirmed",
        event_payload={"incident_id": "INC-9", "confirmed": True},
        workflow_id=wait_version.workflow_id,
    )
    resumed = wait_for(
        lambda: admin.workflows.runs.get(waiting_run.workflow_run_id),
        is_done=lambda item: item.status not in {"waiting_event", "queued", "running"},
        interval=0.5,
        timeout=60,
    )
    check("Wait For Event: the event resumes the run to completion", resumed.status == "completed")

    # -- 8. A good version, rollback, calibration, Evaluations -------------------------------
    better = json.loads(json.dumps(stored_manifest))
    better["description"] += " (v3: same rules, new description)"
    v3 = admin.workflows.versions.create(workflow_id, better)
    v3 = admin.workflows.versions.publish(v3.version_id)
    promote(v3.version_id)
    check("a good version passes the gate and prod moves to it", deployed("prod") == v3.version_id)
    admin.workflows.rollback_deployment(workflow_id, "prod")
    check("rollback puts prod back on version 1", deployed("prod") == v1.version_id)

    agent_id = f"{NAME_PREFIX}agent-{suffix}"
    admin.agents.create(agent_id, experiment_id=f"exp-{suffix}", name="incident commander")
    calibration = admin.workflows.create_calibration_run(workflow_id, agent_id=agent_id)
    job = admin.jobs.wait(calibration["job"]["job_id"], timeout=900 if real else 300)
    eval_results = job.eval_results or {}
    check(
        "a workflow calibration runs against the test set and reports its baseline",
        bool(eval_results.get("workflow"))
        and eval_results.get("n_examples", 0) >= 20
        and "baseline" in eval_results,
        f"{job.status}: {(eval_results.get('gate') or {}).get('reasons')}",
    )
    # Whatever the outcome, the evidence says what measured it (the UI's "Simulated evaluation" and
    # "Structural score" markers read these fields, also when no candidate was found).
    check(
        "the calibration evidence says what measured its baseline",
        eval_results.get("simulated") is (not real)
        and eval_results.get("scorer") == "structural"
        and (eval_results.get("provider") == "fake") is (not real),
        {k: eval_results.get(k) for k in ("provider", "simulated", "scorer")},
    )
    result["calibration"] = {
        "status": job.status,
        "n_examples": eval_results.get("n_examples"),
        "baseline_quality": (eval_results.get("baseline") or {}).get("overall"),
        "candidates": len(eval_results.get("calibration_candidates") or []),
        "provider": eval_results.get("provider"),
        "simulated": eval_results.get("simulated"),
        "scorer": eval_results.get("scorer"),
    }
    if real:
        judge_spec = load_json(assets / JUDGE_FILE)
        judge = admin.judges.create(
            f"{judge_spec['name']}-{suffix}",
            instructions=judge_spec["instructions"],
            feedback_value_type=judge_spec["feedback_value_type"],
        )
        scorecard = admin.evaluations.create(
            dataset.dataset_id,
            predict_target="workflow",
            subject_ref=v1.version_id,
            scorers=["contains_expected", f"Judge.{judge.judge_id}"],
            max_examples=EVAL_MAX_EXAMPLES,
        )
        sampling = (scorecard.evidence or {}).get("sampling") or {}
        verdict_score = scorecard.aggregate.get("contains_expected")
        check(
            "Evaluations scores the workflow version for real over the test set",
            scorecard.status == "completed"
            and scorecard.n_examples == EVAL_MAX_EXAMPLES
            and sampling.get("available_examples") == len(cases)
            and sampling.get("truncated") is True
            and verdict_score is not None
            and verdict_score >= 0.9,
            f"{scorecard.passed_count}/{scorecard.n_examples} passed, contains_expected "
            f"{verdict_score}, judge {scorecard.aggregate.get(f'Judge.{judge.judge_id}')}",
        )
        judge_score = scorecard.aggregate.get(f"Judge.{judge.judge_id}")
        check(
            "the model-graded judge ran next to the deterministic scorer",
            judge_score is not None and 0.0 <= judge_score <= 1.0,
            judge_score,
        )
        result["evaluation"] = {
            "run_id": scorecard.run_id,
            "n_examples": scorecard.n_examples,
            "available_examples": sampling.get("available_examples"),
            "pass_rate": scorecard.pass_rate,
            "contains_expected": verdict_score,
            "judge": judge_score,
            "model": scorecard.model,
        }
    else:
        evaluation = refused(
            lambda: admin.evaluations.create(
                dataset.dataset_id,
                predict_target="workflow",
                subject_ref=v1.version_id,
                scorers=["contains_expected"],
            )
        )
        check(
            "Evaluations refuses workflow scoring without a real model (400)",
            evaluation is not None
            and evaluation.status_code == 400
            and "real LLM provider" in str(evaluation),
        )

    if model_variant:
        result["variant"] = run_model_variant(
            admin, assets=assets, cases=cases, suffix=suffix, check=check
        )

    if retried:
        result["resubmitted_after_provider_errors"] = sorted(set(retried))
    if failures:
        raise LabVerificationError(f"{len(failures)} check(s) failed: {failures}")
    report("\nAll documented outcomes verified.")
    return result


def parse_proposal(text: str) -> dict[str, Any] | None:
    """The JSON object a model replied with, however it fenced or padded it; ``None`` if none."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def run_model_variant(
    admin: CaliberClient,
    *,
    assets: Path,
    cases: list[dict[str, Any]],
    suffix: str,
    check: Callable[..., None],
) -> dict[str, Any]:
    """The optional variant: a model agent proposes a triage, the decision tables still decide.

    What a real model changes is the proposal and the stakeholder update, and nothing else:
    the verdict (severity and action), the path and the approval gate come from the tables, so
    they are asserted exactly. How often the model agrees with the tables is reported, not asserted
    (it varies from run to run).
    """
    manifest = load_json(assets / VARIANT_FILE)
    manifest["name"] = f"{NAME_PREFIX}incident-commander-model-{suffix}"
    imported = admin.workflows.import_workflow(manifest=manifest)
    version = admin.workflows.versions.list(imported.workflow_id)[0]
    validation = admin.workflows.versions.validate(version.version_id)
    check("the model variant validates", validation["valid"] and not validation["errors"])
    wanted = {case["id"]: case for case in cases if case["id"] in VARIANT_INCIDENTS}
    verdict_ok: list[str] = []
    gate_ok: list[str] = []
    parsed: dict[str, dict[str, Any]] = {}
    for case_id, case in wanted.items():
        preview = admin.workflows.versions.preview_run(
            version.version_id, input=json.dumps(case["input"])
        )
        output = str(preview.get("output") or "")
        verdict = parse_proposal(
            output[output.find("Verdict: ") :] if "Verdict: " in output else ""
        )
        expected = case["expected"]
        if (
            preview["status"] == "completed"
            and verdict
            and verdict.get("severity") == expected["severity"]
            and verdict.get("action") == expected["action"]
        ):
            verdict_ok.append(case_id)
        # Preview passes the approval node as if approved, so the release text appears exactly
        # when the *tables* say rollback, whatever the model proposed.
        if output.startswith("ROLLBACK_APPROVED") is (expected["action"] == "rollback"):
            gate_ok.append(case_id)
        proposal = parse_proposal(str((verdict or {}).get("model_proposal") or ""))
        if proposal:
            parsed[case_id] = proposal
    check(
        "the model variant's verdicts equal the tables' (the model cannot change a decision)",
        sorted(verdict_ok) == sorted(wanted),
        f"{len(verdict_ok)}/{len(wanted)}",
    )
    check(
        "a rollback is released exactly when the tables say rollback, not when the model does",
        sorted(gate_ok) == sorted(wanted),
        f"{len(gate_ok)}/{len(wanted)}",
    )
    agree_severity = sum(
        1 for i, p in parsed.items() if p.get("severity") == wanted[i]["expected"]["severity"]
    )
    agree_action = sum(
        1 for i, p in parsed.items() if p.get("action") == wanted[i]["expected"]["action"]
    )
    check(
        "the model produced a readable proposal for most incidents",
        len(parsed) >= len(wanted) - 2,
        f"{len(parsed)}/{len(wanted)} parsed",
    )
    # A live run: a rollback incident still stops at the human gate with the model node present.
    admin.workflows.update(imported.workflow_id, status="active")
    published = admin.workflows.versions.publish(version.version_id)
    rollback_case = wanted["IN01"]
    queued = admin.workflows.runs.submit(
        workflow_version_id=published.version_id,
        input=json.dumps(rollback_case["input"]),
        idempotency_key=f"{suffix}-variant-IN01",
    )
    paused = wait_settled(admin, queued.workflow_run_id)
    check(
        "through the queue the rollback still pauses at the approval gate",
        paused.status == "waiting_approval" and node_path(paused)[-1] == APPROVAL_NODE,
        paused.status,
    )
    admin.workflows.runs.cancel(paused.workflow_run_id)
    return {
        "incidents": len(wanted),
        "verdicts_equal_tables": len(verdict_ok),
        "proposals_parsed": len(parsed),
        "model_agrees_severity": agree_severity,
        "model_agrees_action": agree_action,
        "model_disagreements": sorted(
            i
            for i, p in parsed.items()
            if p.get("action") != wanted[i]["expected"]["action"]
            or p.get("severity") != wanted[i]["expected"]["severity"]
        ),
    }


def cleanup(caliber: CaliberClient) -> None:
    """Archive the projects, workflows and test sets this lab created."""
    for project in caliber.projects.list():
        if project.name.startswith(NAME_PREFIX):
            caliber.projects.archive(project.project_id)
    for workflow in caliber.workflows.list():
        if workflow.name.startswith(NAME_PREFIX) and workflow.status != "archived":
            caliber.workflows.update(workflow.workflow_id, status="archived")
    for dataset in caliber.eval_datasets.list():
        if dataset.name.startswith(NAME_PREFIX) and dataset.status != "archived":
            caliber.eval_datasets.update(dataset.dataset_id, status="archived")
    # The judges the real-provider tier created ("<judge name>-<suffix>").
    for judge in caliber.judges.list():
        if (
            judge.name.startswith("StakeholderUpdateHonesty-")
            and getattr(judge, "status", "active") != "archived"
        ):
            caliber.judges.update(judge.judge_id, status="archived")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cleanup", action="store_true", help="archive what earlier runs created")
    parser.add_argument("--suffix", help="name suffix (default: a timestamp)")
    parser.add_argument(
        "--model-variant",
        action="store_true",
        help="also run the optional model-based variant (real provider only)",
    )
    args = parser.parse_args(argv)
    base_url = os.environ["CALIBER_BASE_URL"]
    password = os.environ.get("COOKBOOK_08_PASSWORD", DEFAULT_PASSWORD)
    opened: list[CaliberClient] = []

    def project_client(user_id: str, project_id: str) -> CaliberClient:
        if user_id in (APPROVER_USER, MEMBER_USER):
            token = sign_in(base_url, user_id, password)
        else:
            token = os.environ["CALIBER_TOKEN"]
        client = CaliberClient(base_url, token=token, project=project_id, timeout=REQUEST_TIMEOUT)
        opened.append(client)
        return client

    with env_client(timeout=REQUEST_TIMEOUT) as caliber:
        try:
            if args.cleanup:
                cleanup(caliber)
                return 0
            ensure_accounts(caliber, password)
            result = run(
                caliber,
                project_client=project_client,
                suffix=args.suffix,
                model_variant=args.model_variant,
            )
        except (LabVerificationError, SetupError) as exc:
            print(f"\nVERIFICATION FAILED: {exc}", file=sys.stderr)
            return 1
        finally:
            for client in opened:
                client.close()
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
