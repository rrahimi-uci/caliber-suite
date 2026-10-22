"""Cookbook 19: the lab's upstream, its two spec versions, and the checks the lab's script relies on.

OpenAPI Integrations' business logic (classification, dependency detection, diffing) lives in
CALIBER itself, not in a hand-fakeable external system the way an MCP server is. Rather than
re-implement that logic in a fake (risking the fake drifting from the real behavior the lab's README
and SDK script depend on), these tests feed the lab's own spec files through CALIBER's real
``caliber.integrations.openapi`` functions directly, in-process, with no server and no network. That
proves the dataset is consistent with CALIBER's real code; the live run captured in the lab's README
proves the same contract holds end to end through the HTTP API.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from caliber.integrations.openapi.classify import classify_operation
from caliber.integrations.openapi.dependencies import detect_dependencies
from caliber.integrations.openapi.diff import diff_operations
from caliber.integrations.openapi.normalize import normalize_openapi_document, parse_openapi_text

LAB = (
    Path(__file__).resolve().parents[2]
    / "docs-site/cookbooks/19-openapi-integration-governance-lab"
)
DATASET = LAB / "assets/dataset"
EXPECTED_OPERATION_KEYS = {"GET /tickets", "POST /tickets", "GET /tickets/{ticket_id}"}


def _load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "lab19_ticket_api", LAB / "assets/server/ticket_api.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


tracker = _load_server_module()


def _operations_with_ids(spec_text: str) -> list[dict[str, Any]]:
    """Normalize a spec and assign the ``operation_id`` dependency detection needs."""
    document = parse_openapi_text(spec_text)
    _summary, operations, _warnings = normalize_openapi_document(document)
    for index, operation in enumerate(operations):
        operation["operation_id"] = f"OAIO-{index}"
    return operations


# --------------------------------------------------------------------------------------------- #
# The two spec files match what ticket_api.py actually embeds and serves
# --------------------------------------------------------------------------------------------- #


def test_the_dataset_spec_files_match_what_the_server_embeds() -> None:
    assert (DATASET / "ticket-api-v1.yaml").read_text().strip() == tracker.SPEC_V1.strip()
    assert (DATASET / "ticket-api-v2.yaml").read_text().strip() == tracker.SPEC_V2.strip()


# --------------------------------------------------------------------------------------------- #
# Classification: real CALIBER code, not a guess
# --------------------------------------------------------------------------------------------- #


def test_v1_normalizes_to_three_operations_classified_as_the_readme_claims() -> None:
    operations = _operations_with_ids(tracker.SPEC_V1)
    by_key = {op["operation_key"]: op for op in operations}
    assert set(by_key) == EXPECTED_OPERATION_KEYS
    assert by_key["GET /tickets"]["side_effect_level"] == "read"
    assert by_key["GET /tickets/{ticket_id}"]["side_effect_level"] == "read"
    assert by_key["POST /tickets"]["side_effect_level"] == "write"


def test_classify_operation_is_a_pure_function_of_the_normalized_draft() -> None:
    operations = _operations_with_ids(tracker.SPEC_V1)
    create = next(op for op in operations if op["operation_key"] == "POST /tickets")
    # Re-classifying the same draft is deterministic -- the property import-diff-ability relies on.
    assert classify_operation(create)["side_effect_level"] == "write"


# --------------------------------------------------------------------------------------------- #
# Dependency detection: one auto-wired (explicit link), plus weaker structural/advisory ones
# --------------------------------------------------------------------------------------------- #


def test_the_explicit_link_auto_wires_create_to_get() -> None:
    operations = _operations_with_ids(tracker.SPEC_V1)
    by_key = {op["operation_key"]: op for op in operations}
    rows = detect_dependencies(operations)
    high_confidence = [row for row in rows if row["confidence"] == "high"]
    assert len(high_confidence) == 1
    [link] = high_confidence
    assert link["source"] == "openapi_link"
    assert link["from_operation_id"] == by_key["POST /tickets"]["operation_id"]
    assert link["to_operation_id"] == by_key["GET /tickets/{ticket_id}"]["operation_id"]
    assert link["dependency_type"] == "produces_identifier_for"


def test_weaker_tiers_also_fire_and_stay_reviewable() -> None:
    """Path-hierarchy/identifier-flow land as 'suggested'; same-tag grouping as 'advisory'.

    The route layer (not exercised here -- see the live run in the README) maps confidence to an
    initial row status: high -> auto_wired, medium -> suggested, low has no entry at all, which is
    what makes a same-tag ("low") row land as the catch-all reviewable state rather than being
    silently dropped. This test only pins the confidence tiers CALIBER's pure detector assigns; the
    cookbook's own script proves the review flow against a live server.
    """
    operations = _operations_with_ids(tracker.SPEC_V1)
    rows = detect_dependencies(operations)
    confidences = {row["confidence"] for row in rows}
    assert "medium" in confidences, "path-hierarchy/identifier-flow should also fire for this spec"
    assert "low" in confidences, "all three operations share the 'tickets' tag"
    # Every row must name operations that actually exist (a sanity check on the detector's inputs).
    ids = {op["operation_id"] for op in operations}
    assert all(row["from_operation_id"] in ids and row["to_operation_id"] in ids for row in rows)


# --------------------------------------------------------------------------------------------- #
# Diff: the exact contract the lab's script and README assert
# --------------------------------------------------------------------------------------------- #


def test_reimport_diff_catches_the_deprecation_and_misses_the_required_field() -> None:
    before = _operations_with_ids(tracker.SPEC_V1)
    after = _operations_with_ids(tracker.SPEC_V2)
    diff = diff_operations(before, after)
    assert diff["summary"] == {
        "added_count": 0,
        "removed_count": 0,
        "changed_count": 2,
        "unchanged_count": 1,
        "breaking_count": 1,
    }
    [breaking] = diff["breaking"]
    assert breaking == {
        "operation_key": "GET /tickets/{ticket_id}",
        "reason": "operation was deprecated",
    }
    changed_keys = {entry["operation_key"] for entry in diff["changed"]}
    assert changed_keys == {"GET /tickets", "GET /tickets/{ticket_id}"}
    # The point of the lab: a real behavioral break (a newly required body field) with zero
    # representation in the diff, because request-body field requirements are not a tracked field.
    assert "POST /tickets" not in changed_keys
    assert diff["unchanged"] == ["POST /tickets"]


# --------------------------------------------------------------------------------------------- #
# The upstream itself, exercised over real HTTP (no CALIBER involved)
# --------------------------------------------------------------------------------------------- #


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real instance of the lab's upstream, on an OS-assigned loopback port."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(tracker, "TOKEN", "test-token")
    monkeypatch.setenv("TICKET_API_LEDGER", str(ledger))
    monkeypatch.setattr(tracker, "_spec_version", 1)
    monkeypatch.setattr(tracker, "_tickets", [])
    monkeypatch.setattr(tracker, "_next_id", 1)
    server = tracker.ThreadingHTTPServer(("127.0.0.1", 0), tracker.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, ledger
    finally:
        server.shutdown()
        server.server_close()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    content_type = response.getheader("content-type", "")
    conn.close()
    if not data:
        return response.status, None
    if "json" in content_type:
        return response.status, json.loads(data)
    return response.status, data.decode("utf-8")


def _ledger_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_health_and_spec_need_no_auth(upstream: Any) -> None:
    server, _ledger = upstream
    port = server.server_address[1]
    status, body = _request(port, "GET", "/health")
    assert (status, body) == (200, {"ok": True})
    status, _body = _request(port, "GET", "/openapi.yaml")
    assert status == 200


def test_every_other_route_needs_the_bearer_token(upstream: Any) -> None:
    server, _ledger = upstream
    port = server.server_address[1]
    status, _body = _request(port, "GET", "/tickets")
    assert status == 401
    status, _body = _request(port, "GET", "/tickets", token="wrong")
    assert status == 401


def test_create_then_get_a_ticket_and_the_ledger_proves_it(upstream: Any) -> None:
    server, ledger = upstream
    port = server.server_address[1]
    assert _ledger_lines(ledger) == []
    status, created = _request(
        port, "POST", "/tickets", token="test-token", body={"title": "first"}
    )
    assert status == 201
    assert created == {
        "ticket_id": "TCK-1",
        "title": "first",
        "priority": "normal",
        "status": "open",
    }
    status, fetched = _request(port, "GET", "/tickets/TCK-1", token="test-token")
    assert status == 200 and fetched == created
    status, missing = _request(port, "GET", "/tickets/TCK-404", token="test-token")
    assert status == 404
    lines = _ledger_lines(ledger)
    assert [(line["method"], line["path"], line["ok"]) for line in lines] == [
        ("POST", "/tickets", True),
        ("GET", "/tickets/TCK-1", True),
        ("GET", "/tickets/TCK-404", False),
    ]


def test_a_refused_call_never_reaches_the_ticket_logic(upstream: Any) -> None:
    """An unauthenticated call is refused before it is ledgered at all -- not ledgered as a failure."""
    server, ledger = upstream
    port = server.server_address[1]
    _request(port, "GET", "/tickets")
    _request(port, "POST", "/tickets", body={"title": "should not be ledgered"})
    assert _ledger_lines(ledger) == []


def test_bumping_the_spec_requires_priority_on_new_tickets(upstream: Any) -> None:
    server, ledger = upstream
    port = server.server_address[1]
    status, bumped = _request(port, "POST", "/admin/bump-spec")
    assert (status, bumped) == (200, {"spec_version": 2})
    status, refused = _request(
        port, "POST", "/tickets", token="test-token", body={"title": "no priority"}
    )
    assert status == 400 and refused == {"error": "priority is required"}
    status, created = _request(
        port,
        "POST",
        "/tickets",
        token="test-token",
        body={"title": "has priority", "priority": "high"},
    )
    assert status == 201 and created["priority"] == "high"
    assert [line["ok"] for line in _ledger_lines(ledger)] == [False, True]


def test_the_served_document_actually_changes_after_bump_spec(upstream: Any) -> None:
    server, _ledger = upstream
    port = server.server_address[1]
    status, v1_text = _request(port, "GET", "/openapi.yaml")
    assert status == 200 and "deprecated" not in v1_text
    _request(port, "POST", "/admin/bump-spec")
    status, v2_text = _request(port, "GET", "/openapi.yaml")
    assert status == 200 and "deprecated: true" in v2_text
