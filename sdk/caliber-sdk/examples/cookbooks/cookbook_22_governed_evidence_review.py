"""Cookbook 22: drive a governed evidence review through installed artifacts.

The example uses the existing run-scoped SDK contract: a workflow run exposes
its approval request, the approval view carries the proposal, and an explicit
``runtime_approval_id`` is required for a decision.  Evidence remains an
archive download and replay returns the server's ``matches_original`` field.

The server-side V01 scenario owns the disposable Harborlight downstream and
its scripted model.  This installed example owns only the public API journey;
it never creates a second downstream route, calls a live model, or treats
``request_changes`` metadata as an executable decision.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberError
from caliber_sdk.models import WorkflowRun

API_PREFIX = "/ajax-api/2.0/mlflow/caliber"
APPROVER = ("lab22-approver", "cookbook22-approver-passphrase")
CASE_ID = "MORT-2026-000184"
DOCUMENT = "pay_statement"
CASE_VERSION = "1"
TERMINAL = frozenset({"completed", "succeeded", "failed", "cancelled", "expired"})


def say(message: str) -> None:
    print(message, flush=True)


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    say(f"  ok  {message}")


def expect_status(status: int, action: Any, message: str) -> CaliberAPIError:
    try:
        action()
    except CaliberAPIError as exc:
        check(exc.status_code == status, f"{message} -> HTTP {exc.status_code}")
        return exc
    raise AssertionError(f"{message}: expected HTTP {status}, the call succeeded")


def login_client(base_url: str, user: str, password: str) -> CaliberClient:
    """Sign in and mint a disposable token for the distinct approver."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": "cookbook-22-approver"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=180)


def ensure_approver(admin: CaliberClient) -> None:
    try:
        admin.auth.accounts.create(*APPROVER)
    except CaliberError:
        admin.auth.accounts.update(APPROVER[0], password=APPROVER[1])


def wait_for_state(
    caliber: CaliberClient, run_id: str, *, wanted: frozenset[str], timeout: float = 180
) -> WorkflowRun:
    """Poll a run until it reaches a state the caller can act on."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = caliber.workflows.runs.get(run_id)
        if run.status in wanted:
            return run
        time.sleep(0.25)
    raise AssertionError(f"workflow run {run_id} did not reach {sorted(wanted)}")


def install_review_workflow(caliber: CaliberClient, *, suffix: str) -> tuple[str, str]:
    recipes = {recipe.id: recipe for recipe in caliber.cookbooks.list()}
    check("22" in recipes, "Lab 22 is present in the enabled cookbook catalog")
    installed = caliber.cookbooks.install(
        "22", name=f"cookbook-22-governed-review{suffix}", acknowledge_prerequisites=True
    )
    workflow_id = str(installed["workflow"]["workflow_id"])
    version_id = str(installed["version"]["version_id"])
    caliber.workflows.update(workflow_id, status="active")
    published = caliber.workflows.versions.publish(version_id)
    check(published.version_id == version_id, "the installed Lab 22 version is published")
    return workflow_id, version_id


def run(admin: CaliberClient, *, approver: CaliberClient, suffix: str = "") -> dict[str, Any]:
    """Execute the public SDK journey against a configured reference stack."""
    project = admin.projects.create(
        f"cookbook-22-governed-review{suffix}",
        description="Synthetic Harborlight governed evidence review",
    )
    project_id = project.project_id
    admin.projects.add_member(project_id, APPROVER[0], role="reviewer")

    with admin.workspace_scope(project_id), approver.workspace_scope(project_id):
        workflow_id, version_id = install_review_workflow(admin, suffix=suffix)
        submitted = admin.workflows.runs.submit(
            workflow_version_id=version_id,
            input=f"request {DOCUMENT} for {CASE_ID} at version {CASE_VERSION}",
            idempotency_key=f"cookbook-22-{suffix or 'default'}",
        )
        waiting = wait_for_state(
            admin,
            submitted.workflow_run_id,
            wanted=frozenset({"waiting_approval", *TERMINAL}),
        )
        check(waiting.status == "waiting_approval", "the run parks before any protected effect")

        approvals = admin.workflows.runs.approvals(waiting.workflow_run_id)
        check(len(approvals) == 1, "the run exposes one runtime approval request")
        approval_id = approvals[0].runtime_approval_id
        view = admin.workflows.runs.approval(waiting.workflow_run_id, approval_id)
        proposal = view.proposal
        check(proposal is not None, "the approval view carries the exact proposal")
        assert proposal is not None
        check(
            proposal.resource == f"cases/{CASE_ID}/document-requests",
            "proposal resource is scoped",
        )

        expect_status(
            403,
            lambda: admin.workflows.runs.decide(
                waiting.workflow_run_id,
                runtime_approval_id=approval_id,
                decision="approve",
                reason="initiator cannot approve",
            ),
            "the initiating editor cannot approve its own proposal",
        )
        approved = approver.workflows.runs.decide(
            waiting.workflow_run_id,
            runtime_approval_id=approval_id,
            decision="approve",
            reason="the exact synthetic request is authorized",
        )
        check(
            getattr(approved, "status", "") in {"approved", "pending", "waiting_approval"},
            "distinct approver records approval",
        )
        resumed = admin.workflows.runs.resume(waiting.workflow_run_id)
        check(isinstance(resumed, dict) or resumed is not None, "operator resumes the approved run")
        completed = wait_for_state(admin, waiting.workflow_run_id, wanted=TERMINAL, timeout=300)
        check(completed.status in {"completed", "succeeded"}, "the governed run completes")
        check(completed.accepted is True, "the downstream effect is accepted")

        links = admin.workflows.runs.causal_links(waiting.workflow_run_id, limit=200)
        check(len(links.effects) == 1, "causal links expose one governed effect")
        effect = links.effects[0]
        check(
            effect.proposal_id == proposal.proposal_id
            and effect.approval.approval_id == approval_id
            and effect.effect.committed,
            "proposal, approval and receipt ids form one chain",
        )
        archive = admin.workflows.runs.evidence(waiting.workflow_run_id)
        check(archive[:2] == b"PK", "evidence export is an archive")
        replay = admin.workflows.runs.replay(waiting.workflow_run_id)
        check(replay.matches_original, "read-only replay reports matches_original=true")
        return {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "workflow_version_id": version_id,
            "workflow_run_id": waiting.workflow_run_id,
            "proposal_id": proposal.proposal_id,
            "runtime_approval_id": approval_id,
            "effect_id": effect.effect.effect_id,
            "replay_matches_original": replay.matches_original,
        }


def cleanup(admin: CaliberClient, *, suffix: str = "") -> None:
    for project in admin.projects.list():
        if project.name == f"cookbook-22-governed-review{suffix}":
            admin.projects.archive(project.project_id)
            say(f"archived project {project.project_id}")


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB22_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix=suffix)
        return
    ensure_approver(admin)
    result = run(admin, approver=login_client(base_url, *APPROVER), suffix=suffix)
    say(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
