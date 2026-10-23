"""Cookbook 20's runnable example: unit tests for its own pure helpers.

The gateway surface's business logic (endpoint mapping, usage rollup, cost arithmetic) lives in
CALIBER itself, and the stand-in gateway's HTTP contract is pinned by
``caliber/tests/test_cookbook_20_llm_gateway_cost_lab.py`` against CALIBER's own parsing code.
This file unit-tests the script's helpers that need no server at all (ledger reading, environment
resolution, the by-model lookup, and the cost constants the README's arithmetic is written in).
The lab's README captures a verified, real run of ``run()`` end to end against a live CALIBER
server and the lab's own stand-in gateway.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.cookbooks import cookbook_20_llm_gateway_cost_guardrails as lab


def test_the_cost_constants_are_one_consistent_story() -> None:
    assert lab.TOKENS_PER_CALL == 2000
    assert (
        lab.COST_PER_CALL == (1000 / 1000) * lab.PROMPT_PRICE + (1000 / 1000) * lab.COMPLETION_PRICE
    )


def test_gateway_base_and_key_read_their_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAB20_GATEWAY_PORT", raising=False)
    assert lab.gateway_base() == "http://127.0.0.1:8791"
    monkeypatch.setenv("LAB20_GATEWAY_PORT", "9999")
    assert lab.gateway_base() == "http://127.0.0.1:9999"
    monkeypatch.delenv("LAB20_GATEWAY_KEY", raising=False)
    assert lab.gateway_key() == "lab20-gateway-key"
    monkeypatch.setenv("LAB20_GATEWAY_KEY", "a-real-key")
    assert lab.gateway_key() == "a-real-key"


def test_ledger_path_prefers_the_explicit_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LLM_GATEWAY_LEDGER", str(tmp_path / "explicit.jsonl"))
    monkeypatch.setenv("CALIBER_STATE_DIR", str(tmp_path / "state"))
    assert lab.ledger_path() == tmp_path / "explicit.jsonl"
    monkeypatch.delenv("LLM_GATEWAY_LEDGER")
    assert lab.ledger_path() == tmp_path / "state" / "lab20-gateway-ledger.jsonl"
    monkeypatch.delenv("CALIBER_STATE_DIR")
    with pytest.raises(SystemExit):
        lab.ledger_path()


def test_gateway_calls_counts_only_the_matching_outcome(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    assert lab.gateway_calls(ledger, ok=True) == 0
    ledger.write_text(
        json.dumps(
            {
                "path": "/v1/chat/completions",
                "model": "lab-chat-mini",
                "ok": True,
                "prompt_tokens": 1000,
                "completion_tokens": 1000,
            }
        )
        + "\n"
        + json.dumps(
            {
                "path": "/v1/chat/completions",
                "model": "lab-chat-mini",
                "ok": False,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        )
        + "\n"
    )
    assert lab.gateway_calls(ledger, ok=True) == 1
    assert lab.gateway_calls(ledger, ok=False) == 1


def test_by_model_row_finds_the_model_or_nothing() -> None:
    usage = {"by_model": [{"model": "lab-chat-mini", "calls": 2, "tokens": 4000, "cost_usd": 6.0}]}
    row = lab.by_model_row(usage, "lab-chat-mini")
    assert row is not None and row["calls"] == 2
    assert lab.by_model_row(usage, "other") is None
    assert lab.by_model_row({}, "lab-chat-mini") is None
