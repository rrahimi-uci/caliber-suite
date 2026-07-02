"""Coverage insurance for pure, dependency-free helper modules.

Targets the uncovered canned-tool bodies + helper branches in
``caliber.workflows.demo_tools`` and the guardrail check/judge-client branches
in ``caliber.workflows.guardrails`` that the broader suite doesn't exercise
directly. Every test asserts real return values — no bare calls.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import pytest

from caliber.workflows import demo_tools as dt
from caliber.workflows import guardrails as gr
from caliber.workflows.guardrails import GuardrailContext

# ══════════════════════════════════════════════════════════════════════
# demo_tools — canned scenario tool stubs (read-only / stub returns)
# ══════════════════════════════════════════════════════════════════════


def test_scenario_tool_stubs_return_canned_shapes() -> None:
    assert dt.escalate("angry customer")["escalated"] is True
    assert dt.escalate("angry customer")["reason"] == "angry customer"
    assert dt.lookup_order("A-1") == {
        "order_id": "A-1",
        "items": ["ACME Laptop"],
        "status": "delivered",
    }
    assert dt.lookup_order()["order_id"] == "12345"
    refund = dt.initiate_refund("A-1")
    assert refund["refund_status"] == "initiated"
    assert refund["amount_usd"] == 1299.0
    ship = dt.track_shipment()
    assert ship["tracking_id"] == "1Z999"
    assert ship["eta_days"] == 2
    kb = dt.search_knowledge_base("returns")
    assert kb["query"] == "returns"
    assert "Returns & refunds" in kb["articles"]


def test_travel_and_commerce_tool_stubs() -> None:
    travel = dt.parse_travel_request("fly me to tokyo")
    assert travel["origin"] == "SFO"
    assert travel["destination"] == "TYO"
    assert travel["query"] == "fly me to tokyo"
    flights = dt.search_flights("cheap")
    assert flights["options"][0]["flight"] == "UA837"
    booked = dt.book_flight("UA100")
    assert booked["flight"] == "UA100"
    assert booked["confirmation"] == "ABC123"
    hotels = dt.search_hotels("shibuya")
    assert hotels["options"][0]["hotel"] == "Shibuya Grand"
    assert hotels["query"] == "shibuya"
    hotel = dt.book_hotel()
    assert hotel["hotel"] == "Shibuya Grand"
    assert hotel["amount_usd"] == 1540.0
    product = dt.lookup_product("SKU-9")
    assert product["sku"] == "SKU-9"
    assert product["name"] == "ACME Pro Widget"
    inv = dt.check_inventory()
    assert inv["in_stock"] is True
    assert inv["quantity_available"] == 128
    order = dt.create_order("payload")
    assert order["order_id"] == "ORD-9001"
    assert order["total"] == 147.0
    email = dt.send_confirmation_email("payload")
    assert email == {"sent": True, "channel": "email"}


def test_run_workdir_tool_stubs_are_benign_placeholders() -> None:
    note = dt._FILE_TOOL_NOTE
    assert dt.list_workdir_files() == {"files": [], "note": note}
    assert dt.read_workdir_file() == {"content": "", "note": note}
    assert dt.get_file_metadata() == {"note": note}
    assert dt.write_workdir_file() == {"written": False, "note": note}
    assert dt.create_artifact() == {"registered": False, "note": note}


def test_relative_to_falls_back_to_name_on_unrelated_base() -> None:
    # path not under base -> ValueError branch -> returns the bare name
    assert dt._relative_to(Path("/alpha/beta/gamma.txt"), Path("/other/root")) == "gamma.txt"


def test_clip_output_handles_none_and_bytes() -> None:
    assert dt._clip_output(None) == ""
    assert dt._clip_output(b"hello bytes") == "hello bytes"
    assert dt._clip_output("plain") == "plain"
    assert dt._clip_output("x" * 5000, limit=10) == "x" * 10


def test_iter_files_rejects_non_dir_non_file(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):  # pragma: no cover - platform without FIFOs
        pytest.skip("mkfifo unavailable on this platform")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)  # exists, but neither a regular file nor a directory
    with pytest.raises(NotADirectoryError):
        dt._iter_files(str(fifo), pattern="**/*", recursive=True, max_files=10)


# ══════════════════════════════════════════════════════════════════════
# guardrails — deterministic check branches
# ══════════════════════════════════════════════════════════════════════


def test_budget_limit_skips_non_dict_calls_and_passes_under_limit() -> None:
    ctx = GuardrailContext(
        response_text="", tool_calls=[{"cost_usd": 10.0}, "not-a-dict", {"amount_usd": 5.0}]
    )
    result = gr._budget_limit({"max_usd": 100}, ctx)
    assert result.passed is True
    assert result.kind == "budget_limit"


def test_schema_validation_passes_with_no_required_fields() -> None:
    result = gr._schema_validation({}, GuardrailContext(response_text="{}"))
    assert result.passed is True


def test_schema_validation_passes_when_json_is_not_an_object() -> None:
    # valid JSON, but a list -> nothing structured to validate -> passes
    result = gr._schema_validation(
        {"required_fields": ["a", "b"]}, GuardrailContext(response_text="[1, 2, 3]")
    )
    assert result.passed is True


def test_tool_evidence_skips_non_dicts_and_none_results() -> None:
    ctx = GuardrailContext(
        tool_calls=["bare-string", {"result": None}, {"result": "grounded"}, {"result": {"k": 1}}]
    )
    evidence = gr._tool_evidence(ctx)
    assert "grounded" in evidence
    assert '"k": 1' in evidence
    assert "bare-string" not in evidence


def test_judge_client_returns_none_and_caches_without_openai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fresh cache; default (non-openai) config resolves no judge -> None, then cached.
    monkeypatch.setattr(gr, "_JUDGE_CLIENT", [])
    fake_cfg = types.SimpleNamespace(
        llm_provider="fake",
        llm_api_key_env="OPENAI_API_KEY",
        llm_base_url="",
        llm_diagnosis_model="gpt-x",
    )
    monkeypatch.setattr("caliber.config.CaliberConfig.load", lambda *a, **k: fake_cfg)
    monkeypatch.setattr("caliber.secrets.resolve_secret", lambda *a, **k: None)
    assert gr._judge_client() is None
    # second call hits the cached branch
    assert gr._judge_client() is None
    assert gr._JUDGE_CLIENT == [None]


def test_judge_client_builds_openai_client_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gr, "_JUDGE_CLIENT", [])
    fake_cfg = types.SimpleNamespace(
        llm_provider="OpenAI",  # case-insensitive match
        llm_api_key_env="OPENAI_API_KEY",
        llm_base_url="https://gw.example/v1",
        llm_diagnosis_model="gpt-judge",
    )
    monkeypatch.setattr("caliber.config.CaliberConfig.load", lambda *a, **k: fake_cfg)
    monkeypatch.setattr("caliber.secrets.resolve_secret", lambda *a, **k: "sk-test-key")

    sentinel_client = object()
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = lambda **kwargs: sentinel_client  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    built = gr._judge_client()
    assert built is not None
    client, model = built
    assert client is sentinel_client
    assert model == "gpt-judge"
    # cached on repeat
    assert gr._judge_client() is built


def test_enforce_guardrails_blocks_on_failing_output_check() -> None:
    specs = [
        {
            "node_id": "g1",
            "mode": "post_agent",
            "on_failure": "block",
            "checks": [{"kind": "forbid_substring", "params": {"substring": "secret"}}],
        }
    ]
    with pytest.raises(gr.GuardrailBlockedError):
        gr.enforce_guardrails("this reveals a secret", [], specs)


def test_enforce_guardrails_skips_pre_agent_mode() -> None:
    specs = [
        {
            "node_id": "g-pre",
            "mode": "pre_agent",
            "on_failure": "block",
            "checks": [{"kind": "forbid_substring", "params": {"substring": "secret"}}],
        }
    ]
    # pre_agent guard is not an output guard here -> must not raise
    gr.enforce_guardrails("mentions secret in output", [], specs)


def test_enforce_guardrails_coerces_unknown_failure_mode_to_block() -> None:
    specs = [
        {
            "node_id": "g2",
            "mode": "post_agent",
            "on_failure": "bogus-mode",  # not in the closed vocabulary -> block
            "checks": [{"kind": "non_empty_output", "params": {}}],
        }
    ]
    with pytest.raises(gr.GuardrailBlockedError):
        gr.enforce_guardrails("", [], specs)
