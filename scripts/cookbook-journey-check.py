#!/usr/bin/env python3
"""Cross-lab checks for ``scripts/cookbook-run-all.sh --journey``.

The journey runs several cookbook labs, one after another, on ONE stack. Each lab verifies
itself; this script verifies that the platform's shared records reflect all of them together:

* ``/readiness`` is ready and the only simulated providers are the documented ones;
* the audit log (admin-only) holds the actions each lab performed, by the people who did them;
* the release timeline and the live board show the prompt releases (labs 18 and 01);
* Observability lists the traces of the workflow runs (labs 07 and 16), errors included.

It reads ``CALIBER_API`` and ``CALIBER_TOKEN`` (an admin access token) from the environment, never
prints the token, and exits 0 only if every check held.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

API = os.environ.get("CALIBER_API", "").rstrip("/")
TOKEN = os.environ.get("CALIBER_TOKEN", "")
FAILED: list[str] = []


def get(path: str, **params: Any) -> Any:
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    request = urllib.request.Request(
        f"{API}{path}{'?' + query if query else ''}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"GET {path} -> HTTP {exc.code}: {exc.read()[:300]!r}"
        ) from exc


def check(ok: bool, what: str, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {what}{'  [' + detail + ']' if detail else ''}")
    if not ok:
        FAILED.append(what)


def audit_rows() -> list[dict[str, Any]]:
    """Every audit row (the endpoint pages at 500)."""
    rows: list[dict[str, Any]] = []
    while True:
        page = get("/audit-log", limit=500, offset=len(rows))["data"]
        rows.extend(page["entries"])
        if not page["entries"] or len(rows) >= page["total"]:
            return rows


def has(
    rows: list[dict[str, Any]],
    action: str,
    *,
    actor: str | None = None,
    entity: str | None = None,
    detail: str | None = None,
) -> int:
    """How many rows have this action (optionally by this actor, on an entity id/detail with this prefix)."""
    return sum(
        1
        for r in rows
        if r["action"] == action
        and (actor is None or r["actor"] == actor)
        and (entity is None or str(r["entity_id"]).startswith(entity))
        and (
            detail is None
            or str((r.get("details") or {}).get("name", "")).startswith(detail)
        )
    )


#: What each lab must have left in the audit log on the shared stack: (action, filters, description).
EXPECTED: dict[str, list[tuple[str, dict[str, str], str]]] = {
    "18": [
        ("create_project", {"actor": "maya"}, "maya created the workspace"),
        ("add_project_member", {}, "members were added with roles"),
        (
            "create_personal_access_token",
            {"actor": "priya"},
            "priya issued a bound token",
        ),
        ("revoke_personal_access_token", {"actor": "priya"}, "priya revoked it"),
    ],
    "01": [
        ("prompt.create", {}, "the intake prompt was created"),
        (
            "promote_prompt",
            {"entity": "cookbook-01"},
            "the prompt was promoted to prod",
        ),
        ("rollback_prompt", {"entity": "cookbook-01"}, "the release was rolled back"),
    ],
    "02": [
        ("create_skill", {}, "the skill was created"),
        ("import_skill_package_zip", {}, "the skill package was re-imported"),
        ("rollback_skill", {}, "the skill was rolled back"),
    ],
    "03": [
        ("register_tool", {"detail": "decide_refund"}, "the read tool was registered"),
        (
            "register_tool",
            {"detail": "initiate_refund"},
            "the write tool was registered",
        ),
        ("calibrate_tool", {}, "the tool was calibrated against its fixtures"),
        (
            "approve_workflow_run_runtime_approval",
            {},
            "a second person approved a gated run",
        ),
    ],
    "07": [
        ("import_workflow", {}, "the triage workflow was imported"),
        ("publish_workflow_version", {}, "it was published"),
        ("request_workflow_promotion", {}, "the staging promotion waited for approval"),
        (
            "approve_workflow_promotion",
            {"actor": "lena"},
            "lena approved the promotion",
        ),
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--labs", default="18,01,02,03,07,16")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="print every distinct audit action and count",
    )
    args = parser.parse_args()
    labs = [x.strip() for x in args.labs.split(",") if x.strip()]
    if not API or not TOKEN:
        raise SystemExit("CALIBER_API and CALIBER_TOKEN (admin) are required")

    print("== /readiness")
    readiness = get("/readiness")["data"]
    check(
        bool(readiness["ready"]),
        "the stack is ready after every lab",
        f"ready={readiness['ready']}",
    )
    check(
        set(readiness.get("simulated", [])) <= {"eval", "llm"},
        "only the documented providers are simulated",
        f"simulated={readiness.get('simulated')}",
    )
    check(
        all(
            c.get("ready", True)
            for c in readiness.get("checks", [])
            if c["name"] in ("database", "workflow_queue")
        ),
        "the database and the workflow queue are healthy",
    )

    print("== audit log")
    rows = audit_rows()
    counts = Counter(r["action"] for r in rows)
    check(
        len(rows) > 0,
        "the audit log has entries",
        f"{len(rows)} rows, {len(counts)} distinct actions",
    )
    if args.dump:
        for action, n in sorted(counts.items()):
            print(f"     {n:5d}  {action}")
    for lab in labs:
        for action, filters, what in EXPECTED.get(lab, []):
            n = has(rows, action, **filters)
            check(
                n > 0,
                f"lab {lab}: {what}",
                f"{action}{' ' + str(filters) if filters else ''}: {n}x",
            )

    print("== release timeline and live board")
    timeline = get("/releases/timeline", limit=200)["data"]
    promoted = [e for e in timeline if e["action"] == "promote_prompt"]
    live = get("/releases/live")["data"]
    if "01" in labs:
        mine = [e for e in promoted if str(e["entity_id"]).startswith("cookbook-01")]
        check(
            bool(mine),
            "lab 01's prompt release is on the timeline",
            f"{len(mine)} promotions",
        )
        check(
            any(
                r["artifact_type"] == "prompt"
                and str(r.get("artifact_name")).startswith("cookbook-01")
                for r in live
            ),
            "lab 01's prompt is on the live board",
        )
    if "18" in labs:
        check(
            any(e["actor"] == "priya" for e in promoted),
            "lab 18's prompt release by priya is on the timeline",
        )
        check(
            any(r["artifact_type"] == "prompt" and r["by"] == "priya" for r in live),
            "lab 18's prompt is on the live board, released by priya",
        )
    if "02" in labs:
        check(
            any(e["action"] == "rollback_skill" for e in timeline),
            "lab 02's skill rollback is on the timeline",
        )
    live_workflows = [r for r in live if r["artifact_type"] == "workflow"]
    if "07" in labs:
        staging = [r for r in live_workflows if r["alias"] == "staging"]
        check(
            bool(staging),
            "lab 07's workflow is live on staging",
            f"{[r['artifact_id'] for r in staging]}",
        )

    print("== observability")
    traces = get("/observability/traces", limit=500)["data"]["traces"]
    statuses = Counter(str(t.get("status")) for t in traces)
    check(
        len(traces) > 0,
        "Observability lists traces",
        f"{len(traces)} traces, {dict(statuses)}",
    )
    by_workflow: Counter[str] = Counter()
    errors_by_workflow: Counter[str] = Counter()
    unmapped = 0
    for trace in traces:
        try:
            run = get(f"/workflow-runs/by-trace/{trace['trace_id']}")["data"]
        except SystemExit:
            unmapped += 1
            continue
        by_workflow[run["workflow_id"]] += 1
        if str(trace.get("status")) == "ERROR":
            errors_by_workflow[run["workflow_id"]] += 1
    # Traces with no run row are Preview and test-run traces (labs 03 and 07 run some synchronously).
    print(
        f"info {unmapped} of {len(traces)} traces belong to Preview or test runs, not to queued workflow runs"
    )
    if "07" in labs:
        for row in [r for r in live_workflows if r["alias"] == "staging"]:
            n = by_workflow.get(row["artifact_id"], 0)
            check(
                n >= 44,
                "Observability holds the traces of lab 07's workflow runs",
                f"{row['artifact_id']}: {n} traces",
            )
    if "16" in labs:
        failing = [w for w, n in errors_by_workflow.items() if n >= 12]
        check(
            bool(failing),
            "lab 16's failing runs are error traces of one workflow",
            f"{dict(errors_by_workflow)}",
        )

    print()
    if FAILED:
        print(f"JOURNEY CHECKS FAILED: {len(FAILED)}")
        return 1
    print("journey checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
