"""``caliberctl workspace``: the Git-backed project lifecycle delegates.

The property under test throughout is the exit-state mapping the plan
requires: ``pending``, ``changes-requested``, ``out-of-date``, ``blocked``,
and ``reconcile-required`` must each surface as their own exit code rather
than collapsing into :data:`exits.FAILURE`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import body_of

from caliber_cli import exits


@pytest.fixture
def bundle(tmp_path: Path) -> str:
    path = tmp_path / "bundle.tar"
    path.write_bytes(b"pretend-source-bundle")
    return str(path)


# --- --project is a hard requirement, for every leaf --------------------


def test_a_workspace_command_without_project_exits_usage_before_any_request(stub: Any) -> None:
    """Every Workspace route is project-path-scoped; there is no safe ambient
    default the way there is for header-scoped resources."""
    run = stub({})
    assert run(["workspace", "package", "list"]) == exits.USAGE


# --- import: bounded, digest-pinned, and reconcile-required is its own code --


def test_import_create_waits_and_reports_reconcile_required(stub: Any, bundle: str) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/revision-imports": {
                "import_job_id": "IMP-1",
                "status": "queued",
            },
            "GET /projects/PRJ-1/revision-imports/IMP-1": {
                "import_job_id": "IMP-1",
                "status": "reconcile_required",
            },
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "import",
            "create",
            "--repository",
            "git@example.com:acme/app.git",
            "--commit-sha",
            "a" * 40,
            "--bundle",
            bundle,
            "--idempotency-key",
            "import-1",
            "--timeout",
            "5",
        ]
    )
    assert code == exits.RECONCILE_REQUIRED


def test_import_create_no_wait_exits_ok_without_polling(stub: Any, bundle: str) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/revision-imports": {
                "import_job_id": "IMP-1",
                "status": "queued",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "import",
            "create",
            "--repository",
            "git@example.com:acme/app.git",
            "--commit-sha",
            "a" * 40,
            "--bundle",
            bundle,
            "--idempotency-key",
            "import-1",
            "--no-wait",
        ]
    )
    assert code == exits.OK


def test_import_create_missing_bundle_exits_usage(stub: Any) -> None:
    run = stub({})
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "import",
            "create",
            "--repository",
            "git@example.com:acme/app.git",
            "--commit-sha",
            "a" * 40,
            "--bundle",
            "/no/such/bundle.tar",
            "--idempotency-key",
            "import-1",
        ]
    )
    assert code == exits.USAGE


def test_import_status_still_running_exits_timeout(stub: Any) -> None:
    run = stub(
        {
            "GET /projects/PRJ-1/revision-imports/IMP-1": {
                "import_job_id": "IMP-1",
                "status": "queued",
            }
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "import", "status", "IMP-1"])
    assert code == exits.TIMEOUT


def test_import_reconcile_exits_ok(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/revision-imports/IMP-1:reconcile": {
                "job": {"import_job_id": "IMP-1", "status": "reconcile_required"},
                "observed": True,
                "observation": "no drift",
            }
        }
    )
    assert run(["--project", "PRJ-1", "workspace", "import", "reconcile", "IMP-1"]) == exits.OK


# --- package (revision) is read-only from the CLI's side ---------------------


def test_package_show(stub: Any) -> None:
    run = stub(
        {
            "GET /projects/PRJ-1/revisions/WSR-1": {
                "revision_id": "WSR-1",
                "status": "ready",
            }
        }
    )
    assert run(["--project", "PRJ-1", "workspace", "package", "show", "WSR-1"]) == exits.OK


def test_package_diff(stub: Any) -> None:
    run = stub(
        {
            "GET /projects/PRJ-1/revisions/WSR-2/diff": {
                "base_revision_id": "WSR-1",
                "revision_id": "WSR-2",
            }
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "package", "diff", "WSR-2", "--base", "WSR-1"])
    assert code == exits.OK


# --- Change Requests: pending / changes-requested / out-of-date / accepted ---


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("draft", exits.AWAITING_HUMAN),
        ("open", exits.AWAITING_HUMAN),
        ("technically_approved", exits.AWAITING_HUMAN),
        ("qa_in_progress", exits.AWAITING_HUMAN),
        ("changes_requested", exits.CHANGES_REQUESTED),
        ("out_of_date", exits.OUT_OF_DATE),
        ("accepted", exits.OK),
        ("closed", exits.FAILURE),
    ],
)
def test_cr_show_maps_every_status_to_its_own_exit_code(
    stub: Any, status: str, expected: int
) -> None:
    run = stub(
        {
            "GET /projects/PRJ-1/change-requests/CR-1": {
                "change_request_id": "CR-1",
                "status": status,
            }
        }
    )
    assert run(["--project", "PRJ-1", "workspace", "cr", "show", "CR-1"]) == expected


def test_cr_create_sends_a_draft_request_and_reports_it_as_pending(stub: Any) -> None:
    sent: dict[str, Any] = {}

    def record(request: httpx.Request) -> Any:
        sent.update(body_of(request) or {})
        return {"change_request_id": "CR-1", "status": "draft"}

    run = stub({"POST /projects/PRJ-1/change-requests": record})
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "cr",
            "create",
            "--title",
            "Tighten the intake prompt",
            "--head-revision-id",
            "WSR-1",
            "--semantic-version",
            "1.1.0",
            "--reviewer",
            "@alice",
            "--reviewer",
            "@bob",
        ]
    )
    assert code == exits.AWAITING_HUMAN
    assert sent["title"] == "Tighten the intake prompt"
    assert sent["head_revision_id"] == "WSR-1"
    assert sent["semantic_version"] == "1.1.0"
    assert sent["reviewer_user_ids"] == ["@alice", "@bob"]


def test_cr_submit(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:submit": {
                "change_request_id": "CR-1",
                "status": "open",
            }
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "cr", "submit", "CR-1"])
    assert code == exits.AWAITING_HUMAN


def test_cr_update_head(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:update-head": {
                "change_request_id": "CR-1",
                "status": "open",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "cr",
            "update",
            "CR-1",
            "--revision-id",
            "WSR-2",
            "--expected-lock-version",
            "3",
        ]
    )
    assert code == exits.AWAITING_HUMAN


def test_cr_rebase_recovers_an_out_of_date_request(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:rebase": {
                "change_request_id": "CR-1",
                "status": "open",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "cr",
            "rebase",
            "CR-1",
            "--revision-id",
            "WSR-3",
            "--expected-lock-version",
            "4",
        ]
    )
    assert code == exits.AWAITING_HUMAN


def test_cr_review_request_changes_exits_its_own_code_regardless_of_the_bare_decision_shape(
    stub: Any,
) -> None:
    """``submit_review`` returns the review, not the request -- the exit code
    comes from the decision just recorded, the same choice
    ``gate_verdict_record`` makes for its own verdict."""
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1/reviews": {
                "review_id": "REV-1",
                "decision": "request_changes",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "cr",
            "review",
            "CR-1",
            "--head-id",
            "HEAD-1",
            "--decision",
            "request_changes",
        ]
    )
    assert code == exits.CHANGES_REQUESTED


def test_cr_review_approve_exits_ok(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1/reviews": {
                "review_id": "REV-1",
                "decision": "approve",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "cr",
            "review",
            "CR-1",
            "--head-id",
            "HEAD-1",
            "--decision",
            "approve",
        ]
    )
    assert code == exits.OK


def test_cr_close(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:close": {
                "change_request_id": "CR-1",
                "status": "closed",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "cr",
            "close",
            "CR-1",
            "--reason",
            "superseded",
            "--expected-lock-version",
            "2",
        ]
    )
    assert code == exits.FAILURE


def test_cr_accept_ok(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:accept": {
                "change_request_id": "CR-1",
                "status": "accepted",
            }
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "cr", "accept", "CR-1"])
    assert code == exits.OK


def test_cr_accept_sends_no_body(stub: Any) -> None:
    """``accept_route`` never parses a request body -- see this route's own
    docstring on why a client-supplied QA claim can't be trusted. The CLI
    command takes no evidence flags at all, so confirm the SDK call it
    delegates to still posts an empty body rather than silently growing one."""
    sent: dict[str, Any] = {}

    def record(request: httpx.Request) -> Any:
        sent["body"] = body_of(request)
        return {"change_request_id": "CR-1", "status": "accepted"}

    run = stub({"POST /projects/PRJ-1/change-requests/CR-1:accept": record})
    code = run(["--project", "PRJ-1", "workspace", "cr", "accept", "CR-1"])
    assert code == exits.OK
    assert sent["body"] == {}


def test_cr_accept_without_a_qa_go_decision_exits_awaiting_human(stub: Any) -> None:
    """A `409 qa_go_decision_required` means the command worked and QA simply
    has not recorded a passing decision for the current head yet -- not a
    hard failure, so it must not collapse into ``exits.FAILURE`` the way an
    ordinary 409 does."""
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:accept": httpx.Response(
                409, json={"detail": "qa_go_decision_required"}
            )
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "cr", "accept", "CR-1"])
    assert code == exits.AWAITING_HUMAN


def test_cr_accept_out_of_date_conflict_exits_failure(stub: Any) -> None:
    """A different 409 (a competing acceptance already moved the project's
    accepted revision) is an ordinary conflict, not the "not ready yet"
    case -- only ``qa_go_decision_required`` gets the distinct code."""
    run = stub(
        {
            "POST /projects/PRJ-1/change-requests/CR-1:accept": httpx.Response(
                409, json={"detail": "change_request_out_of_date"}
            )
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "cr", "accept", "CR-1"])
    assert code == exits.FAILURE


# --- releases: blocked / awaiting a decision / approved -----------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("draft", exits.AWAITING_HUMAN),
        ("evaluating", exits.AWAITING_HUMAN),
        ("awaiting_quality_signoff", exits.AWAITING_HUMAN),
        ("awaiting_approval", exits.AWAITING_HUMAN),
        ("blocked", exits.GATE_FAILED),
        ("approved", exits.OK),
        ("rejected", exits.FAILURE),
    ],
)
def test_release_status_maps_every_status_to_its_own_exit_code(
    stub: Any, status: str, expected: int
) -> None:
    run = stub({"GET /projects/PRJ-1/releases/REL-1": {"release_id": "REL-1", "status": status}})
    assert run(["--project", "PRJ-1", "workspace", "release", "status", "REL-1"]) == expected


def test_release_create_sends_the_pinned_digests(stub: Any) -> None:
    sent: dict[str, Any] = {}

    def record(request: httpx.Request) -> Any:
        sent.update(body_of(request) or {})
        return {"release_id": "REL-1", "status": "draft"}

    run = stub({"POST /projects/PRJ-1/releases": record})
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "release",
            "create",
            "--revision-id",
            "WSR-1",
            "--environment-id",
            "development",
            "--environment-config-sha256",
            "a" * 64,
            "--runtime-dependencies-sha256",
            "b" * 64,
            "--policy-sha256",
            "c" * 64,
            "--request-idempotency-key",
            "release-1",
        ]
    )
    assert code == exits.AWAITING_HUMAN
    assert sent["revision_id"] == "WSR-1"
    assert sent["environment_id"] == "development"


def test_release_quality_signoff_no_go_exits_gate_failed(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/releases/REL-1/quality-signoff": {
                "decision_id": "DEC-1",
                "decision": "no_go",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "release",
            "quality-signoff",
            "REL-1",
            "--decision",
            "no_go",
            "--gate-evidence-sha256",
            "d" * 64,
        ]
    )
    assert code == exits.GATE_FAILED


def test_release_approve_go_exits_ok(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/releases/REL-1/approve": {
                "decision_id": "DEC-2",
                "decision": "go",
            }
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "release",
            "approve",
            "REL-1",
            "--decision",
            "go",
            "--gate-evidence-sha256",
            "e" * 64,
        ]
    )
    assert code == exits.OK


def test_release_evaluate_waits_and_reports_a_failed_attempt(stub: Any) -> None:
    run = stub(
        {
            "POST /projects/PRJ-1/releases/REL-1/evaluate": {
                "evaluation_id": "EVAL-1",
                "status": "running",
            },
            "GET /projects/PRJ-1/releases/REL-1/evaluations/EVAL-1": {
                "evaluation_id": "EVAL-1",
                "status": "failed",
            },
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "release",
            "evaluate",
            "REL-1",
            "--idempotency-key",
            "eval-1",
            "--evaluation-plan-sha256",
            "f" * 64,
            "--input-sha256",
            "g" * 64,
            "--timeout",
            "5",
        ]
    )
    assert code == exits.FAILURE


# --- release operations: applied / reconcile-required --------------------


def test_release_apply_waits_and_reports_reconcile_required(stub: Any) -> None:
    operation = {"operation_id": "OP-1", "status": "pending"}
    run = stub(
        {
            "POST /projects/PRJ-1/releases/REL-1/operations": {"operation": operation, "items": []},
            "POST /projects/PRJ-1/releases/REL-1/operations/OP-1:apply": {
                "operation": operation,
                "items": [],
            },
            "GET /projects/PRJ-1/releases/REL-1/operations/OP-1": {
                "operation": {"operation_id": "OP-1", "status": "reconcile_required"},
                "items": [],
            },
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "release",
            "apply",
            "REL-1",
            "--expected-environment-lock-version",
            "1",
            "--idempotency-key",
            "apply-1",
            "--timeout",
            "5",
        ]
    )
    assert code == exits.RECONCILE_REQUIRED


def test_release_rollback_applies_and_exits_ok(stub: Any) -> None:
    applied = {"operation_id": "OP-2", "status": "applied"}
    run = stub(
        {
            "POST /projects/PRJ-1/releases/REL-1/operations": {
                "operation": {"operation_id": "OP-2", "status": "pending"},
                "items": [],
            },
            "POST /projects/PRJ-1/releases/REL-1/operations/OP-2:apply": {
                "operation": applied,
                "items": [],
            },
        }
    )
    code = run(
        [
            "--project",
            "PRJ-1",
            "workspace",
            "release",
            "rollback",
            "REL-1",
            "--target-release-id",
            "REL-0",
            "--expected-environment-lock-version",
            "1",
            "--idempotency-key",
            "rollback-1",
            "--no-wait",
        ]
    )
    assert code == exits.OK


def test_release_operation_status_not_terminal_exits_timeout(stub: Any) -> None:
    run = stub(
        {
            "GET /projects/PRJ-1/releases/REL-1/operations/OP-1": {
                "operation": {"operation_id": "OP-1", "status": "applying"},
                "items": [],
            }
        }
    )
    code = run(["--project", "PRJ-1", "workspace", "release", "operation-status", "REL-1", "OP-1"])
    assert code == exits.TIMEOUT
