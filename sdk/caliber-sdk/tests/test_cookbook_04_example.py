"""Cookbook 04's runnable example, replayed offline against ``cookbook_04_fake_server``.

The fake runs the lab's real extractor, schema and bucket rules, so these tests prove the script's
logic and the dataset's expectations agree; the live run in the lab README proves the platform
around them.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from cookbook_04_fake_server import FakeDocumentServer

from caliber_sdk import CaliberClient
from examples.cookbooks.cookbook_04_document_to_json_pipeline import (
    LabVerificationError,
    load_cases,
    run,
)


def clients(server: FakeDocumentServer) -> tuple[Any, Any]:
    admin = server.client(CaliberClient, token="calpat_admin")
    reviewer = server.client(CaliberClient, token="calpat_reviewer")
    return admin, reviewer


def test_the_dataset_has_the_documented_mix() -> None:
    outcomes = [c["expectations"]["outcome"] for c in load_cases()]
    assert (
        outcomes.count("extracted"),
        outcomes.count("needs_review"),
        outcomes.count("duplicate"),
    ) == (
        16,
        13,
        1,
    )


def test_the_lab_runs_end_to_end_and_every_document_lands_where_it_should() -> None:
    server = FakeDocumentServer()
    admin, reviewer = clients(server)
    with admin, reviewer:
        summary = run(admin, reviewer=reviewer, suffix="-t")
    assert summary["documents"] == 30
    assert len(summary["extracted"]) == 16 and len(summary["held"]) == 13
    assert summary["still_waiting"] == 11
    assert "already exists" in summary["duplicate_error"]
    assert "found 0" in summary["missing_object_error"]
    assert summary["self_approval_error"].startswith("[403]")
    assert [k for k in summary["output_objects"] if k.startswith("reviewed/")] == [
        "reviewed/inv-2026-0115-no-total.json"
    ]
    assert len(summary["output_objects"]) == 17
    # the order the README documents
    calls = server.calls
    ordered = [
        "POST /cookbooks/04/install",
        "PATCH /workflows/WF-04",
        "POST /workflow-runs",
    ]
    positions = [calls.index(call) for call in ordered]
    assert positions == sorted(positions)
    approve = next(i for i, call in enumerate(calls) if call.endswith("/approval/approve"))
    assert approve > positions[-1]


def test_it_stops_when_the_object_store_is_not_configured() -> None:
    server = FakeDocumentServer(store_ready=False)
    admin, reviewer = clients(server)
    with admin, reviewer, pytest.raises(LabVerificationError, match="configuration checks pass"):
        run(admin, reviewer=reviewer)


def test_it_notices_when_the_initiator_can_approve_their_own_run() -> None:
    class Lenient(FakeDocumentServer):
        def decide(self, request: httpx.Request, token: str, run_id: str, decision: str) -> Any:
            return super().decide(request, "someone-else", run_id, decision)

    server = Lenient()
    admin, reviewer = clients(server)
    with admin, reviewer, pytest.raises(LabVerificationError, match="initiator approved"):
        run(admin, reviewer=reviewer)


def test_it_notices_when_an_invalid_document_is_not_held() -> None:
    class Permissive(FakeDocumentServer):
        def _execute(self, run: dict[str, Any], key: str) -> None:
            super()._execute(run, key)
            if run["status"] == "waiting_approval":
                run["status"] = "completed"

    server = Permissive()
    admin, reviewer = clients(server)
    with admin, reviewer, pytest.raises(LabVerificationError, match="is held for approval"):
        run(admin, reviewer=reviewer)
