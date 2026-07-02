"""Coverage top-up for ``/caliber/gateway`` (``src/caliber/routes/gateway.py``).

Targets branches the existing ``test_routes_gateway.py`` suite doesn't reach:
the real ``_gateway_store`` reach-through to ``mlflow.MlflowClient``, the pure
helper functions (``_map_guardrail``, ``_build_scorer``,
``_guardrail_experiment_id``, ``_scorer_ref``, ``_collect_scorers``) exercised
directly with edge-case inputs, per-endpoint / per-experiment degrade paths in
``_collect_guardrails`` / ``_collect_scorers``, the 502 store-failure branches
on delete/detach/update, the "no fields changed" 400, and the ``since_ms``
query-param parsing on the usage route.

The external boundary (MLflow's tracking store, the gateway-guardrail scorer
classes, and the standalone gateway) is mocked throughout — no network call,
no real MLflow server.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

import caliber.routes.gateway as gateway_route
from tests.workflow_helpers import PREFIX

GATEWAY = f"{PREFIX}/gateway"
GUARDRAILS = f"{GATEWAY}/guardrails"
GUARDRAILS_CATALOG = f"{GUARDRAILS}/catalog"
USAGE = f"{GATEWAY}/usage"


def _endpoint_guardrails(endpoint_id: str) -> str:
    return f"{GATEWAY}/endpoints/{endpoint_id}/guardrails"


def _endpoint_guardrail(endpoint_id: str, guardrail_id: str) -> str:
    return f"{GATEWAY}/endpoints/{endpoint_id}/guardrails/{guardrail_id}"


def _guardrail(guardrail_id: str) -> str:
    return f"{GUARDRAILS}/{guardrail_id}"


def _use_store(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    monkeypatch.setattr(gateway_route, "_gateway_store", lambda: store)


# --- _gateway_store: the real reach-through to mlflow.MlflowClient -----------


def test_gateway_store_returns_tracking_store_from_mlflow_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one test in this module that exercises the *real* ``_gateway_store``
    body (every other test injects a fake via monkeypatching the function
    itself) — it must import ``mlflow``, build an ``MlflowClient``, and reach
    through to ``client._tracking_client.store``."""
    sentinel_store = object()

    class _FakeTrackingClient:
        store = sentinel_store

    class _FakeMlflowClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._tracking_client = _FakeTrackingClient()

    monkeypatch.setattr("mlflow.MlflowClient", _FakeMlflowClient)
    assert gateway_route._gateway_store() is sentinel_store


# --- _map_guardrail -----------------------------------------------------------


def test_map_guardrail_scorer_without_name_attr_falls_back_to_str() -> None:
    """When ``guardrail.scorer`` has no ``.name`` (e.g. a bare identifier
    string rather than a scorer object), the string form is used as-is."""
    guardrail = SimpleNamespace(
        guardrail_id="G9",
        name="raw-scorer-guardrail",
        stage=SimpleNamespace(name="BEFORE"),
        action=SimpleNamespace(name="VALIDATION"),
        scorer="plain-string-scorer",
        action_endpoint_name=None,
    )
    mapped = gateway_route._map_guardrail(guardrail)
    assert mapped["scorer"] == "plain-string-scorer"


# --- _collect_guardrails: per-endpoint config read failure --------------------


def test_guardrails_list_shows_endpoint_with_no_coverage_when_config_read_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _ConfigBoomStore:
        def list_gateway_guardrails(self) -> list[object]:
            return []

        def list_gateway_endpoints(self) -> list[object]:
            return [SimpleNamespace(endpoint_id="E9", name="ep-nine")]

        def list_endpoint_guardrail_configs(self, endpoint_id: str) -> list[object]:
            raise RuntimeError("configs unavailable")

    _use_store(monkeypatch, _ConfigBoomStore())
    resp = client.get(GUARDRAILS)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["reachable"] is True
    cov = {c["endpoint_id"]: c for c in data["coverage"]}
    assert cov["E9"]["endpoint"] == "ep-nine"
    assert cov["E9"]["guardrails"] == []


# --- _build_scorer: template branches -----------------------------------------


