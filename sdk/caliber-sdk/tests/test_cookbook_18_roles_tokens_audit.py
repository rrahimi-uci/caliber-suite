"""Cookbook 18 (projects, roles, tokens, audit): the script, its data files, and its refusals.

The role rules are encoded twice on purpose: in ``role-matrix.csv`` (the lab's expectations) and in
``cookbook_18_fake_server.ROLE_ACTIONS`` (what the stand-in server enforces). The tests below check
the two agree, that the script passes against a server that follows the rules, and that it *fails*,
naming the cell, against one that does not. Running the script against a live server is how the
real role checks are verified; see the lab README.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cookbook_18_fake_server import PLATFORM_SCOPES, ROLE_ACTIONS, FakeCaliber

from caliber_sdk import CaliberClient
from examples.cookbooks import cookbook_18_projects_roles_tokens_audit as lab
from examples.cookbooks.cookbook_18_projects_roles_tokens_audit import (
    DATASET_DIR,
    SetupError,
    VerificationError,
    cleanup,
    column_for,
    load_failure_scenarios,
    load_matrix,
    load_roster,
    run,
    status_allowed,
)

BASE = "https://caliber.test"

#: ``role-matrix.csv`` action -> the project action its route checks.
PROJECT_ACTION_OF = {
    "project.read": "read",
    "members.list": "read",
    "dataset.create": "resource.write.evidence",
    "prompt.create": "resource.write.runtime",
    "prompt.promote": "release.apply",
    "evaluation.run": "resource.execute",
    "project.update": "project.update",
    "members.update": "project.manage_members",
}


def harness(fake: FakeCaliber) -> dict[str, Any]:
    """Keyword arguments that point :func:`run` at ``fake`` instead of a network."""

    def user_client(token: str, project: str | None = None) -> CaliberClient:
        return CaliberClient(
            BASE,
            token=token,
            project=project,
            http_client=httpx.Client(transport=fake.transport()),
            max_retries=0,
        )

    def post(url: str, **kwargs: Any) -> httpx.Response:
        with httpx.Client(transport=fake.transport()) as client:
            return client.post(url, **kwargs)

    return {
        "base_url": BASE,
        "login_attempt": fake.login_attempt,
        "user_client": user_client,
        "use_caliberctl": False,
        "http_post": post,
        "sleep": fake.age,
        "run_key": "unit",
    }


def admin_client(fake: FakeCaliber) -> CaliberClient:
    return CaliberClient(
        BASE,
        token=fake.ADMIN_TOKEN,
        http_client=httpx.Client(transport=fake.transport()),
        max_retries=0,
    )


def run_lab(fake: FakeCaliber, **overrides: Any) -> dict[str, object]:
    with admin_client(fake) as caliber:
        return run(caliber, **{**harness(fake), **overrides})


def new_fake(**kwargs: Any) -> FakeCaliber:
    return FakeCaliber(load_roster(), **kwargs)


# ---------------------------------------------------------------------------------------
# The data files
# ---------------------------------------------------------------------------------------


def test_roster_is_a_realistic_team_with_every_role_and_a_non_member() -> None:
    roster = load_roster()
    assert len(roster) == 9
    assert len({person["user_id"] for person in roster}) == 9
    roles = [person["project_role"] for person in roster]
    assert {"owner", "editor", "reviewer", "viewer", "none"} == set(roles)
    # At least two of every role, so a column is never about one person.
    for role in ("owner", "editor", "reviewer", "viewer"):
        assert roles.count(role) >= 2, role
    assert all(person["platform_scopes"] in PLATFORM_SCOPES for person in roster)
    # The project's creator is the first owner, and every owner can actually hold the role.
    assert roster[0]["user_id"] == "maya" and roster[0]["project_role"] == "owner"
    for person in roster:
        if person["project_role"] == "owner":
            assert person["platform_scopes"] == "operator+approver", person["user_id"]
    # Both kinds of viewer are present, so both kinds of 403 are exercised.
    assert {column_for(person) for person in roster} == {
        "owner",
        "editor",
        "reviewer",
        "viewer",
        "viewer_no_operator",
        "non_member",
    }


def test_matrix_columns_cover_every_kind_of_person_and_every_cell_is_filled() -> None:
    matrix = load_matrix()
    columns = {"owner", "editor", "reviewer", "viewer", "viewer_no_operator", "non_member"}
    assert len(matrix) == len(PROJECT_ACTION_OF)
    for row in matrix:
        assert columns <= set(row), row["action_id"]
        for column in columns:
            assert row[column], (row["action_id"], column)
            assert all(part.isdigit() for part in row[column].split("|"))


def test_matrix_agrees_with_the_role_rules_it_documents() -> None:
    """The CSV and the rules (``PROJECT_ACTIONS``) are two statements of one fact."""
    for row in load_matrix():
        action = row["project_action"]
        assert action == PROJECT_ACTION_OF[row["action_id"]], row["action_id"]
        allowed_roles = ROLE_ACTIONS[action]
        for role in ("owner", "editor", "reviewer", "viewer"):
            status = int(row[role].split("|")[0])
            # A member the rules allow never gets 403/404; one they do not always gets 403.
            if role in allowed_roles:
                assert status in {200, 201, 400}, (row["action_id"], role, status)
            else:
                assert status == 403, (row["action_id"], role, status)
        assert row["non_member"] == "404"
        assert row["viewer_no_operator"] in {"200", "403"}
        # No operator scope means a write is refused by the platform scope first.
        if row["project_action"] != "read":
            assert row["viewer_no_operator"] == "403"


def test_failure_scenarios_are_unique_documented_and_all_implemented() -> None:
    scenarios = load_failure_scenarios()
    ids = [scenario["id"] for scenario in scenarios]
    assert ids == sorted(set(ids)) and ids[0] == "F01"
    for scenario in scenarios:
        assert scenario["title"] and scenario["request"]
        assert scenario["expected_status"] in {400, 401, 403, 404, 409}
        assert isinstance(scenario["detail_contains"], str)
    source = Path(lab.__file__).read_text(encoding="utf-8")
    for scenario_id in ids:
        assert f'"{scenario_id}"' in source, f"{scenario_id} is documented but never run"


@pytest.mark.parametrize(
    ("expected", "actual", "allowed"),
    [("200", 200, True), ("200", 201, False), ("400|201", 201, True), ("400|201", 403, False)],
)
def test_status_allowed(expected: str, actual: int, allowed: bool) -> None:
    assert status_allowed(expected, actual) is allowed


@pytest.mark.parametrize(
    ("role", "scopes", "column"),
    [
        ("owner", "operator+approver", "owner"),
        ("viewer", "operator", "viewer"),
        ("viewer", "none", "viewer_no_operator"),
        ("none", "operator", "non_member"),
    ],
)
def test_column_for(role: str, scopes: str, column: str) -> None:
    assert column_for({"project_role": role, "platform_scopes": scopes}) == column


def test_dataset_dir_is_the_labs_assets() -> None:
    assert DATASET_DIR.name == "dataset" and DATASET_DIR.parent.parent.name.startswith("18-")
    with (DATASET_DIR / "team-roster.csv").open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle))[0] == "user_id"
    assert json.loads((DATASET_DIR / "failure-scenarios.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------
# The script against a server that follows the rules
# ---------------------------------------------------------------------------------------


def test_the_lab_verifies_against_a_server_that_follows_the_rules() -> None:
    fake = new_fake()
    lines: list[str] = []
    summary = run_lab(fake, report=lines.append)

    assert summary["members"] == 8
    assert summary["matrix_requests"] == 72
    assert summary["simulated_providers"] == ["eval", "llm"]
    assert summary["csrf_enabled"] is False
    assert set(cast(dict[str, str], summary["failure_scenarios"])) == {
        f"F{n:02d}" for n in range(1, 16)
    }
    assert not [line for line in lines if line.startswith("FAIL")]
    assert any("F16" in line and line.startswith("skip") for line in lines)
    # The release the lab makes is on the timeline and the live board exactly once.
    released = str(summary["released_prompt"])
    assert [row["entity_id"] for row in fake.timeline].count(released) == 1
    assert fake.live[released]["by"] == "priya"
    # Offboarding took effect.
    assert fake.members[str(summary["project_id"])]["victor"]["status"] == "inactive"
    # The secret never reached an audit row.
    assert all("calpat_" not in json.dumps(row) for row in fake.audit)


def test_a_second_run_reuses_the_accounts_and_makes_a_new_project() -> None:
    fake = new_fake()
    first = run_lab(fake)
    # Sessions were revoked at the end; accounts and the first run's projects remain.
    second = run_lab(fake, run_key="second")
    assert second["project_id"] != first["project_id"]
    assert len(fake.accounts) == 9
    assert [row["action"] for row in fake.audit].count("create_account") == 9


def test_with_csrf_enforced_the_unprotected_write_is_refused_and_the_sdk_replays() -> None:
    fake = new_fake(csrf_enabled=True)
    lines: list[str] = []
    summary = run_lab(fake, report=lines.append)
    assert summary["csrf_enabled"] is True
    assert any("F16" in line and line.startswith("ok") for line in lines)
    assert any("replays a write" in line for line in lines)


def test_an_all_real_deployment_is_reported_as_such() -> None:
    fake = new_fake(simulated=())
    summary = run_lab(fake)
    assert summary["simulated_providers"] == []


# ---------------------------------------------------------------------------------------
# The script notices when the server does not follow the rules
# ---------------------------------------------------------------------------------------


def test_a_viewer_who_can_release_fails_the_matrix_and_names_the_cell() -> None:
    fake = new_fake(bend="viewer-can-promote")
    with pytest.raises(VerificationError) as caught:
        run_lab(fake)
    message = str(caught.value)
    assert "role matrix" in message
    assert "victor/prompt.promote: expected 403, got 200" in message


def test_an_account_without_the_platform_scopes_the_roster_needs_is_a_setup_error() -> None:
    fake = new_fake(scopes_override={"quinn": "operator"})  # the roster says operator+approver
    with pytest.raises(SetupError, match=r"quinn holds .* but the roster needs"):
        run_lab(fake)


def test_a_non_admin_token_is_refused_up_front() -> None:
    fake = new_fake()
    fake.accounts["maya"] = False
    status, session = fake.login_attempt("maya", fake.password)
    assert status == 200
    with (
        CaliberClient(
            BASE, token=session, http_client=httpx.Client(transport=fake.transport())
        ) as not_admin,
        pytest.raises(SetupError, match="admin personal access token"),
    ):
        run(not_admin, **harness(fake))


def test_a_wrong_demo_password_is_a_setup_error_that_says_how_to_fix_it() -> None:
    fake = new_fake(password="another-password-123")
    with pytest.raises(SetupError, match="CALIBER_LAB18_PASSWORD"):
        run_lab(fake)


def test_a_deployment_that_is_not_ready_fails_the_run() -> None:
    fake = new_fake()
    original = fake._readiness

    def not_ready() -> dict[str, Any]:
        return {**original(), "ready": False, "blockers": ["mlflow: unreachable"]}

    fake._readiness = not_ready  # type: ignore[method-assign]
    with pytest.raises(VerificationError, match="readiness is ready"):
        run_lab(fake)


# ---------------------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------------------


def sign_in_for(fake: FakeCaliber) -> Any:
    return lambda user, _password: fake.login_attempt(user, fake.password)[1]


def test_cleanup_archives_the_labs_projects_and_disables_the_accounts() -> None:
    fake = new_fake()
    run_lab(fake)
    fake.projects["PRJ-other"] = {  # someone else's project must be left alone
        "project_id": "PRJ-other",
        "name": "not-the-lab",
        "owner": "maya",
        "description": "",
        "status": "active",
    }
    fake.members["PRJ-other"] = {"maya": {"role": "owner", "status": "active"}}

    with admin_client(fake) as caliber:
        result = cleanup(
            caliber,
            base_url=BASE,
            sign_in=sign_in_for(fake),
            user_client=harness(fake)["user_client"],
        )
    assert result == {"archived_projects": 2, "disabled_accounts": 9}
    assert fake.projects["PRJ-other"]["status"] == "active"
    assert [p["status"] for p in fake.projects.values()].count("archived") == 2
    assert all(fake.accounts.values())  # disabled
    # A later run re-enables them instead of failing to sign in.
    summary = run_lab(fake, run_key="after-cleanup")
    assert summary["members"] == 8
    assert not any(fake.accounts.values())


def test_cleanup_with_no_lead_account_only_reports() -> None:
    fake = new_fake()
    with admin_client(fake) as caliber:
        result = cleanup(
            caliber,
            base_url=BASE,
            sign_in=sign_in_for(fake),
            user_client=harness(fake)["user_client"],
        )
    assert result == {"archived_projects": 0, "disabled_accounts": 0}


# ---------------------------------------------------------------------------------------
# The parts that normally talk to a real server, driven through the stand-in
# ---------------------------------------------------------------------------------------


def route_httpx_to(fake: FakeCaliber, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the module-level ``httpx.get``/``httpx.post`` the script uses reach ``fake``."""

    def make(verb: str) -> Any:
        def send(url: str, **kwargs: Any) -> httpx.Response:
            kwargs.pop("timeout", None)
            with httpx.Client(transport=fake.transport()) as client:
                return client.request(verb, url, **kwargs)

        return send

    monkeypatch.setattr(httpx, "get", make("GET"))
    monkeypatch.setattr(httpx, "post", make("POST"))


