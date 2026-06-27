"""Tests for the /memory management HTTP route.

The mem0 service is mocked for the enabled paths (no real pgvector/LLM); the
disabled path exercises the real ``MemoryService.from_config`` raising on the
default (memory-disabled) test config.
"""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from caliber.memory import MemoryService

BASE = "/ajax-api/2.0/mlflow/caliber/memory"
SEARCH = BASE + "/search"


class _FakeService:
    def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
        return {"results": [{"id": "m1", "memory": text, "event": "ADD"}]}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"results": [{"memory": "prefers blue", "score": 0.9, "id": "m1"}]}

    def get_all(self, **kwargs: Any) -> dict[str, Any]:
        return {"results": [{"memory": "prefers blue", "id": "m1"}]}

    def delete_all(self, **kwargs: Any) -> dict[str, Any]:
        return {}


def _enable(monkeypatch) -> _FakeService:
    fake = _FakeService()
    monkeypatch.setattr(MemoryService, "from_config", lambda config: fake)
    return fake


# ---------------------------------------------------------------------------
# disabled (default test config) → 503
# ---------------------------------------------------------------------------


def test_add_returns_503_when_memory_disabled(client: TestClient) -> None:
    resp = client.post(BASE, json={"text": "x", "agent_id": "wf-1"})
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


def test_search_returns_503_when_memory_disabled(client: TestClient) -> None:
    resp = client.post(SEARCH, json={"query": "q", "agent_id": "wf-1"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# enabled (service mocked)
# ---------------------------------------------------------------------------


def test_add_memory(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.post(BASE, json={"text": "the user prefers blue", "agent_id": "wf-1"})
    assert resp.status_code == 201
    result = resp.json()["data"]["result"]
    assert result["results"][0]["memory"] == "the user prefers blue"


def test_search_memory(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.post(SEARCH, json={"query": "what color?", "agent_id": "wf-1", "top_k": 3})
    assert resp.status_code == 200
    assert resp.json()["data"]["result"]["results"][0]["memory"] == "prefers blue"


def test_list_memories(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.get(BASE, params={"agent_id": "wf-1", "top_k": "20"})
    assert resp.status_code == 200
    assert resp.json()["data"]["result"]["results"][0]["id"] == "m1"


def test_delete_memories(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.request("DELETE", BASE, json={"agent_id": "wf-1"})
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["deleted"] is True
    assert body["scope"] == {"agent_id": "wf-1"}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_add_requires_text(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.post(BASE, json={"agent_id": "wf-1"})
    assert resp.status_code == 400
    assert "text" in resp.json()["detail"].lower()


def test_add_requires_scope(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.post(BASE, json={"text": "remember"})
    assert resp.status_code == 400
    assert "agent_id" in resp.json()["detail"]


def test_search_requires_query(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.post(SEARCH, json={"agent_id": "wf-1"})
    assert resp.status_code == 400


def test_list_requires_scope(client: TestClient, monkeypatch) -> None:
    _enable(monkeypatch)
    resp = client.get(BASE)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# backend failure → 502
# ---------------------------------------------------------------------------


def test_backend_failure_maps_to_502(client: TestClient, monkeypatch) -> None:
    class _Boom:
        def search(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("pgvector unreachable")

    monkeypatch.setattr(MemoryService, "from_config", lambda config: _Boom())
    resp = client.post(SEARCH, json={"query": "q", "agent_id": "wf-1"})
    assert resp.status_code == 502
    assert "pgvector unreachable" in resp.json()["detail"]
