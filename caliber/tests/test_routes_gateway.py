"""Integration tests for ``/caliber/gateway`` (MLflow AI Gateway discovery).

The external gateway HTTP call is faked by monkeypatching the route module's
``httpx`` so no real gateway is needed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.gateway as gateway_route
from caliber.db.models import CaliberAuditLog

GATEWAY = "/ajax-api/2.0/mlflow/caliber/gateway"
GUARDRAILS = f"{GATEWAY}/guardrails"
GUARDRAILS_CATALOG = f"{GATEWAY}/guardrails/catalog"
USAGE = f"{GATEWAY}/usage"


def _endpoint_guardrails(endpoint_id: str) -> str:
    return f"{GATEWAY}/endpoints/{endpoint_id}/guardrails"


def _guardrail(guardrail_id: str) -> str:
    return f"{GUARDRAILS}/{guardrail_id}"

_ENDPOINTS_PAYLOAD = {
    "endpoints": [
        {
            "name": "chat-openai",
            "endpoint_type": "llm/v1/chat",
            "model": {"name": "gpt-4o-mini", "provider": "openai"},
            "endpoint_url": "/gateway/chat-openai/invocations",
            "limit": None,
        },
        {
            "name": "chat-anthropic",
            "endpoint_type": "llm/v1/chat",
            "model": {"name": "claude-3-5-sonnet-20241022", "provider": "anthropic"},
            "endpoint_url": "/gateway/chat-anthropic/invocations",
            "limit": None,
        },
    ]
}


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient`` — returns a payload or raises."""

    payload: dict = {}
    error: Exception | None = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, _url: str) -> _FakeResponse:
        if _FakeAsyncClient.error is not None:
            raise _FakeAsyncClient.error
        return _FakeResponse(_FakeAsyncClient.payload)


def _set_config(client: TestClient, **updates: object) -> None:
    """Replace app.state.config with a frozen copy carrying the updates."""
    current = client.app.state.config
    client.app.state.config = current.model_copy(update=updates)


def _fake_httpx(monkeypatch: pytest.MonkeyPatch, *, payload: dict, error: Exception | None) -> None:
    _FakeAsyncClient.payload = payload
    _FakeAsyncClient.error = error
    monkeypatch.setattr(gateway_route, "httpx", SimpleNamespace(AsyncClient=_FakeAsyncClient))


def test_gateway_not_configured(client: TestClient) -> None:
    _set_config(client, gateway_uri="")
    resp = client.get(GATEWAY)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["configured"] is False
    assert data["reachable"] is False
    assert data["endpoints"] == []


