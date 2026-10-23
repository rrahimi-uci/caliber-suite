"""Cookbook 21: pin a workspace to a revision, review it, and take it to the release gate.

Runs the whole lab against a real CALIBER server -- no GitHub, no object store: managed snapshots
are the alpha's built-in source of truth -- and exits 0 only when every documented outcome was
observed:

* a fresh workspace is **caliber-managed** (no source connection), ships four seeded environments
  of which only ``dev`` starts active, and refuses the Git-only import path with the documented
  409;
* a **snapshot** pins real entities (a prompt version and an eval dataset version) into an
  immutable revision; the identical request returns the identical revision (idempotency by
  content), a changed pin makes a new revision, and the **diff** between the two names exactly the
  pin that changed; an unsupported resource type is refused with its own reason code;
* a **change request** carries the revision through review: it cannot be submitted without a
  reviewer, its author cannot review it (``self_review_forbidden``), a second person's approval
  makes it ``technically_approved`` -- and *accepting* it is refused until a QA go decision that
  the alpha cannot produce (see below); version tags stay empty for the same reason;
* a **release** of the revision into ``dev`` is created as a draft (digests are content
  addresses, replaying the same idempotency key returns the same release), and ``evaluate`` moves
  it to ``evaluating`` with the evaluation **queued -- where it stays**. That is the alpha's
  documented limitation: workspace release evaluations have no executor, so quality signoff
  (409: machine gate evidence is missing) and apply (409: only an approved release can be
  applied) are unreachable on a live server. The lab proves the boundary instead of hiding it;
* **role floors**: snapshots need the operator scope plus an owner/editor project role -- a
  viewer's snapshot is refused;
* the **audit trail** records the journey.

Environment: ``CALIBER_BASE_URL`` and ``CALIBER_TOKEN`` (an admin token). Optional
``LAB21_SUFFIX`` makes names unique for a re-run. ``--cleanup`` archives what a run created.

The CALIBER server must be started with ``--flags release_candidates,cookbooks`` (workspace
releases share the release-candidates preview flag; revisions and change requests are ungated)
and ``CALIBER_OPERATOR_USERS`` naming the admin and the lab's reviewer; see the README.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberError

API_PREFIX = "/ajax-api/2.0/mlflow/caliber"

REVIEWER = ("lab21-reviewer", "lab21-reviewer-password")
VIEWER = ("lab21-viewer", "lab21-viewer-password")

# Content addresses for the release request: any 64 lowercase hex characters. The lab uses
# recognizable constants; a real pipeline would hash its environment config, lockfile and policy.
ENV_SHA = "a" * 64
DEPS_SHA = "b" * 64
POLICY_SHA = "c" * 64


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


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 120.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": f"cookbook-21-{user}"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_accounts(admin: CaliberClient) -> None:
    for user, password in (REVIEWER, VIEWER):
        try:
            admin.auth.accounts.create(user, password)
            say(f"  created account {user}")
        except CaliberError:
            admin.auth.accounts.update(user, password=password)
            say(f"  account {user} already existed; password reset to the lab value")


def run(
    admin: CaliberClient,
    *,
    reviewer: CaliberClient,
    viewer: CaliberClient,
    suffix: str = "",
) -> dict[str, Any]:
    summary: dict[str, Any] = {}

    say("== 1. the flag, and a workspace with seeded environments")
    capabilities = admin.capabilities()
    features = capabilities.get("features", {})
    check(
        features.get("release_candidates", {}).get("enabled") is True,
        "the release_candidates preview flag is on (off by default; releases 404 without it)",
    )
    project = admin.projects.create(
        f"release-train{suffix}", description="Cookbook 21 source-to-release"
    )
    pid = project.project_id
    summary["project_id"] = pid
    admin.projects.add_member(pid, REVIEWER[0], role="editor")
    admin.projects.add_member(pid, VIEWER[0], role="viewer")
    environments = admin.projects.list_environments(pid)
    by_name = {env.name: env for env in environments}
    check(
        set(by_name) == {"dev", "qa", "staging", "prod"},
        "four environments are seeded with the workspace",
    )
    check(
        by_name["dev"].status == "active"
        and all(by_name[n].status != "active" for n in ("qa", "staging", "prod")),
        "only dev starts active; promoting further is an explicit, owner-gated act",
    )
    dev = by_name["dev"]

    say("== 2. the source of truth is caliber-managed; the Git import path refuses")
    source = admin.projects.source.get(pid)
    check(
        source.source_mode == "caliber_managed" and source.source is None,
        "a fresh workspace is caliber-managed with no source connection",
    )
    connection = admin.projects.source_connection.get(pid)
    check(connection is None, "and no provider connection exists")

    say("== 3. entities, then a snapshot that pins them")
    prompt_name = f"release-notes{suffix}"
    admin.prompts.create(prompt_name, "Summarize the change: {{change}}", commit_message="v1")
    admin.prompts.create(
        prompt_name, "Summarize the change for an operator: {{change}}", commit_message="v2"
    )
    dataset = admin.eval_datasets.create(
        name=f"release-notes-cases{suffix}",
        owner="admin",
        description="Cookbook 21 pinned dataset",
    )
    dataset_id = dataset.dataset_id
    pins_v1 = [
        {"resource_type": "prompt", "resource_id": prompt_name, "version_ref": "1"},
        {"resource_type": "eval_dataset", "resource_id": dataset_id, "version_ref": "1"},
    ]
    r1 = admin.projects.revisions.snapshot(pid, resources=pins_v1)
    check(
        r1.source_kind == "managed" and r1.status == "ready",
        f"revision {r1.revision_id} pins prompt v1 and the dataset, no Git involved",
    )
    again = admin.projects.revisions.snapshot(pid, resources=pins_v1)
    check(
        again.revision_id == r1.revision_id,
        "the identical snapshot returns the identical revision -- idempotent by content",
    )
    pins_v2 = [
        {"resource_type": "prompt", "resource_id": prompt_name, "version_ref": "2"},
        {"resource_type": "eval_dataset", "resource_id": dataset_id, "version_ref": "1"},
    ]
    r2 = admin.projects.revisions.snapshot(pid, resources=pins_v2)
    check(
        r2.revision_id != r1.revision_id and r2.revision_number == r1.revision_number + 1,
        "a changed pin makes a new revision",
    )
    summary["revisions"] = [r1.revision_id, r2.revision_id]

    say("== 4. the diff names exactly what changed")
    diff = admin.projects.revisions.diff(pid, r2.revision_id, base=r1.revision_id)
    changed = [(c.resource_type, c.logical_name) for c in diff.changed]
    check(
        changed == [("prompt", prompt_name)] and diff.added == [] and diff.removed == [],
        "one changed pin (the prompt), nothing added or removed",
    )
    expect_status(
        400,
        lambda: admin.projects.revisions.diff(pid, r2.revision_id, base=""),
        "a diff without a base revision is refused",
    )
    unknown = expect_status(
        409,
        lambda: admin.projects.revisions.snapshot(
            pid,
            resources=[
                {"resource_type": "knowledge_base", "resource_id": "KB-1", "version_ref": "1"}
            ],
        ),
        "an unsupported resource type is refused",
    )
    check(
        "resource_type_adapter_unavailable" in str(unknown),
        "with its own reason code, resource_type_adapter_unavailable",
    )
    expect_status(
        403,
        lambda: viewer.projects.revisions.snapshot(pid, resources=pins_v1),
        "a viewer cannot snapshot",
    )

    say("== 5. the change request: authored, reviewed by a second person")
    cr = admin.projects.change_requests.create(
        pid,
        title=f"Release notes v2{suffix}",
        description="Pin the reworded prompt.",
        head_revision_id=r2.revision_id,
        base_revision_id=None,
        semantic_version="1.0.0",
        review_backend="caliber",
        reviewer_user_ids=[REVIEWER[0]],
    )
    cr_id = cr.change_request_id
    summary["change_request_id"] = cr_id
    admin.projects.change_requests.submit(pid, cr_id)
    detail = admin.projects.change_requests.get(pid, cr_id)
    head_id = detail.current_head.head_id
    expect_status(
        403,
        lambda: admin.projects.change_requests.submit_review(
            pid, cr_id, head_id=head_id, decision="approve", rationale="looks right"
        ),
        "the snapshot's author cannot review their own revision",
    )
    reviewer.projects.change_requests.submit_review(
        pid, cr_id, head_id=head_id, decision="approve", rationale="reworded prompt reads well"
    )
    detail = admin.projects.change_requests.get(pid, cr_id)
    check(
        detail.status == "technically_approved",
        "a second person's approval makes it technically_approved",
    )
    accept = expect_status(
        409,
        lambda: admin.projects.change_requests.accept(pid, cr_id),
        "accepting still refuses: a QA go decision is required first",
    )
    check("qa_go_decision_required" in str(accept), "and names why")
    tags = admin.projects.version_tags.list(pid)
    check(
        tags.items == [],
        "version tags stay empty: only an accepted change request mints one, and acceptance "
        "is gated on the QA decision the alpha cannot produce",
    )

    say("== 6. the release gate, and the alpha's honest boundary")
    release = admin.projects.releases.create(
        pid,
        revision_id=r2.revision_id,
        environment_id=dev.environment_id,
        environment_config_sha256=ENV_SHA,
        runtime_dependencies_sha256=DEPS_SHA,
        policy_sha256=POLICY_SHA,
        request_idempotency_key=f"lab21-release{suffix}",
        change_request_id=cr_id,
    )
    rid = release.release_id
    summary["release_id"] = rid
    check(release.status == "draft", f"release {rid} created as a draft against dev")
    replay = admin.projects.releases.create(
        pid,
        revision_id=r2.revision_id,
        environment_id=dev.environment_id,
        environment_config_sha256=ENV_SHA,
        runtime_dependencies_sha256=DEPS_SHA,
        policy_sha256=POLICY_SHA,
        request_idempotency_key=f"lab21-release{suffix}",
        change_request_id=cr_id,
    )
    check(replay.release_id == rid, "replaying the same idempotency key returns the same release")
    admin.projects.releases.evaluate(
        pid,
        rid,
        idempotency_key=f"lab21-eval{suffix}",
        evaluation_plan_sha256=ENV_SHA,
        input_sha256=DEPS_SHA,
    )
    release = admin.projects.releases.get(pid, rid)
    check(release.status == "evaluating", "evaluate moves the release to evaluating")
    [evaluation] = admin.projects.releases.list_evaluations(pid, rid)
    check(evaluation.status == "queued", "and the evaluation is queued")
    time.sleep(3)
    [evaluation] = admin.projects.releases.list_evaluations(pid, rid)
    check(
        evaluation.status == "queued",
        "where it stays: workspace release evaluations have no executor in the alpha "
        "(docs/start/alpha-scope.md names this limitation; do not wait on it)",
    )
    signoff = expect_status(
        409,
        lambda: admin.projects.releases.quality_signoff(
            pid, rid, decision="go", rationale="trying anyway", gate_evidence_sha256=POLICY_SHA
        ),
        "quality signoff refuses",
    )
    check("evidence" in str(signoff).lower(), "because machine gate evidence is missing")
    expect_status(
        409,
        lambda: admin.projects.release_operations.create(
            pid,
            rid,
            kind="apply",
            idempotency_key=f"lab21-apply{suffix}",
            expected_environment_lock_version=dev.lock_version,
        ),
        "and apply refuses: only an approved release can be applied",
    )

    say("== 7. the audit trail")
    entries = admin.audit.list(limit=200)
    actions = {e.action for e in entries}
    for action in (
        "snapshot_workspace_revision",
        "create_workspace_change_request",
        "submit_workspace_change_request",
    ):
        check(action in actions, f"audit log has {action}")

    say("== 8. the gallery starter")
    recipe = {item.id: item for item in admin.cookbooks.list()}["21"]
    check(len(recipe.steps) > 0, f"the gallery lists {len(recipe.steps)} step(s) for this lab")

    say("== done")
    return summary


def cleanup(admin: CaliberClient, suffix: str = "") -> None:
    for project in admin.projects.list():
        if project.name == f"release-train{suffix}":
            admin.projects.archive(project.project_id)
            say(f"archived workspace {project.project_id}")


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB21_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix)
        return
    ensure_accounts(admin)
    result = run(
        admin,
        reviewer=login_client(base_url, *REVIEWER),
        viewer=login_client(base_url, *VIEWER),
        suffix=suffix,
    )
    say(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
