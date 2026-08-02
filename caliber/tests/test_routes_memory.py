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


# ---------------------------------------------------------------------------
# Scope authorization: the caller does not get to choose whose memories to read
# ---------------------------------------------------------------------------


def test_reading_another_users_memories_is_refused(monkeypatch, client: TestClient) -> None:
    """``user_id`` partitions a shared store and arrived straight from the body.

    Behind only ``require_user``, any authenticated caller could read another
    person's memories by naming them. The partition was a filing convention.
    """
    _enable(monkeypatch)

    # The shared fixture client is the admin user, whom this check deliberately
    # permits; a cross-user read is only a defect for an ordinary caller.
    resp = client.post(
        SEARCH,
        json={"query": "q", "user_id": "@someone-else"},
        headers={"X-CALIBER-User": "@notadmin"},
    )

    assert resp.status_code == 403
    assert "your own identity" in resp.json()["detail"]


def test_reading_your_own_memories_is_allowed(monkeypatch, client: TestClient) -> None:
    _enable(monkeypatch)

    resp = client.post(
        SEARCH,
        json={"query": "q", "user_id": "@notadmin"},
        headers={"X-CALIBER-User": "@notadmin"},
    )

    assert resp.status_code == 200


def test_an_invisible_governed_agent_is_not_readable(
    monkeypatch, client: TestClient, db_session
) -> None:
    """When the label *is* a governed resource, visibility applies.

    A 404 rather than 403: confirming the agent exists to someone who cannot see
    it is itself a disclosure, which is how every other scoped route behaves.
    """
    from caliber.db.models import CaliberAgentConfig

    _enable(monkeypatch)
    db_session.add(
        CaliberAgentConfig(
            agent_id="AG-private",
            experiment_id="EXP-private",
            name="private",
            owner="@someone-else",
            visibility="user",
        )
    )
    db_session.commit()

    resp = client.post(
        SEARCH,
        json={"query": "q", "agent_id": "AG-private"},
        headers={"X-CALIBER-User": "@notadmin"},
    )

    assert resp.status_code == 404


def test_a_free_form_partition_label_is_still_usable(monkeypatch, client: TestClient) -> None:
    """The documented limitation, pinned so it is a decision and not a surprise.

    Memory scope keys are free-form: the in-run tools scope by workflow id and
    callers use labels that were never rows. A label with no governed resource
    behind it has no owner to protect, so it passes. Closing that requires
    memory partitions to become owned resources — a data-model change, not a
    filter. This test exists so removing the affordance is a deliberate act.
    """
    _enable(monkeypatch)

    resp = client.post(SEARCH, json={"query": "q", "agent_id": "wf-not-a-registered-agent"})

    assert resp.status_code == 200