def test_gateway_lists_endpoints(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(client, gateway_uri="http://gw:5002", llm_base_url="")
    _fake_httpx(monkeypatch, payload=_ENDPOINTS_PAYLOAD, error=None)

    resp = client.get(GATEWAY)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["configured"] is True
    assert data["reachable"] is True
    assert data["routing_through_gateway"] is False
    names = {e["name"]: e for e in data["endpoints"]}
    assert set(names) == {"chat-openai", "chat-anthropic"}
    assert names["chat-openai"]["provider"] == "openai"
    assert names["chat-openai"]["model"] == "gpt-4o-mini"
    assert names["chat-openai"]["endpoint_type"] == "llm/v1/chat"


def test_gateway_reports_routing_when_llm_base_url_points_at_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(
        client,
        gateway_uri="http://gw:5002",
        llm_base_url="http://gw:5002/gateway",
    )
    _fake_httpx(monkeypatch, payload=_ENDPOINTS_PAYLOAD, error=None)

    data = client.get(GATEWAY).json()["data"]
    assert data["routing_through_gateway"] is True


def test_gateway_unreachable_degrades(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(client, gateway_uri="http://gw:5002")
    _fake_httpx(monkeypatch, payload={}, error=ConnectionError("connection refused"))

    resp = client.get(GATEWAY)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["configured"] is True
    assert data["reachable"] is False
    assert "connection refused" in (data["error"] or "")
    assert data["endpoints"] == []


# --- Guardrails ---------------------------------------------------------------


class _FakeGuardrail:
    def __init__(self, gid: str, name: str, stage: str, action: str, scorer: str) -> None:
        self.guardrail_id = gid
        self.name = name
        self.stage = stage
        self.action = action
        self.scorer = SimpleNamespace(name=scorer)
        self.action_endpoint_name = None


class _FakeEndpoint:
    def __init__(self, endpoint_id: str, name: str) -> None:
        self.endpoint_id = endpoint_id
        self.name = name


class _FakeConfig:
    def __init__(self, guardrail_id: str, execution_order: int, enabled: bool = True) -> None:
        self.guardrail_id = guardrail_id
        self.execution_order = execution_order
        self.enabled = enabled


class _FakeScorerVersion:
    def __init__(self, scorer_id: str, scorer_name: str, scorer_version: int) -> None:
        self.scorer_id = scorer_id
        self.scorer_name = scorer_name
        self.scorer_version = scorer_version


class _FakeStore:
    """Stands in for the MLflow tracking store's gateway-guardrail + scorer API."""

    def __init__(self) -> None:
        self.attached: list[tuple[str, str, int]] = []
        self.removed: list[tuple[str, str]] = []
        self.updated: list[tuple[str, str, dict[str, Any]]] = []
        self.registered: list[tuple[str, str, str]] = []  # (experiment_id, name, serialized)
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._guardrails = [
            _FakeGuardrail("G1", "pii-before", "BEFORE", "VALIDATION", "DetectPII"),
            _FakeGuardrail("G2", "tox-after", "AFTER", "SANITIZATION", "ToxicLanguage"),
        ]
        self._endpoints = [_FakeEndpoint("E1", "chat-openai")]
        self._configs: dict[str, list[_FakeConfig]] = {"E1": [_FakeConfig("G1", 0)]}
        self._scorers = [_FakeScorerVersion("SC1", "existing_pii", 3)]

    def list_gateway_guardrails(self) -> list[_FakeGuardrail]:
        return self._guardrails

    def list_gateway_endpoints(self) -> list[_FakeEndpoint]:
        return self._endpoints

    def list_endpoint_guardrail_configs(self, endpoint_id: str) -> list[_FakeConfig]:
        return self._configs.get(endpoint_id, [])

    def add_guardrail_to_endpoint(
        self, *, endpoint_id: str, guardrail_id: str, execution_order: int
    ) -> None:
        self.attached.append((endpoint_id, guardrail_id, execution_order))

    def remove_guardrail_from_endpoint(self, *, endpoint_id: str, guardrail_id: str) -> None:
        self.removed.append((endpoint_id, guardrail_id))

    def update_endpoint_guardrail_config(
        self, *, endpoint_id: str, guardrail_id: str, **changes: Any
    ) -> None:
        self.updated.append((endpoint_id, guardrail_id, changes))

    # --- scorer + create/delete (define a guardrail) ---

    def get_experiment_by_name(self, name: str) -> Any:
        return SimpleNamespace(experiment_id="7") if name else None

    def list_scorers(self, experiment_id: str) -> list[_FakeScorerVersion]:
        return self._scorers if experiment_id == "7" else []

    def get_scorer(self, experiment_id: str, name: str) -> _FakeScorerVersion:
        return _FakeScorerVersion("SCnew", name, 1)

    def register_scorer(self, experiment_id: str, name: str, serialized_scorer: str) -> Any:
        self.registered.append((experiment_id, name, serialized_scorer))
        return _FakeScorerVersion("SCnew", name, 1)

    def create_gateway_guardrail(
        self,
        *,
        name: str,
        scorer_id: str,
        scorer_version: int,
        stage: Any,
        action: Any,
        action_endpoint_id: str | None = None,
    ) -> _FakeGuardrail:
        record = {
            "name": name,
            "scorer_id": scorer_id,
            "scorer_version": scorer_version,
            "stage": getattr(stage, "name", str(stage)),
            "action": getattr(action, "name", str(action)),
            "action_endpoint_id": action_endpoint_id,
        }
        self.created.append(record)
        guardrail = _FakeGuardrail(
            "Gnew", name, record["stage"], record["action"], scorer_id
        )
        guardrail.action_endpoint_name = action_endpoint_id
        return guardrail

    def delete_gateway_guardrail(self, guardrail_id: str) -> None:
        self.deleted.append(guardrail_id)


def _use_store(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    monkeypatch.setattr(gateway_route, "_gateway_store", lambda: store)


def test_guardrails_list(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(monkeypatch, _FakeStore())
    data = client.get(GUARDRAILS).json()["data"]
    assert data["configured"] is True and data["reachable"] is True
    gmap = {g["guardrail_id"]: g for g in data["guardrails"]}
    assert set(gmap) == {"G1", "G2"}
    assert gmap["G1"]["stage"] == "BEFORE" and gmap["G1"]["action"] == "VALIDATION"
    assert gmap["G1"]["scorer"] == "DetectPII"
    cov = {c["endpoint_id"]: c for c in data["coverage"]}
    assert cov["E1"]["endpoint"] == "chat-openai"
    assert [g["guardrail_id"] for g in cov["E1"]["guardrails"]] == ["G1"]


def test_guardrails_degrade_when_api_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomStore:
        def list_gateway_guardrails(self) -> list[object]:
            raise RuntimeError("gateway not enabled")

    _use_store(monkeypatch, _BoomStore())
    data = client.get(GUARDRAILS).json()["data"]
    assert data["configured"] is True
    assert data["reachable"] is False
    assert "gateway not enabled" in (data["error"] or "")
    assert data["guardrails"] == []


def test_guardrails_not_configured_when_store_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> object:
        raise ImportError("no mlflow")

    monkeypatch.setattr(gateway_route, "_gateway_store", _boom)
    data = client.get(GUARDRAILS).json()["data"]
    assert data["configured"] is False and data["reachable"] is False


def test_attach_guardrail_calls_store_and_audits(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore()
    _use_store(monkeypatch, store)
    resp = client.post(_endpoint_guardrails("E1"), json={"guardrail_id": "G2", "execution_order": 1})
    assert resp.status_code == 201, resp.text
    assert store.attached == [("E1", "G2", 1)]
    row = db_session.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.action == "attach_gateway_guardrail")
    ).scalars().first()
    assert row is not None and row.entity_id == "E1"


def test_attach_guardrail_requires_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, _FakeStore())
    resp = client.post(
        _endpoint_guardrails("E1"),
        json={"guardrail_id": "G2"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


def test_detach_guardrail_calls_store_and_audits(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore()
    _use_store(monkeypatch, store)
    resp = client.delete(f"{_endpoint_guardrails('E1')}/G1")
    assert resp.status_code == 200, resp.text
    assert store.removed == [("E1", "G1")]
    assert (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "detach_gateway_guardrail")
        )
        .scalars()
        .first()
        is not None
    )


def test_update_guardrail_config_calls_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore()
    _use_store(monkeypatch, store)
    resp = client.patch(f"{_endpoint_guardrails('E1')}/G1", json={"execution_order": 3})
    assert resp.status_code == 200, resp.text
    assert store.updated == [("E1", "G1", {"execution_order": 3})]


def test_attach_guardrail_degrades_to_502_on_store_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomStore:
        def add_guardrail_to_endpoint(self, **_: Any) -> None:
            raise RuntimeError("upstream rejected")

    _use_store(monkeypatch, _BoomStore())
    resp = client.post(_endpoint_guardrails("E1"), json={"guardrail_id": "G2"})
    assert resp.status_code == 502
    assert "upstream rejected" in resp.text


# --- Define guardrails (catalog / create / delete) ----------------------------


def test_guardrail_catalog_lists_templates_and_scorers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(client, tracing_experiment="caliber")
    _use_store(monkeypatch, _FakeStore())
    data = client.get(GUARDRAILS_CATALOG).json()["data"]
    assert data["configured"] is True and data["reachable"] is True
    types = {t["type"] for t in data["templates"]}
    assert types == {"pii", "toxicity", "guidelines", "regex"}
    # the existing registered scorer is offered for reuse
    scorers = {s["scorer_id"]: s for s in data["scorers"]}
    assert scorers["SC1"]["name"] == "existing_pii" and scorers["SC1"]["version"] == 3


def test_create_guardrail_native_pii_registers_and_creates(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_config(client, tracing_experiment="caliber")
    store = _FakeStore()
    _use_store(monkeypatch, store)
    resp = client.post(
        GUARDRAILS,
        json={
            "name": "block-pii-output",
            "stage": "AFTER",
            "action": "SANITIZATION",
            "scorer_type": "pii",
            "config": {"pii_types": ["email", "ssn"]},
        },
    )
    assert resp.status_code == 201, resp.text
    # a scorer was registered and a guardrail created with the chosen stage/action
    assert len(store.registered) == 1
    exp_id, scorer_name, serialized = store.registered[0]
    assert exp_id == "7" and "PIIDetection" in serialized
    assert store.created == [
        {
            "name": "block-pii-output",
            "scorer_id": "SCnew",
            "scorer_version": 1,
            "stage": "AFTER",
            "action": "SANITIZATION",
            "action_endpoint_id": None,
        }
    ]
    row = (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "create_gateway_guardrail")
        )
        .scalars()
        .first()
    )
    assert row is not None and row.entity_type == "gateway_guardrail"


def test_create_guardrail_existing_scorer_skips_registration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore()
    _use_store(monkeypatch, store)
    resp = client.post(
        GUARDRAILS,
        json={
            "name": "reuse-scorer",
            "stage": "BEFORE",
            "action": "VALIDATION",
            "scorer_id": "SC1",
            "scorer_version": 3,
        },
    )
    assert resp.status_code == 201, resp.text
    assert store.registered == []  # reused, not re-registered
    assert store.created[0]["scorer_id"] == "SC1"
    assert store.created[0]["scorer_version"] == 3


def test_create_guardrail_custom_guidelines_requires_text(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, _FakeStore())
    resp = client.post(
        GUARDRAILS,
        json={
            "name": "needs-guidelines",
            "stage": "AFTER",
            "action": "VALIDATION",
            "scorer_type": "guidelines",
            "config": {},
        },
    )
    assert resp.status_code == 400
    assert "guidelines" in resp.text


def test_create_guardrail_rejects_unknown_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, _FakeStore())
    resp = client.post(
        GUARDRAILS,
        json={"name": "x", "stage": "BEFORE", "action": "VALIDATION", "scorer_type": "bogus"},
    )
    assert resp.status_code == 400


def test_create_guardrail_rejects_both_or_neither_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, _FakeStore())
    # neither
    r1 = client.post(GUARDRAILS, json={"name": "x", "stage": "BEFORE", "action": "VALIDATION"})
    assert r1.status_code == 400
    # both
    r2 = client.post(
        GUARDRAILS,
        json={
            "name": "x",
            "stage": "BEFORE",
            "action": "VALIDATION",
            "scorer_type": "pii",
            "scorer_id": "SC1",
            "scorer_version": 1,
        },
    )
    assert r2.status_code == 400


