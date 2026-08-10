"""The CLI's contract: exit codes, stream discipline, and refusal to prompt."""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx
import pytest
from conftest import body_of

from caliber_cli import exits
from caliber_cli.cli import build_parser, main


def _record_install(seen: list[str]) -> Any:
    def handler(request: httpx.Request) -> Any:
        seen.append("installed")
        return {}

    return handler


def out_err(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, captured.err


# --- exit codes are the interface -----------------------------------------


def test_whoami_reports_an_anonymous_identity_as_unauthenticated(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """``/me`` answers 200 for a bad credential; a script must not read that as ok.

    A pipeline that ran ``whoami`` to confirm its token and got exit 0 for
    "nobody" would proceed on a false premise.
    """
    run = stub({"GET /me": {"user_id": "anonymous", "scopes": [], "is_admin": False}})
    assert run(["whoami"]) == exits.UNAUTHENTICATED
    _, err = out_err(capsys)
    assert "CALIBER_TOKEN" in err


def test_whoami_succeeds_for_a_real_identity(stub: Any) -> None:
    run = stub({"GET /me": {"user_id": "@alice", "scopes": ["caliber.admin"]}})
    assert run(["whoami"]) == exits.OK


def test_a_job_that_stopped_for_a_person_exits_its_own_code(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The distinction the whole exit-code table exists for.

    ``candidate_ready`` is neither failure nor success. Reported as success a
    pipeline continues as though someone approved the candidate; reported as
    failure it flags a broken build for a system working correctly.
    """
    run = stub({"GET /jobs/RFN-1": {"job_id": "RFN-1", "status": "candidate_ready"}})
    assert run(["job", "wait", "RFN-1", "--timeout", "5"]) == exits.AWAITING_HUMAN
    _, err = out_err(capsys)
    assert "a person has to act" in err


def test_a_failed_job_exits_failure(stub: Any) -> None:
    run = stub({"GET /jobs/RFN-1": {"job_id": "RFN-1", "status": "failed"}})
    assert run(["job", "wait", "RFN-1", "--timeout", "5"]) == exits.FAILURE


def test_an_applied_job_exits_ok(stub: Any) -> None:
    run = stub({"GET /jobs/RFN-1": {"job_id": "RFN-1", "status": "applied"}})
    assert run(["job", "wait", "RFN-1", "--timeout", "5"]) == exits.OK


def test_a_no_go_signoff_exits_gate_failed(stub: Any) -> None:
    """The command worked and the answer was "do not ship".

    Distinct from FAILURE so a caller does not retry a decision.
    """
    run = stub({"POST /releases/candidates/RC-1/signoffs": {"signoff_id": "S-1"}})
    code = run(
        [
            "release",
            "sign",
            "RC-1",
            "--decision",
            "no-go",
            "--rationale",
            "regression on the refunds set",
        ]
    )
    assert code == exits.GATE_FAILED


def test_a_go_signoff_exits_ok_and_sends_the_rationale(stub: Any) -> None:
    sent: dict[str, Any] = {}

    def record(request: httpx.Request) -> Any:
        sent.update(body_of(request) or {})
        return {"signoff_id": "S-1"}

    run = stub({"POST /releases/candidates/RC-1/signoffs": record})
    assert (
        run(["release", "sign", "RC-1", "--decision", "go", "--rationale", "all gates green"])
        == exits.OK
    )
    assert sent == {"decision": "go", "rationale": "all gates green"}


def test_a_run_still_in_progress_exits_timeout_rather_than_failure(stub: Any) -> None:
    """ "Not yet" is not "wrong". Nothing is known to have failed."""
    run = stub({"GET /workflow-runs/RUN-1": {"workflow_run_id": "RUN-1", "status": "running"}})
    assert run(["workflow", "status", "RUN-1"]) == exits.TIMEOUT


def test_an_api_failure_exits_failure_and_prints_the_servers_detail(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run = stub(
        {"GET /workflows": httpx.Response(403, json={"detail": "scope caliber.operator required"})}
    )
    assert run(["workflow", "list"]) == exits.FAILURE
    _, err = out_err(capsys)
    assert "caliber.operator required" in err


def test_a_401_exits_unauthenticated_because_the_fix_is_always_the_same(stub: Any) -> None:
    """403 stays FAILURE: authenticated, and lacking a scope, is a real failure."""
    run = stub({"GET /workflows": httpx.Response(401, json={"detail": "token revoked"})})
    assert run(["workflow", "list"]) == exits.UNAUTHENTICATED


def test_a_missing_credential_exits_usage_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Never able to try is not the same as the deployment refusing something."""
    monkeypatch.delenv("CALIBER_BASE_URL", raising=False)
    assert main(["whoami"]) == exits.USAGE
    _, err = out_err(capsys)
    assert "CALIBER_BASE_URL" in err


# --- nothing prompts ------------------------------------------------------


def test_revoking_without_confirmation_fails_rather_than_asking(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """A tool that blocked on a TTY would hang a CI job instead of failing it."""
    calls: list[str] = []

    def record_delete(request: httpx.Request) -> Any:
        calls.append("deleted")
        return {}

    run = stub({"DELETE /auth/tokens/PAT-1": record_delete})
    assert run(["token", "revoke", "PAT-1"]) == exits.USAGE
    assert calls == [], "the token was revoked without confirmation"
    _, err = out_err(capsys)
    assert "--yes" in err


def test_revoking_with_confirmation_revokes(stub: Any) -> None:
    run = stub({"DELETE /auth/tokens/PAT-1": {"token_id": "PAT-1", "revoked": True}})
    assert run(["token", "revoke", "PAT-1", "--yes"]) == exits.OK


def test_an_incomplete_command_prints_help_and_exits_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``caliberctl token`` did nothing, so it must not exit 0."""
    assert main(["token"]) == exits.USAGE
    out, _ = out_err(capsys)
    assert "revoke" in out


def test_the_bare_command_prints_help_and_exits_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == exits.USAGE
    out, _ = out_err(capsys)
    assert "caliberctl" in out


# --- stream discipline ----------------------------------------------------


def test_json_mode_puts_only_json_on_stdout(stub: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """So ``caliberctl ... --json | jq`` needs no filtering step.

    ``token create`` is the sharpest case: it emits a human-facing warning about
    the secret being shown once, and that warning must not corrupt the stream a
    script is reading the secret from.
    """
    run = stub({"POST /auth/tokens": {"token_id": "PAT-1", "name": "ci", "token": "calpat_s3cret"}})
    assert run(["--json", "token", "create", "ci"]) == exits.OK

    out, err = out_err(capsys)
    assert jsonlib.loads(out)["token"] == "calpat_s3cret"
    assert "only time" in err


def test_progress_notes_go_to_stderr_even_in_table_mode(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """So redirecting stdout captures the result and not the chatter."""
    run = stub(
        {
            "POST /workflow-runs": {"workflow_run_id": "RUN-1", "status": "queued"},
            "GET /workflow-runs/RUN-1": {"workflow_run_id": "RUN-1", "status": "succeeded"},
        }
    )
    assert run(["workflow", "run", "--workflow-id", "WF-1"]) == exits.OK
    out, err = out_err(capsys)
    assert "submitted run RUN-1" in err
    assert "submitted" not in out


def test_quiet_suppresses_notes_but_never_errors(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run = stub({"GET /workflows": httpx.Response(500, json={"detail": "boom"})})
    assert run(["--quiet", "workflow", "list"]) == exits.FAILURE
    _, err = out_err(capsys)
    assert "boom" in err


def test_a_listed_token_never_carries_a_secret(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run = stub({"GET /auth/tokens": {"tokens": [{"token_id": "PAT-1", "name": "ci"}]}})
    assert run(["--json", "token", "list"]) == exits.OK
    out, _ = out_err(capsys)
    assert "token" not in jsonlib.loads(out)[0]


def test_an_empty_list_says_so_rather_than_printing_a_bare_header(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "No results" and "never fetched" look identical otherwise."""
    run = stub({"GET /workflows": []})
    assert run(["workflow", "list"]) == exits.OK
    _, err = out_err(capsys)
    assert "no results" in err


# --- the CLI invents no semantics -----------------------------------------


def test_the_run_command_passes_the_idempotency_key_through_unchanged(stub: Any) -> None:
    """Never generated: a key this tool invented would differ on the caller's retry."""
    sent: dict[str, Any] = {}

    def record(request: httpx.Request) -> Any:
        sent.update(body_of(request) or {})
        return {"workflow_run_id": "RUN-1", "status": "succeeded"}

    run = stub(
        {
            "POST /workflow-runs": record,
            "GET /workflow-runs/RUN-1": {"workflow_run_id": "RUN-1", "status": "succeeded"},
        }
    )
    run(["workflow", "run", "--version-id", "WFV-1", "--idempotency-key", "deploy-42"])
    assert sent["idempotency_key"] == "deploy-42"


def test_omitting_the_idempotency_key_sends_no_key(stub: Any) -> None:
    sent: dict[str, Any] = {}

    def record(request: httpx.Request) -> Any:
        sent.update(body_of(request) or {})
        return {"workflow_run_id": "RUN-1", "status": "succeeded"}

    run = stub(
        {
            "POST /workflow-runs": record,
            "GET /workflow-runs/RUN-1": {"workflow_run_id": "RUN-1", "status": "succeeded"},
        }
    )
    run(["workflow", "run", "--version-id", "WFV-1"])
    assert "idempotency_key" not in sent


def test_a_json_input_is_parsed_and_a_bare_string_is_passed_through(stub: Any) -> None:
    """Some workflows take an object and some take a string; guessing wrong
    would block a legitimate call, so a non-JSON value is sent as-is."""
    sent: list[Any] = []

    def record(request: httpx.Request) -> Any:
        sent.append((body_of(request) or {}).get("input"))
        return {"workflow_run_id": "RUN-1", "status": "succeeded"}

    run = stub(
        {
            "POST /workflow-runs": record,
            "GET /workflow-runs/RUN-1": {"workflow_run_id": "RUN-1", "status": "succeeded"},
        }
    )
    run(["workflow", "run", "--version-id", "WFV-1", "--input", '{"claim_id": "C-1"}'])
    run(["workflow", "run", "--version-id", "WFV-1", "--input", "just text"])
    assert sent == [{"claim_id": "C-1"}, "just text"]


def test_submitting_requires_a_target(capsys: pytest.CaptureFixture[str]) -> None:
    """``--workflow-id`` or ``--version-id``, and argparse enforces exactly one."""
    with pytest.raises(SystemExit) as caught:
        main(["workflow", "run"])
    assert caught.value.code == exits.USAGE


def test_no_wait_returns_the_queued_run_without_polling(stub: Any) -> None:
    calls: list[str] = []

    def record(request: httpx.Request) -> Any:
        calls.append("polled")
        return {"workflow_run_id": "RUN-1", "status": "succeeded"}

    run = stub(
        {
            "POST /workflow-runs": {"workflow_run_id": "RUN-1", "status": "queued"},
            "GET /workflow-runs/RUN-1": record,
        }
    )
    assert run(["workflow", "run", "--version-id", "WFV-1", "--no-wait"]) == exits.OK
    assert calls == []


# --- cookbooks and plugins ------------------------------------------------


def test_installing_an_unready_cookbook_names_every_unmet_check(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """All of them at once, rather than one 400 per attempt."""
    installed: list[str] = []
    run = stub(
        {
            "GET /cookbooks": {
                "recipes": [
                    {
                        "id": "03",
                        "title": "Policy-Safe",
                        "readiness": {
                            "status": "configuration_required",
                            "checks": [
                                {"label": "Runtime approvals", "status": "configuration_required"},
                                {"label": "Model provider", "status": "configuration_required"},
                            ],
                        },
                    }
                ]
            },
            "POST /cookbooks/03/install": _record_install(installed),
        }
    )
    assert run(["cookbook", "install", "03"]) == exits.FAILURE
    assert installed == []
    _, err = out_err(capsys)
    assert "Runtime approvals" in err
    assert "Model provider" in err


def test_force_installs_despite_unmet_checks(stub: Any) -> None:
    """Readiness is computed from the live environment; an operator may know more."""
    run = stub(
        {
            "GET /cookbooks": {
                "recipes": [{"id": "03", "readiness": {"status": "configuration_required"}}]
            },
            "POST /cookbooks/03/install": {"workflow": {"status": "paused"}},
        }
    )
    assert run(["cookbook", "install", "03", "--force"]) == exits.OK


def test_an_unknown_recipe_is_a_usage_error_naming_the_real_ones(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run = stub({"GET /cookbooks": {"recipes": [{"id": "01"}, {"id": "02"}]}})
    assert run(["cookbook", "install", "99"]) == exits.USAGE
    _, err = out_err(capsys)
    assert "'01', '02'" in err


def test_a_plugin_that_failed_to_load_makes_the_command_fail(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deployment allowlisted it, so a green exit would report a
    configuration that is not actually in effect."""
    run = stub(
        {
            "GET /capabilities": {
                "extensibility": {
                    "optimizers": [{"name": "MetaPrompt", "artifact_types": ["prompt"]}],
                    "plugins": [
                        {
                            "name": "acme",
                            "distribution": "acme-plugin",
                            "allowlisted": True,
                            "error": "ImportError: no module named acme",
                        }
                    ],
                    "allowlist_env_var": "CALIBER_PLUGIN_ALLOWLIST",
                }
            }
        }
    )
    assert run(["plugin", "list"]) == exits.FAILURE
    _, err = out_err(capsys)
    assert "failed to load" in err


def test_an_installed_but_unlisted_plugin_is_a_note_not_a_failure(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The normal state for a freshly installed plugin, and the note says how
    to enable it."""
    run = stub(
        {
            "GET /capabilities": {
                "extensibility": {
                    "optimizers": [],
                    "plugins": [
                        {"name": "acme", "distribution": "acme-plugin", "allowlisted": False}
                    ],
                    "allowlist_env_var": "CALIBER_PLUGIN_ALLOWLIST",
                }
            }
        }
    )
    assert run(["plugin", "list"]) == exits.OK
    _, err = out_err(capsys)
    assert "CALIBER_PLUGIN_ALLOWLIST" in err


# --- the parser itself ----------------------------------------------------


def test_every_command_and_subcommand_has_a_handler() -> None:
    """A subparser registered without a handler exits 0 having done nothing.

    Walked rather than listed so a command added later is covered without this
    test being updated -- which is the only way a test like this stays true.
    """
    import argparse

    def walk(parser: argparse.ArgumentParser, path: str) -> list[str]:
        problems: list[str] = []
        actions = [
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        ]
        if not actions:
            handler = parser.get_default("handler")
            if handler is None:
                problems.append(f"{path} has no handler")
            return problems
        for action in actions:
            for name, sub in action.choices.items():
                problems.extend(walk(sub, f"{path} {name}".strip()))
        return problems

    assert walk(build_parser(), "caliberctl") == []


def test_the_help_text_states_the_exit_codes() -> None:
    """They are the interface, so they belong in ``--help`` and not only a README."""
    text = build_parser().format_help()
    for fragment in ("0 ok", "3 awaiting a human", "4 gate", "5 timed out"):
        assert fragment in text


# --- prompts and services -------------------------------------------------


def test_prompt_list_reads_the_registry(stub: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """Reports the registry coordinate -- prompt name, version, alias -- because
    a prompt is an MLflow registry object rather than a CALIBER row."""
    run = stub({"GET /prompts": [{"agent_id": "AGT-1", "prompt_name": "triage", "version": 4}]})
    assert run(["--json", "prompt", "list"]) == exits.OK
    out, _ = out_err(capsys)
    assert jsonlib.loads(out)[0]["prompt_name"] == "triage"


def test_publishing_a_service_without_confirmation_does_nothing(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Publishing widens the reachable surface: invocation moves to an external
    endpoint authenticated by per-service tokens rather than a user credential."""
    published: list[str] = []

    def record(request: httpx.Request) -> Any:
        published.append("published")
        return {}

    run = stub({"POST /workflows/WF-1/service": record})
    assert run(["service", "publish", "WF-1"]) == exits.USAGE
    assert published == []
    _, err = out_err(capsys)
    assert "external HTTP service" in err


def test_publishing_with_confirmation_publishes(stub: Any) -> None:
    run = stub({"POST /workflows/WF-1/service": {"workflow_id": "WF-1", "status": "published"}})
    assert run(["service", "publish", "WF-1", "--yes"]) == exits.OK


def test_unpublishing_requires_confirmation_because_it_breaks_callers(
    stub: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    run = stub({"DELETE /workflows/WF-1/service": {"status": "unpublished"}})
    assert run(["service", "unpublish", "WF-1"]) == exits.USAGE
    assert run(["service", "unpublish", "WF-1", "--yes"]) == exits.OK


def test_the_service_surface_is_scoped_to_a_workflow(stub: Any) -> None:
    """There is no unscoped service listing; an earlier SDK method that invented
    ``GET /services`` returned 404."""
    run = stub({"GET /workflows/WF-1/service": {"workflow_id": "WF-1", "status": "published"}})
    assert run(["service", "show", "WF-1"]) == exits.OK
