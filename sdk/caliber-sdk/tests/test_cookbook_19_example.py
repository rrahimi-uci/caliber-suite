"""Cookbook 19's runnable example: unit tests for its own pure helpers.

OpenAPI Integrations' business logic (classification, dependency detection, diffing) lives in
CALIBER itself, not in an external system this script talks to the way cookbook 05 talks to an
MCP server. Hand-faking that logic would risk the fake drifting from the real behavior the script
and the lab's README depend on, so there is no ``cookbook_19_fake_server.py`` here. Instead:

* this file unit-tests the script's own helpers that need no server at all (ledger path
  resolution, reading the upstream's ledger format, filling the workflow template);
* ``caliber/tests/test_cookbook_19_openapi_integration_governance_lab.py`` feeds the lab's spec
  files through CALIBER's real classification/dependency/diff code directly, in-process;
* the lab's README captures a verified, real run of ``run()`` end to end against a live CALIBER
  server and the lab's own upstream.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.cookbooks import cookbook_19_openapi_integration_governance as lab


def test_ledger_path_prefers_the_explicit_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TICKET_API_LEDGER", str(tmp_path / "explicit.jsonl"))
    monkeypatch.setenv("CALIBER_STATE_DIR", str(tmp_path / "state"))
    assert lab.ledger_path() == tmp_path / "explicit.jsonl"
    monkeypatch.delenv("TICKET_API_LEDGER")
    assert lab.ledger_path() == tmp_path / "state" / "lab19-ticket-ledger.jsonl"
    monkeypatch.delenv("CALIBER_STATE_DIR")
    with pytest.raises(SystemExit):
        lab.ledger_path()


def test_ledger_helpers_read_the_upstreams_own_record(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    assert lab.ledger_entries(ledger) == []
    assert lab.executed(ledger, "POST", "/tickets") == 0
    ledger.write_text(
        json.dumps({"method": "POST", "path": "/tickets", "ok": True})
        + "\n"
        + json.dumps({"method": "POST", "path": "/tickets", "ok": False})
        + "\n"
        + json.dumps({"method": "GET", "path": "/tickets/TCK-1", "ok": True})
        + "\n"
    )
    # Only the real (ok=True) execution of the matching method+path counts.
    assert lab.executed(ledger, "POST", "/tickets") == 1
    assert (
        lab.executed(ledger, "GET", "/tickets") == 1
    )  # startswith() also matches the /TCK-1 suffix
    assert lab.executed(ledger, "DELETE", "/tickets") == 0


def test_ticket_api_base_and_token_read_their_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAPI_TICKET_PORT", raising=False)
    assert lab.ticket_api_base() == "http://127.0.0.1:8781"
    monkeypatch.setenv("OPENAPI_TICKET_PORT", "9999")
    assert lab.ticket_api_base() == "http://127.0.0.1:9999"
    monkeypatch.delenv("OPENAPI_TICKET_TOKEN", raising=False)
    assert lab.ticket_api_token() == "ticket-api-token"
    monkeypatch.setenv("OPENAPI_TICKET_TOKEN", "a-real-token")
    assert lab.ticket_api_token() == "a-real-token"


def test_workflow_yaml_fills_the_tool_name_placeholder() -> None:
    text = lab.workflow_yaml("create_ticket_v1")
    assert "__TOOL_NAME__" not in text
    assert "tool_name: create_ticket_v1" in text
    assert "registry_ref: tool.create_ticket_v1.v1" in text
