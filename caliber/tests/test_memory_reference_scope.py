"""Reference profile: memory partitions are owned and derived, never free-form (B12 memory, #667).

Development keeps the old behaviour (see ``test_routes_memory.py``); here the profile is ``reference``, so a
partition must be a governed resource the caller can see (or the caller's own identity), a refused partition is
one indistinguishable ``404``, writes are verbatim, bulk deletion is unavailable until the owned lifecycle (M20)
exists, and the service layer enforces the same so nothing reaches mem0 around the route.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.config import CaliberConfig
from caliber.db.models import CaliberAgentConfig, CaliberWorkflow, CaliberWorkflowRun
from caliber.memory import MemoryLifecycleUnavailableError, MemoryService
from caliber.workflows.memory_tools import bind_run_memory_tools

BASE = "/ajax-api/2.0/mlflow/caliber/memory"
SEARCH = BASE + "/search"
NOT_FOUND = "memory partition not found"
OTHER = {"X-CALIBER-User": "@notadmin"}


class _Mem0:
    """Records every call that reaches the (fake) mem0 instance."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

        class _Entities:
            def list(inner, **kwargs: Any) -> list[Any]:  # noqa: N805
                self.calls.append(("entities", kwargs))
                return []

        self.entity_store = _Entities()

    def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("add", {"text": text, **kwargs}))
        return {"results": [{"id": "m1", "memory": text}]}

    def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("search", {"query": query, **kwargs}))
        return {"results": []}

    def get_all(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_all", kwargs))
        return {"results": []}

    def delete_all(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_all", kwargs))
        return {}

    def reached(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def mem0(monkeypatch: pytest.MonkeyPatch) -> _Mem0:
    fake = _Mem0()
    monkeypatch.setattr(
        MemoryService, "from_config", classmethod(lambda cls, config: _strict_service(fake))
    )
    return fake


def _strict_service(fake: _Mem0, *, strict: bool = True) -> MemoryService:
    return MemoryService(fake, default_infer=False, strict=strict)


@pytest.fixture
def reference_client(client: TestClient) -> TestClient:
    client.app.state.config = client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"profile": "reference", "memory_enabled": True, "memory_infer": False}
    )
    return client


def _agent(db: Session, agent_id: str, *, owner: str, visibility: str = "user") -> None:
    db.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id=f"EXP-{agent_id}",
            name=agent_id,
            owner=owner,
            visibility=visibility,
        )
    )
    db.commit()


# ---------------------------------------------------------------- partitions are governed resources


def test_a_free_form_agent_label_is_refused_in_the_reference_profile(
    reference_client: TestClient, mem0: _Mem0
) -> None:
    for call in (
        lambda: reference_client.post(SEARCH, json={"query": "q", "agent_id": "wf-not-a-resource"}),
        lambda: reference_client.get(BASE, params={"agent_id": "wf-not-a-resource"}),
        lambda: reference_client.post(
            BASE, json={"text": "x", "agent_id": "wf-not-a-resource", "infer": False}
        ),
    ):
        resp = call()
        assert resp.status_code == 404
        assert resp.json()["detail"] == NOT_FOUND
    assert mem0.calls == [], "nothing reached mem0"


def test_foreign_missing_and_free_form_partitions_are_indistinguishable(
    reference_client: TestClient, mem0: _Mem0, db_session: Session
) -> None:
    _agent(db_session, "AG-private", owner="@someone-else")
    answers = {
        label: reference_client.post(SEARCH, json={"query": "q", "agent_id": label}, headers=OTHER)
        for label in ("AG-private", "AG-missing", "just-a-label")
    }
    assert {r.status_code for r in answers.values()} == {404}
    assert {r.text for r in answers.values()} == {answers["AG-missing"].text}
    for label, resp in answers.items():
        assert label not in resp.text, "the refusal never echoes the id"
    assert mem0.calls == []


def test_a_visible_agent_is_usable_and_the_partition_reaches_mem0_unchanged(
    reference_client: TestClient, mem0: _Mem0, db_session: Session
) -> None:
    _agent(db_session, "AG-mine", owner="@notadmin")
    resp = reference_client.post(SEARCH, json={"query": "q", "agent_id": "AG-mine"}, headers=OTHER)
    assert resp.status_code == 200
    assert mem0.calls == [("search", {"query": "q", "top_k": 10, "agent_id": "AG-mine"})]


