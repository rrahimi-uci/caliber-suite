"""Cookbook 09: find why a workflow fails in production, patch it, prove the patch, roll it back.

Runs the whole lab through the SDK against a live server and exits 0 only when every check below
holds. It is the API half of the lab README (docs-site/cookbooks/09-workflow-debugger-self-healing-
lab/README.md) and prints the same values the README tells you to expect.

    export CALIBER_BASE_URL=http://127.0.0.1:5001 CALIBER_TOKEN=calpat_...
    PYTHONPATH=sdk/caliber-sdk python \
      sdk/caliber-sdk/examples/cookbooks/cookbook_09_self_healing_workflows.py

The server must be able to call the lab's stand-in account service (``assets/upstream/
account_service.py``, port 8780): start it, and start CALIBER with ``CALIBER_EGRESS_ALLOWED_HOSTS=
127.0.0.1``. The workflow runs on the shipped alpha runtime; nothing here needs a model.

What it does, in order:

1. Imports ``assets/workflow/ticket-intake.v1.workflow.yaml`` (``refund-intake``) and publishes it.
2. Runs the 35 tickets in ``assets/dataset/failing-inputs.jsonl`` through version 1 and checks that
   exactly the documented ones fail, at the documented node, with the documented error.
3. Reads the debugger evidence for one failing run of each kind: the error span in the trace, the
   run's events and checkpoints, and the retry lineage after a retry that fails the same way.
4. Asks ``propose-patch`` for a fix, and shows what it can and cannot do for a code node.
5. Writes the patch (three node bodies) as version 2 of the same workflow, validates and
   publishes it.
6. Reruns all 35 tickets on version 2: none fails, and the 11 that already worked produce
   byte-identical output (the regression slice).
7. Promotes version 1, then version 2, to the ``staging`` alias, runs through the alias, rolls the
   alias back, and shows that ``prod`` refuses a version with no deploy gate.

``--cleanup`` deletes the workflow the run created when it succeeds.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from examples.cookbooks._helpers import env_client

ASSETS = Path(
    os.environ.get(
        "COOKBOOK_09_ASSETS",
        Path(__file__).resolve().parents[4]
        / "docs-site/cookbooks/09-workflow-debugger-self-healing-lab/assets",
    )
)

#: Nodes whose code the patch replaces; ``assets/workflow/patch/<node>.py`` is each node's new body.
PATCHED_NODES = ("validate", "check_reference", "decide")
#: What the dataset must contain, and what version 1 must do with it (quoted in the README).
EXPECTED_V1_FAILURES = {"validate": 17, "check_reference": 3, "decide": 4}
RUN_TIMEOUT_SECONDS = 180.0


class VerificationError(AssertionError):
    """A documented outcome did not hold; the message says which."""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def load_rows(assets: Path = ASSETS) -> list[dict[str, Any]]:
    path = assets / "dataset/failing-inputs.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def failing_node(error: str | None) -> str | None:
    """The node named in a run's error summary (``python_code node 'validate' failed: ...``)."""
    match = re.search(r"node '([a-z_]+)'", error or "")
    return match.group(1) if match else None


def run_all(caliber: CaliberClient, rows: list[dict[str, Any]], **target: str) -> dict[str, Any]:
    """Submit every row to ``target`` (a version id, or workflow id and alias); wait for each."""
    submitted = {
        row["id"]: caliber.workflows.runs.submit(input=row["input"]["payload"], **target)
        for row in rows
    }
    return {
        row_id: caliber.workflows.runs.wait(
            run.workflow_run_id, timeout=RUN_TIMEOUT_SECONDS, raise_on_failure=False
        )
        for row_id, run in submitted.items()
    }


def output_of(run: Any) -> dict[str, Any]:
    summary = run.extra.get("summary") or {}
    output: dict[str, Any] = json.loads(summary.get("output") or "{}")
    return output


def verify_version_1(rows: list[dict[str, Any]], runs: dict[str, Any]) -> Counter[str]:
    failed_at: Counter[str] = Counter()
    for row in rows:
        expected, run = row["expected"], runs[row["id"]]
        if expected["v1_status"] == "completed":
            check(run.status == "completed", f"{row['id']}: v1 should complete, got {run.status}")
            out = output_of(run)
            check(
                (out["decision"], out["reason_code"])
                == (expected["decision"], expected["reason_code"]),
                f"{row['id']}: v1 decided {out}, expected {expected['decision']}",
            )
            continue
        error = run.extra.get("error_summary") or ""
        check(run.status == "failed", f"{row['id']}: v1 should fail, got {run.status}")
        check(
            failing_node(error) == expected["v1_failing_node"],
            f"{row['id']}: failed at {failing_node(error)}, expected {expected['v1_failing_node']}",
        )
        check(expected["v1_error_contains"] in error, f"{row['id']}: unexpected error {error!r}")
        failed_at[expected["v1_failing_node"]] += 1
    check(dict(failed_at) == EXPECTED_V1_FAILURES, f"v1 failures by node: {dict(failed_at)}")
    return failed_at


