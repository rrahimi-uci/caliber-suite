"""Cookbook 07: a support triage workflow, run through the queue with human approval, for real.

This is the executable form of ``docs-site/cookbooks/07-support-triage-resolution-loop/README.md``.
It drives a real CALIBER server and exits 0 only when every documented outcome was verified:

1. install the Cookbooks starter (``07``): a model-free skeleton that always pauses for approval,
   and prove the approval rules on it: the person who started the run cannot approve it (403), an
   approver with no ``caliber.approver`` scope cannot (403), a second person with the scope can,
   and a rejection ends the run without releasing the action;
2. import the lab's triage workflow (``assets/workflows/support-triage.workflow.json``), validate
   it, and confirm Preview refuses it (a ``python_code`` node cannot be isolated in Preview);
3. publish it and move the ``staging`` alias onto it (when the server requires approval for
   ``staging`` the promotion waits, and the script approves it as the second person);
4. import ``assets/dataset/ticket-cases.jsonl`` (44 tickets) as a test set, read it back, and run
   every ticket through the queue against the alias; check decision, severity, SLA, approval flag
   and flags against the expected values, and the path each run took through the graph;
5. answer the approval gate on live runs: approve one, reject one (nothing is released) and
   cancel the rest;
6. publish a deliberately bad version, move the alias onto it, and watch the same dataset catch the
   regression; then roll the alias back and see the dataset pass again;
7. read the run-level audit trail for the approved run.

Environment (see the lab README): ``CALIBER_BASE_URL`` (the server root, for example
``http://127.0.0.1:5001``, without the API path) and ``CALIBER_TOKEN`` (an ADMIN personal access
token). The script creates two more accounts (``lena``, an approver, and ``sam``, an operator
without the approver scope), which the server must know: start it with
``CALIBER_OPERATOR_USERS`` and ``CALIBER_APPROVER_USERS`` as the README shows. Optional:
``COOKBOOK_07_ASSETS`` (another assets directory), ``COOKBOOK_07_PASSWORD`` (the password).

    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \
        -m examples.cookbooks.cookbook_07_support_triage_copilot
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

import httpx

from caliber_sdk import CaliberClient, wait_for
from caliber_sdk.errors import CaliberAPIError, CaliberPermissionError
from caliber_sdk.transport import API_PREFIX
from examples.cookbooks._helpers import PAUSED_OR_TERMINAL_RUN_STATES, env_client

NAME_PREFIX = "cookbook-07-"
APPROVER_USER = "lena"
MEMBER_USER = "sam"
DEFAULT_PASSWORD = "cookbook07-lab-passphrase"
#: Steps that decide what a ticket is; the rest of the graph only acts on the decision.
CLASSIFY_NODE = "classify"
#: The node each decision must end at when no approval is needed.
TERMINAL_NODE = {
    "reply": "draft_reply",
    "clarify": "clarify_question",
    "escalate_support": "support_note",
}
APPROVAL_NODE = "review"
RELEASED_NODE = "action_request"
#: The regression the dataset must catch: a "tidy-up" that stops flagging production impact.
BAD_RULE = ("urgent_markers = has([", "urgent_markers = False and has([")

Report = Callable[[str], None]


class LabVerificationError(AssertionError):
    """At least one documented outcome of the lab did not hold."""


class SetupError(RuntimeError):
    """The deployment is not set up the way the lab needs (a fix, not a defect)."""


# ---------------------------------------------------------------------------
# The asset files
# ---------------------------------------------------------------------------


def default_assets_dir() -> Path:
    override = os.environ.get("COOKBOOK_07_ASSETS")
    if override:
        return Path(override)
    return (
        Path(__file__).resolve().parents[4]
        / "docs-site/cookbooks/07-support-triage-resolution-loop/assets"
    )


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    for row in rows:
        if not {"id", "input", "expected"} <= set(row):
            raise LabVerificationError(f"{path.name}: row {row.get('id')} lacks id/input/expected")
    return rows


def load_manifest(assets: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (assets / "workflows/support-triage.workflow.json").read_text(encoding="utf-8")
    )
    shipped = (assets / "workflows/classify-ticket.py").read_text(encoding="utf-8")
    if manifest["nodes"][CLASSIFY_NODE]["code"] != shipped:
        raise LabVerificationError(
            "the classify node in support-triage.workflow.json no longer matches "
            "workflows/classify-ticket.py; rebuild the manifest from the file"
        )
    return manifest


# ---------------------------------------------------------------------------
# Reading runs
# ---------------------------------------------------------------------------


def summary_of(run: Any) -> dict[str, Any]:
    return dict((run.extra or {}).get("summary") or {})


def node_path(run: Any) -> list[str]:
    return list(summary_of(run).get("node_path") or [])


def triage_of(run: Any) -> dict[str, Any] | None:
    """The record the ``classify`` node produced, read from the run's own step trace."""
    for step in summary_of(run).get("steps") or []:
        if step.get("node_id") == CLASSIFY_NODE:
            # A Python Code node's ``result`` port holds everything the code returned; the triage
            # record is its own ``result`` key.
            returned = (step.get("output_by_port") or {}).get("result")
            if isinstance(returned, dict) and isinstance(returned.get("result"), dict):
                return dict(returned["result"])
            return None
    return None