@pytest.mark.parametrize("csrf", [False, True])
def test_the_http_login_returns_the_session_token_and_carries_the_csrf_token(
    csrf: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = new_fake(csrf_enabled=csrf)
    fake.accounts["maya"] = False
    route_httpx_to(fake, monkeypatch)
    attempt = lab.default_login_attempt(BASE)

    status, token = attempt("maya", fake.password)
    assert status == 200 and token.startswith("sess-maya")
    assert lab.sign_in_with(attempt)("maya", fake.password).startswith("sess-maya")

    assert attempt("maya", "wrong-password-123") == (401, "invalid credentials")
    assert attempt("nobody", fake.password) == (401, "invalid credentials")
    with pytest.raises(SetupError, match="invalid credentials"):
        lab.sign_in_with(attempt)("maya", "wrong-password-123")


def test_the_http_login_reports_a_body_that_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url: str, **_kw: Any) -> httpx.Response:
        return httpx.Response(404, text="not found")

    def post(url: str, **_kw: Any) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(httpx, "post", post)
    assert lab.default_login_attempt(BASE)("maya", "x") == (502, "bad gateway")


def test_the_default_user_client_is_tracked_so_it_can_be_closed() -> None:
    opened: list[CaliberClient] = []
    client = lab.default_user_client(BASE, opened)("calpat_x", "PRJ-1")
    assert opened == [client]
    client.close()