def test_a_visible_workflow_is_a_usable_partition(
    reference_client: TestClient, mem0: _Mem0, db_session: Session
) -> None:
    """The in-run tools scope by workflow id, so a workflow the caller can see is a governed partition."""
    db_session.add(
        CaliberWorkflow(workflow_id="WF-mine", name="mine", owner="@notadmin", visibility="user")
    )
    db_session.commit()
    ok = reference_client.get(BASE, params={"agent_id": "WF-mine"}, headers=OTHER)
    assert ok.status_code == 200
    db_session.add(
        CaliberWorkflow(
            workflow_id="WF-theirs", name="theirs", owner="@someone-else", visibility="user"
        )
    )
    db_session.commit()
    refused = reference_client.get(BASE, params={"agent_id": "WF-theirs"}, headers=OTHER)
    assert refused.status_code == 404 and refused.json()["detail"] == NOT_FOUND
    assert mem0.reached() == ["get_all"]


def test_a_run_id_must_be_a_visible_run(
    reference_client: TestClient, mem0: _Mem0, db_session: Session
) -> None:
    _agent(db_session, "AG-mine", owner="@notadmin")
    db_session.add(
        CaliberWorkflowRun(workflow_run_id="RUN-1", workflow_id="WF-x", created_by="@someone-else")
    )
    db_session.commit()
    resp = reference_client.get(
        BASE, params={"agent_id": "AG-mine", "run_id": "RUN-ghost"}, headers=OTHER
    )
    assert resp.status_code == 404 and resp.json()["detail"] == NOT_FOUND
    assert mem0.calls == []


def test_a_run_id_alone_is_not_a_partition(reference_client: TestClient, mem0: _Mem0) -> None:
    resp = reference_client.get(BASE, params={"run_id": "RUN-1"})
    assert resp.status_code == 403
    assert "not by a run alone" in resp.json()["detail"]
    assert mem0.calls == []


# ---------------------------------------------------------------- the principal's own namespace is derived


def test_a_request_that_names_no_partition_acts_on_the_callers_own(
    reference_client: TestClient, mem0: _Mem0
) -> None:
    assert reference_client.post(SEARCH, json={"query": "q"}, headers=OTHER).status_code == 200
    assert reference_client.get(BASE, headers=OTHER).status_code == 200
    assert reference_client.post(
        BASE, json={"text": "remember", "infer": False}, headers={"X-CALIBER-User": "@notadmin"}
    ).status_code in (201, 403)
    searched = [c for c in mem0.calls if c[0] in {"search", "get_all"}]
    assert all(call[1]["user_id"] == "@notadmin" for call in searched)
    assert len(searched) == 2


def test_user_id_must_be_the_callers_own_even_for_an_admin(
    reference_client: TestClient, mem0: _Mem0
) -> None:
    """Development lets an admin read another user's memory; the reference profile leaves that to the lifecycle."""
    admin_other = reference_client.post(SEARCH, json={"query": "q", "user_id": "@someone-else"})
    assert admin_other.status_code == 404 and admin_other.json()["detail"] == NOT_FOUND
    stranger = reference_client.post(
        SEARCH, json={"query": "q", "user_id": "@someone-else"}, headers=OTHER
    )
    assert stranger.status_code == 404 and stranger.json()["detail"] == NOT_FOUND
    own = reference_client.post(SEARCH, json={"query": "q", "user_id": "@notadmin"}, headers=OTHER)
    assert own.status_code == 200
    assert mem0.calls[-1][1]["user_id"] == "@notadmin"
    assert len(mem0.calls) == 1


# ---------------------------------------------------------------- unsupported durable operations


def test_bulk_deletion_is_unavailable_until_the_lifecycle_exists(
    reference_client: TestClient, mem0: _Mem0, db_session: Session
) -> None:
    _agent(db_session, "AG-mine", owner="@notadmin")
    admin = reference_client.request("DELETE", BASE, json={"agent_id": "AG-mine"})
    assert admin.status_code == 403
    assert "lifecycle" in admin.json()["detail"]
    ordinary = reference_client.request("DELETE", BASE, json={"agent_id": "AG-mine"}, headers=OTHER)
    assert ordinary.status_code == 403, "an ordinary caller never could delete"
    assert mem0.calls == [], "no delete reached mem0"


@pytest.mark.parametrize("body", [{}, {"infer": True}])
def test_a_write_must_ask_for_verbatim_storage(
    reference_client: TestClient, mem0: _Mem0, db_session: Session, body: dict[str, Any]
) -> None:
    _agent(db_session, "AG-mine", owner="@admin-user")
    resp = reference_client.post(BASE, json={"text": "remember", "agent_id": "AG-mine", **body})
    assert resp.status_code == 403
    assert "infer=false" in resp.json()["detail"]
    assert mem0.calls == []


def test_a_verbatim_write_to_a_visible_partition_is_stored(
    reference_client: TestClient, mem0: _Mem0, db_session: Session
) -> None:
    _agent(db_session, "AG-mine", owner="@someone", visibility="project")
    resp = reference_client.post(
        BASE, json={"text": "remember", "agent_id": "AG-mine", "infer": False}
    )
    assert resp.status_code == 201
    assert mem0.calls == [
        ("add", {"text": "remember", "metadata": None, "infer": False, "agent_id": "AG-mine"})
    ]