def verify_version_2(rows: list[dict[str, Any]], runs: dict[str, Any]) -> Counter[str]:
    decisions: Counter[str] = Counter()
    for row in rows:
        expected, run = row["expected"], runs[row["id"]]
        check(
            run.status == "completed",
            f"{row['id']}: v2 should complete, got {run.status}: {run.extra.get('error_summary')}",
        )
        out = output_of(run)
        check(
            (out["decision"], out["reason_code"])
            == (expected["decision"], expected["reason_code"]),
            f"{row['id']}: v2 decided {out}, expected "
            f"{expected['decision']}/{expected['reason_code']}",
        )
        if "upstream_status" in expected:
            check(out.get("upstream_status") == expected["upstream_status"], f"{row['id']}: {out}")
        if "v2_errors_contain" in expected:
            check(
                any(expected["v2_errors_contain"] in e for e in out.get("errors", [])),
                f"{row['id']}: rejection reasons {out.get('errors')}",
            )
        decisions[out["decision"]] += 1
    return decisions


def debugger_evidence(caliber: CaliberClient, run: Any, node: str) -> dict[str, Any]:
    """What the run debugger shows for a failed run: where it stopped, and what it recorded."""
    trace = caliber.workflows.runs.trace(run.workflow_run_id)
    error_spans = [s["name"] for s in trace["spans"] if s.get("status") == "ERROR"]
    events = caliber.workflows.runs.events(run.workflow_run_id)
    failed = [e for e in events if e["event_type"] == "workflow.run.failed"]
    steps = [(s["node_id"], s["status"]) for s in (run.extra["summary"].get("steps") or [])]
    checkpoints = caliber.workflows.runs.checkpoints(run.workflow_run_id)
    check(f"node.{node}" in error_spans, f"{run.workflow_run_id}: error spans {error_spans}")
    check(bool(failed), "no workflow.run.failed event recorded")
    check(checkpoints == [], f"a node failure should leave no checkpoint, got {checkpoints}")
    return {
        "run_id": run.workflow_run_id,
        "error_spans": error_spans,
        "completed_steps": [name for name, status in steps if status == "ok"],
        "checkpoints": len(checkpoints),
    }


def retry_fails_the_same_way(caliber: CaliberClient, run: Any, node: str) -> dict[str, Any]:
    retried = caliber.workflows.runs.retry(run.workflow_run_id, reason="retry the same input")
    again = caliber.workflows.runs.wait(
        retried["workflow_run_id"], timeout=RUN_TIMEOUT_SECONDS, raise_on_failure=False
    )
    lineage = caliber.workflows.runs.lineage(again.workflow_run_id)
    check(again.status == "failed", f"retry of a code fault should fail again, got {again.status}")
    check(failing_node(again.extra.get("error_summary")) == node, "retry failed at another node")
    check(again.extra.get("attempt_number") == 2, "the retry should be attempt 2")
    check(lineage["total_attempts"] == 2, f"lineage: {lineage['total_attempts']} attempts")
    return {"retry_run_id": again.workflow_run_id, "status": again.status, "attempts": 2}


def propose_patch_shows_its_limits(caliber: CaliberClient, version_id: str) -> dict[str, Any]:
    patch = caliber.workflows.versions.propose_patch(
        version_id,
        evidence={
            "node_id": "check_reference",
            "root_cause": "check_reference timed out after 2.0s",
        },
    )
    check(patch["patch_kind"] == "prompt", f"unexpected patch kind {patch['patch_kind']}")
    check(patch["semantic_ops"] == [], "propose-patch should have no graph change for a code node")
    refused = None
    try:
        caliber.workflows.versions.propose_patch(
            version_id, evidence={"category": "guardrail", "node_id": "check_reference"}
        )
    except CaliberAPIError as exc:
        refused = exc.status_code
    check(refused == 400, f"a guardrail patch aimed at a code node should be a 400, got {refused}")
    return {"patch_id": patch["patch_id"], "patch_kind": patch["patch_kind"], "refused": refused}


def write_version_2(
    caliber: CaliberClient, workflow_id: str, version_1_id: str, assets: Path
) -> Any:
    manifest = caliber.workflows.versions.get(version_1_id).manifest
    for node in PATCHED_NODES:
        manifest["nodes"][node]["code"] = (assets / f"workflow/patch/{node}.py").read_text()
    version_2 = caliber.workflows.versions.create(workflow_id, manifest)
    report = caliber.workflows.versions.validate(version_2.version_id)
    check(report["valid"] is True, f"version 2 does not validate: {report}")
    return caliber.workflows.versions.publish(version_2.version_id)


