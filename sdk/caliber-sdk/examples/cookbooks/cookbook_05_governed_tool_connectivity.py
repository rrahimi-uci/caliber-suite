"""Cookbook 05: govern an MCP server's tools, from registration to the trace an invoke leaves.

Runs the whole lab against a real CALIBER server and a real MCP server (the lab's own
``assets/server/repo_tracker_mcp.py``, started by CALIBER over stdio) and exits 0 only when every
documented outcome was observed:

* register the server in a workspace (people and roles as in the README), discover its six tools;
* see that nothing is callable until classified, then classify each tool from
  ``tool-classification.jsonl``;
* invoke an allowed read tool, see the refusals (blocked, unknown, rate limited) and the tool's own
  errors, and prove from the server's ledger that a refused call never reached it;
* save 28 test cases, calibrate three read tools, and catch a deliberately broken expectation;
* bind the write tool in a workflow and watch the approval gate hold it until a second person
  decides;
* read the audit trail and the ``mcp.<server>.<tool>`` traces, and see which calls leave none.

Environment: ``CALIBER_BASE_URL`` (the server root) and ``CALIBER_TOKEN`` (a token for an admin
account). ``CALIBER_STATE_DIR`` (set by ``scripts/cookbook-env.sh``) or ``LAB05_LEDGER`` says
where the MCP server's call ledger lives (an absolute path, no spaces). Optional ``LAB05_SUFFIX``
makes every name unique so the script can be re-run on a stack that already holds an earlier run
(the rate-limit check needs a minute between runs). ``--cleanup`` removes what a run created.

The CALIBER server must be started with the lab's accounts as platform scopes and with the MCP
server script allowed to run as a stdio command; see the README.
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

LAB_DIR = (
    Path(__file__).resolve().parents[4] / "docs-site/cookbooks/05-mcp-connectivity-governance-lab"
)
ASSETS = LAB_DIR / "assets"
SERVER_SCRIPT = ASSETS / "server/repo_tracker_mcp.py"
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPERATOR = ("lab05-operator", "lab05-operator-password")
APPROVER = ("lab05-approver", "lab05-approver-password")
VIEWER = ("lab05-viewer", "lab05-viewer-password")
WRITE_REPO = "acme/release-notes-bot"
SETTLED = frozenset(
    {"waiting_approval", "completed", "succeeded", "failed", "cancelled", "timed_out"}
)
FINISHED = frozenset({"completed", "succeeded", "failed", "cancelled", "timed_out"})
EXPECTED_TOOLS = {
    "search_repositories",
    "list_issues",
    "get_issue",
    "scan_dependencies",
    "issue_write",
    "create_release",
}


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


def read_jsonl(name: str) -> list[dict[str, Any]]:
    path = ASSETS / "dataset" / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def ledger_path() -> Path:
    explicit = os.environ.get("LAB05_LEDGER")
    if explicit:
        return Path(explicit)
    state = os.environ.get("CALIBER_STATE_DIR")
    if not state:
        raise SystemExit("set CALIBER_STATE_DIR (source the env.sh) or LAB05_LEDGER")
    return Path(state) / "lab05-sdk-ledger.jsonl"


def ledger_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def executed(path: Path, tool: str) -> int:
    """How many times the MCP server itself executed ``tool`` (read from its own ledger)."""
    return sum(1 for entry in ledger_entries(path) if entry["tool"] == tool)


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 120.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": f"cookbook-05-{user}"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_accounts(admin: CaliberClient) -> None:
    for user, password in (OPERATOR, APPROVER, VIEWER):
        try:
            admin.auth.accounts.create(user, password)
            say(f"  created account {user}")
        except CaliberError:
            admin.auth.accounts.update(user, password=password)
            say(f"  account {user} already existed; password reset to the lab value")


def server_args(ledger: Path) -> list[str]:
    return [str(SERVER_SCRIPT), "--ledger", str(ledger)]


def workflow_yaml(server_id: str, tool: str, level: str, approval: bool) -> str:
    text = (ASSETS / "workflow/file-issue.workflow.yaml").read_text()
    for placeholder, value in (
        ("__SERVER_ID__", server_id),
        ("__TOOL__", tool),
        ("__LEVEL__", level),
        ("__APPROVAL__", "true" if approval else "false"),
    ):
        text = text.replace(placeholder, value)
    return text


def settle(caliber: CaliberClient, run_id: str, states: frozenset[str] = SETTLED) -> Any:
    deadline = time.monotonic() + 120
    run = caliber.workflows.runs.get(run_id)
    while time.monotonic() < deadline:
        run = caliber.workflows.runs.get(run_id)
        if run.status in states:
            return run
        time.sleep(1)
    raise AssertionError(f"run {run_id} did not settle (last status {run.status!r})")


def mcp_traces(admin: CaliberClient, prefix: str) -> list[Any]:
    return [
        trace
        for trace in admin.observability.traces(limit=200)
        if str(trace.extra.get("name", "")).startswith(prefix)
    ]


def trace_names(admin: CaliberClient, prefix: str) -> dict[str, int]:
    """Count the ``mcp.<server>.<tool>`` traces once MLflow has finished writing them."""
    previous: dict[str, int] | None = None
    for _ in range(20):
        counts: dict[str, int] = {}
        for trace in mcp_traces(admin, prefix):
            name = str(trace.extra["name"])
            counts[name] = counts.get(name, 0) + 1
        if counts and counts == previous:
            return counts
        previous = counts
        time.sleep(1.5)
    return previous or {}


def run(
    admin: CaliberClient,
    *,
    operator: CaliberClient,
    approver: CaliberClient,
    viewer: CaliberClient,
    ledger: Path,
    suffix: str = "",
) -> dict[str, Any]:
    classification = {row["tool"]: row for row in read_jsonl("tool-classification.jsonl")}
    cases = read_jsonl("calibration-cases.jsonl")
    checks = {row["id"]: row for row in read_jsonl("invoke-checks.jsonl")}
    summary: dict[str, Any] = {"calibration_cases": len(cases)}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("")
    name = f"repo-tracker{suffix}"
    prefix = f"mcp.{name}."

    say("== 1. workspace and people")
    project = admin.projects.create(f"mcp-ops{suffix}", description="Cookbook 05 MCP governance")
    admin.projects.add_member(project.project_id, OPERATOR[0], role="editor")
    admin.projects.add_member(project.project_id, APPROVER[0], role="reviewer")
    admin.projects.add_member(project.project_id, VIEWER[0], role="viewer")
    summary["project_id"] = project.project_id
    say(
        f"  workspace {project.project_id}: admin owns it, operator edits, "
        "approver reviews, viewer reads"
    )

    with ExitStack() as scope:
        for client in (admin, operator, approver, viewer):
            scope.enter_context(client.workspace_scope(project.project_id))

        say("== 2. register the MCP server")
        expect_status(
            403,
            lambda: operator.mcp_servers.create(name, transport="stdio", command="${PYTHON}"),
            "an operator without the admin scope cannot register a server",
        )
        server = admin.mcp_servers.create(
            name,
            description="Engineering repo tracker (the lab's own MCP server)",
            transport="stdio",
            command="${PYTHON}",
            args=server_args(ledger),
        )
        check(server.status == "active", f"registered {server.server_id} ({server.transport})")
        execution = server.extra["execution"]
        check(
            execution["ready"] and execution["boundary"] == "local_containment",
            "readiness: ready, boundary local_containment (a child process, not a sandbox)",
        )
        check(
            any("does not block filesystem or network" in w for w in execution["warnings"]),
            "readiness warns that stdio containment does not block files or network",
        )
        expect_status(
            409,
            lambda: admin.mcp_servers.create(name, transport="stdio", command="${PYTHON}"),
            "registering the same name twice is refused",
        )
        summary["server_id"] = server.server_id
        sid = server.server_id

        say("== 3. test the connection and discover the tools")
        expect_status(
            403,
            lambda: viewer.mcp_servers.test_connection(sid),
            "a viewer cannot test the connection",
        )
        probe = admin.mcp_servers.test_connection(sid)
        names = {tool["name"] for tool in probe["tools"]}
        check(
            probe["success"] and names == EXPECTED_TOOLS,
            f"connected: {len(names)} tools discovered",
        )
        check(
            all(tool["input_schema"].get("properties") for tool in probe["tools"]),
            "every tool carries an input schema",
        )
        write_schema = next(t for t in probe["tools"] if t["name"] == "issue_write")["input_schema"]
        check(
            set(write_schema["required"]) == {"repo", "title"},
            f"issue_write requires {sorted(write_schema['required'])}",
        )
        refreshed = admin.mcp_servers.discover_tools(sid)
        check(refreshed["tool_count"] == len(EXPECTED_TOOLS), "discover-tools agrees: 6 tools")
        listing = admin.mcp_servers.tools(sid)["tools"]
        check(
            all(not t["classified"] and t["policy"]["allowed"] is False for t in listing),
            "every discovered tool starts unclassified and denied",
        )
        summary["tools"] = sorted(names)

        say("== 4. an unclassified tool cannot be called")
        refusal = admin.mcp_servers.invoke_tool(sid, **_invoke(checks["F01"]))
        check(
            refusal["success"] is False
            and checks["F01"]["expect"]["error_contains"] in refusal["error"],
            f"refused: {refusal['error']}",
        )
        check(
            executed(ledger, "search_repositories") == 0,
            "the server's ledger is empty: nothing ran",
        )

        say("== 5. classify every tool")
        unsafe = classification["issue_write"]
        error = expect_status(
            400,
            lambda: admin.mcp_servers.update_tool_policy(
                sid, "issue_write", allowed=True, side_effect_level="write", requires_approval=False
            ),
            "an allowed write tool without requires_approval is rejected",
        )
        check("requires_approval" in str(error), "the rejection names requires_approval")
        expect_status(
            403,
            lambda: operator.mcp_servers.update_tool_policy(
                sid,
                "search_repositories",
                allowed=True,
                side_effect_level="read",
                requires_approval=False,
            ),
            "an operator without the admin scope cannot classify",
        )
        for tool, row in classification.items():
            policy = {
                key: row[key]
                for key in (
                    "allowed",
                    "side_effect_level",
                    "requires_approval",
                    "rate_limit_per_minute",
                )
                if key in row
            }
            admin.mcp_servers.update_tool_policy(sid, tool, **policy)
        listing = {t["name"]: t for t in admin.mcp_servers.tools(sid)["tools"]}
        check(all(t["classified"] for t in listing.values()), "all six tools are classified")
        check(
            all(
                listing[tool]["policy"]["allowed"] == row["allowed"]
                and listing[tool]["policy"]["side_effect_level"] == row["side_effect_level"]
                and listing[tool]["policy"]["requires_approval"] == row["requires_approval"]
                for tool, row in classification.items()
            ),
            "the stored policies equal tool-classification.jsonl",
        )
        check(
            unsafe["requires_approval"] and listing["issue_write"]["policy"]["requires_approval"],
            "issue_write is allowed but requires approval",
        )
        check(
            listing["create_release"]["policy"]["allowed"] is False,
            "create_release (external_action) is blocked",
        )

        say("== 6. invoke an allowed read tool")
        call = admin.mcp_servers.invoke_tool(
            sid, "search_repositories", arguments={"query": "language:python topic:mcp"}
        )
        found = [item["full_name"] for item in call["result"]["items"]]
        check(
            call["success"] and found == ["acme/mcp-gateway", "acme/mcp-servers-internal"],
            f"search returned {found} in {call['duration_ms']} ms",
        )
        check(
            executed(ledger, "search_repositories") == 1, "the server's ledger shows one execution"
        )
        expect_status(
            403,
            lambda: viewer.mcp_servers.invoke_tool(
                sid, "search_repositories", arguments={"query": "go"}
            ),
            "a viewer cannot invoke a tool",
        )
        expect_status(
            403,
            lambda: operator.mcp_servers.invoke_tool(
                sid, "search_repositories", arguments={"query": "go"}
            ),
            "an operator without the admin scope cannot invoke a tool",
        )

        say("== 7. save test cases and calibrate the read tools")
        by_tool: dict[str, list[dict[str, Any]]] = {}
        for row in cases:
            by_tool.setdefault(row["tool_name"], []).append(
                {
                    "name": f"{row['id']} {row['name']}",
                    "input": row["input"],
                    "assertion": row["assertion"],
                }
            )
        expect_status(
            403,
            lambda: viewer.mcp_servers.save_test_cases(
                sid, "search_repositories", by_tool["search_repositories"]
            ),
            "a viewer cannot save test cases",
        )
        rates: dict[str, Any] = {}
        for tool, tool_cases in by_tool.items():
            operator.mcp_servers.save_test_cases(sid, tool, tool_cases)
            started = time.monotonic()
            scored = operator.mcp_servers.calibrate_tool(sid, tool)
            rates[tool] = {
                "passed": scored["passed"],
                "total": scored["total"],
                "pass_rate": scored["pass_rate"],
            }
            check(
                (scored["total"], scored["passed"], scored["pass_rate"])
                == (len(tool_cases), len(tool_cases), 1.0),
                f"{tool}: {scored['passed']}/{scored['total']} passed, "
                f"pass_rate {scored['pass_rate']} "
                f"({round(time.monotonic() - started)}s)",
            )
        summary["calibration"] = rates
        broken = [dict(c) for c in by_tool["search_repositories"]]
        wrong = broken[0]["name"]
        broken[0]["assertion"] = {"type": "output_contains", "value": '"total_count": 9'}
        operator.mcp_servers.save_test_cases(sid, "search_repositories", broken)
        drilled = operator.mcp_servers.calibrate_tool(sid, "search_repositories")
        failing = [c["name"] for c in drilled["cases"] if not c["passed"]]
        check(
            failing == [wrong] and drilled["pass_rate"] == 0.9375,
            f"a wrong expectation fails exactly one case: {failing}, "
            f"pass_rate {drilled['pass_rate']}",
        )
        operator.mcp_servers.save_test_cases(
            sid, "search_repositories", by_tool["search_repositories"]
        )
        expect_status(
            409,
            lambda: operator.mcp_servers.calibrate_tool(sid, "issue_write"),
            "a tool that requires approval cannot be calibrated (no approval checkpoint)",
        )
        expect_status(
            400,
            lambda: operator.mcp_servers.calibrate_tool(sid, "scan_dependencies"),
            "a tool with no saved cases cannot be calibrated",
        )
        expect_status(
            404,
            lambda: operator.mcp_servers.calibrate_tool(sid, "drop_database"),
            "calibrating a tool the server does not have is a 404",
        )
        before = {tool: executed(ledger, tool) for tool in EXPECTED_TOOLS}

        say("== 8. refusals and failures")
        results: dict[str, Any] = {}
        for check_id in ("F02", "F03", "F05", "F06", "F07", "F08", "F09"):
            row = checks[check_id]
            started = time.monotonic()
            got = admin.mcp_servers.invoke_tool(sid, **_invoke(row))
            seconds = round(time.monotonic() - started)
            results[check_id] = got
            check(
                got["success"] is False and row["expect"]["error_contains"] in got["error"],
                f"{check_id} {row['what']} -> {row['expect']['error_contains']!r}"
                + (f" ({seconds}s)" if row.get("slow") else ""),
            )
        row = checks["F04"]
        accepted = 1  # F09's timed-out call spent one of the three calls a minute
        refused_after = None
        for _ in range(5):
            got = admin.mcp_servers.invoke_tool(sid, **_invoke(row))
            if got["success"]:
                accepted += 1
            else:
                refused_after = accepted
                check(
                    row["expect"]["error_contains"] in got["error"],
                    f"F04 {row['what']} -> {got['error']}",
                )
                break
        check(refused_after == 3, "the fourth call in the minute was the refused one")
        check(
            executed(ledger, "create_release") == 0 and before["create_release"] == 0,
            "the blocked create_release never reached the server (ledger: 0 executions)",
        )
        check(
            all(
                executed(ledger, t) == before[t]
                for t in ("create_release", "issue_write", "get_issue")
            ),
            "no other refusal reached the server either",
        )

        say("== 9. an approval-class write tool, from the admin Playground")
        row = checks["F10"]
        written = admin.mcp_servers.invoke_tool(sid, **_invoke(row))
        check(
            written["success"]
            and written["result"]
            == {
                "created": True,
                "repo": WRITE_REPO,
                "number": 1,
                "title": row["arguments"]["title"],
            },
            f"direct invoke ran: {written['result']}",
        )
        check(
            executed(ledger, "issue_write") == 1,
            "the ledger records the write: the Playground is a break-glass surface, "
            "not the approval gate",
        )
        issues = admin.mcp_servers.invoke_tool(
            sid, "list_issues", arguments={"repo": WRITE_REPO, "state": "open"}
        )
        check(issues["result"]["total_count"] == 1, "list_issues shows the issue that was filed")

        say("== 10. the approval gate in a workflow")
        text = workflow_yaml(sid, "issue_write", "write", True)
        preview = admin.workflows.preview_import(manifest_yaml=text)
        dependency = preview["dependencies"][0]
        check(
            preview["ready_to_import"]
            and dependency["kind"] == "mcp_tool"
            and dependency["status"] == "resolved",
            f"import preview resolves {dependency['reference']}",
        )
        workflow = admin.workflows.import_workflow(
            manifest_yaml=text, name=f"mcp-file-issue{suffix}"
        )
        version = admin.workflows.versions.list(workflow.workflow_id)[0]
        summary["workflow_id"] = workflow.workflow_id
        payload = {
            "repo": WRITE_REPO,
            "title": "Release notes bot posts twice on re-run",
            "body": "Filed by a workflow, after a second person approved it.",
            "labels": ["bug"],
        }
        first = admin.workflows.runs.submit(
            workflow_version_id=version.version_id, input=json.dumps(payload)
        )
        waiting = settle(admin, first.workflow_run_id)
        check(
            waiting.status == "waiting_approval"
            and waiting.extra["current_node_id"] == "call_tool",
            "the run paused at the call_tool node",
        )
        check(executed(ledger, "issue_write") == 1, "nothing was written while the run waited")
        exc = expect_status(
            403,
            lambda: admin.workflows.runs.approve(first.workflow_run_id, reason="self"),
            "the initiator cannot approve their own run",
        )
        check("second person" in str(exc), "the refusal says a second person must decide")
        expect_status(
            403,
            lambda: viewer.workflows.runs.approve(first.workflow_run_id, reason="viewer"),
            "a viewer cannot approve",
        )
        approver.workflows.runs.approve(first.workflow_run_id, reason="matches the release process")
        check(executed(ledger, "issue_write") == 1, "approving alone does not run the tool")
        admin.workflows.runs.resume(first.workflow_run_id)
        done = settle(admin, first.workflow_run_id, FINISHED)
        check(done.status == "completed", f"after Resume the run {done.status}")
        check(executed(ledger, "issue_write") == 2, "the workflow's write executed exactly once")
        issues = admin.mcp_servers.invoke_tool(
            sid, "list_issues", arguments={"repo": WRITE_REPO, "state": "open"}
        )
        titles = [i["title"] for i in issues["result"]["issues"]]
        check(
            payload["title"] in titles and issues["result"]["total_count"] == 2,
            "the issue the workflow filed is on the server",
        )
        summary["approved_run"] = done.workflow_run_id

        say("== 11. a blocked tool inside a workflow")
        blocked_text = workflow_yaml(sid, "create_release", "external_action", False)
        blocked = admin.workflows.import_workflow(
            manifest_yaml=blocked_text, name=f"mcp-release{suffix}"
        )
        blocked_version = admin.workflows.versions.list(blocked.workflow_id)[0]
        refused = expect_status(
            400,
            lambda: admin.workflows.runs.submit(
                workflow_version_id=blocked_version.version_id,
                input=json.dumps({"repo": WRITE_REPO, "tag": "v2.4.0"}),
            ),
            "a run is refused before it starts",
        )
        check(
            "MCP runtime preflight failed" in str(refused) and "blocked by policy" in str(refused),
            "the preflight names the tool: blocked by policy",
        )
        check(
            executed(ledger, "create_release") == 0, "create_release still never reached the server"
        )

        say("== 12. servers CALIBER will not run")
        rogue = admin.mcp_servers.create(
            f"rogue-shell{suffix}", transport="stdio", command="/bin/sh", args=["-c", "echo pwned"]
        )
        check(
            rogue.extra["execution"]["ready"] is False
            and rogue.extra["execution"]["command_allowed"] is False,
            "a /bin/sh command is registered but not ready: not on the command allowlist",
        )
        tried = admin.mcp_servers.test_connection(rogue.server_id)
        check(
            tried["success"] is False and "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST" in tried["error"],
            f"test-connection refuses it: {tried['error']}",
        )
        inline = admin.mcp_servers.create(
            f"rogue-python{suffix}", transport="stdio", command="${PYTHON}", args=["-c", "print(1)"]
        )
        tried = admin.mcp_servers.test_connection(inline.server_id)
        check(
            tried["success"] is False and "allowlisted '-m' module" in tried["error"],
            "python -c is refused: only an allowlisted -m module or script may run",
        )
        loopback = admin.mcp_servers.create(
            f"loopback-http{suffix}",
            transport="streamable-http",
            uri="http://127.0.0.1:9/mcp",
        )
        tried = admin.mcp_servers.test_connection(loopback.server_id)
        check(
            tried["success"] is False and "blocked loopback range" in tried["error"],
            "a loopback HTTP endpoint is refused by the egress policy, even though the host "
            "allowlist names it",
        )
        summary["refused_servers"] = [rogue.server_id, inline.server_id, loopback.server_id]
        history = admin.mcp_servers.history(rogue.server_id)
        check(
            [h["action"] for h in history] == ["create_mcp_server"],
            "a refused test leaves only the registration in that server's history",
        )

        say("== 13. history and audit")
        actions = [entry["action"] for entry in admin.mcp_servers.history(sid)]
        for action in (
            "create_mcp_server",
            "update_mcp_tool_policy",
            "update_mcp_tool_test_cases",
            "calibrate_mcp_tool",
        ):
            check(action in actions, f"history has {action}")
        check(
            actions.count("update_mcp_tool_policy") == len(classification),
            f"one policy change per tool ({len(classification)})",
        )
        audited = {(e.actor, e.action) for e in admin.audit.list(limit=200) if e.entity_id == sid}
        check(
            ("lab05-operator", "calibrate_mcp_tool") in audited,
            "the audit log names lab05-operator for the calibrations",
        )

        say("== 14. the traces")
        counts = trace_names(admin, prefix)
        check(
            counts.get(f"{prefix}issue_write") == 1,
            "one standalone issue_write trace: the Playground write",
        )
        spans: list[str] = []
        for _ in range(20):  # MLflow writes the run's trace a moment after the run settles
            spans = [
                s["name"] for s in admin.workflows.runs.trace(summary["approved_run"])["spans"]
            ]
            if len(spans) >= 5:
                break
            time.sleep(1.5)
        check(
            sorted(spans)
            == sorted(
                [
                    "workflow.run",
                    "node.call_tool",
                    "tool.issue_write",
                    f"{prefix}issue_write",
                    "node.done",
                ]
            ),
            "the workflow's write is the same span, nested in the run's own trace: "
            f"{sorted(spans)}",
        )
        for tool in ("create_release", "drop_database"):
            check(f"{prefix}{tool}" not in counts, f"no trace for {tool}: a refusal leaves none")
        check(
            counts.get(f"{prefix}scan_dependencies") == 3,
            "scan_dependencies: three traces (one of them the timeout), none for the refused call",
        )
        traces = [t for t in mcp_traces(admin, prefix) if t.extra["name"] == f"{prefix}issue_write"]
        detail = admin.observability.trace(traces[0].trace_id)
        span = detail["spans"][0]
        attrs = span["attributes"]
        check(
            span["span_type"] == "TOOL"
            and attrs["caliber.mcp.transport"] == "stdio"
            and attrs["caliber.mcp.server_id"] == sid
            and "caliber.tool.input" in attrs
            and "caliber.tool.output" in attrs,
            "the trace is one TOOL span with the server, transport, input, output and latency",
        )
        failed_trace = [
            t
            for t in mcp_traces(admin, prefix)
            if t.extra["name"] == f"{prefix}search_repositories"
        ]
        check(
            any(str(t.status).upper().endswith("ERROR") for t in failed_trace),
            "a tool's own error is traced as ERROR",
        )
        summary["traces"] = counts

        say("== 15. the gallery starter")
        recipe = {item.id: item for item in admin.cookbooks.list()}["05"]
        check(len(recipe.steps) == 10, "the gallery lists 10 steps for this lab")
        installed = admin.cookbooks.install(
            "05",
            name=f"Cookbook 05 starter{suffix}",
            acknowledge_prerequisites=bool(recipe.prerequisites),
        )
        manifest = installed["version"]["manifest"]
        node_types = sorted(n["type"] for n in manifest["nodes"].values())
        check(
            node_types == ["agent", "note", "output", "start"] and not manifest["tools"],
            "the starter is a generic single-agent draft with no tools: it does not use the "
            "MCP server",
        )
        summary["starter_workflow_id"] = installed["workflow"]["workflow_id"]
    return summary


def _invoke(row: dict[str, Any]) -> dict[str, Any]:
    return {"tool_name": row["tool_name"], "arguments": row["arguments"]}


def cleanup(admin: CaliberClient, suffix: str) -> None:
    wanted = {
        f"repo-tracker{suffix}",
        f"rogue-shell{suffix}",
        f"rogue-python{suffix}",
        f"loopback-http{suffix}",
    }
    workflows = {
        f"mcp-file-issue{suffix}",
        f"mcp-release{suffix}",
        f"Cookbook 05 starter{suffix}",
    }
    for workflow in admin.workflows.list():
        if workflow.name in workflows:
            admin.workflows.delete(workflow.workflow_id)
            say(f"deleted workflow {workflow.name}")
    for project in admin.projects.list():
        if project.name == f"mcp-ops{suffix}":
            with admin.workspace_scope(project.project_id):
                for server in admin.mcp_servers.list():
                    if server.name in wanted:
                        admin.mcp_servers.delete(server.server_id)
                        say(f"deleted MCP server {server.name}")
            admin.projects.archive(project.project_id)
            say(f"archived workspace {project.name}")


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB05_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix)
        return
    ensure_accounts(admin)
    result = run(
        admin,
        operator=login_client(base_url, *OPERATOR),
        approver=login_client(base_url, *APPROVER),
        viewer=login_client(base_url, *VIEWER),
        ledger=ledger_path(),
        suffix=suffix,
    )
    say("== done")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