def test_create_guardrail_requires_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, _FakeStore())
    resp = client.post(
        GUARDRAILS,
        json={"name": "x", "stage": "BEFORE", "action": "VALIDATION", "scorer_type": "pii"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


def test_create_guardrail_degrades_to_502_on_store_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomStore(_FakeStore):
        def create_gateway_guardrail(self, **_: Any) -> Any:
            raise RuntimeError("workspace rejected scorer")

    _use_store(monkeypatch, _BoomStore())
    resp = client.post(
        GUARDRAILS,
        json={"name": "x", "stage": "BEFORE", "action": "VALIDATION", "scorer_type": "pii"},
    )
    assert resp.status_code == 502
    assert "workspace rejected scorer" in resp.text


def test_delete_guardrail_calls_store_and_audits(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _FakeStore()
    _use_store(monkeypatch, store)
    resp = client.delete(_guardrail("G2"))
    assert resp.status_code == 200, resp.text
    assert store.deleted == ["G2"]
    assert (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "delete_gateway_guardrail")
        )
        .scalars()
        .first()
        is not None
    )


def test_delete_guardrail_requires_operator(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, _FakeStore())
    resp = client.delete(_guardrail("G2"), headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


# --- Usage --------------------------------------------------------------------


def test_usage_route_returns_helper_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "buckets": [{"ts": 1000, "count": 2, "tokens": 30, "cost_usd": 0.5}],
        "totals": {"count": 2, "tokens": 30, "cost_usd": 0.5},
        "by_model": [{"model": "gpt-4o", "calls": 2, "tokens": 30, "cost_usd": 0.5}],
    }
    monkeypatch.setattr(gateway_route, "gateway_usage_payload", lambda **_: payload)
    data = client.get(USAGE).json()["data"]
    assert data["totals"]["cost_usd"] == 0.5
    assert data["by_model"][0]["model"] == "gpt-4o"


def test_usage_payload_by_model_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    """gateway_usage_payload groups span token/cost by caliber.model."""
    from caliber.routes import observability as obs

    def _span(model: str | None, tokens: int, cost: float) -> SimpleNamespace:
        attrs: dict[str, str] = {"caliber.tokens": str(tokens), "caliber.cost_usd": str(cost)}
        if model is not None:
            attrs["caliber.model"] = f'"{model}"'  # attributes are JSON-encoded
        return SimpleNamespace(attributes=attrs, span_type="LLM")

    trace = SimpleNamespace(
        info=SimpleNamespace(
            trace_id="tr-1",
            tags={},
            request_time=1000,
            execution_duration=42,
            request_preview="",
            response_preview="",
            state="OK",
        ),
        data=SimpleNamespace(
            spans=[_span("gpt-4o", 10, 0.2), _span("gpt-4o", 5, 0.1), _span("claude-sonnet-4", 8, 0.3)]
        ),
    )
    fake_mlflow = SimpleNamespace(
        search_traces=lambda **_: [trace],
    )
    monkeypatch.setitem(__import__("sys").modules, "mlflow", fake_mlflow)
    monkeypatch.setattr(obs, "_experiment_name_map", lambda _m: {})
    monkeypatch.setattr(obs, "_experiment_ids", lambda _m, _c: ["0"])

    payload = obs.gateway_usage_payload(experiment_filter="", configured="", since_ms=None)
    by_model = {r["model"]: r for r in payload["by_model"]}
    assert by_model["gpt-4o"]["tokens"] == 15
    assert by_model["gpt-4o"]["calls"] == 2
    assert round(by_model["gpt-4o"]["cost_usd"], 6) == 0.3
    assert by_model["claude-sonnet-4"]["tokens"] == 8
