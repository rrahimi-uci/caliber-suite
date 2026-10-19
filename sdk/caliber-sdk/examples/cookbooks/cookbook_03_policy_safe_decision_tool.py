"""Cookbook 03: a deterministic refund-policy tool, hardened, then used behind an approval gate.

Runs the whole lab against a real CALIBER server and exits 0 only when every documented
outcome was observed:

* register a read tool (``decide_refund``) and a write tool (``initiate_refund``) in a workspace;
* run both in the sandbox (the read tool live in a subprocess, the write tool mocked);
* save the 49 fixture cases, calibrate, record the run and pin it as the baseline;
* provoke a regression and see it against the baseline;
* import a workflow that decides, routes and issues the refund only after a second person approves;
* approve, reject, deny and manual-review runs; self-approval and weak roles refused;
* read the audit trail, and see where the sandbox's process boundary stops.

Environment: ``CALIBER_BASE_URL`` (the server root) and ``CALIBER_TOKEN`` (a token for an admin
account). Optional ``LAB03_SUFFIX`` makes every name unique so the script can be re-run on a
stack that already holds an earlier run. ``--cleanup`` removes what a run created.

The server needs the lab's accounts to carry platform scopes: ``CALIBER_APPROVER_USERS`` must
include ``approver1`` (see the README).
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberError

LAB_DIR = Path(__file__).resolve().parents[4] / "docs-site/cookbooks/03-tool-hardening-contract-lab"
ASSETS = LAB_DIR / "assets"
APPROVER = ("approver1", "lab03-approver1-password")
VIEWER = ("viewer1", "lab03-viewer1-password")
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"
SETTLED = frozenset(
    {"waiting_approval", "completed", "succeeded", "failed", "cancelled", "timed_out"}
)
FINISHED = frozenset({"completed", "succeeded", "failed", "cancelled", "timed_out"})


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


def load_dataset() -> list[dict[str, Any]]:
    path = ASSETS / "dataset/refund-fixtures.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def fixture_cases(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool fixtures: the exact JSON the tool must return, compared as sorted-key JSON."""
    return [
        {
            "name": f"{row['id']} {row['name']}",
            "input": row["inputs"],
            "assertion": {
                "type": "equals",
                "value": json.dumps(row["expectations"], sort_keys=True),
            },
        }
        for row in rows
    ]


