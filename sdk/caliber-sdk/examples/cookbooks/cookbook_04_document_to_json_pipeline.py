"""Cookbook 04: a document-to-JSON intake pipeline over the object store.

Runs the whole lab against a real CALIBER server and exits 0 only when every documented outcome
was observed:

* a workspace for the lab, with a second person (``reviewer``) in it;
* two buckets, ``doc-intake`` and ``doc-extracted``, and the 30 sample documents uploaded to
  ``doc-intake/incoming/``;
* the built-in Document-to-JSON Pipeline installed as a draft, then resumed (it installs paused);
* one run per document: the run input is the object key. A valid invoice is written to
  ``doc-extracted/invoices/<invoice number>.json``; an invalid one stops at the approval gate; the
  same invoice sent twice is refused by the output bucket;
* every document's outcome checked against ``assets/dataset/extraction-cases.jsonl``, the extracted
  JSON compared field by field and the schema errors compared by path;
* the approval gate: the account that started a run cannot approve it (403), the reviewer approves
  one document (the approved JSON lands under ``reviewed/``) and rejects another (the run fails
  and nothing is written);
* a run for a key that does not exist fails with a clear message.

Environment: ``CALIBER_BASE_URL`` (the server root) and ``CALIBER_TOKEN`` (a token for an admin
account). The server must be started with the object store behind it
(``scripts/cookbook-env.sh up lab04 --flags cookbooks --s3``) and with ``reviewer`` holding the
operator and approver platform scopes (``CALIBER_OPERATOR_USERS`` and ``CALIBER_APPROVER_USERS``
both ``admin,reviewer``). Optional ``LAB04_SUFFIX`` sets the suffix that makes the workspace and
workflow names unique (default: the current time, so the script can be re-run on a stack that holds
an earlier run). ``--cleanup`` removes what runs created.

Nothing here calls a model: the extractor is a deterministic python_code node and the validation is
a JSON Schema, so the outcome is the same under the default simulated providers as under a real one.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberError
from examples.cookbooks._helpers import PAUSED_OR_TERMINAL_RUN_STATES, get_recipe

LAB_DIR = (
    Path(__file__).resolve().parents[4]
    / "docs-site/cookbooks/04-document-extraction-structuring-lab"
)
ASSETS = LAB_DIR / "assets"
INPUT_BUCKET = "doc-intake"
OUTPUT_BUCKET = "doc-extracted"
REVIEWER = ("reviewer", "reviewer-pass-123")
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"
FINISHED = frozenset({"completed", "succeeded", "failed", "cancelled", "timed_out"})


class LabVerificationError(AssertionError):
    """A documented outcome did not hold."""


def say(message: str) -> None:
    print(message, flush=True)


def check(condition: object, message: str) -> None:
    if not condition:
        raise LabVerificationError(message)
    say(f"  ok  {message}")


def load_cases() -> list[dict[str, Any]]:
    path = ASSETS / "dataset/extraction-cases.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 60.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens",
        json={"name": f"cookbook-04-{user}", "scopes": ["caliber.operator", "caliber.approver"]},
        cookies=login.cookies,
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_reviewer(admin: CaliberClient) -> None:
    user, password = REVIEWER
    try:
        admin.auth.accounts.create(user, password)
        say(f"  created account {user}")
    except CaliberError:
        admin.auth.accounts.update(user, password=password)
        say(f"  account {user} already existed; password reset to the lab value")


def wait_for(
    caliber: CaliberClient, run_id: str, *, until: frozenset[str], timeout: float = 120.0
) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        run = caliber.workflows.runs.get(run_id)
        if run.status in until:
            return run
        if time.monotonic() > deadline:
            raise LabVerificationError(f"run {run_id} stayed {run.status!r} for {timeout:.0f}s")
        time.sleep(0.5)


def error_text(run: Any) -> str:
    return str(run.extra.get("error_summary") or run.error or "")


def schema_errors(caliber: CaliberClient, run_id: str) -> list[dict[str, Any]]:
    """The JSON Schema errors the validate_schema node reported, read from the run's events."""
    for event in caliber.workflows.runs.events(run_id, limit=200):
        step = (event.get("payload") or {}).get("step") or {}
        if (
            event.get("event_type") == "workflow.run.step"
            and step.get("node_id") == "validate_schema"
        ):
            return list((step.get("output_by_port") or {}).get("metadata", {}).get("errors") or [])
    return []


def reset_buckets(caliber: CaliberClient) -> None:
    existing = {bucket.name for bucket in caliber.object_store.buckets()}
    for bucket in (INPUT_BUCKET, OUTPUT_BUCKET):
        if bucket not in existing:
            caliber.object_store.create_bucket(bucket)
            say(f"  created bucket {bucket}")
    keys = [o.key for o in caliber.object_store.objects(OUTPUT_BUCKET, recursive=True)]
    if keys:
        caliber.object_store.delete_objects(OUTPUT_BUCKET, keys=keys)
        say(f"  emptied {OUTPUT_BUCKET} ({len(keys)} objects)")