def test_development_is_unchanged(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Mem0()
    monkeypatch.setattr(
        MemoryService, "from_config", classmethod(lambda cls, config: MemoryService(fake))
    )
    assert client.post(SEARCH, json={"query": "q", "agent_id": "wf-free-label"}).status_code == 200
    assert client.request("DELETE", BASE, json={"agent_id": "wf-free-label"}).status_code == 200
    assert client.post(BASE, json={"text": "x", "agent_id": "wf-free-label"}).status_code == 201
    assert fake.reached() == ["search", "delete_all", "add"]


# ---------------------------------------------------------------- the service enforces it for every caller


def test_the_strict_service_refuses_what_the_route_refuses_without_touching_mem0() -> None:
    fake = _Mem0()
    service = _strict_service(fake)
    for call in (
        lambda: service.delete_all(agent_id="AG"),
        lambda: service.add("x", agent_id="AG", infer=True),
        lambda: service.search("q", run_id="RUN-1"),
        lambda: service.get_all(run_id="RUN-1"),
        lambda: service.add("x", run_id="RUN-1", infer=False),
        lambda: service.list_entities(run_id="RUN-1"),
    ):
        with pytest.raises(MemoryLifecycleUnavailableError):
            call()
    assert fake.calls == []


def test_the_strict_service_serves_owned_partitions() -> None:
    fake = _Mem0()
    service = _strict_service(fake)
    service.add("x", agent_id="AG", infer=False)
    service.add(
        "y", user_id="@u", run_id="RUN-1", infer=None
    )  # the server default is verbatim here
    service.search("q", agent_id="AG", run_id="RUN-1")
    service.get_all(user_id="@u")
    service.list_entities(agent_id="AG")
    assert fake.reached() == ["add", "add", "search", "get_all", "entities"]


def test_the_development_service_keeps_the_old_freedoms() -> None:
    fake = _Mem0()
    service = _strict_service(fake, strict=False)
    service.add("x", run_id="RUN-1", infer=True)
    service.delete_all(run_id="RUN-1")
    assert fake.reached() == ["add", "delete_all"]


def test_the_service_is_strict_exactly_when_the_profile_is_reference(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    captured: dict[str, Any] = {}

    class _Memory:
        @staticmethod
        def from_config(_config: Any) -> _Mem0:
            return _Mem0()

    monkeypatch.setitem(sys.modules, "mem0", types.SimpleNamespace(Memory=_Memory))
    for profile, expect in (("development", False), ("reference", True)):
        config = app_config.model_copy(
            update={"profile": profile, "memory_enabled": True, "memory_infer": False}
        )
        service = MemoryService.from_config(config)
        captured[profile] = service._strict
        assert service._strict is expect


def test_in_run_memory_tools_go_through_the_strict_service(
    app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tools build the service themselves, so the rule has to live in it: a run-only scope is refused."""
    fake = _Mem0()
    monkeypatch.setattr(
        MemoryService, "from_config", classmethod(lambda cls, config: _strict_service(fake))
    )
    config = app_config.model_copy(update={"profile": "reference", "memory_enabled": True})
    tools = bind_run_memory_tools(config, agent_id="WF-1", run_id="RUN-1")
    assert tools["memory_search"]("q")["count"] == 0
    assert tools["memory_add"]("remember this")["added"] is True
    assert [name for name, _ in fake.calls] == ["search", "add"]
    assert fake.calls[1][1]["infer"] is False and fake.calls[1][1]["agent_id"] == "WF-1"
    fake.calls.clear()
    monkeypatch.setattr(
        MemoryService, "from_config", classmethod(lambda cls, config: _strict_service(fake))
    )
    run_only = bind_run_memory_tools(config, agent_id=None, run_id="RUN-1")
    assert run_only == {}, "no agent scope, no tools"


def test_a_lifecycle_refusal_from_the_service_is_a_403_not_a_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Refuses:
        def search(self, *a: Any, **k: Any) -> Any:
            raise MemoryLifecycleUnavailableError("not until the lifecycle exists")

    monkeypatch.setattr(MemoryService, "from_config", classmethod(lambda cls, config: _Refuses()))
    resp = client.post(SEARCH, json={"query": "q", "agent_id": "free-label"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "not until the lifecycle exists"


def test_development_still_allows_a_visible_governed_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db_session: Session
) -> None:
    fake = _Mem0()
    monkeypatch.setattr(
        MemoryService, "from_config", classmethod(lambda cls, config: MemoryService(fake))
    )
    _agent(db_session, "AG-visible", owner="@notadmin")
    resp = client.post(SEARCH, json={"query": "q", "agent_id": "AG-visible"}, headers=OTHER)
    assert resp.status_code == 200
    assert fake.reached() == ["search"]