def run_output(run: Any) -> str:
    """The final text of a settled run (the server keeps it on the run summary)."""
    return str(summary_of(run).get("output") or "")


def problems_for(case: dict[str, Any], run: Any) -> list[str]:
    """What differs between what the dataset expects and what the run did."""
    expected = case["expected"]
    triage = triage_of(run)
    if triage is None:
        return [f"no classify step (status {run.status}, error {run.error})"]
    found: list[str] = []
    for key in ("decision", "severity", "sla_minutes", "requires_approval", "flags"):
        if triage.get(key) != expected[key]:
            found.append(f"{key}: expected {expected[key]!r}, got {triage.get(key)!r}")
    path = node_path(run)
    if expected["requires_approval"]:
        if run.status != "waiting_approval" or not path or path[-1] != APPROVAL_NODE:
            found.append(f"expected a pause at {APPROVAL_NODE!r}, got {run.status} via {path}")
    else:
        terminal = TERMINAL_NODE[expected["decision"]]
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
    caliber: CaliberClient, cases: list[dict[str, Any]], *, workflow_id: str, alias: str, key: str
) -> dict[str, Any]:
    """Queue every case against the alias (asynchronously), then wait for all of them."""
    queued = {
        case["id"]: caliber.workflows.runs.submit(
            workflow_id=workflow_id,
            alias=alias,
            input=json.dumps(case["input"]),
            idempotency_key=f"{key}-{case['id']}",
        )
        for case in cases
    }
    return {case_id: wait_settled(caliber, run.workflow_run_id) for case_id, run in queued.items()}


def grade(cases: list[dict[str, Any]], runs: dict[str, Any]) -> dict[str, list[str]]:
    return {case["id"]: found for case in cases if (found := problems_for(case, runs[case["id"]]))}


def refused(call: Callable[[], Any]) -> CaliberAPIError | None:
    """The API error a call raises, or ``None`` when it succeeded."""
    try:
        call()
    except CaliberAPIError as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def sign_in(base_url: str, user_id: str, password: str) -> str:
    """``POST /auth/login``; the session token doubles as a bearer token for scripts."""
    url = f"{base_url.rstrip('/')}{API_PREFIX}/auth/login"
    csrf = httpx.get(f"{base_url.rstrip('/')}{API_PREFIX}/csrf", timeout=30)
    headers: dict[str, str] = {}
    if csrf.status_code == 200:
        body = csrf.json().get("data") or {}
        if body.get("enabled") and body.get("token"):
            headers["X-CALIBER-CSRF"] = str(body["token"])
    response = httpx.post(
        url, json={"user_id": user_id, "password": password}, headers=headers, timeout=30
    )
    if response.status_code != 200:
        raise SetupError(
            f"could not sign in as {user_id!r}: {response.status_code} {response.text[:200]}. "
            "An account left by an earlier run may have another password: reset it under "
            "Administration, or set COOKBOOK_07_PASSWORD"
        )
    return str(response.cookies["caliber_session"])