def test_build_scorer_pii_types_must_be_a_list() -> None:
    with pytest.raises(gateway_route._GuardrailBuildError, match="pii_types"):
        gateway_route._build_scorer("pii", "block-pii", {"pii_types": "email"})


def test_build_scorer_toxicity_builds_safety_scorer() -> None:
    scorer = gateway_route._build_scorer("toxicity", "tox-guard", {"model": "gateway:/judge"})
    assert scorer.__class__.__name__ == "Safety"
    assert scorer.name == "tox_guard"
    assert scorer.model == "gateway:/judge"


def test_build_scorer_guidelines_from_string_splits_lines() -> None:
    scorer = gateway_route._build_scorer(
        "guidelines", "must-be-polite", {"guidelines": "Be nice.\n\n  Be concise.  \n"}
    )
    assert scorer.__class__.__name__ == "Guidelines"
    assert scorer.guidelines == ["Be nice.", "Be concise."]


def test_build_scorer_guidelines_from_list_of_values() -> None:
    scorer = gateway_route._build_scorer(
        "guidelines", "must-be-polite", {"guidelines": [" Be nice. ", 42, ""]}
    )
    assert scorer.guidelines == ["Be nice.", "42"]


def test_build_scorer_regex_requires_pattern() -> None:
    with pytest.raises(gateway_route._GuardrailBuildError, match="pattern"):
        gateway_route._build_scorer("regex", "must-match", {})


def test_build_scorer_regex_fullmatch_case_insensitive() -> None:
    scorer = gateway_route._build_scorer(
        "regex",
        "must-match",
        {"pattern": "^ANSWER:", "match_type": "fullmatch", "case_insensitive": True},
    )
    assert scorer.__class__.__name__ == "RegexMatch"
    assert scorer.pattern == "^ANSWER:"
    assert scorer.match_type == "fullmatch"
    assert scorer.case_insensitive is True


