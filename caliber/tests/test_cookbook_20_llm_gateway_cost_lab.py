"""Cookbook 20: the lab's stand-in gateway, and the contracts the lab's numbers rely on.

The lab's claims are arithmetic: a flat 1000+1000 token bill per call, so a $1/$2-per-1K pricing
row makes every call cost exactly $3.00. These tests pin the stand-in to that contract over real
HTTP (discovery shape, the OpenAI response CALIBER's executor parses, bearer enforcement, the
pause/reset admin surface, the write-ledger-before-respond rule), and pin the stand-in's discovery
payload against CALIBER's own ``_map_endpoint`` so the Endpoints tab can never silently drift from
what the lab serves. The live run captured in the lab's README proves the same contract holds end
to end through the HTTP API, workflow runtime and usage rollup.
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
import yaml

from caliber.routes.gateway import _map_endpoint
from caliber.workflows.manifest import parse_manifest

LAB = Path(__file__).resolve().parents[2] / "docs-site/cookbooks/20-llm-gateway-cost-guardrails-lab"


def _load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "lab20_llm_gateway", LAB / "assets/server/llm_gateway.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


gateway = _load_server_module()


# --------------------------------------------------------------------------------------------- #
# The dataset's arithmetic and the workflow asset agree with the README's claims
# --------------------------------------------------------------------------------------------- #


def test_the_flat_bill_makes_the_readmes_cost_arithmetic_exact() -> None:
    """1000+1000 tokens at $1/$2 per 1K is $3.00 -- the number every lab surface asserts."""
    assert gateway.PROMPT_TOKENS == 1000 and gateway.COMPLETION_TOKENS == 1000
    cost = (gateway.PROMPT_TOKENS / 1000) * 1.0 + (gateway.COMPLETION_TOKENS / 1000) * 2.0
    assert cost == 3.0


def test_the_workflow_asset_names_the_gateways_chat_model() -> None:
    """The join the whole lab rides on: agent model == discovery model == pricing model_id."""
    manifest_text = (LAB / "assets/workflow/metered-reply.workflow.yaml").read_text()
    manifest = parse_manifest(yaml.safe_load(manifest_text))
    agents = [node for node in manifest.nodes.values() if node.type == "agent"]
    assert len(agents) == 1
    chat = next(e for e in gateway.ENDPOINTS["endpoints"] if e["endpoint_type"] == "llm/v1/chat")
    assert agents[0].model == chat["model"]["name"] == "lab-chat-mini"


def test_discovery_rows_survive_calibers_own_endpoint_mapping() -> None:
    """The stand-in's payload is shaped like the MLflow AI Gateway's, as CALIBER parses it."""
    mapped = [_map_endpoint(raw) for raw in gateway.ENDPOINTS["endpoints"]]
    assert [(e.name, e.endpoint_type, e.provider, e.model) for e in mapped] == [
        ("chat-lab", "llm/v1/chat", "lab", "lab-chat-mini"),
        ("embeddings-lab", "llm/v1/embeddings", "lab", "lab-embed-mini"),
    ]


def test_the_chat_response_parses_as_a_real_openai_completion() -> None:
    """CALIBER's executor reads the reply through the openai SDK's own model."""
    openai_types = pytest.importorskip("openai.types.chat")
    payload = {
        "id": "chatcmpl-lab20",
        "object": "chat.completion",
        "created": 1,
        "model": "lab-chat-mini",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": gateway.REPLY},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": gateway.PROMPT_TOKENS,
            "completion_tokens": gateway.COMPLETION_TOKENS,
            "total_tokens": gateway.PROMPT_TOKENS + gateway.COMPLETION_TOKENS,
        },
    }
    completion = openai_types.ChatCompletion.model_validate(payload)
    assert completion.usage is not None and completion.usage.total_tokens == 2000
    assert completion.choices[0].message.content == gateway.REPLY


# --------------------------------------------------------------------------------------------- #
# The stand-in itself, exercised over real HTTP (no CALIBER involved)
# --------------------------------------------------------------------------------------------- #


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A real instance of the lab's gateway, on an OS-assigned loopback port."""
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(gateway, "KEY", "test-key")
    monkeypatch.setenv("LLM_GATEWAY_LEDGER", str(ledger))
    monkeypatch.setattr(gateway, "_paused", False)
    server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
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
    conn.close()
    return response.status, json.loads(data) if data else None