def test_caliberctl_is_found_beside_the_interpreter_or_on_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    monkeypatch.setattr(sys, "executable", str(fake_python))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert lab.find_caliberctl() is None
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/caliberctl")
    assert lab.find_caliberctl() == ["/usr/local/bin/caliberctl"]
    (tmp_path / "caliberctl").write_text("")
    assert lab.find_caliberctl() == [str(tmp_path / "caliberctl")]


def test_caliberctl_output_is_parsed_and_a_failing_exit_is_a_verification_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout='{"user_id": "priya"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CALIBER_PROJECT", "PRJ-ambient")
    result = lab.run_caliberctl(["caliberctl"], BASE, "calpat_secret", "whoami")
    assert result == {"user_id": "priya"}
    assert seen["command"] == ["caliberctl", "--json", "whoami"]
    assert seen["env"]["CALIBER_TOKEN"] == "calpat_secret"
    # The ambient project would change which project the bound token appears to act in.
    assert "CALIBER_PROJECT" not in seen["env"]

    def failing(command: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 6, stdout="", stderr="no usable credential")

    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(VerificationError, match="exited 6: no usable credential"):
        lab.run_caliberctl(["caliberctl"], BASE, "calpat_secret", "whoami")


def test_the_caliberctl_checks_read_what_the_cli_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = new_fake()

    def cli(_command: list[str], _base: str, token: str, *args: str) -> Any:
        row = next(t for t in fake.tokens.values() if t["secret"] == token)
        if args == ("whoami",):
            return {"user_id": row["user_id"], "scopes": row["scopes"]}
        if args == ("token", "list"):
            return {
                "tokens": [
                    {k: v for k, v in t.items() if k != "secret"} for t in fake.tokens.values()
                ]
            }
        assert args[:2] == ("--project", row["project_id"])
        return [{"name": name} for name in fake.prompts]

    monkeypatch.setattr(lab, "find_caliberctl", lambda: ["caliberctl"])
    monkeypatch.setattr(lab, "run_caliberctl", cli)
    lines: list[str] = []
    run_lab(fake, use_caliberctl=True, report=lines.append)
    assert [line for line in lines if "caliberctl" in line and line.startswith("ok")] == [
        "ok   caliberctl whoami sees the same identity  [['caliber.operator']]",
        "ok   caliberctl token list shows the bound token",
        "ok   caliberctl prompt list shows the released prompt",
    ]