def tool_body(filename: str, suffix: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((ASSETS / "tools" / filename).read_text())
    body["name"] = f"{body['name']}{suffix}"
    return body


def register(caliber: CaliberClient, body: dict[str, Any]) -> Any:
    body = dict(body)
    name, version = body.pop("name"), body.pop("version")
    module, callable_name = body.pop("module_path"), body.pop("callable_name")
    return caliber.tools.register(
        name,
        version=version,
        module_path=module,
        callable_name=callable_name,
        input_schema=body.pop("input_schema", None),
        output_schema=body.pop("output_schema", None),
        **body,
    )


def workflow_yaml(suffix: str) -> str:
    text = (ASSETS / "workflow/refund-decision.workflow.yaml").read_text()
    for name in ("decide_refund", "initiate_refund"):
        text = text.replace(name, f"{name}{suffix}")
    return text


def settle(caliber: CaliberClient, run_id: str, states: frozenset[str] = SETTLED) -> Any:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = caliber.workflows.runs.get(run_id)
        if run.status in states:
            return run
        time.sleep(1)
    raise AssertionError(f"run {run_id} did not settle (last status {run.status!r})")


def steps_of(run: Any) -> list[dict[str, Any]]:
    return list((run.extra.get("summary") or {}).get("steps") or [])


def refund_calls(run: Any) -> list[dict[str, Any]]:
    return [
        call
        for step in steps_of(run)
        if step["node_id"] == "refund"
        for call in step.get("tool_calls") or []
    ]


def submit_case(
    caliber: CaliberClient, version_id: str, row: dict[str, Any], **override: Any
) -> Any:
    case = {**row["inputs"], **override}
    run = caliber.workflows.runs.submit(workflow_version_id=version_id, input=json.dumps(case))
    return settle(caliber, run.workflow_run_id)


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 60.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": f"cookbook-03-{user}"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_accounts(admin: CaliberClient) -> None:
    for user, password in (APPROVER, VIEWER):
        try:
            admin.auth.accounts.create(user, password)
            say(f"  created account {user}")
        except CaliberError:
            admin.auth.accounts.update(user, password=password)
            say(f"  account {user} already existed; password reset to the lab value")


def run(
    admin: CaliberClient,
    *,
    approver: CaliberClient,
    viewer: CaliberClient,
    suffix: str = "",
) -> dict[str, Any]:
    rows = load_dataset()
    cases = fixture_cases(rows)
    by_id = {row["id"]: row for row in rows}
    summary: dict[str, Any] = {"fixtures": len(cases)}

    say("== 1. workspace and people")
    project = admin.projects.create(
        f"refund-ops{suffix}", description="Cookbook 03 refund operations"
    )
    admin.projects.add_member(project.project_id, APPROVER[0], role="reviewer")
    admin.projects.add_member(project.project_id, VIEWER[0], role="viewer")
    summary["project_id"] = project.project_id
    say(f"  workspace {project.project_id}: admin owns it, approver1 reviews, viewer1 reads")

    with ExitStack() as scope:
        for client in (admin, approver, viewer):
            scope.enter_context(client.workspace_scope(project.project_id))

        say("== 2. register the tools")
        decide = register(admin, tool_body("decide-refund.tool.json", suffix))
        refund = register(admin, tool_body("initiate-refund.tool.json", suffix))
        check(
            decide.side_effect_level == "read" and decide.allow_in_preview,
            "decide_refund is a read tool allowed in preview",
        )
        check(
            refund.side_effect_level == "write" and refund.requires_approval,
            "initiate_refund is a write tool with requires_approval",
        )
        check(
            not decide.builtin and decide.availability == "available",
            "registered tool carries no Built-in label and is available",
        )
        unsafe = tool_body("initiate-refund.tool.json", f"_unsafe{suffix}")
        unsafe.pop("requires_approval")
        error = expect_status(
            400,
            lambda: register(admin, unsafe),
            "a write tool without requires_approval is rejected",
        )
        check("requires_approval" in str(error), "the rejection names requires_approval")
        summary["tools"] = {"decide_refund": decide.tool_id, "initiate_refund": refund.tool_id}

        say("== 3. sandbox")
        sample = by_id["R01"]["inputs"]
        live = admin.tools.test_invoke(decide.tool_id, input=sample)
        check(
            live["isolation"] == "subprocess" and live["mocked"] is False,
            "decide_refund ran live in a subprocess",
        )
        check(live["output"] == by_id["R01"]["expectations"], f"live output is {live['output']}")
        mock = admin.tools.test_invoke(
            refund.tool_id, input={"order_id": "ord_1007", "amount": 49.0}
        )
        check(
            mock["mocked"] is True and mock["isolation"] == "mocked",
            "initiate_refund was mocked, not called",
        )
        bad = admin.tools.test_invoke(
            decide.tool_id, input={"order_state": "delivered", "bogus": 1}
        )
        check(
            bad["error"] and "unexpected keyword argument" in bad["error"],
            "an unknown field is a tool error, not a decision",
        )
        expect_status(
            403,
            lambda: viewer.tools.test_invoke(decide.tool_id, input=sample),
            "viewer1 cannot run a tool",
        )
        expect_status(
            403,
            lambda: approver.tools.test_invoke(decide.tool_id, input=sample),
            "approver1 (no operator scope) cannot run a tool",
        )

        say("== 4. fixtures, calibration, test run, baseline")
        admin.tools.save_test_cases(decide.tool_id, cases)
        expect_status(
            403, lambda: viewer.tools.calibrate(decide.tool_id), "viewer1 cannot calibrate"
        )
        started = time.monotonic()
        scored = admin.tools.calibrate(decide.tool_id)
        seconds = round(time.monotonic() - started)
        total, passed = scored.extra["total"], scored.extra["passed"]
        check(
            (total, passed, scored.pass_rate) == (len(cases), len(cases), 1.0),
            f"calibration {passed}/{total} passed, pass_rate {scored.pass_rate} ({seconds}s)",
        )

        def record(result: Any) -> Any:
            return admin.tools.create_test_run(
                tool_id=decide.tool_id,
                kind="suite",
                tool_version=decide.version,
                results=[
                    {
                        "name": item["name"],
                        "input": next(c["input"] for c in cases if c["name"] == item["name"]),
                        "output": item["output"],
                        "error": item["error"],
                        "verdict": "pass" if item["passed"] else "fail",
                        "score": 1 if item["passed"] else 0,
                        "duration_ms": item["duration_ms"],
                        "reasoning": "Assertion satisfied."
                        if item["passed"]
                        else "Assertion failed.",
                    }
                    for item in result.extra["cases"]
                ],
            )

        baseline = record(scored)
        check(
            baseline["passed_count"] == len(cases) and baseline["kind"] == "suite",
            f"suite run {baseline['test_run_id']} recorded",
        )
        admin.tools.set_baseline(decide.tool_id, test_run_id=baseline["test_run_id"])
        workspace = admin.tools.workspace(decide.tool_id)
        check(
            workspace["baseline_run_id"] == baseline["test_run_id"],
            "the run is pinned as the baseline",
        )
        summary["calibration"] = {"pass_rate": scored.pass_rate, "passed": passed, "total": total}
        summary["baseline_run_id"] = baseline["test_run_id"]

        say("== 5. regression drill")
        wrong = "R09 amount boundary: exactly 200.00"
        drill = [
            c
            for c in cases
            if c["name"].startswith(("R01", "R02", "R05", "R06", "R09", "R10", "R35"))
        ]
        broken = [dict(c) for c in drill]
        for case in broken:
            if case["name"].startswith("R09"):
                case["assertion"] = {
                    "type": "equals",
                    "value": json.dumps(
                        {
                            "decision": "manual_review",
                            "reason_code": "AMOUNT_OVER_THRESHOLD",
                            "requires_approval": True,
                        },
                        sort_keys=True,
                    ),
                }
        admin.tools.save_test_cases(decide.tool_id, broken)
        drilled = admin.tools.calibrate(decide.tool_id)
        failing = [c["name"] for c in drilled.extra["cases"] if not c["passed"]]
        check(failing == [wrong], f"the drill fails exactly one case: {failing}")
        later = record(drilled)
        base_pass = {
            r["name"]
            for r in admin.tools.test_run(baseline["test_run_id"])["results"]
            if r["verdict"] == "pass"
        }
        regressions = [
            r["name"]
            for r in admin.tools.test_run(later["test_run_id"])["results"]
            if r["verdict"] == "fail" and r["name"] in base_pass
        ]
        check(regressions == [wrong], "compared with the baseline, that case is the one regression")
        admin.tools.save_test_cases(decide.tool_id, cases)
        summary["regression"] = {"pass_rate": drilled.pass_rate, "regressed": regressions}

        say("== 6. the workflow")
        text = workflow_yaml(suffix)
        preview = admin.workflows.preview_import(manifest_yaml=text)
        check(
            preview["ready_to_import"] and preview["validation"]["valid"], "import preview is valid"
        )
        warnings = [w["code"] for w in preview["validation"]["warnings"]]
        check(warnings == ["write_tool_without_approval"], f"one advisory warning only: {warnings}")
        workflow = admin.workflows.import_workflow(
            manifest_yaml=text, name=f"refund-decision{suffix}"
        )
        version = admin.workflows.versions.list(workflow.workflow_id)[0]
        summary["workflow_id"] = workflow.workflow_id

        say("== 7. approve: a second person decides")
        first = submit_case(admin, version.version_id, by_id["R01"])
        check(
            first.status == "waiting_approval" and first.extra["current_node_id"] == "refund",
            "run paused at the refund node",
        )
        check(refund_calls(first) == [], "the refund has not been issued")
        pending = approver.workflows.runs.approvals(first.workflow_run_id)
        check(len(pending) == 1 and pending[0]["status"] == "pending", "one approval is pending")
        check(
            pending[0]["policy_snapshot"]["allow_self_approval"] is False
            and pending[0]["policy_snapshot"]["required_role"] == "caliber.approver",
            "policy: approver scope, no self-approval",
        )
        exc = expect_status(
            403,
            lambda: admin.workflows.runs.approve(first.workflow_run_id, reason="self"),
            "the initiator cannot approve their own run",
        )
        check("second person" in str(exc), "the refusal says a second person must decide")
        expect_status(
            403,
            lambda: viewer.workflows.runs.approve(first.workflow_run_id, reason="viewer"),
            "viewer1 cannot approve",
        )
        approver.workflows.runs.approve(first.workflow_run_id, reason="within policy")
        still = admin.workflows.runs.get(first.workflow_run_id)
        decided = admin.workflows.runs.approvals(first.workflow_run_id)[0]
        check(
            still.status == "waiting_approval"
            and decided["status"] == "approved"
            and decided["decided_by"] == "approver1",
            "approval recorded by approver1; the run still waits for Resume",
        )
        expect_status(
            403,
            lambda: approver.workflows.runs.resume(first.workflow_run_id),
            "approver1 cannot Resume (needs operator)",
        )
        admin.workflows.runs.resume(first.workflow_run_id)
        done = settle(admin, first.workflow_run_id, FINISHED)
        calls = refund_calls(done)
        check(
            done.status == "completed" and len(calls) == 1,
            "run completed with exactly one refund call",
        )
        issued = calls[0]["result"]
        check(
            issued
            == {
                "amount_usd": 49.0,
                "order_id": by_id["R01"]["inputs"]["order_id"],
                "refund_status": "initiated",
            },
            f"refund issued: {issued}",
        )
        summary["approved_run"] = {
            "run_id": done.workflow_run_id,
            "approved_by": decided["decided_by"],
            "refund": issued,
        }

        say("== 8. reject")
        second = submit_case(admin, version.version_id, by_id["R01"])
        check(second.status == "waiting_approval", "second run paused")
        approver.workflows.runs.reject(
            second.workflow_run_id, reason="customer already refunded elsewhere"
        )
        rejected = settle(admin, second.workflow_run_id, FINISHED)
        check(
            rejected.status == "failed" and rejected.extra["error_code"] == "approval_rejected",
            "run failed with approval_rejected",
        )
        check(refund_calls(rejected) == [], "the refund never ran")
        summary["rejected_run"] = {
            "run_id": rejected.workflow_run_id,
            "error_code": rejected.extra["error_code"],
        }

        say("== 9. deny and manual review")
        denied = submit_case(admin, version.version_id, by_id["R23"])
        check(
            denied.status == "completed" and refund_calls(denied) == [],
            "a deny decision completes without any approval or refund",
        )
        verdict = json.loads(denied.extra["summary"]["output"])
        check(
            verdict["decision"] == "deny" and verdict["reason_code"] == "NOT_REFUNDABLE_STATE",
            f"output is the decision: {verdict}",
        )
        check(
            admin.workflows.runs.approvals(denied.workflow_run_id) == [],
            "no approval was ever requested",
        )
        review = submit_case(admin, version.version_id, by_id["R11"])
        check(review.status == "waiting_approval", "a manual_review decision pauses for approval")
        approver.workflows.runs.approve(review.workflow_run_id, reason="manager confirmed")
        admin.workflows.runs.resume(review.workflow_run_id)
        review_done = settle(admin, review.workflow_run_id, FINISHED)
        amount = refund_calls(review_done)[0]["result"]["amount_usd"]
        check(
            review_done.status == "completed" and amount == by_id["R11"]["inputs"]["amount"],
            f"approved manual review refunded {amount}",
        )
        summary["denied_run"] = denied.workflow_run_id
        summary["manual_review_run"] = {"run_id": review_done.workflow_run_id, "amount_usd": amount}

        say("== 10. audit trail and effect ledger")
        entries = admin.audit.list(limit=500)
        actions = {(e.actor, e.action) for e in entries}
        for want in (
            ("admin", "register_tool"),
            ("admin", "calibrate_tool"),
            ("admin", "create_tool_test_run"),
            ("admin", "set_tool_baseline"),
            ("admin", "import_workflow"),
            ("admin", "enqueue_workflow_run"),
            ("admin", "resume_workflow_run"),
            ("approver1", "approve_workflow_run_runtime_approval"),
        ):
            check(want in actions, f"audit has {want[1]} by {want[0]}")
        reject_actions = {a for actor, a in actions if actor == "approver1" and "reject" in a}
        check(reject_actions, f"audit has the rejection by approver1: {sorted(reject_actions)}")
        ledger = admin.raw.get(
            "/system/effects", params={"status": "any", "workflow_run_id": done.workflow_run_id}
        )
        rows_n = len(ledger["effects"])
        say(
            f"  effect ledger rows for the approved run: {rows_n} "
            "(the ledger covers webhook and api_request nodes, not registered tools)"
        )
        summary["effect_ledger_rows"] = len(ledger["effects"])

        say("== 11. where the sandbox boundary stops")
        probe = admin.tools.register(
            f"probe_python{suffix}",
            version="1.0",
            module_path="caliber.workflows.demo_tools",
            callable_name="sandbox_python",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect_level="read",
            allow_in_preview=True,
        )
        slow = admin.tools.test_invoke(
            probe.tool_id, input={"code": "import time; time.sleep(300)", "timeout_seconds": 600}
        )
        check(
            slow["output"] is None and "timed out" in (slow["error"] or ""),
            f"a stalled tool is killed: {slow['error']}",
        )
        big = admin.tools.test_invoke(
            probe.tool_id,
            input={"code": "open('big.bin','wb').write(b'x'*5000000)", "timeout_seconds": 10},
        )
        check(
            "File too large" in big["output"]["stderr"], "the file-size limit stops a large write"
        )
        env_probe = admin.tools.test_invoke(
            probe.tool_id,
            input={"code": "import os; print(sorted(os.environ))", "timeout_seconds": 10},
        )
        check(
            "OPENAI_API_KEY" not in env_probe["output"]["stdout"]
            and "CALIBER" not in env_probe["output"]["stdout"],
            "the child starts with an empty environment",
        )
        health = f"{os.environ.get('CALIBER_BASE_URL', '').rstrip('/')}{API_PREFIX}/health"
        reach = admin.tools.test_invoke(
            probe.tool_id,
            input={
                "code": (
                    "import urllib.request; "
                    f"print(urllib.request.urlopen('{health}', timeout=3).status)"
                ),
                "timeout_seconds": 10,
            },
        )
        check(
            reach["output"]["stdout"].strip() == "200",
            "BUT the child can still reach the network: this is a process boundary, not isolation",
        )
        summary["sandbox_probes"] = {
            "timeout": slow["error"],
            "file_limit": True,
            "network_reachable": True,
        }

        say("== 12. the gallery starter")
        installed = admin.cookbooks.install(
            "03", name=f"Cookbook 03 starter{suffix}", acknowledge_prerequisites=True
        )
        manifest = admin.workflows.versions.get(installed["version"]["version_id"]).manifest
        kinds = {n["type"] for n in manifest["nodes"].values()}
        check(
            {"data_transform", "human_approval"} <= kinds,
            "the installed draft has a decision table and a Human Approval node",
        )
        summary["starter_workflow_id"] = installed["workflow"]["workflow_id"]

    return summary


def cleanup(admin: CaliberClient, suffix: str) -> None:
    for tool in admin.tools.list():
        if tool.name in {
            f"{n}{suffix}" for n in ("decide_refund", "initiate_refund", "probe_python")
        }:
            admin.tools.archive(tool.tool_id)
            say(f"archived tool {tool.name}")
    for workflow in admin.workflows.list():
        if workflow.name in {f"refund-decision{suffix}", f"Cookbook 03 starter{suffix}"}:
            admin.workflows.delete(workflow.workflow_id)
            say(f"deleted workflow {workflow.name}")
    for project in admin.projects.list():
        if project.name == f"refund-ops{suffix}":
            admin.projects.archive(project.project_id)
            say(f"archived workspace {project.name}")


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB03_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix)
        return
    ensure_accounts(admin)
    approver = login_client(base_url, *APPROVER)
    viewer = login_client(base_url, *VIEWER)
    result = run(admin, approver=approver, viewer=viewer, suffix=suffix)
    say("== done")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