def upload_documents(caliber: CaliberClient, cases: list[dict[str, Any]]) -> None:
    for case in cases:
        key = case["inputs"]["key"]
        filename = key.split("/", 1)[1]
        caliber.object_store.upload(
            INPUT_BUCKET,
            filename=filename,
            content=(ASSETS / "dataset/sources" / filename).read_bytes(),
            key=key,
        )
    stored = {o.key for o in caliber.object_store.objects(INPUT_BUCKET, prefix="incoming/")}
    check(
        stored == {c["inputs"]["key"] for c in cases},
        f"{len(cases)} documents are in {INPUT_BUCKET}/incoming/",
    )


def start(caliber: CaliberClient, version_id: str, key: str) -> Any:
    return caliber.workflows.runs.submit(workflow_version_id=version_id, input=key)


def read_json(caliber: CaliberClient, bucket: str, key: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(caliber.object_store.download(bucket, key))
    return document


def verify_extracted(caliber: CaliberClient, case: dict[str, Any], run: Any) -> None:
    expected = case["expectations"]["record"]
    check(run.status == "completed", f"{case['id']} {case['inputs']['key']} completed")
    stored = read_json(caliber, OUTPUT_BUCKET, f"invoices/{expected['invoice_number']}.json")
    mismatched = [k for k, v in expected.items() if stored.get(k) != v]
    check(
        not mismatched,
        f"{case['id']} invoices/{expected['invoice_number']}.json matches the expected JSON",
    )
    check(
        stored["source_key"] == case["inputs"]["key"],
        f"{case['id']} keeps its source key (lineage)",
    )


def verify_held(caliber: CaliberClient, case: dict[str, Any], run: Any) -> None:
    check(
        run.status == "waiting_approval",
        f"{case['id']} {case['inputs']['key']} is held for approval",
    )
    paths = sorted({e["path"] for e in schema_errors(caliber, run.workflow_run_id)})
    wanted = sorted(case["expectations"]["errors"])
    check(paths == wanted, f"{case['id']} reports schema errors at exactly {wanted} (got {paths})")


def run(
    admin: CaliberClient,
    *,
    reviewer: CaliberClient,
    suffix: str = "",
) -> dict[str, Any]:
    cases = load_cases()
    summary: dict[str, Any] = {"documents": len(cases)}

    say("== 0. prerequisites")
    recipe = get_recipe(admin, "04")
    blockers = [
        c["label"] for c in recipe.unmet_checks if c.get("status") == "configuration_required"
    ]
    check(not blockers, f"the cookbook's configuration checks pass (blocked by: {blockers})")
    status = admin.object_store.status()
    check(status.get("connected") is True, f"the object store answers at {status.get('endpoint')}")

    say("== 1. workspace and reviewer")
    project = admin.projects.create(
        f"invoice-intake{suffix}", description="Cookbook 04 invoice intake"
    )
    admin.projects.add_member(project.project_id, REVIEWER[0], role="reviewer")
    summary["project_id"] = project.project_id

    say("== 2. buckets and documents")
    reset_buckets(admin)
    upload_documents(admin, cases)

    with admin.workspace_scope(project.project_id), reviewer.workspace_scope(project.project_id):
        say("== 3. install the pipeline and resume it")
        installed = admin.cookbooks.install(
            "04",
            name=f"doc-intake-to-json{suffix}",
            acknowledge_prerequisites=bool(recipe.prerequisites),
        )
        workflow_id = installed["workflow"]["workflow_id"]
        version_id = installed["version"]["version_id"]
        check(installed["workflow"]["status"] == "paused", "the cookbook installs paused")
        check(admin.workflows.versions.validate(version_id)["valid"], "the draft version validates")
        admin.workflows.update(workflow_id, status="active")
        summary.update(workflow_id=workflow_id, version_id=version_id)

        say("== 4. one run per document")
        first = [c for c in cases if c["expectations"]["outcome"] != "duplicate"]
        runs = {c["id"]: start(admin, version_id, c["inputs"]["key"]) for c in first}
        for case in first:
            runs[case["id"]] = wait_for(
                admin, runs[case["id"]].workflow_run_id, until=PAUSED_OR_TERMINAL_RUN_STATES
            )
        for case in first:
            outcome = case["expectations"]["outcome"]
            if outcome == "extracted":
                verify_extracted(admin, case, runs[case["id"]])
            else:
                verify_held(admin, case, runs[case["id"]])
        summary["extracted"] = sorted(
            c["id"] for c in first if c["expectations"]["outcome"] == "extracted"
        )
        summary["held"] = sorted(
            c["id"] for c in first if c["expectations"]["outcome"] == "needs_review"
        )

        say("== 5. a duplicate invoice is refused")
        for case in (c for c in cases if c["expectations"]["outcome"] == "duplicate"):
            dup = wait_for(
                admin,
                start(admin, version_id, case["inputs"]["key"]).workflow_run_id,
                until=FINISHED,
            )
            check(dup.status == "failed", f"{case['id']} duplicate run failed")
            check(
                case["expectations"]["error_contains"] in error_text(dup),
                f"it says why: {error_text(dup)}",
            )
            original = read_json(
                admin, OUTPUT_BUCKET, f"invoices/{case['expectations']['duplicate_of']}.json"
            )
            check(
                original["source_key"].endswith("inv-2026-0101-acme.txt"),
                "the first invoice's JSON is untouched",
            )
            summary["duplicate_error"] = error_text(dup)

        say("== 6. a key that matches no object")
        missing = wait_for(
            admin,
            start(admin, version_id, "incoming/does-not-exist.txt").workflow_run_id,
            until=FINISHED,
        )
        check(
            missing.status == "failed" and "found 0" in error_text(missing),
            f"fails clearly: {error_text(missing)}",
        )
        summary["missing_object_error"] = error_text(missing)

        say("== 7. the approval gate")
        approve_case = next(
            c for c in first if c["inputs"]["key"].endswith("inv-2026-0115-no-total.txt")
        )
        reject_case = next(
            c for c in first if c["inputs"]["key"].endswith("inv-2026-0117-no-vendor.txt")
        )
        approve_id = runs[approve_case["id"]].workflow_run_id
        reject_id = runs[reject_case["id"]].workflow_run_id
        try:
            admin.workflows.runs.approve(approve_id, reason="self approval")
        except CaliberAPIError as exc:
            check(
                exc.status_code == 403,
                f"the account that started the run cannot approve it (HTTP {exc.status_code})",
            )
            summary["self_approval_error"] = str(exc)
        else:
            raise LabVerificationError("the initiator approved their own run")
        reviewer.workflows.runs.approve(
            approve_id, reason="vendor confirmed the missing total by phone"
        )
        reviewer.workflows.runs.resume(approve_id)
        done = wait_for(admin, approve_id, until=FINISHED)
        check(
            done.status == "completed", f"{approve_case['id']} completes after approval and resume"
        )
        stem = approve_case["inputs"]["key"].split("/")[-1].rsplit(".", 1)[0]
        approved = read_json(admin, OUTPUT_BUCKET, f"reviewed/{stem}.json")
        check(
            approved["review"]["decision"] == "approved",
            f"reviewed/{stem}.json records the approval",
        )
        check(
            [e["path"] for e in approved["review"]["schema_errors"]] == ["total"],
            "the approved JSON keeps the schema errors it was approved with",
        )
        reviewer.workflows.runs.reject(reject_id, reason="not one of our suppliers")
        rejected = wait_for(admin, reject_id, until=FINISHED)
        check(rejected.status == "failed", f"{reject_case['id']} fails after rejection")
        check(
            rejected.extra.get("error_code") == "approval_rejected",
            "its error code is approval_rejected",
        )

        say("== 8. what reached the output bucket")
        stored = {o.key for o in admin.object_store.objects(OUTPUT_BUCKET, recursive=True)}
        expected = {
            f"invoices/{c['expectations']['record']['invoice_number']}.json"
            for c in cases
            if c["expectations"]["outcome"] == "extracted"
        } | {f"reviewed/{stem}.json"}
        check(
            stored == expected,
            f"{OUTPUT_BUCKET} holds exactly the {len(expected)} expected objects",
        )
        summary["output_objects"] = sorted(stored)
        summary["still_waiting"] = len(summary["held"]) - 2

    return summary


def cleanup(admin: CaliberClient) -> None:
    """Remove what runs created: the objects, the buckets and the workspaces (archived)."""
    for bucket in (INPUT_BUCKET, OUTPUT_BUCKET):
        try:
            keys = [o.key for o in admin.object_store.objects(bucket, recursive=True)]
            if keys:
                admin.object_store.delete_objects(bucket, keys=keys)
            # Folders created in the web app leave an empty marker object behind.
            for folder in admin.object_store.folders(bucket):
                admin.object_store.delete_objects(bucket, prefix=folder)
            admin.object_store.delete_bucket(bucket)
            say(f"  removed bucket {bucket}")
        except CaliberError as exc:
            say(f"  bucket {bucket}: {exc}")
    for project in admin.projects.list():
        if project.name.startswith("invoice-intake") and project.status == "active":
            admin.projects.archive(project.project_id)
            say(f"  archived workspace {project.name}")


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB04_SUFFIX") or f"-{int(time.time())}"
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin)
        return
    ensure_reviewer(admin)
    reviewer = login_client(base_url, *REVIEWER)
    result = run(admin, reviewer=reviewer, suffix=suffix)
    say("== done")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