def _ledger_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_health_and_discovery_need_no_auth(upstream: Any) -> None:
    server, _ledger = upstream
    port = server.server_address[1]
    assert _request(port, "GET", "/health") == (200, {"ok": True})
    status, listing = _request(port, "GET", "/api/2.0/endpoints/")
    assert status == 200 and [e["name"] for e in listing["endpoints"]] == [
        "chat-lab",
        "embeddings-lab",
    ]


def test_chat_needs_the_bearer_key_and_the_refusal_is_in_the_ledger(upstream: Any) -> None:
    server, ledger = upstream
    port = server.server_address[1]
    status, body = _request(
        port, "POST", "/v1/chat/completions", body={"model": "lab-chat-mini", "messages": []}
    )
    assert status == 401 and body["error"]["type"] == "auth_error"
    status, _body = _request(
        port,
        "POST",
        "/v1/chat/completions",
        token="wrong",
        body={"model": "lab-chat-mini", "messages": []},
    )
    assert status == 401
    assert [line["ok"] for line in _ledger_lines(ledger)] == [False, False]


def test_a_real_call_bills_the_flat_rate_and_the_ledger_proves_it(upstream: Any) -> None:
    server, ledger = upstream
    port = server.server_address[1]
    status, completion = _request(
        port,
        "POST",
        "/v1/chat/completions",
        token="test-key",
        body={"model": "lab-chat-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert status == 200
    assert completion["usage"] == {
        "prompt_tokens": 1000,
        "completion_tokens": 1000,
        "total_tokens": 2000,
    }
    assert completion["model"] == "lab-chat-mini"
    [line] = _ledger_lines(ledger)
    assert line == {
        "path": "/v1/chat/completions",
        "model": "lab-chat-mini",
        "ok": True,
        "prompt_tokens": 1000,
        "completion_tokens": 1000,
    }


def test_pause_degrades_discovery_and_chat_until_resume(upstream: Any) -> None:
    server, ledger = upstream
    port = server.server_address[1]
    assert _request(port, "POST", "/admin/pause")[0] == 200
    assert _request(port, "GET", "/api/2.0/endpoints/")[0] == 503
    status, _ = _request(
        port, "POST", "/v1/chat/completions", token="test-key", body={"model": "m", "messages": []}
    )
    assert status == 503
    # Health stays up: the process is alive, the gateway behind it is "down".
    assert _request(port, "GET", "/health")[0] == 200
    assert _request(port, "POST", "/admin/resume")[0] == 200
    assert _request(port, "GET", "/api/2.0/endpoints/")[0] == 200
    # The refused-while-paused call is in the ledger as not-ok.
    assert [line["ok"] for line in _ledger_lines(ledger)] == [False]


def test_reset_unpauses_for_a_rerun_on_the_same_process(upstream: Any) -> None:
    server, _ledger = upstream
    port = server.server_address[1]
    _request(port, "POST", "/admin/pause")
    assert _request(port, "POST", "/admin/reset") == (200, {"paused": False})
    assert _request(port, "GET", "/api/2.0/endpoints/")[0] == 200


def test_unknown_routes_are_404(upstream: Any) -> None:
    server, _ledger = upstream
    port = server.server_address[1]
    assert _request(port, "GET", "/v1/models")[0] == 404
    assert _request(port, "POST", "/v1/embeddings", token="test-key", body={})[0] == 404