def test_without_caliberctl_installed_the_cli_checks_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lab, "find_caliberctl", lambda: None)
    lines: list[str] = []
    run_lab(new_fake(), use_caliberctl=True, report=lines.append)
    assert any(line.startswith("skip caliberctl is not installed") for line in lines)


def test_a_matrix_row_the_script_cannot_run_is_reported_not_skipped(tmp_path: Path) -> None:
    directory = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, directory)
    with (directory / "role-matrix.csv").open("a", encoding="utf-8") as handle:
        handle.write(
            "prompt.delete,Delete a prompt,resource.write.runtime,200,200,403,403,403,404\n"
        )
    with pytest.raises(VerificationError, match="names actions the script cannot run"):
        run_lab(new_fake(), dataset_dir=directory)


def test_a_prompt_inventory_that_leaks_a_workflow_node_prompt_is_reported() -> None:
    """The defect this lab found: the inventory listed every workflow's agent-node prompts."""
    with pytest.raises(VerificationError, match="non-member's prompt inventory shows nothing"):
        run_lab(new_fake(bend="workflow-prompts-unscoped"))


def test_a_non_member_who_can_see_the_project_is_reported() -> None:
    with pytest.raises(VerificationError, match="the outsider sees 404"):
        run_lab(new_fake(bend="non-member-sees-project"))


