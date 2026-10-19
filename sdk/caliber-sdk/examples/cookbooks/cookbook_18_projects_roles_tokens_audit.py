"""Cookbook 18: run a team's project, roles, access tokens and audit trail for real.

A new team lead has to answer four questions before anyone else touches CALIBER:

* Who can do what in this project?  (project roles: owner, editor, reviewer, viewer)
* How does automation act as one person inside one project?  (a project-bound access token)
* What happens when a token is revoked, expires, or is used in the wrong project?
* Where is the record of all of it?  (the audit log, the release timeline, readiness)

This script answers them against a live server and exits 0 only when every documented outcome
was observed. The expectations are data, not code: ``team-roster.csv`` (nine people),
``role-matrix.csv`` (the status code each kind of person must get for each action) and
``failure-scenarios.json`` (the refusals this lab provokes), all under
``docs-site/cookbooks/18-projects-roles-tokens-audit/assets/dataset``.

Run it (see the lab README for the full setup, including the accounts and platform scopes)::

    export CALIBER_BASE_URL=http://127.0.0.1:5001      # the deployment URL, not the /ajax-api path
    export CALIBER_TOKEN=calpat_...                    # an ADMIN personal access token
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \\
        -m examples.cookbooks.cookbook_18_projects_roles_tokens_audit

Nothing here needs a real model. The one step that does, an evaluation, is expected to answer 400
"requires a real LLM provider" on a simulated deployment: that answer still proves the caller
passed the project-role gate, which is all this lab is about.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from examples.cookbooks._helpers import configuration_blockers, get_recipe

REPO_ROOT = Path(__file__).resolve().parents[4]
DATASET_DIR = (
    REPO_ROOT / "docs-site" / "cookbooks" / "18-projects-roles-tokens-audit" / "assets" / "dataset"
)

#: The demo password every roster account gets. It is lab-only and public in this file: never
#: reuse it, and never create these accounts on a deployment anyone else can reach.
DEFAULT_LAB_PASSWORD = "lab18-demo-passphrase"

#: How long the short-lived token lives before the script checks that it expired.
EXPIRY_SECONDS = 3

#: The roster's ``platform_scopes`` column, as the scopes ``GET /auth/session`` must report.
PLATFORM_SCOPE_SETS: dict[str, frozenset[str]] = {
    "none": frozenset({"caliber.viewer"}),
    "operator": frozenset({"caliber.viewer", "caliber.operator"}),
    "operator+approver": frozenset({"caliber.viewer", "caliber.operator", "caliber.approver"}),
}

#: The environment variables that give each roster account its platform scopes.
SCOPE_ENV_HINT = (
    "restart CALIBER with CALIBER_OPERATOR_USERS and CALIBER_APPROVER_USERS listing the roster "
    "(the lab README, Prerequisites and setup, has the exact values)"
)

Report = Callable[[str], None]
LoginAttempt = Callable[[str, str], tuple[int, str]]
SignIn = Callable[[str, str], str]
UserClient = Callable[[str, str | None], CaliberClient]
HttpPost = Callable[..., httpx.Response]
Sleep = Callable[[float], None]


class VerificationError(AssertionError):
    """A documented outcome was not observed."""


class SetupError(RuntimeError):
    """The deployment is not set up the way the lab needs (a fix, not a defect)."""


# ---------------------------------------------------------------------------
# The dataset files
# ---------------------------------------------------------------------------


def load_roster(directory: Path = DATASET_DIR) -> list[dict[str, str]]:
    with (directory / "team-roster.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_matrix(directory: Path = DATASET_DIR) -> list[dict[str, str]]:
    with (directory / "role-matrix.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def load_failure_scenarios(directory: Path = DATASET_DIR) -> list[dict[str, Any]]:
    document = json.loads((directory / "failure-scenarios.json").read_text(encoding="utf-8"))
    return list(document["scenarios"])


def column_for(person: Mapping[str, str]) -> str:
    """Which ``role-matrix.csv`` column describes this roster person."""
    role = person["project_role"]
    if role == "none":
        return "non_member"
    if role == "viewer" and person["platform_scopes"] == "none":
        return "viewer_no_operator"
    return role


def status_allowed(expected: str, actual: int) -> bool:
    """``expected`` is one status, or several joined with ``|`` (any of them is a pass)."""
    return actual in {int(part) for part in expected.split("|")}


# ---------------------------------------------------------------------------
# Small helpers over the SDK
# ---------------------------------------------------------------------------


def call(
    client: CaliberClient,
    method: str,
    path: str,
    *,
    project: str | None = None,
    json_body: Any = None,
    params: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    """One API call, returning ``(status, data-or-detail)`` instead of raising on 4xx.

    The role matrix is about the status code itself, so it goes through the SDK's transport (the
    same one the typed methods use, with the same headers, retries and error mapping) rather than
    a typed method that would hide the 201-versus-200 distinction.
    """
    try:
        response = client.raw.raw.request(
            method, path, json=json_body, params=params, project=project
        )
    except CaliberAPIError as exc:
        return exc.status_code, exc.detail or str(exc)
    return response.status_code, response.data


def _api(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/ajax-api/2.0/mlflow/caliber{path}"


def default_login_attempt(base_url: str) -> LoginAttempt:
    """``POST /auth/login``, returning ``(status, session token or detail)``.

    When the deployment enforces CSRF, login is a write like any other, so it carries the
    anonymous token ``GET /csrf`` issues for exactly this purpose.
    """

    def attempt(user_id: str, password: str) -> tuple[int, str]:
        headers: dict[str, str] = {}
        csrf = httpx.get(_api(base_url, "/csrf"), timeout=30)
        if csrf.status_code == 200:
            body = csrf.json().get("data") or {}
            if body.get("enabled") and body.get("token"):
                headers["X-CALIBER-CSRF"] = str(body["token"])
        response = httpx.post(
            _api(base_url, "/auth/login"),
            json={"user_id": user_id, "password": password},
            headers=headers,
            timeout=30,
        )
        if response.status_code == 200:
            return 200, str(response.cookies["caliber_session"])
        try:
            return response.status_code, str(response.json().get("detail", response.text))
        except ValueError:
            return response.status_code, response.text

    return attempt


def sign_in_with(attempt: LoginAttempt) -> SignIn:
    """A sign-in that returns the session token, which a script uses as a bearer token.

    The server also sets it as an HttpOnly cookie for the browser; the bearer form is the
    supported path for automation.
    """

    def sign_in(user_id: str, password: str) -> str:
        status, value = attempt(user_id, password)
        if status != 200:
            raise SetupError(
                f"could not sign in as {user_id!r}: {status} {value}. An account left by an "
                "earlier run may use another password: reset it under Administration, or set "
                "CALIBER_LAB18_PASSWORD"
            )
        return value

    return sign_in


def default_user_client(base_url: str, opened: list[CaliberClient]) -> UserClient:
    def user_client(token: str, project: str | None = None) -> CaliberClient:
        client = CaliberClient(base_url, token=token, project=project)
        opened.append(client)
        return client

    return user_client


def find_caliberctl() -> list[str] | None:
    """The ``caliberctl`` command of this interpreter's environment, if it is installed."""
    beside = Path(sys.executable).parent / "caliberctl"
    if beside.exists():
        return [str(beside)]
    found = shutil.which("caliberctl")
    return [found] if found else None