def ensure_accounts(caliber: CaliberClient, password: str) -> None:
    existing = {account.user_id: account for account in caliber.auth.accounts.list()}
    for user in (APPROVER_USER, MEMBER_USER):
        if user not in existing:
            caliber.auth.accounts.create(user, password)
        elif getattr(existing[user], "disabled", False):
            caliber.auth.accounts.update(user, disabled=False)


# ---------------------------------------------------------------------------
# The lab
# ---------------------------------------------------------------------------


def run(
    caliber: CaliberClient,
    *,
    project_client: Callable[[str, str], CaliberClient],
    suffix: str | None = None,
    assets: Path | None = None,
    report: Report = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the whole lab. ``project_client(user_id, project_id)`` returns that person's client,
    bound to the project; ``caliber`` is the admin client (it creates the project)."""
    assets = assets or default_assets_dir()
    suffix = suffix or time.strftime("%Y%m%d-%H%M%S")
    cases = load_cases(assets / "dataset/ticket-cases.jsonl")
    manifest = load_manifest(assets)
    failures: list[str] = []
    result: dict[str, Any] = {"suffix": suffix}

    def check(label: str, ok: bool, detail: Any = "") -> None:
        report(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail != "" else ""))
        if not ok:
            failures.append(label)

    me = caliber.me.get()
    if not me.is_admin:
        raise SetupError("CALIBER_TOKEN must be an admin personal access token")
    if "07" not in {recipe.id for recipe in caliber.cookbooks.list()}:
        raise SetupError("Cookbooks are off: start the server with CALIBER_FEATURE_COOKBOOKS=true")

    # -- 0. The project and the three people ------------------------------------------------
    project = caliber.projects.create(
        f"{NAME_PREFIX}support-ops-{suffix}", description="Cookbook 07 support triage"
    )
    for user in (APPROVER_USER, MEMBER_USER):
        caliber.projects.add_member(project.project_id, user, role="reviewer")
    admin = project_client(me.user_id, project.project_id)
    approver = project_client(APPROVER_USER, project.project_id)
    member = project_client(MEMBER_USER, project.project_id)
    scopes = {
        user: set(client.me.get().scopes) for user, client in (("lena", approver), ("sam", member))
    }
    if "caliber.approver" not in scopes["lena"] or "caliber.approver" in scopes["sam"]:
        raise SetupError(
            "start CALIBER with CALIBER_OPERATOR_USERS=admin,lena,sam and "
            "CALIBER_APPROVER_USERS=admin,lena (lena approves, sam must not)"
        )
    check("lena holds caliber.approver; sam does not", True, f"project {project.project_id}")
    result["project_id"] = project.project_id

    # -- 1. The starter: the approval rules -------------------------------------------------
    installed = admin.cookbooks.install(
        "07", name=f"{NAME_PREFIX}starter-{suffix}", acknowledge_prerequisites=True
    )
    starter_workflow = installed["workflow"]["workflow_id"]
    check(
        "the installed starter arrives paused (activation needs review)",
        installed["workflow"]["status"] == "paused" and installed["activation_requires_review"],
    )
    admin.workflows.update(starter_workflow, status="active")
    starter = admin.workflows.versions.publish(installed["version"]["version_id"])

    def starter_run(label: str) -> Any:
        queued = admin.workflows.runs.submit(
            workflow_version_id=starter.version_id,
            input="How do I enable SSO for my workspace?",
            idempotency_key=f"{suffix}-starter-{label}",
        )
        return wait_settled(admin, queued.workflow_run_id)

    paused = starter_run("approve")
    check(
        "the starter pauses at its approval node",
        paused.status == "waiting_approval" and node_path(paused)[-1] == APPROVAL_NODE,
        paused.workflow_run_id,
    )
    own = refused(lambda: admin.workflows.runs.approve(paused.workflow_run_id, reason="self"))
    check(
        "the person who started the run cannot approve it (403)",
        isinstance(own, CaliberPermissionError) and "second person" in str(own),
    )
    no_scope = refused(lambda: member.workflows.runs.approve(paused.workflow_run_id, reason="x"))
    check(
        "an operator without caliber.approver cannot approve (403)",
        isinstance(no_scope, CaliberPermissionError) and "caliber.approver" in str(no_scope),
    )
    approver.workflows.runs.approve(paused.workflow_run_id, reason="Looks right to me.")
    approver.workflows.runs.resume(paused.workflow_run_id)
    done = wait_settled(admin, paused.workflow_run_id)
    check(
        "a second person with the scope approves and the run completes", done.status == "completed"
    )

    rejected = starter_run("reject")
    approver.workflows.runs.reject(rejected.workflow_run_id, reason="Not this one.")
    rejected = wait_settled(admin, rejected.workflow_run_id)
    check(
        "a rejection ends the run as failed with approval_rejected",
        rejected.status == "failed" and rejected.extra.get("error_code") == "approval_rejected",
        rejected.extra.get("error_code"),
    )
    result["starter"] = {
        "approved_run": done.workflow_run_id,
        "rejected_run": rejected.workflow_run_id,
    }

    # -- 2. The lab workflow: import, validate, Preview refuses -----------------------------
    imported = admin.workflows.import_workflow(
        manifest=manifest, name=f"{NAME_PREFIX}support-triage-{suffix}"
    )
    workflow_id = imported.workflow_id
    v1 = admin.workflows.versions.list(workflow_id)[0]
    report_ = admin.workflows.versions.validate(v1.version_id)
    check("the imported manifest validates", report_["valid"] and not report_["errors"])
    preview = admin.workflows.versions.preview_run(v1.version_id, input="How do I enable SSO?")
    check(
        "Preview refuses a workflow with a python_code node",
        preview["status"] == "error" and "classify (python_code)" in preview["error"],
        preview["error"][:90],
    )
    v1 = admin.workflows.versions.publish(v1.version_id)
    result["workflow_id"] = workflow_id

    synchronous = refused(lambda: admin.workflows.versions.run(v1.version_id, input="x"))
    check(
        "a synchronous run of a workflow with an approval gate is refused (400): use the queue",
        synchronous is not None
        and synchronous.status_code == 400
        and "queued /workflow-runs" in str(synchronous),
    )

    # -- 3. Staging: a gated promotion waits for a second person ----------------------------
    def promote(version_id: str) -> str:
        moved = admin.workflows.promote_deployment(workflow_id, "staging", version_id=version_id)
        if moved["rotated"]:
            return "rotated"
        pending = moved["promotion"]
        check(
            "the staging promotion waits for approval",
            pending["status"] == "pending" and moved["deployment"] is None,
            pending["promotion_id"],
        )
        approver.workflows.promotions.approve(pending["promotion_id"], reason="Dataset passed.")
        return "approved"

    def staging_version() -> str | None:
        rows = {row["alias"]: row["version_id"] for row in admin.workflows.deployments(workflow_id)}
        return rows.get("staging")

    result["promotion"] = promote(v1.version_id)
    check("staging now serves version 1", staging_version() == v1.version_id)

    # -- 4. The dataset: import it, read it back, run every ticket --------------------------
    dataset = admin.eval_datasets.create(
        f"{NAME_PREFIX}ticket-cases-{suffix}", owner=me.user_id, description="Cookbook 07 tickets"
    )
    for row in cases:
        admin.eval_datasets.add_example(
            dataset.dataset_id,
            input=row["input"],
            expected=row["expected"],
            tags=row.get("tags", []),
        )
    stored = admin.eval_datasets.examples(dataset.dataset_id)
    check("the test set holds every ticket", len(stored) == len(cases), f"{len(stored)} rows")
    # The runs below use the rows read back from the server, matched to the file by content, so
    # a row that did not survive the import would fail here rather than go unnoticed.
    canonical = Counter(json.dumps(row["input"], sort_keys=True) for row in cases)
    returned = Counter(json.dumps(row.input, sort_keys=True) for row in stored)
    check("the stored rows are the file's rows", canonical == returned)
    served = cases

    started = time.monotonic()
    runs = submit_cases(admin, served, workflow_id=workflow_id, alias="staging", key=f"{suffix}-v1")
    mismatches = grade(served, runs)
    seconds = round(time.monotonic() - started, 1)
    check(
        "every ticket routed as expected (decision, severity, SLA, approval, flags, path)",
        not mismatches,
        f"{len(served) - len(mismatches)}/{len(served)} in {seconds}s"
        + (f", {mismatches}" if mismatches else ""),
    )
    decisions = Counter(triage["decision"] for run in runs.values() if (triage := triage_of(run)))
    waiting = [case_id for case_id, run in runs.items() if run.status == "waiting_approval"]
    result["batch"] = {
        "cases": len(served),
        "decisions": dict(sorted(decisions.items())),
        "waiting_approval": len(waiting),
    }
    check(
        "exactly the approval-required tickets are paused at the gate",
        sorted(waiting)
        == sorted(case["id"] for case in cases if case["expected"]["requires_approval"]),
        f"{len(waiting)} paused",
    )

    # -- 5. Answer the gate on live runs ----------------------------------------------------
    approve_id = next(
        case["id"]
        for case in cases
        if case["expected"]["requires_approval"] and case["expected"]["decision"] == "escalate_bug"
    )
    reject_id = next(
        case["id"]
        for case in cases
        if case["expected"]["requires_approval"]
        and case["expected"]["decision"] == "escalate_support"
    )
    approver.workflows.runs.approve(runs[approve_id].workflow_run_id, reason="Confirmed defect.")
    approver.workflows.runs.resume(runs[approve_id].workflow_run_id)
    approved = wait_settled(admin, runs[approve_id].workflow_run_id)
    check(
        f"{approve_id}: approved, resumed, and the action was released",
        approved.status == "completed" and run_output(approved).startswith("APPROVED_ACTION"),
    )
    approver.workflows.runs.reject(runs[reject_id].workflow_run_id, reason="Over the refund limit.")
    refused_run = wait_settled(admin, runs[reject_id].workflow_run_id)
    check(
        f"{reject_id}: rejected, and no action was released",
        refused_run.status == "failed"
        and RELEASED_NODE not in node_path(refused_run)
        and "APPROVED_ACTION" not in run_output(refused_run),
    )
    leftovers = [case_id for case_id in waiting if case_id not in {approve_id, reject_id}]
    for case_id in leftovers:
        admin.workflows.runs.cancel(runs[case_id].workflow_run_id)
    cancelled = {
        admin.workflows.runs.get(runs[case_id].workflow_run_id).status for case_id in leftovers
    }
    check(
        "the remaining paused runs cancel cleanly",
        cancelled == {"cancelled"},
        f"{len(leftovers)} runs",
    )

    # -- 6. A bad version: the dataset catches it, rollback restores it ---------------------
    bad_manifest = json.loads(json.dumps(manifest))
    code = bad_manifest["nodes"][CLASSIFY_NODE]["code"]
    if BAD_RULE[0] not in code:
        raise LabVerificationError("the classify code no longer contains the rule the lab breaks")
    bad_manifest["nodes"][CLASSIFY_NODE]["code"] = code.replace(*BAD_RULE, 1)
    v2 = admin.workflows.versions.create(workflow_id, bad_manifest)
    v2 = admin.workflows.versions.publish(v2.version_id)
    promote(v2.version_id)
    check("staging now serves version 2 (the bad one)", staging_version() == v2.version_id)
    bug_cases = [case for case in cases if case["expected"]["decision"] == "escalate_bug"]
    bad_runs = submit_cases(
        admin, bug_cases, workflow_id=workflow_id, alias="staging", key=f"{suffix}-v2"
    )
    caught = grade(bug_cases, bad_runs)
    # The bad rule drops "high" and "urgent" for a bug unless the ticket reports data loss.
    expected_caught = sorted(
        case["id"]
        for case in bug_cases
        if case["expected"]["severity"] in {"high", "urgent"}
        and "bug-data-loss" not in case["tags"]
    )
    check(
        "the dataset catches the regression (the bug tickets that lost their severity)",
        sorted(caught) == expected_caught and bool(caught),
        f"{len(caught)} of {len(bug_cases)} bug tickets wrong: {sorted(caught)}",
    )
    for run_ in bad_runs.values():
        if run_.status == "waiting_approval":
            admin.workflows.runs.cancel(run_.workflow_run_id)
    admin.workflows.rollback_deployment(workflow_id, "staging")
    check("rollback puts staging back on version 1", staging_version() == v1.version_id)
    again = submit_cases(
        admin, bug_cases, workflow_id=workflow_id, alias="staging", key=f"{suffix}-v1b"
    )
    still = grade(bug_cases, again)
    check(
        "after the rollback the same tickets pass again", not still, f"{len(bug_cases)} bug tickets"
    )
    for run_ in again.values():
        if run_.status == "waiting_approval":
            admin.workflows.runs.cancel(run_.workflow_run_id)
    result["regression"] = {"caught": sorted(caught), "after_rollback": len(still)}

    # -- 7. Run-level audit for the approved run --------------------------------------------
    run_id = approved.workflow_run_id
    trail = {(e.action, e.actor) for e in admin.audit.list(entity_id=run_id, limit=50)}
    approvals = admin.audit.list(action="approve_workflow_run_runtime_approval", limit=200)
    trail |= {(e.action, e.actor) for e in approvals if e.details.get("workflow_run_id") == run_id}
    check(
        "the audit log names who enqueued, who approved and who resumed the approved run",
        {
            ("enqueue_workflow_run", me.user_id),
            ("approve_workflow_run_runtime_approval", APPROVER_USER),
            ("resume_workflow_run", APPROVER_USER),
        }
        <= trail,
        f"{len(trail)} entries",
    )
    result["audited_run"] = run_id

    if failures:
        raise LabVerificationError(f"{len(failures)} check(s) failed: {failures}")
    report("\nAll documented outcomes verified.")
    return result


def cleanup(caliber: CaliberClient) -> None:
    """Archive the projects and workflows this lab created."""
    for project in caliber.projects.list():
        if project.name.startswith(NAME_PREFIX):
            caliber.projects.archive(project.project_id)
    for workflow in caliber.workflows.list():
        if workflow.name.startswith(NAME_PREFIX) and workflow.status != "archived":
            caliber.workflows.update(workflow.workflow_id, status="archived")
    for dataset in caliber.eval_datasets.list():
        if dataset.name.startswith(NAME_PREFIX) and dataset.status != "archived":
            caliber.eval_datasets.update(dataset.dataset_id, status="archived")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--cleanup", action="store_true", help="archive what earlier runs created")
    parser.add_argument("--suffix", help="name suffix (default: a timestamp)")
    args = parser.parse_args(argv)
    base_url = os.environ["CALIBER_BASE_URL"]
    password = os.environ.get("COOKBOOK_07_PASSWORD", DEFAULT_PASSWORD)
    opened: list[CaliberClient] = []

    def project_client(user_id: str, project_id: str) -> CaliberClient:
        if user_id in (APPROVER_USER, MEMBER_USER):
            token = sign_in(base_url, user_id, password)
        else:
            token = os.environ["CALIBER_TOKEN"]
        client = CaliberClient(base_url, token=token, project=project_id)
        opened.append(client)
        return client

    with env_client() as caliber:
        try:
            if args.cleanup:
                cleanup(caliber)
                return 0
            ensure_accounts(caliber, password)
            result = run(caliber, project_client=project_client, suffix=args.suffix)
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