def exercise_the_alias(
    caliber: CaliberClient, workflow_id: str, version_1: str, version_2: str, failing_payload: str
) -> dict[str, Any]:
    """Promote, run through the alias, roll back; and show that ``prod`` is refused."""
    wf = caliber.workflows
    wf.promote_deployment(workflow_id, "staging", version_id=version_1)
    wf.promote_deployment(workflow_id, "staging", version_id=version_2)
    serving = {d["alias"]: d["version_id"] for d in wf.deployments(workflow_id)}
    check(serving == {"staging": version_2}, f"staging should serve version 2, got {serving}")
    fixed = caliber.workflows.runs.wait(
        wf.runs.submit(
            workflow_id=workflow_id, alias="staging", input=failing_payload
        ).workflow_run_id,
        timeout=RUN_TIMEOUT_SECONDS,
        raise_on_failure=False,
    )
    check(
        fixed.status == "completed",
        f"through staging@v2 the ticket should complete: {fixed.status}",
    )

    wf.rollback_deployment(workflow_id, "staging")
    serving = {d["alias"]: d["version_id"] for d in wf.deployments(workflow_id)}
    check(serving == {"staging": version_1}, f"rollback should restore version 1, got {serving}")
    broken = caliber.workflows.runs.wait(
        wf.runs.submit(
            workflow_id=workflow_id, alias="staging", input=failing_payload
        ).workflow_run_id,
        timeout=RUN_TIMEOUT_SECONDS,
        raise_on_failure=False,
    )
    check(
        broken.status == "failed",
        f"after the rollback the ticket should fail again: {broken.status}",
    )

    refusal: tuple[int, str] | None = None
    try:
        wf.promote_deployment(workflow_id, "prod", version_id=version_2)
    except CaliberAPIError as exc:
        refusal = (exc.status_code, str(exc))
    if refusal is None or refusal[0] != 400 or "requires a deploy gate" not in refusal[1]:
        raise VerificationError(f"prod should refuse a version with no deploy gate, got {refusal}")
    return {"through_alias": [fixed.status, broken.status], "prod_refused": refusal[0]}


def run(
    caliber: CaliberClient,
    *,
    assets: Path = ASSETS,
    suffix: str | None = None,
    cleanup: bool = False,
) -> dict[str, Any]:
    suffix = suffix or str(int(time.time()))
    rows = load_rows(assets)
    check(len(rows) == 35, f"expected 35 tickets, found {len(rows)}")

    # 1. import and publish version 1
    manifest_yaml = (assets / "workflow/ticket-intake.v1.workflow.yaml").read_text()
    workflow = caliber.workflows.import_workflow(
        manifest_yaml=manifest_yaml, name=f"refund-intake-{suffix}"
    )
    version_1 = caliber.workflows.versions.publish(
        caliber.workflows.versions.list(workflow.workflow_id)[0].version_id
    )

    # 2. version 1 against the dataset
    runs_v1 = run_all(caliber, rows, workflow_version_id=version_1.version_id)
    failed_at = verify_version_1(rows, runs_v1)

    # 3. the debugger's evidence, and a retry
    evidence = {}
    for node in ("validate", "check_reference", "decide"):
        sample = next(r for r in rows if r["expected"]["v1_failing_node"] == node)
        evidence[node] = debugger_evidence(caliber, runs_v1[sample["id"]], node)
    retry = retry_fails_the_same_way(caliber, runs_v1["F29"], "check_reference")

    # 4. propose-patch
    patch = propose_patch_shows_its_limits(caliber, version_1.version_id)

    # 5. version 2
    version_2 = write_version_2(caliber, workflow.workflow_id, version_1.version_id, assets)

    # 6. version 2 against the dataset, and the regression slice
    runs_v2 = run_all(caliber, rows, workflow_version_id=version_2.version_id)
    decisions = verify_version_2(rows, runs_v2)
    slice_ids = [r["id"] for r in rows if r["expected"]["v1_status"] == "completed"]
    for row_id in slice_ids:
        check(
            runs_v1[row_id].extra["summary"]["output"]
            == runs_v2[row_id].extra["summary"]["output"],
            f"{row_id}: the patch changed the output of a ticket that already worked",
        )

    # 7. the alias
    f12 = next(r for r in rows if r["id"] == "F12")["input"]["payload"]
    alias = exercise_the_alias(
        caliber, workflow.workflow_id, version_1.version_id, version_2.version_id, f12
    )

    if cleanup:
        caliber.workflows.delete(workflow.workflow_id)

    return {
        "workflow_id": workflow.workflow_id,
        "version_1": version_1.version_id,
        "version_2": version_2.version_id,
        "tickets": len(rows),
        "v1": {"completed": len(rows) - sum(failed_at.values()), "failed": sum(failed_at.values())},
        "v1_failed_at": dict(sorted(failed_at.items())),
        "debugger": evidence,
        "retry": retry,
        "propose_patch": patch,
        "v2": {"completed": len(rows), "failed": 0, "decisions": dict(sorted(decisions.items()))},
        "regression_slice": len(slice_ids),
        "alias": alias,
    }


def main() -> None:
    with env_client() as caliber:
        result = run(caliber, cleanup="--cleanup" in sys.argv)
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
