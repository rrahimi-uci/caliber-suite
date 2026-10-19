"""Cookbook 11: a release signoff record, from twenty candidates to one signed GO.

Runs the whole lab against a real CALIBER server and exits 0 only when every documented outcome was
observed:

* create the 20 candidates in ``release-candidates.jsonl`` and check the server's weighted score,
  status and blockers against the expectations written independently in the dataset;
* the ways a candidate is refused (bad weights, scores, keys; no role) and the one thing that is
  not checked (that the artifact exists);
* waivers: reason, expiry, who may record one, a waiver that clears a blocker, one that cannot make
  a low score ready;
* the Allure-compatible report job, and why a report made before a waiver is a stale snapshot;
* go and no-go signoffs, who may record them, and that a signed candidate cannot change;
* the audit trail, and the honest part: a signed GO deployed nothing.

Environment: ``CALIBER_BASE_URL`` (the server root) and ``CALIBER_TOKEN`` (a token for an admin
account). Optional ``LAB11_SUFFIX`` makes every name unique so the script can be re-run on a stack
that already holds an earlier run. ``--cleanup`` removes what a run created.

The server needs ``CALIBER_OPERATOR_USERS`` to list ``lab11-operator`` next to ``admin`` and
``CALIBER_FEATURE_RELEASE_CANDIDATES=true`` (see the README).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberError

LAB_DIR = Path(__file__).resolve().parents[4] / "docs-site/cookbooks/11-release-readiness-factory"
ASSETS = LAB_DIR / "assets"
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPERATOR = ("lab11-operator", "lab11-operator-password")
VIEWER = ("lab11-viewer", "lab11-viewer-password")
PROMPT = "payments-assistant"


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


def load_candidates() -> list[dict[str, Any]]:
    path = ASSETS / "dataset/release-candidates.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def candidate_body(row: dict[str, Any], suffix: str) -> dict[str, Any]:
    """The ``POST /releases/candidates`` body for one dataset row (the New candidate form's)."""
    return {
        "name": f"{row['name']}{suffix}",
        "artifact_type": row["artifact_type"],
        "artifact_ref": row["artifact_ref"],
        "version_ref": row["version_ref"],
        "required_score": row["required_score"],
        "criteria": row["criteria"],
        "evidence": row["evidence"],
        "planned_action": {"action": f"promote_{row['artifact_type']}", "target": "prod"},
        "rollback_target": {"version_ref": row["rollback_version"], "target": "prod"},
    }


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 60.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": f"cookbook-11-{user}"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_accounts(admin: CaliberClient) -> None:
    for user, password in (OPERATOR, VIEWER):
        try:
            admin.auth.accounts.create(user, password)
            say(f"  created account {user}")
        except CaliberError:
            admin.auth.accounts.update(user, password=password)
            say(f"  account {user} already existed; password reset to the lab value")


def statuses(report: dict[str, Any]) -> list[str]:
    return [item["status"] for item in report["results"]]


def run(
    admin: CaliberClient,
    *,
    operator: CaliberClient,
    viewer: CaliberClient,
    suffix: str = "",
) -> dict[str, Any]:
    rows = load_candidates()
    by_id = {row["id"]: row for row in rows}
    summary: dict[str, Any] = {"candidates": len(rows)}
    prompt = f"{PROMPT}{suffix}"
    ids: dict[str, str] = {}

    say("== 1. a real artifact to release: a prompt with two versions")
    admin.prompts.create(prompt, "You are a payments assistant. Question: {{question}}")
    admin.prompts.register_version(
        prompt,
        "You are a careful payments assistant. Quote the refund policy. Question: {{question}}",
        commit_message="cite the policy",
    )
    before = {v["version"]: v["aliases"] for v in admin.prompts.versions(prompt)}
    check(before == {1: [], 2: []}, f"{prompt}: version 1 and 2 exist, no alias points at either")

    say("== 2. create the 20 candidates as the operator")
    for row in rows:
        body = candidate_body(row, suffix)
        if row["id"] == "RC20":  # the walkthrough candidate points at the real prompt
            body.update(artifact_ref=prompt, version_ref="2")
            body["rollback_target"] = {"version_ref": "1", "target": "prod"}
        made = operator.releases.create_candidate(**body)
        ids[row["id"]] = made.candidate_id
        expected = row["expect"]
        blockers = [b["criterion_key"] for b in made.extra["blockers"]]
        check(
            made.weighted_score == expected["weighted_score"]
            and made.status == expected["status"]
            and blockers == expected["blockers"],
            f"{row['id']} {made.weighted_score:<7} {made.status:<7} blockers={blockers}",
        )
    ready = sum(1 for r in rows if r["expect"]["status"] == "ready")
    check(ready == 14 and len(rows) - ready == 6, "14 ready, 6 blocked, as the dataset predicts")
    listed = {c.candidate_id for c in admin.releases.candidates()}
    check(set(ids.values()) <= listed, "all 20 are listed")
    summary["ids"] = ids

    say("== 3. what a candidate is refused for")
    ok_body = candidate_body(by_id["RC01"], f"{suffix} refused")
    for label, change in (
        ("a criterion weight of 0", {"criteria": [{**by_id["RC01"]["criteria"][0], "weight": 0}]}),
        ("a score above 1", {"criteria": [{**by_id["RC01"]["criteria"][0], "score": 1.2}]}),
        (
            "a key with a space",
            {"criteria": [{**by_id["RC01"]["criteria"][0], "key": "eval pass"}]},
        ),
        ("no criteria at all", {"criteria": []}),
        ("a required score above 1", {"required_score": 1.5}),
        ("an unknown field", {"approved": True}),
    ):
        expect_status(
            400,
            lambda change=change: operator.releases.create_candidate(**{**ok_body, **change}),
            f"{label} is rejected",
        )
    expect_status(
        403,
        lambda: viewer.releases.create_candidate(**ok_body),
        "a viewer cannot create a candidate",
    )
    ghost = operator.releases.create_candidate(
        **{
            **ok_body,
            "name": f"ghost{suffix}",
            "artifact_ref": "no-such-prompt",
            "version_ref": "99",
        }
    )
    check(
        ghost.status == "ready",
        "a candidate for an artifact that does not exist is accepted: CALIBER records the "
        "reference and does not look it up",
    )
    summary["ghost_candidate"] = ghost.candidate_id

    say("== 4. waivers")
    rc16, rc17, rc05 = ids["RC16"], ids["RC17"], ids["RC05"]
    expect_status(
        403,
        lambda: operator.releases.add_waiver(
            rc16, criterion_key="rollback_rehearsed", reason="Rehearsal scheduled for Monday."
        ),
        "an operator cannot record a waiver (admin only)",
    )
    for label, payload, status in (
        (
            "an unknown criterion key",
            {"criterion_key": "nope", "reason": "A long enough reason."},
            400,
        ),
        (
            "a reason under 8 characters",
            {"criterion_key": "rollback_rehearsed", "reason": "ok"},
            400,
        ),
        (
            "an expiry in the past",
            {
                "criterion_key": "rollback_rehearsed",
                "reason": "A long enough reason.",
                "expires_at": "2020-01-01T00:00:00Z",
            },
            400,
        ),
    ):
        expect_status(
            status,
            lambda payload=payload: admin.releases.add_waiver(rc16, **payload),
            f"{label} is rejected",
        )
    first_report = admin.releases.generate_report(rc16)
    check(
        statuses(first_report["report"]) == ["passed", "passed", "failed", "passed"],
        "RC16 report before the waiver: rollback_rehearsed failed",
    )
    expires = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
    waived = admin.releases.add_waiver(
        rc16,
        criterion_key="rollback_rehearsed",
        reason=(
            "Rollback rehearsal booked for the 25th; on-call holds the previous image. "
            "Owner: release-eng."
        ),
        expires_at=expires,
    )
    check(
        waived["status"] == "ready"
        and waived["blockers"] == []
        and waived["weighted_score"] == 0.721,
        "RC16: the waiver clears the only blocker; the weighted score is still 0.721",
    )
    check(
        waived["waivers"][0]["approved_by"] == "admin" and waived["waivers"][0]["expires_at"],
        "the waiver records who approved it, when, and its expiry",
    )
    again = operator.releases.evaluate(rc16)
    check(
        again.status == "ready" and again.weighted_score == 0.721,
        "Re-evaluate recomputes from the stored criteria and waivers: unchanged",
    )
    second_report = admin.releases.generate_report(rc16)
    check(
        statuses(second_report["report"]) == ["passed", "passed", "skipped", "passed"],
        "RC16 report after the waiver: the criterion is skipped (waived)",
    )
    check(
        first_report["report"]["candidate_snapshot_sha256"]
        != second_report["report"]["candidate_snapshot_sha256"],
        "the first report's snapshot hash is stale once the candidate changed",
    )
    for key, left in (("regression_vs_baseline", 1), ("human_review", 0)):
        waived = admin.releases.add_waiver(
            rc17, criterion_key=key, reason=f"Exception for {key}: tracked in the release ticket."
        )
        check(
            len(waived["blockers"]) == left and waived["status"] == "blocked",
            f"RC17 after waiving {key}: {left} blocker left, still blocked "
            "(weighted 0.6575 < 0.85)",
        )
    expect_status(
        409,
        lambda: admin.releases.sign(rc17, decision="go", rationale="Both blockers are waived."),
        "GO on RC17 is refused: no blocker is left, but the score is under the bar",
    )
    expect_status(
        409,
        lambda: admin.releases.sign(rc05, decision="go", rationale="Everything else is green."),
        "GO on RC05 is refused: a blocking criterion (human_review) is unmet",
    )

    say("== 5. sign off")
    rc20 = ids["RC20"]
    expect_status(
        403,
        lambda: operator.releases.sign(rc20, decision="go", rationale="Looks good to me."),
        "an operator cannot sign off (admin only)",
    )
    expect_status(
        400,
        lambda: admin.releases.sign(rc20, decision="go", rationale="ok"),
        "a rationale under 8 characters is rejected",
    )
    report = operator.releases.generate_report(rc20)
    check(
        report["status"] == "completed"
        and report["format"] == "allure-json"
        and len(report["report"]["candidate_snapshot_sha256"]) == 64
        and statuses(report["report"]) == ["passed"] * 4,
        f"RC20 report job {report['report_job_id']}: completed, 4 passed, snapshot hash recorded",
    )
    check(
        admin.releases.report_job(report["report_job_id"])["report"]["candidate_snapshot_sha256"]
        == report["report"]["candidate_snapshot_sha256"],
        "the job can be read back by id",
    )
    live_before = admin.releases.live()
    timeline_before = admin.releases.timeline()
    signed = admin.releases.sign(
        rc20,
        decision="go",
        rationale=(
            "Quality 0.94 on the sealed set, no regression, expert review complete, "
            "rollback v1 ready."
        ),
    )
    check(
        signed["decision"] == "go"
        and signed["decided_by"] == "admin"
        and signed["candidate_snapshot"]["status"] == "ready"
        and signed["candidate_snapshot"]["weighted_score"] == 0.9375,
        f"RC20 signed GO by admin ({signed['signoff_id']}); the snapshot froze score 0.9375",
    )
    no_go = admin.releases.sign(
        ids["RC19"],
        decision="no_go",
        rationale="Trigger precision 0.70 and recall 0.66 are below the 0.92 / 0.85 gates.",
    )
    check(no_go["decision"] == "no_go", "RC19 (blocked) signed NO-GO: a refusal is a decision too")
    go18 = admin.releases.sign(
        ids["RC18"],
        decision="go",
        rationale="All gates met; macros KB v11 reviewed by support leads.",
    )
    check(go18["decision"] == "go", "RC18 signed GO")
    detail = admin.releases.get_candidate(rc20)
    check(
        detail.status == "signed_go"
        and len(detail.extra["signoffs"]) == 1
        and detail.extra["signoffs"][0]["signoff_id"] == signed["signoff_id"]
        and len(detail.extra["report_jobs"]) == 1,
        "the candidate shows signed_go with its signoff and report job",
    )
    summary["signoff"] = {
        "signoff_id": signed["signoff_id"],
        "report_job_id": report["report_job_id"],
    }

    say("== 6. a signed candidate is final")
    expect_status(409, lambda: admin.releases.evaluate(rc20), "Re-evaluate is refused (409)")
    expect_status(
        409,
        lambda: admin.releases.add_waiver(
            rc20, criterion_key="latency_p95", reason="A late exception must not be possible."
        ),
        "a waiver is refused (409)",
    )
    expect_status(
        409,
        lambda: admin.releases.sign(rc20, decision="no_go", rationale="Changing my mind later."),
        "a second signoff is refused (409)",
    )
    check(admin.releases.get_candidate(rc20).status == "signed_go", "it is still signed_go")
    bare = operator.releases.create_candidate(
        **{**ok_body, "name": f"no-rollback{suffix}", "rollback_target": {}, "planned_action": {}}
    )
    expect_status(
        409,
        lambda: admin.releases.sign(
            bare.candidate_id, decision="go", rationale="Ship it without a way back."
        ),
        "GO on a ready candidate with no planned action or rollback target is refused",
    )

    say("== 7. signing off deployed nothing")
    after = {v["version"]: v["aliases"] for v in admin.prompts.versions(prompt)}
    check(after == before, f"{prompt}: no alias moved (still {after})")
    check(
        admin.releases.live() == live_before and admin.releases.timeline() == timeline_before,
        "the live board and the release timeline did not change",
    )
    admin.prompts.promote(prompt, 2)
    promoted = {v["version"]: v["aliases"] for v in admin.prompts.versions(prompt)}
    check(
        promoted == {1: [], 2: ["prod"]},
        "the real release is a separate act: Promote on the prompt moves the alias",
    )
    events = [e["action"] for e in admin.releases.timeline()]
    check("promote_prompt" in events, "and that, not the signoff, is what the timeline records")

    say("== 8. who did what")
    actions: dict[str, set[str]] = {}
    for entry in admin.audit.list(limit=200):
        if entry.entity_id in ids.values():
            actions.setdefault(entry.action, set()).add(entry.actor)
    for action, actors in (
        ("create_release_candidate", {"lab11-operator"}),
        ("waive_release_blocker", {"admin"}),
        ("generate_release_allure_report", {"admin", "lab11-operator"}),
        ("signoff_release_candidate", {"admin"}),
        ("evaluate_release_candidate", {"lab11-operator"}),
    ):
        check(actions.get(action) == actors, f"audit: {action} by {sorted(actors)}")
    return summary


def cleanup(admin: CaliberClient, suffix: str) -> None:
    try:
        admin.prompts.delete(f"{PROMPT}{suffix}")
        say(f"deleted prompt {PROMPT}{suffix}")
    except CaliberError:
        say(f"no prompt {PROMPT}{suffix} to delete")
    say(
        "release candidates are an audit record and cannot be deleted; signed ones are immutable. "
        "Use a new LAB11_SUFFIX to run again, or reset the stack."
    )


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB11_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=120)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix)
        return
    ensure_accounts(admin)
    result = run(
        admin,
        operator=login_client(base_url, *OPERATOR),
        viewer=login_client(base_url, *VIEWER),
        suffix=suffix,
    )
    say("== done")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