def test_main_runs_the_lab_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = new_fake()
    route_httpx_to(fake, monkeypatch)
    real_client = CaliberClient

    def client(base_url: str, **kwargs: Any) -> CaliberClient:
        return real_client(
            base_url,
            http_client=httpx.Client(transport=fake.transport()),
            max_retries=0,
            **kwargs,
        )

    monkeypatch.setattr(lab, "CaliberClient", client)
    monkeypatch.setattr(lab, "find_caliberctl", lambda: None)
    monkeypatch.setattr(time, "sleep", fake.age)
    monkeypatch.setenv("CALIBER_BASE_URL", BASE)
    monkeypatch.setenv("CALIBER_TOKEN", fake.ADMIN_TOKEN)

    assert lab.main([]) == 0
    printed = capsys.readouterr().out
    assert "ok   the role matrix holds (72 requests" in printed
    assert json.loads(printed[printed.index("{\n") :])["members"] == 8

    # --cleanup archives and disables, and exits zero too.
    assert lab.main(["--cleanup"]) == 0
    assert '"disabled_accounts": 9' in capsys.readouterr().out

    # A server that breaks a rule is reported on stderr with exit 1, never a traceback.
    broken = new_fake(bend="viewer-can-promote")
    route_httpx_to(broken, monkeypatch)
    monkeypatch.setattr(
        lab,
        "CaliberClient",
        lambda base_url, **kwargs: real_client(
            base_url,
            http_client=httpx.Client(transport=broken.transport()),
            max_retries=0,
            **kwargs,
        ),
    )
    monkeypatch.setattr(time, "sleep", broken.age)
    assert lab.main([]) == 1
    assert "NOT VERIFIED" in capsys.readouterr().err