def run_caliberctl(command: list[str], base_url: str, token: str, *args: str) -> Any:
    environment = {**os.environ, "CALIBER_BASE_URL": base_url, "CALIBER_TOKEN": token}
    environment.pop("CALIBER_PROJECT", None)
    completed = subprocess.run(
        [*command, "--json", *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationError(
            f"caliberctl {' '.join(args)} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# The lab
# ---------------------------------------------------------------------------


def run(
    caliber: CaliberClient,
    *,
    base_url: str,
    password: str = DEFAULT_LAB_PASSWORD,
    run_key: str | None = None,
    login_attempt: LoginAttempt | None = None,
    user_client: UserClient | None = None,
    dataset_dir: Path = DATASET_DIR,
    use_caliberctl: bool = True,
    http_post: HttpPost = httpx.post,
    sleep: Sleep | None = None,
    report: Report | None = None,
) -> dict[str, object]:
    """Run the whole lab. ``caliber`` is an ADMIN client (an admin personal access token)."""
    say: Report = report or (lambda _line: None)
    key = run_key or uuid.uuid4().hex[:6]
    opened: list[CaliberClient] = []
    attempt_login = login_attempt or default_login_attempt(base_url)
    sign_in = sign_in_with(attempt_login)
    make_client = user_client or default_user_client(base_url, opened)
    roster = load_roster(dataset_dir)
    matrix = load_matrix(dataset_dir)
    scenarios = load_failure_scenarios(dataset_dir)
    people = {person["user_id"]: person for person in roster}
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        say(f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            failures.append(f"{label} {detail}".strip())

    try:
        # -- 1. The deployment is healthy, and says what is simulated -----------------------
        admin = caliber.me.get()
        check("the token is an admin", bool(admin.is_admin), admin.user_id)
        if not admin.is_admin:
            raise SetupError("CALIBER_TOKEN must be an admin personal access token")
        health = caliber.health()
        check("GET /health reports ok", health.get("status") == "ok", str(health))
        readiness = caliber.readiness()
        check(
            "GET /readiness is ready",
            readiness.get("ready") is True,
            f"blockers: {readiness.get('blockers')}",
        )
        simulated = sorted(readiness.get("simulated") or [])
        providers: dict[str, Any] = next(
            (item for item in readiness.get("checks", []) if item.get("name") == "providers"), {}
        )
        say(f"     simulated providers: {simulated or 'none (all real)'}")
        check(
            "readiness names every simulated provider",
            (not simulated and readiness.get("all_real") is True)
            or (
                bool(simulated)
                and providers.get("ready") is False
                and "simulated" in str(providers)
            ),
            str(providers.get("detail")),
        )
        installed_workflow = ""
        cookbooks_enabled = bool(caliber.capabilities()["features"]["cookbooks"]["enabled"])

        # -- 2. Accounts: an admin creates them, each one signs in ---------------------------
        listed = {account.user_id: account for account in caliber.auth.accounts.list()}
        existing = set(listed)
        created = [user for user in people if user not in existing]
        for user in created:
            caliber.auth.accounts.create(user, password)
        for user in people:
            if user in listed and listed[user].disabled:
                caliber.auth.accounts.update(user, disabled=False)  # left disabled by --cleanup
        check(
            "every roster account exists",
            True,
            f"{len(created)} created, {len(people) - len(created)} already there",
        )

        tokens: dict[str, str] = {user: sign_in(user, password) for user in people}
        clients: dict[str, CaliberClient] = {
            user: make_client(token, None) for user, token in tokens.items()
        }
        for user, person in people.items():
            session = clients[user].auth.session()
            expected_scopes = PLATFORM_SCOPE_SETS[person["platform_scopes"]]
            if set(session.scopes) != expected_scopes:
                raise SetupError(
                    f"{user} holds {sorted(session.scopes)} but the roster needs "
                    f"{sorted(expected_scopes)}: {SCOPE_ENV_HINT}"
                )
        check("every account holds exactly the platform scopes the roster lists", True)

        # -- 3. The project: maya creates it, then adds the team ------------------------------
        lead = clients["maya"]
        project = lead.projects.create(
            f"lab18-support-copilot-{key}", description="Support copilot (cookbook 18)"
        )
        project_id = project.project_id
        check(
            "the creator is the project's primary owner",
            project.owner == "maya" and project.access_role == "owner",
        )
        for person in roster:
            if person["project_role"] == "none":
                continue
            if person["user_id"] == "maya":
                continue
            lead.projects.add_member(project_id, person["user_id"], role=person["project_role"])
        members = {member.user_id: member.role for member in lead.projects.list_members(project_id)}
        wanted = {
            person["user_id"]: person["project_role"]
            for person in roster
            if person["project_role"] != "none"
        }
        check("the member list is exactly the roster", members == wanted, f"{len(members)} members")
        wrong_roles = []
        for user, person in people.items():
            if person["project_role"] == "none":
                status, _ = call(
                    clients[user], "GET", f"/projects/{project_id}", project=project_id
                )
                if status != 404:
                    wrong_roles.append(f"{user}:{status}")
                continue
            seen = clients[user].projects.get(project_id).access_role
            if seen != person["project_role"]:
                wrong_roles.append(f"{user}:{seen}")
        check(
            "every person sees the role the roster gave them (the outsider sees 404)",
            not wrong_roles,
            ", ".join(wrong_roles),
        )

        # Fixtures the matrix acts on, created by the owner so every person starts equal.
        target_prompt = f"lab18-{key}-matrix-target"
        call(
            lead,
            "POST",
            "/prompts",
            project=project_id,
            json_body={
                "name": target_prompt,
                "template": "Answer the customer question: {{question}}",
            },
        )
        matrix_dataset = lead.raw.raw.request(
            "POST",
            "/eval-datasets",
            json={"name": f"lab18-{key}-matrix-set", "owner": "maya"},
            project=project_id,
        ).data["dataset_id"]
        lead.raw.raw.request(
            "POST",
            f"/eval-datasets/{matrix_dataset}/examples",
            json={
                "input": {"question": "Where is my order?"},
                "expected": {"answer": "Check the tracking link"},
            },
            project=project_id,
        )

        # -- 4. The role matrix: every person x every action, from the data file --------------
        def attempt(user: str, action_id: str) -> tuple[int, Any]:
            client = clients[user]
            tag = f"lab18-{key}-{user}"
            requests: dict[str, tuple[str, str, Any]] = {
                "project.read": ("GET", f"/projects/{project_id}", None),
                "members.list": ("GET", f"/projects/{project_id}/members", None),
                "dataset.create": ("POST", "/eval-datasets", {"name": f"{tag}-set", "owner": user}),
                "prompt.create": (
                    "POST",
                    "/prompts",
                    {"name": f"{tag}-prompt", "template": "Reply to {{customer}}."},
                ),
                "prompt.promote": (
                    "POST",
                    f"/prompts/{target_prompt}/aliases/prod",
                    {"version": 1},
                ),
                "evaluation.run": (
                    "POST",
                    "/evaluations",
                    {
                        "dataset_id": matrix_dataset,
                        "scorers": ["non_empty"],
                        "predict_target": "prompt",
                        "subject_ref": f"{target_prompt}@1",
                        "label": f"lab18 {user}",
                    },
                ),
                "project.update": (
                    "PATCH",
                    f"/projects/{project_id}",
                    {"description": f"edited by {user}"},
                ),
                "members.update": (
                    "PATCH",
                    f"/projects/{project_id}/members/wen",
                    {"role": "viewer"},
                ),
            }
            method, path, body = requests[action_id]
            return call(client, method, path, project=project_id, json_body=body)

        unknown_actions = [
            row["action_id"] for row in matrix if not _known_action(row["action_id"])
        ]
        if unknown_actions:
            raise VerificationError(
                f"role-matrix.csv names actions the script cannot run: {unknown_actions}"
            )
        cells = 0
        wrong_cells: list[str] = []
        outcomes: Counter[str] = Counter()
        for person in roster:
            user = person["user_id"]
            column = column_for(person)
            for row in matrix:
                status, detail = attempt(user, row["action_id"])
                cells += 1
                expected = row[column]
                if status_allowed(expected, status):
                    outcomes[f"{column}:{status}"] += 1
                else:
                    wrong_cells.append(
                        f"{user}/{row['action_id']}: expected {expected}, got {status} "
                        f"({str(detail)[:80]})"
                    )
        check(
            f"the role matrix holds ({cells} requests: {len(roster)} people "
            f"x {len(matrix)} actions)",
            not wrong_cells,
            "; ".join(wrong_cells),
        )

        # -- 5. A project-bound token, used from the SDK and from caliberctl ------------------
        editor = clients["priya"]
        issued = editor.auth.tokens.create(
            f"support-ci-{key}",
            scopes=["caliber.operator"],
            project_id=project_id,
        )
        check(
            "the token carries its project binding",
            issued.project_id == project_id,
            issued.token_id,
        )
        listed_tokens = editor.raw.get("/auth/tokens")["tokens"]
        check(
            "the secret is only on the issuing response, never in the list",
            bool(issued.token) and all("token" not in row for row in listed_tokens),
        )
        bound = make_client(issued.token, None)
        who = bound.me.get()
        check("the bound token acts as its owner", who.user_id == "priya", who.user_id)
        check(
            "with no project named, it acts in its bound project",
            bound.projects.get(project_id).access_role == "editor",
        )

        prompt_name = f"lab18-{key}-support-reply"
        bound.prompts.create(
            prompt_name,
            "Reply to the customer politely.\n\nQuestion: {{question}}",
            commit_message="v1",
        )
        release = bound.prompts.promote(prompt_name, 1)
        check(
            "the prompt is released to @prod by the token",
            release.get("release_status") == "applied",
            str(release.get("operation_id")),
        )

        if cookbooks_enabled:
            recipe = get_recipe(caliber, "18")
            blockers = configuration_blockers(recipe)
            check(
                "cookbook 18 is in the catalog with its guided steps",
                bool(recipe.steps),
                f"{len(recipe.steps)} steps",
            )
            if not blockers:
                installed = bound.cookbooks.install(
                    "18",
                    name=f"Cookbook 18 - Projects, roles, tokens and audit ({key})",
                    acknowledge_prerequisites=bool(recipe.prerequisites),
                )
                installed_workflow = str(installed["workflow"].get("workflow_id"))
                check(
                    "the lab installs into the project through the bound token",
                    installed["workflow"].get("project_id") == project_id,
                    installed_workflow,
                )

        # The prompt inventory follows the project it is asked for: a member sees the project's
        # prompts (and the agent prompt of the workflow just installed), a non-member sees none
        # of them.
        def inventory(user: str) -> list[str]:
            _status, rows = call(clients[user], "GET", "/prompts", project=project_id)
            return [str(row.get("agent_id")) for row in rows] if isinstance(rows, list) else []

        project_prompts = {target_prompt, prompt_name}
        seen_by_viewer = inventory("victor")
        check(
            "a viewer's prompt inventory lists the project's prompts",
            project_prompts <= set(seen_by_viewer),
            f"{len(seen_by_viewer)} prompts",
        )
        leaked = [
            name
            for name in inventory("nora")
            if name in project_prompts or (installed_workflow and installed_workflow in name)
        ]
        check(
            "a non-member's prompt inventory shows nothing from the project",
            not leaked,
            ", ".join(leaked),
        )

        if use_caliberctl:
            command = find_caliberctl()
            if command is None:
                say("skip caliberctl is not installed in this environment")
            else:
                whoami = run_caliberctl(command, base_url, issued.token, "whoami")
                check(
                    "caliberctl whoami sees the same identity",
                    whoami.get("user_id") == "priya",
                    str(whoami.get("scopes")),
                )
                listed = run_caliberctl(command, base_url, issued.token, "token", "list")
                rows = listed if isinstance(listed, list) else listed.get("tokens", [])
                check(
                    "caliberctl token list shows the bound token",
                    any(
                        row.get("token_id") == issued.token_id
                        and row.get("project_id") == project_id
                        for row in rows
                    ),
                )
                prompts = run_caliberctl(
                    command, base_url, issued.token, "--project", project_id, "prompt", "list"
                )
                names = [
                    item.get("name") or item.get("prompt_name") or item.get("agent_id")
                    for item in (
                        prompts if isinstance(prompts, list) else prompts.get("prompts", [])
                    )
                ]
                check("caliberctl prompt list shows the released prompt", prompt_name in names)

        # -- 6. The release timeline and the live board show the release ----------------------
        timeline = bound.raw.get(
            "/releases/timeline", params={"entity_type": "prompt", "limit": 200}
        )
        mine = [row for row in timeline if row.get("entity_id") == prompt_name]
        check(
            "the release timeline has exactly one promote event for the prompt, by priya",
            len(mine) == 1
            and mine[0]["action"] == "promote_prompt"
            and mine[0]["actor"] == "priya",
            str(mine[0].get("details")) if mine else "none",
        )
        live = bound.releases.live()
        live_row = next((row for row in live if row.get("artifact_id") == prompt_name), None)
        check(
            "the live board lists the prompt at v1 on @prod",
            live_row is not None
            and live_row.get("version_id") == "v1"
            and live_row.get("alias") == "prod",
            str(live_row),
        )

        # -- 7. A second project, and the token used outside the project it is bound to ------
        other = lead.projects.create(
            f"lab18-billing-assistant-{key}",
            description="A second project the token is not bound to",
        )
        failure_results: dict[str, str] = {}

        def verdict(scenario_id: str, status: int, detail: Any) -> None:
            scenario = next(item for item in scenarios if item["id"] == scenario_id)
            ok = (
                status == scenario["expected_status"]
                and str(scenario["detail_contains"]).lower() in str(detail).lower()
            )
            check(f"{scenario_id} {scenario['title']}", ok, f"{status} {str(detail)[:90]}")
            failure_results[scenario_id] = str(status)

        verdict(
            "F01",
            *call(
                clients["omar"],
                "DELETE",
                f"/projects/{project_id}/members/maya",
                project=project_id,
            ),
        )
        verdict(
            "F02",
            *call(
                clients["omar"],
                "PATCH",
                f"/projects/{project_id}/members/maya",
                project=project_id,
                json_body={"role": "viewer"},
            ),
        )
        verdict(
            "F03",
            *call(
                clients["priya"],
                "PATCH",
                f"/projects/{project_id}/members/priya",
                project=project_id,
                json_body={"role": "owner"},
            ),
        )
        verdict(
            "F04",
            *call(
                clients["quinn"],
                "POST",
                f"/projects/{project_id}/members",
                project=project_id,
                json_body={"user_id": "nora", "role": "viewer"},
            ),
        )
        verdict(
            "F05",
            *call(
                clients["maya"],
                "PATCH",
                f"/projects/{project_id}/members/victor",
                project=project_id,
                json_body={"role": "owner"},
            ),
        )
        verdict(
            "F06",
            *call(
                clients["maya"],
                "POST",
                f"/projects/{project_id}/members",
                project=project_id,
                json_body={"user_id": "nora", "role": "admin"},
            ),
        )
        verdict(
            "F07",
            *call(
                clients["priya"],
                "POST",
                "/auth/tokens",
                json_body={"name": "too-much", "scopes": ["caliber.admin"]},
            ),
        )
        verdict(
            "F08",
            *call(
                clients["nora"],
                "POST",
                "/auth/tokens",
                json_body={"name": "not-mine", "project_id": project_id},
            ),
        )
        viewer_scoped = clients["maya"].auth.tokens.create(
            f"viewer-only-{key}", scopes=["caliber.viewer"]
        )
        verdict(
            "F09",
            *call(
                make_client(viewer_scoped.token, None),
                "POST",
                "/prompts",
                project=project_id,
                json_body={"name": f"lab18-{key}-denied", "template": "x {{y}}"},
            ),
        )
        clients["maya"].auth.tokens.revoke(viewer_scoped.token_id)
        verdict(
            "F10", *call(bound, "GET", f"/projects/{other.project_id}", project=other.project_id)
        )
        check(
            "the bound token still works in its own project after the refusal",
            bound.projects.get(project_id).project_id == project_id,
        )

        # Revocation: priya revokes the bound token; it stops working at once.
        revoked = editor.auth.tokens.revoke(issued.token_id)
        check("revoking reports a live token was revoked", revoked is True)
        verdict("F11", *call(bound, "GET", "/projects", project=None))
        check(
            "whoami does not raise for a revoked token: it answers anonymous",
            bound.me.get().is_anonymous,
        )

        # Expiry: a token whose expires_at passes stops authenticating.
        soon = (datetime.now(timezone.utc) + timedelta(seconds=EXPIRY_SECONDS)).isoformat()
        short = editor.auth.tokens.create(f"short-lived-{key}", expires_at=soon)
        short_client = make_client(short.token, None)
        status_before, _ = call(short_client, "GET", "/projects")
        check("a token works before it expires", status_before == 200, str(status_before))
        (sleep or time.sleep)(EXPIRY_SECONDS + 1)
        verdict("F12", *call(short_client, "GET", "/projects"))
        verdict("F13", *call(make_client("calpat_not-a-real-token", None), "GET", "/projects"))

        verdict("F14", *attempt_login("maya", "not-the-password-123"))
        verdict("F15", *call(clients["maya"], "GET", "/audit-log", params={"limit": 1}))

        # CSRF is off by default. When the deployment turns it on, a write without the header
        # is refused, and the SDK (which fetches a token and replays the write) is not affected.
        csrf = caliber.raw.get("/csrf")
        csrf_enabled = bool(csrf.get("enabled"))
        if csrf_enabled:
            plain = http_post(
                _api(base_url, f"/projects/{project_id}/members"),
                json={"user_id": "nora", "role": "viewer"},
                headers={
                    "Authorization": f"Bearer {tokens['maya']}",
                    "X-CALIBER-Project": project_id,
                },
                timeout=30,
            )
            verdict("F16", plain.status_code, plain.json().get("detail", ""))
            lead.projects.add_member(project_id, "nora", role="viewer")
            lead.projects.remove_member(project_id, "nora")
            check("the SDK replays a write with a CSRF token and it succeeds", True)
        else:
            say("skip F16: CSRF enforcement is off on this deployment (the default)")

        # -- 8. Offboarding: removing a member takes the project away from them ---------------
        removed = lead.projects.remove_member(project_id, "victor")
        status_after, _ = call(
            clients["victor"], "GET", f"/projects/{project_id}", project=project_id
        )
        check(
            "a removed member gets 404 straight away",
            removed is True and status_after == 404,
            str(status_after),
        )
        remaining = {member.user_id for member in lead.projects.list_members(project_id)}
        check(
            "the member list no longer shows them",
            "victor" not in remaining and len(remaining) == len(wanted) - 1,
        )

        # -- 9. The audit log: every one of those actions, filterable ------------------------
        project_rows = caliber.audit.list(entity_id=project_id, limit=500)
        by_action = Counter(row.action for row in project_rows)
        check(
            "audit: create_project by maya for this project",
            any(row.action == "create_project" and row.actor == "maya" for row in project_rows),
        )
        check(
            f"audit: add_project_member once per member ({len(wanted) - 1} added by maya)",
            by_action["add_project_member"] == len(wanted) - 1 + (1 if csrf_enabled else 0),
            str(by_action["add_project_member"]),
        )
        added = {
            row.details.get("user_id"): row.details.get("role")
            for row in project_rows
            if row.action == "add_project_member"
        }
        check(
            "audit: the roles recorded match the roster",
            all(added.get(user) == role for user, role in wanted.items() if user != "maya"),
        )
        check(
            "audit: update_project_member rows name the owners who edited",
            {row.actor for row in project_rows if row.action == "update_project_member"}
            == {"maya", "omar"},
        )
        check(
            "audit: update_project rows name the owners and editors who edited",
            {row.actor for row in project_rows if row.action == "update_project"}
            == {"maya", "omar", "priya", "sam"},
        )
        check(
            "audit: remove_project_member victor by maya",
            any(
                row.action == "remove_project_member"
                and row.actor == "maya"
                and row.details.get("user_id") == "victor"
                for row in project_rows
            ),
        )
        denied_rows = [
            row
            for row in project_rows
            if row.actor in {"quinn", "rosa", "victor", "wen", "nora"}
            and row.action in {"update_project", "update_project_member", "add_project_member"}
        ]
        check(
            "audit: refused requests leave no project audit rows",
            not denied_rows,
            str([row.actor for row in denied_rows]),
        )

        token_rows = caliber.audit.list(
            entity_type="personal_access_token", actor="priya", limit=500
        )
        created_row = next(
            (
                row
                for row in token_rows
                if row.entity_id == issued.token_id and row.action == "create_personal_access_token"
            ),
            None,
        )
        revoked_row = next(
            (
                row
                for row in token_rows
                if row.entity_id == issued.token_id and row.action == "revoke_personal_access_token"
            ),
            None,
        )
        check(
            "audit: the bound token's creation names its project, and its revocation is recorded",
            created_row is not None
            and created_row.details.get("project_id") == project_id
            and revoked_row is not None,
        )
        check(
            "audit: no token secret is in any audit row",
            issued.token not in json.dumps([row.details for row in token_rows]),
        )
        promote_rows = caliber.audit.list(action="promote_prompt", entity_id=prompt_name, limit=50)
        check(
            "audit: promote_prompt for the released prompt, by priya, not overridden",
            len(promote_rows) == 1
            and promote_rows[0].actor == "priya"
            and promote_rows[0].details.get("overridden") is False,
        )
        account_rows = caliber.audit.list(entity_type="account", action="create_account", limit=500)
        check(
            "audit: create_account by admin for every roster account",
            {row.entity_id for row in account_rows} >= set(people)
            and {row.actor for row in account_rows} <= {admin.user_id},
        )
        sign_ins = caliber.audit.list(
            entity_type="session", action="sign_in", actor="maya", limit=5
        )
        check("audit: sign_in rows name who signed in", bool(sign_ins))

        # -- 10. End the sessions the script opened -------------------------------------------
        ended = sum(caliber.auth.accounts.revoke_sessions(user) for user in people)
        check("the sessions the script opened are revoked", ended >= len(people), str(ended))
    finally:
        for client in opened:
            client.close()

    if failures:
        raise VerificationError(f"{len(failures)} check(s) failed:\n  " + "\n  ".join(failures))
    return {
        "project_id": project_id,
        "other_project_id": other.project_id,
        "members": len(wanted),
        "matrix_requests": cells,
        "matrix_outcomes": dict(sorted(outcomes.items())),
        "failure_scenarios": dict(sorted(failure_results.items())),
        "bound_token_id": issued.token_id,
        "released_prompt": prompt_name,
        "simulated_providers": simulated,
        "csrf_enabled": csrf_enabled,
        "run_key": key,
    }


_ACTIONS = frozenset(
    {
        "project.read",
        "members.list",
        "dataset.create",
        "prompt.create",
        "prompt.promote",
        "evaluation.run",
        "project.update",
        "members.update",
    }
)


def _known_action(action_id: str) -> bool:
    return action_id in _ACTIONS


def cleanup(
    caliber: CaliberClient,
    *,
    base_url: str,
    password: str = DEFAULT_LAB_PASSWORD,
    sign_in: SignIn | None = None,
    user_client: UserClient | None = None,
    dataset_dir: Path = DATASET_DIR,
    report: Report | None = None,
) -> dict[str, int]:
    """Archive the projects this lab created and disable the roster accounts.

    The lab's projects are named ``lab18-...``; only their primary owner (maya) can archive them.
    Disabling an account also revokes its sessions. Nothing here deletes data: an archived
    project is hidden, not erased, and a later run re-enables the accounts it needs.
    """
    say: Report = report or (lambda _line: None)
    roster = load_roster(dataset_dir)
    sign = sign_in or sign_in_with(default_login_attempt(base_url))
    opened: list[CaliberClient] = []
    make_client = user_client or default_user_client(base_url, opened)
    archived = 0
    try:
        existing = {account.user_id for account in caliber.auth.accounts.list()}
        if "maya" in existing:
            lead = make_client(sign("maya", password), None)
            for project in lead.projects.list():
                if project.name.startswith("lab18-") and project.owner == "maya":
                    lead.projects.archive(project.project_id)
                    archived += 1
                    say(f"archived {project.name} ({project.project_id})")
        disabled = 0
        for person in roster:
            if person["user_id"] in existing:
                caliber.auth.accounts.update(person["user_id"], disabled=True)
                disabled += 1
        say(f"disabled {disabled} roster account(s)")
    finally:
        for client in opened:
            client.close()
    return {"archived_projects": archived, "disabled_accounts": disabled}


def main(argv: Sequence[str] | None = None) -> int:
    base_url = os.environ["CALIBER_BASE_URL"]
    token = os.environ["CALIBER_TOKEN"]
    password = os.environ.get("CALIBER_LAB18_PASSWORD", DEFAULT_LAB_PASSWORD)
    lines: list[str] = []

    def report(line: str) -> None:
        lines.append(line)
        print(line, flush=True)

    with CaliberClient(base_url, token=token) as caliber:
        if "--cleanup" in (argv or ()):
            print(json.dumps(cleanup(caliber, base_url=base_url, password=password, report=report)))
            return 0
        try:
            summary = run(caliber, base_url=base_url, password=password, report=report)
        except (VerificationError, SetupError) as exc:
            print(f"\nNOT VERIFIED: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
