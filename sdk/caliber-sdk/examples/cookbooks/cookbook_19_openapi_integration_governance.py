"""Cookbook 19: curate an OpenAPI spec into governed tools, from import to a workflow's approval.

Runs the whole lab against a real CALIBER server and a real upstream (the lab's own
``assets/server/ticket_api.py``, a tiny Ticket API CALIBER calls over real loopback HTTP) and exits
0 only when every documented outcome was observed:

* create an integration shell, import the Ticket API spec from the upstream's own
  ``/openapi.yaml`` (through CALIBER's guarded egress path), and see a host CALIBER was not told
  to trust refused;
* see the three operations CALIBER normalized and classified (read, read, write), and the
  dependency it auto-wired between creating a ticket and reading one back, plus an advisory one
  from the shared tag it leaves for a human to confirm or reject;
* curate two tool drafts -- a read-only tool **pack** binding two operations into one callable,
  and a write tool that defaults to requiring approval -- and meet the preview gate that refuses
  to fire an unapproved write until an operator explicitly opts in (the break-glass path), the
  same way the admin Playground is a break-glass surface in cookbook 05;
* publish both drafts into CALIBER's ordinary tool registry and prove the role floors: a viewer
  cannot curate anything, a reviewer can preview but not publish, and publishing needs both a
  project role and the platform operator scope;
* bind the published write tool in a workflow and watch the approval gate hold it until a second
  person decides, exactly as cookbook 05's MCP write does;
* reimport the upstream's spec after it changes and read the diff: two changes, one breaking (an
  operation newly marked deprecated) -- and confirm the diff reports no change at all for the
  other edit, a request body field that became required, which none of its tracked fields cover;
* read the audit trail for every curation action.

Environment: ``CALIBER_BASE_URL`` (the server root) and ``CALIBER_TOKEN`` (a token for an admin
account). ``OPENAPI_TICKET_PORT`` (set by ``scripts/cookbook-env.sh``, default 8781) says where
the lab's stand-in Ticket API is listening; ``OPENAPI_TICKET_TOKEN`` is the bearer token both
CALIBER (as an ``env://`` secret reference) and the stand-in expect. ``TICKET_API_LEDGER`` (the
same path ``ticket_api.py`` was started with, or ``CALIBER_STATE_DIR``/``lab19-ticket-ledger.jsonl``
by default) is where the script reads the stand-in's own call log -- the proof that a refused
preview never reached it.
Optional ``LAB19_SUFFIX`` makes every name unique so the script can be re-run on a stack that
already holds an earlier run. ``--cleanup`` removes what a run created.

The CALIBER server must be started with the integrations flag on, the stand-in's host on
``CALIBER_EGRESS_ALLOWED_HOSTS``, and ``OPENAPI_TICKET_TOKEN`` set in its own environment; see the
README.
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
    Path(__file__).resolve().parents[4]
    / "docs-site/cookbooks/19-openapi-integration-governance-lab"
)
ASSETS = LAB_DIR / "assets"
API_PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPERATOR = ("lab19-operator", "lab19-operator-password")
REVIEWER = ("lab19-reviewer", "lab19-reviewer-password")
VIEWER = ("lab19-viewer", "lab19-viewer-password")
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


def ticket_api_base() -> str:
    port = os.environ.get("OPENAPI_TICKET_PORT", "8781")
    return f"http://127.0.0.1:{port}"


def ticket_api_token() -> str:
    return os.environ.get("OPENAPI_TICKET_TOKEN", "ticket-api-token")


def ledger_path() -> Path:
    """Where the stand-in Ticket API writes its own call log.

    The same path must be passed to ``ticket_api.py`` as ``TICKET_API_LEDGER`` when it is started
    (see the README) -- this is the script reading the *server's* record, not a log of its own.
    """
    explicit = os.environ.get("TICKET_API_LEDGER")
    if explicit:
        return Path(explicit)
    state = os.environ.get("CALIBER_STATE_DIR")
    if not state:
        raise SystemExit("set CALIBER_STATE_DIR (source the env.sh) or TICKET_API_LEDGER")
    return Path(state) / "lab19-ticket-ledger.jsonl"


def ledger_entries(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def executed(path: Path, method: str, path_prefix: str) -> int:
    """How many times the stand-in server itself executed a call (read from its own ledger)."""
    return sum(
        1
        for e in ledger_entries(path)
        if e["method"] == method and e["path"].startswith(path_prefix) and e["ok"]
    )


def bump_upstream_spec() -> None:
    httpx.post(f"{ticket_api_base()}/admin/bump-spec").raise_for_status()


def reset_upstream() -> None:
    """Put the stand-in back to pristine v1 (bump-spec is permanent for the process)."""
    httpx.post(f"{ticket_api_base()}/admin/reset").raise_for_status()


def login_client(
    base_url: str, user: str, password: str, *, timeout: float = 120.0
) -> CaliberClient:
    """Sign in as ``user`` and mint a personal access token (a script cannot reuse a cookie)."""
    api = base_url.rstrip("/") + API_PREFIX
    login = httpx.post(f"{api}/auth/login", json={"user_id": user, "password": password})
    login.raise_for_status()
    issued = httpx.post(
        f"{api}/auth/tokens", json={"name": f"cookbook-19-{user}"}, cookies=login.cookies
    )
    issued.raise_for_status()
    return CaliberClient(base_url, token=issued.json()["data"]["token"], timeout=timeout)


def ensure_accounts(admin: CaliberClient) -> None:
    for user, password in (OPERATOR, REVIEWER, VIEWER):
        try:
            admin.auth.accounts.create(user, password)
            say(f"  created account {user}")
        except CaliberError:
            admin.auth.accounts.update(user, password=password)
            say(f"  account {user} already existed; password reset to the lab value")


def workflow_yaml(tool_name: str) -> str:
    text = (ASSETS / "workflow/file-ticket.workflow.yaml").read_text()
    return text.replace("__TOOL_NAME__", tool_name)


def settle(caliber: CaliberClient, run_id: str, states: frozenset[str] = SETTLED) -> Any:
    deadline = time.monotonic() + 120
    run = caliber.workflows.runs.get(run_id)
    while time.monotonic() < deadline:
        run = caliber.workflows.runs.get(run_id)
        if run.status in states:
            return run
        time.sleep(1)
    raise AssertionError(f"run {run_id} did not settle (last status {run.status!r})")


def run(
    admin: CaliberClient,
    *,
    operator: CaliberClient,
    reviewer: CaliberClient,
    viewer: CaliberClient,
    ledger: Path,
    suffix: str = "",
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("")
    # A fresh v1 upstream and an empty ledger: the run's assertions (TCK-1, zero tickets, exact
    # ledger counts) describe a pristine start, and an earlier run leaves the upstream at v2.
    reset_upstream()
    spec_url = f"{ticket_api_base()}/openapi.yaml"
    auth_binding = {"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN"}

    say("== 1. workspace and people")
    project = admin.projects.create(
        f"openapi-ops{suffix}", description="Cookbook 19 OpenAPI governance"
    )
    admin.projects.add_member(project.project_id, OPERATOR[0], role="editor")
    admin.projects.add_member(project.project_id, REVIEWER[0], role="reviewer")
    admin.projects.add_member(project.project_id, VIEWER[0], role="viewer")
    summary["project_id"] = project.project_id
    say(
        f"  workspace {project.project_id}: admin owns it, operator edits, "
        "reviewer reviews, viewer reads"
    )

    with ExitStack() as scope:
        for client in (admin, operator, reviewer, viewer):
            scope.enter_context(client.workspace_scope(project.project_id))

        say("== 2. create the integration")
        expect_status(
            403,
            lambda: viewer.openapi_integrations.create(f"Ticketing{suffix}"),
            "a viewer cannot create an integration",
        )
        integration = admin.openapi_integrations.create(
            f"Ticketing{suffix}", description="The lab's stand-in Ticket API"
        )
        check(integration.status == "draft", f"created {integration.integration_id} (draft)")
        expect_status(
            409,
            lambda: admin.openapi_integrations.create(f"Ticketing{suffix}"),
            "creating the same name twice is refused",
        )
        iid = integration.integration_id

        say("== 3. import the spec from the upstream's own URL")
        probe = admin.openapi_integrations.validate_spec_source(iid, spec_url=spec_url)
        check(
            probe["reachable"] and probe["allowed"],
            "the upstream's spec URL is reachable and allowed",
        )
        version = admin.openapi_integrations.import_spec(
            iid, spec_url=spec_url, source_ref="ticket-api"
        )
        check(version.version_id.startswith("OAIV"), f"imported {version.version_id} from the URL")
        operations = {
            op.operation_key: op for op in admin.openapi_integrations.list_operations(iid)
        }
        check(
            {op.side_effect_level for op in operations.values()} == {"read", "write"}
            and operations["POST /tickets"].side_effect_level == "write",
            "three operations classified: two read, one write",
        )
        summary["integration_id"] = iid

        say("== 4. the egress guard refuses a host CALIBER was not told to trust")
        blocked = admin.openapi_integrations.validate_spec_source(
            iid, spec_url="http://169.254.169.254/openapi.yaml"
        )
        check(
            blocked["allowed"] is False,
            f"a cloud-metadata-range host is refused: {blocked['detail']}",
        )

        say("== 5. dependencies: one auto-wired, one advisory")
        deps = admin.openapi_integrations.list_dependencies(iid)
        auto = [d for d in deps if d.status == "auto_wired"]
        advisory = [d for d in deps if d.status not in {"auto_wired", "confirmed", "rejected"}]
        check(len(auto) >= 1, f"{len(auto)} auto-wired dependency(ies) from the create->get path")
        expect_status(
            409,
            lambda: admin.openapi_integrations.review_dependency(
                iid, auto[0].dependency_id, status="confirmed"
            ),
            "an auto-wired dependency cannot be reviewed",
        )
        if advisory:
            confirmed = admin.openapi_integrations.review_dependency(
                iid, advisory[0].dependency_id, status="confirmed"
            )
            check(
                confirmed.status == "confirmed",
                "the advisory (same-tag) dependency can be confirmed",
            )

        say("== 6. curate: a read-only tool pack and a governed write draft")
        expect_status(
            403,
            lambda: viewer.openapi_integrations.generate_tool_drafts(
                iid, operation_ids=[operations["GET /tickets"].operation_id]
            ),
            "a viewer cannot generate tool drafts",
        )
        read_pack = admin.openapi_integrations.generate_tool_drafts(
            iid,
            operation_ids=[
                operations["GET /tickets"].operation_id,
                operations["GET /tickets/{ticket_id}"].operation_id,
            ],
            group_as_pack=True,
            server_url=ticket_api_base(),
            auth_binding=auth_binding,
            allow_in_preview=True,
        )[0]
        check(
            sorted([read_pack.operation_id, *read_pack.additional_operation_ids])
            == sorted(
                [
                    operations["GET /tickets"].operation_id,
                    operations["GET /tickets/{ticket_id}"].operation_id,
                ]
            ),
            f"{read_pack.draft_id}: a tool pack binding both read operations",
        )
        write_draft = admin.openapi_integrations.generate_tool_drafts(
            iid,
            operation_ids=[operations["POST /tickets"].operation_id],
            server_url=ticket_api_base(),
            auth_binding=auth_binding,
        )[0]
        check(
            write_draft.side_effect_level == "write" and write_draft.requires_approval,
            f"{write_draft.draft_id}: the write draft defaults to requires_approval",
        )
        summary["read_pack_draft_id"] = read_pack.draft_id
        summary["write_draft_id"] = write_draft.draft_id

        say("== 7. preview: the read pack runs for real; the write draft is refused")
        listed = reviewer.openapi_integrations.preview_tool_draft(
            iid, read_pack.draft_id, input={"operation": "GET /tickets"}
        )
        check(
            listed["result"]["status_code"] == 200 and listed["result"]["json"]["total_count"] == 0,
            "a reviewer can preview the read pack's listTickets operation: zero tickets exist yet",
        )
        check(executed(ledger, "GET", "/tickets") == 1, "the upstream's own log shows one real GET")
        got_by_id_before_any_exist = reviewer.openapi_integrations.preview_tool_draft(
            iid,
            read_pack.draft_id,
            input={
                "operation": "GET /tickets/{ticket_id}",
                "path_params": {"ticket_id": "TCK-404"},
            },
        )
        check(
            got_by_id_before_any_exist["result"]["status_code"] == 404,
            "the same pack also serves getTicket, selected by operation key: 404 for an unknown id",
        )
        expect_status(
            409,
            lambda: admin.openapi_integrations.preview_tool_draft(
                iid, write_draft.draft_id, input={"body": {"title": "should not run"}}
            ),
            "the write draft cannot be previewed until allow_in_preview is set",
        )
        check(
            executed(ledger, "POST", "/tickets") == 0,
            "the upstream never saw the refused preview",
        )

        say("== 8. break-glass: opt the write draft into preview, then re-gate it")
        admin.openapi_integrations.update_tool_draft(
            iid, write_draft.draft_id, allow_in_preview=True
        )
        direct = admin.openapi_integrations.preview_tool_draft(
            iid,
            write_draft.draft_id,
            input={"body": {"title": "Filed from the Playground-equivalent"}},
        )
        ticket = direct["result"]["json"]
        check(
            direct["result"]["status_code"] == 201 and ticket["ticket_id"] == "TCK-1",
            f"direct preview ran: {ticket['ticket_id']} "
            "(a deliberate, explicit opt-in -- the OpenAPI analogue of "
            "cookbook 05's break-glass Playground)",
        )
        check(
            executed(ledger, "POST", "/tickets") == 1, "the upstream's log shows the one real write"
        )
        admin.openapi_integrations.update_tool_draft(
            iid, write_draft.draft_id, allow_in_preview=False
        )
        expect_status(
            409,
            lambda: admin.openapi_integrations.preview_tool_draft(
                iid, write_draft.draft_id, input={"body": {"title": "should be refused again"}}
            ),
            "turning allow_in_preview back off re-gates it",
        )

        say("== 9. publish both drafts into the tool registry")
        expect_status(
            403,
            lambda: reviewer.openapi_integrations.publish_tool_draft(iid, read_pack.draft_id),
            "a reviewer (resource.execute, not resource.publish) cannot publish",
        )
        published_pack = operator.openapi_integrations.publish_tool_draft(
            iid, read_pack.draft_id, name=f"ticket_reader{suffix}", version="1.0"
        )
        published_write = operator.openapi_integrations.publish_tool_draft(
            iid, write_draft.draft_id, name=f"create_ticket{suffix}", version="1.0"
        )
        check(
            published_pack["tool"]["execution_backend"] == "openapi_http"
            and published_write["draft"]["requires_approval"],
            "both drafts are now ordinary registry tools; the write one still requires approval",
        )
        read_tool = admin.tools.get(published_pack["tool"]["tool_id"])
        write_tool = admin.tools.get(published_write["tool"]["tool_id"])
        check(
            read_tool.name == f"ticket_reader{suffix}" and write_tool.requires_approval,
            "GET /tools/{tool_id} shows them in the standard registry",
        )
        summary["read_tool_id"] = read_tool.tool_id
        summary["write_tool_id"] = write_tool.tool_id

        say("== 10. the approval gate in a workflow")
        text = workflow_yaml(write_tool.name)
        workflow = admin.workflows.import_workflow(manifest_yaml=text, name=f"file-ticket{suffix}")
        version_row = admin.workflows.versions.list(workflow.workflow_id)[0]
        first = admin.workflows.runs.submit(
            workflow_version_id=version_row.version_id,
            input=json.dumps({"body": {"title": "Filed by a workflow, after approval"}}),
        )
        waiting = settle(admin, first.workflow_run_id)
        check(
            waiting.status == "waiting_approval"
            and waiting.extra["current_node_id"] == "call_tool",
            "the run paused at the call_tool node",
        )
        check(executed(ledger, "POST", "/tickets") == 1, "nothing written while the run waited")
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
        expect_status(
            403,
            lambda: operator.workflows.runs.approve(first.workflow_run_id, reason="editor"),
            "an editor (resource.write.runtime, not resource.approve) cannot approve either",
        )
        reviewer.workflows.runs.approve(first.workflow_run_id, reason="matches the release process")
        check(executed(ledger, "POST", "/tickets") == 1, "approving alone does not run the tool")
        admin.workflows.runs.resume(first.workflow_run_id)
        done = settle(admin, first.workflow_run_id, FINISHED)
        check(done.status == "completed", f"after Resume the run {done.status}")
        check(
            executed(ledger, "POST", "/tickets") == 2, "the workflow's write executed exactly once"
        )
        summary["approved_run"] = done.workflow_run_id

        say("== 11. reimport and diff against a changed spec")
        bump_upstream_spec()
        reimported = admin.openapi_integrations.reimport(iid)
        diff = reimported["diff"]
        check(
            diff["summary"]
            == {
                "added_count": 0,
                "removed_count": 0,
                "changed_count": 2,
                "unchanged_count": 1,
                "breaking_count": 1,
            },
            f"reimported {reimported['version']['version_id']}: "
            "changed 2 (1 breaking), 1 unchanged",
        )
        [breaking] = diff["breaking"]
        check(
            breaking
            == {"operation_key": "GET /tickets/{ticket_id}", "reason": "operation was deprecated"},
            f"the one breaking change the diff catches: "
            f"{breaking['operation_key']} {breaking['reason']}",
        )
        create_changes = next(
            (c for c in diff["changed"] if c["operation_key"] == "POST /tickets"), None
        )
        check(
            create_changes is None,
            "what the diff does NOT catch: createTicket's body gaining a required 'priority' field "
            "is not a tracked field, so the diff reports no change for it at all",
        )
        summary["diff"] = diff["summary"]

        say("== 12. credential binding validation")
        resolvable = admin.openapi_integrations.validate_credential_binding(
            iid, auth_binding=auth_binding
        )
        check(
            resolvable["valid"] is True,
            "the bearer secret_ref resolves (OPENAPI_TICKET_TOKEN is set)",
        )
        unresolvable = admin.openapi_integrations.validate_credential_binding(
            iid, auth_binding={"kind": "bearer", "secret_ref": "env://OPENAPI_TICKET_TOKEN_TYPO"}
        )
        check(unresolvable["valid"] is False, "a typo'd secret reference does not resolve")

        say("== 13. audit trail")
        entries = admin.audit.list(limit=200)
        on_integration = {e.action for e in entries if e.entity_id == iid}
        for action in (
            "create_openapi_integration",
            "import_openapi_integration_version",
            "reimport_openapi_integration_version",
            "generate_openapi_tool_drafts",
        ):
            check(action in on_integration, f"audit log has {action} on the integration")
        on_a_draft = {
            e.action for e in entries if e.entity_id in {read_pack.draft_id, write_draft.draft_id}
        }
        for action in (
            "update_openapi_tool_draft",
            "preview_openapi_tool_draft",
            "publish_openapi_tool_draft",
        ):
            check(action in on_a_draft, f"audit log has {action} on a draft")

        say("== 14. the gallery starter")
        recipe = {item.id: item for item in admin.cookbooks.list()}["19"]
        check(len(recipe.steps) >= 1, f"the gallery lists {len(recipe.steps)} step(s) for this lab")
        installed = admin.cookbooks.install(
            "19",
            name=f"Cookbook 19 starter{suffix}",
            acknowledge_prerequisites=bool(recipe.prerequisites),
        )
        manifest = installed["version"]["manifest"]
        node_types = sorted(n["type"] for n in manifest["nodes"].values())
        check(
            node_types == ["agent", "note", "output", "start"] and not manifest["tools"],
            "the starter is a generic single-agent draft with no tools: it does not use the "
            "OpenAPI-derived tools",
        )
        summary["starter_workflow_id"] = installed["workflow"]["workflow_id"]

    return summary


def cleanup(admin: CaliberClient, suffix: str) -> None:
    wanted_workflows = {f"file-ticket{suffix}", f"Cookbook 19 starter{suffix}"}
    for workflow in admin.workflows.list():
        if workflow.name in wanted_workflows:
            admin.workflows.delete(workflow.workflow_id)
            say(f"deleted workflow {workflow.name}")
    wanted_tools = {f"ticket_reader{suffix}", f"create_ticket{suffix}"}
    for tool in admin.tools.list():
        if tool.name in wanted_tools:
            admin.tools.update(tool.tool_id, status="archived")
            say(f"archived tool {tool.name}")
    for project in admin.projects.list():
        if project.name == f"openapi-ops{suffix}":
            with admin.workspace_scope(project.project_id):
                for integration in admin.openapi_integrations.list(status="all"):
                    if integration.name == f"Ticketing{suffix}":
                        admin.openapi_integrations.archive(integration.integration_id)
                        say(f"archived integration {integration.name}")
            admin.projects.archive(project.project_id)
            say(f"archived workspace {project.name}")


def main() -> None:
    base_url = os.environ["CALIBER_BASE_URL"]
    suffix = os.environ.get("LAB19_SUFFIX", "")
    admin = CaliberClient(base_url, token=os.environ["CALIBER_TOKEN"], timeout=180)
    if "--cleanup" in sys.argv:
        cleanup(admin, suffix)
        return
    ensure_accounts(admin)
    result = run(
        admin,
        operator=login_client(base_url, *OPERATOR),
        reviewer=login_client(base_url, *REVIEWER),
        viewer=login_client(base_url, *VIEWER),
        ledger=ledger_path(),
        suffix=suffix,
    )
    say("== done")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