def test_build_scorer_wraps_unexpected_construction_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scorer class raising mid-construction (e.g. a pydantic validation
    error) is wrapped as a ``_GuardrailBuildError`` rather than propagating
    raw, so the route can map it to a 400 instead of a 500."""

    def _boom(**_: object) -> None:
        raise ValueError("bad pydantic config")

    monkeypatch.setattr("mlflow.genai.scorers.builtin_scorers.PIIDetection", _boom)
    with pytest.raises(gateway_route._GuardrailBuildError, match="bad pydantic config"):
        gateway_route._build_scorer("pii", "block-pii", {})


def test_build_scorer_unknown_type_raises() -> None:
    with pytest.raises(gateway_route._GuardrailBuildError, match="unknown scorer_type"):
        gateway_route._build_scorer("unknown-type", "x", {})


# --- _guardrail_experiment_id / _scorer_ref / _collect_scorers ---------------


def test_guardrail_experiment_id_falls_back_when_lookup_raises() -> None:
    class _BoomLookupStore:
        def get_experiment_by_name(self, name: str) -> object:
            raise RuntimeError("store too old")

    config = SimpleNamespace(tracing_experiment="caliber")
    assert gateway_route._guardrail_experiment_id(_BoomLookupStore(), config) == "0"


def test_scorer_ref_looks_up_scorer_when_registered_result_lacks_ids() -> None:
    class _LookupStore:
        def get_scorer(self, experiment_id: str, name: str) -> SimpleNamespace:
            assert experiment_id == "0"
            assert name == "my-scorer"
            return SimpleNamespace(scorer_id="SC-LOOKED-UP", scorer_version=5)

    registered = SimpleNamespace()  # no scorer_id / scorer_version attrs
    scorer_id, version = gateway_route._scorer_ref(_LookupStore(), registered, "0", "my-scorer")
    assert (scorer_id, version) == ("SC-LOOKED-UP", 5)


def test_collect_scorers_dedupes_repeated_scorer_ids() -> None:
    class _DupStore:
        def list_scorers(self, experiment_id: str) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(scorer_id="DUP", scorer_name="first", scorer_version=1),
                SimpleNamespace(scorer_id="DUP", scorer_name="second", scorer_version=2),
            ]

    config = SimpleNamespace(tracing_experiment="")  # no lookup name -> resolved experiment "0"
    scorers = gateway_route._collect_scorers(_DupStore(), config)
    assert scorers == [{"name": "first", "scorer_id": "DUP", "version": 1}]


def test_collect_scorers_skips_experiment_when_list_scorers_raises() -> None:
    class _PartialFailStore:
        def get_experiment_by_name(self, name: str) -> SimpleNamespace:
            return SimpleNamespace(experiment_id="7")

        def list_scorers(self, experiment_id: str) -> list[SimpleNamespace]:
            if experiment_id == "7":
                raise RuntimeError("experiment scorers unavailable")
            return [SimpleNamespace(scorer_id="SC0", scorer_name="fallback", scorer_version=1)]

    config = SimpleNamespace(tracing_experiment="caliber")
    scorers = gateway_route._collect_scorers(_PartialFailStore(), config)
    assert scorers == [{"name": "fallback", "scorer_id": "SC0", "version": 1}]


# --- Guardrail catalog route: degrade paths -----------------------------------


def test_guardrail_catalog_degrades_when_store_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> object:
        raise ImportError("mlflow gateway module missing")

    monkeypatch.setattr(gateway_route, "_gateway_store", _boom)
    resp = client.get(GUARDRAILS_CATALOG)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["configured"] is False
    assert data["reachable"] is False
    assert "mlflow gateway module missing" in (data["error"] or "")
    # Templates are static and still listed even though the store is unavailable.
    assert {t["type"] for t in data["templates"]} == {"pii", "toxicity", "guidelines", "regex"}


def test_guardrail_catalog_reachable_false_when_scorer_lookup_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, object())  # store resolves fine; _collect_scorers is what fails

    def _boom(_store: object, _config: object) -> list[object]:
        raise RuntimeError("scorer API too old")

    monkeypatch.setattr(gateway_route, "_collect_scorers", _boom)
    resp = client.get(GUARDRAILS_CATALOG)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["configured"] is True
    assert data["reachable"] is False
    assert "scorer API too old" in (data["error"] or "")
    assert data["templates"]  # templates still present despite the scorer-lookup failure
    assert data["scorers"] == []


# --- Delete / detach / update: store-failure -> 502, empty-body -> 400 -------


def test_delete_guardrail_store_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomStore:
        def delete_gateway_guardrail(self, guardrail_id: str) -> None:
            raise RuntimeError("guardrail still attached")

    _use_store(monkeypatch, _BoomStore())
    resp = client.delete(_guardrail("G1"))
    assert resp.status_code == 502
    assert "guardrail still attached" in resp.text


def test_detach_guardrail_store_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomStore:
        def remove_guardrail_from_endpoint(self, **_: Any) -> None:
            raise RuntimeError("endpoint locked")

    _use_store(monkeypatch, _BoomStore())
    resp = client.delete(_endpoint_guardrail("E1", "G1"))
    assert resp.status_code == 502
    assert "endpoint locked" in resp.text


def test_update_guardrail_config_requires_at_least_one_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_store(monkeypatch, object())
    resp = client.patch(_endpoint_guardrail("E1", "G1"), json={})
    assert resp.status_code == 400
    assert "at least one field" in resp.text


def test_update_guardrail_config_store_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _BoomStore:
        def update_endpoint_guardrail_config(self, **_: Any) -> None:
            raise RuntimeError("config rejected")

    _use_store(monkeypatch, _BoomStore())
    resp = client.patch(_endpoint_guardrail("E1", "G1"), json={"enabled": False})
    assert resp.status_code == 502
    assert "config rejected" in resp.text


# --- Usage: since_ms query-param parsing --------------------------------------


def test_usage_since_ms_valid_int_is_forwarded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_usage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"buckets": [], "totals": {}, "by_model": []}

    monkeypatch.setattr(gateway_route, "gateway_usage_payload", _fake_usage)
    resp = client.get(f"{USAGE}?since_ms=123456")
    assert resp.status_code == 200, resp.text
    assert captured["since_ms"] == 123456


def test_usage_since_ms_invalid_falls_back_to_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def _fake_usage(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"buckets": [], "totals": {}, "by_model": []}

    monkeypatch.setattr(gateway_route, "gateway_usage_payload", _fake_usage)
    resp = client.get(f"{USAGE}?since_ms=not-a-number")
    assert resp.status_code == 200, resp.text
    assert captured["since_ms"] is None
