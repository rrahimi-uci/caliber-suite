"""Integration tests for prompt registry routes."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberPromptTestRun,
    CaliberRefinementJob,
    CaliberVerificationItem,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.routes import prompts as prompt_routes

PREFIX = "/ajax-api/2.0/mlflow/caliber/prompts"


@pytest.fixture
def multi_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restore the (dormant) multi-alias dev/staging/prod write + discovery model.

    v1 ships single-environment: prompts resolve and publish to one live alias
    only (see ``_PROMPT_DISCOVERY_ALIASES`` in ``caliber.routes.prompts``). These
    tests cover the multi-stage model that is restored by listing the aliases
    again. Both constants are read at request time, so patching the module
    globals is sufficient.
    """
    aliases = ("prod", "staging", "dev")
    monkeypatch.setattr(prompt_routes, "_PROMPT_DISCOVERY_ALIASES", aliases)
    monkeypatch.setattr(prompt_routes, "_PROMPT_WRITE_ALIASES", frozenset(aliases))


def _insert_agent(session: Session, **overrides: object) -> CaliberAgentConfig:
    defaults: dict[str, object] = {
        "agent_id": "support-agent",
        "experiment_id": "exp-support",
        "name": "Support Agent",
        "owner": "@sarah",
        "artifact_types": ["prompt"],
        "eval_thresholds": {},
        "optimizer_config": {},
        "approval_policy": {},
    }
    defaults.update(overrides)
    row = CaliberAgentConfig(**defaults)
    session.add(row)
    session.commit()
    return row


def _insert_workflow_version(session: Session) -> None:
    session.add(
        CaliberWorkflow(
            workflow_id="demo-travel-booking",
            name="Travel Booking Pipeline",
            description="demo",
            owner="@demo",
            status="active",
        )
    )
    session.add(
        CaliberWorkflowVersion(
            version_id="WV-DEMO-0001",
            workflow_id="demo-travel-booking",
            version_number=3,
            status="published",
            manifest={
                "workflow_id": "demo-travel-booking",
                "nodes": {
                    "triage_agent": {
                        "id": "triage_agent",
                        "type": "agent",
                        "name": "Triage Agent",
                        "instructions": {
                            "type": "inline",
                            "text": "Route travel intents to specialists.",
                        },
                    },
                    "router_agent": {
                        "id": "router_agent",
                        "type": "agent",
                        "name": "Router Agent",
                        "instructions": {
                            "type": "mlflow_prompt",
                            "ref": "router_ref",
                        },
                    },
                },
                "artifacts": {
                    "prompts": {
                        "router_ref": {
                            "registry_name": "wf-custom-registry",
                            "alias": "prod",
                        }
                    }
                },
            },
            manifest_hash="demo-hash",
            created_by="@demo",
        )
    )
    session.commit()


def _insert_eval_dataset(session: Session, dataset_id: str = "EDS-1") -> CaliberEvalDataset:
    dataset = CaliberEvalDataset(
        dataset_id=dataset_id,
        name=f"dataset-{dataset_id.lower()}",
        description="test dataset",
        owner="@test",
        tags=["prompt-opt"],
        status="active",
        version=1,
    )
    session.add(dataset)
    session.commit()
    return dataset


def _install_mlflow(
    monkeypatch,
    *,
    load_refs: dict[str, Any] | None = None,
    search_items: list[object] | None = None,
    register_impl: Any | None = None,
    set_alias_impl: Any | None = None,
    client_cls: type | None = None,
    use_genai: bool = True,
    include_load: bool = True,
    include_search: bool = True,
    include_register: bool = True,
    include_set_alias: bool = True,
) -> dict[str, list[Any]]:
    load_refs = load_refs or {}
    search_items = search_items or []
    register_calls: list[Any] = []
    alias_calls: list[Any] = []

    def load_prompt(ref: str, allow_missing: bool = False) -> object | None:
        value = load_refs.get(ref)
        if isinstance(value, Exception):
            raise value
        return value

    def search_prompts() -> list[object]:
        return search_items

    def register_prompt(**kwargs: object) -> object:
        register_calls.append(kwargs)
        if register_impl is not None:
            return register_impl(**kwargs)
        name = str(kwargs["name"])
        return SimpleNamespace(version=7, uri=f"prompts:/{name}/7")

    def set_prompt_alias(name: str, alias: str, version: int) -> None:
        alias_calls.append((name, alias, version))
        if set_alias_impl is not None:
            set_alias_impl(name, alias, version)

    mlflow_mod = types.ModuleType("mlflow")
    if use_genai:
        genai_attrs: dict[str, object] = {}
        if include_load:
            genai_attrs["load_prompt"] = load_prompt
        if include_search:
            genai_attrs["search_prompts"] = search_prompts
        if include_register:
            genai_attrs["register_prompt"] = register_prompt
        if include_set_alias:
            genai_attrs["set_prompt_alias"] = set_prompt_alias
        mlflow_mod.genai = SimpleNamespace(**genai_attrs)
    else:
        if include_load:
            mlflow_mod.load_prompt = load_prompt  # type: ignore[attr-defined]
        if include_search:
            mlflow_mod.search_prompts = search_prompts  # type: ignore[attr-defined]
        if include_register:
            mlflow_mod.register_prompt = register_prompt  # type: ignore[attr-defined]
        if include_set_alias:
            mlflow_mod.set_prompt_alias = set_prompt_alias  # type: ignore[attr-defined]

    if client_cls is not None:
        mlflow_mod.MlflowClient = client_cls  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "mlflow", mlflow_mod)
    return {"register_calls": register_calls, "alias_calls": alias_calls}


def test_list_prompts_merges_mlflow_caliber_and_workflow_rows(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support", name="Support")
    _insert_agent(db_session, agent_id="orders-agent", experiment_id="exp-orders", name="Orders")
    _insert_workflow_version(db_session)

    _install_mlflow(
        monkeypatch,
        search_items=[
            SimpleNamespace(
                name="support-agent", description="support", creation_timestamp=1, tags={}
            ),
            SimpleNamespace(
                name="external-prompt", description="external", creation_timestamp=2, tags={}
            ),
            # A workflow agent node whose prompt *is* deployed to the registry.
            SimpleNamespace(
                name="wf-custom-registry", description="router", creation_timestamp=3, tags={}
            ),
        ],
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent",
                version=3,
                template="Support template",
                tags={"caliber.approval_id": "APR-123"},
            ),
            "prompts:/external-prompt@prod": SimpleNamespace(
                name="external-prompt",
                version=1,
                template="External template",
                tags={},
            ),
            "prompts:/wf-custom-registry@prod": SimpleNamespace(
                name="wf-custom-registry",
                version=1,
                template="Router template",
                tags={},
            ),
        },
    )

    response = client.get(PREFIX)
    assert response.status_code == 200
    rows = {row["agent_id"]: row for row in response.json()["data"]}

    # Deployed prompts are listed and enriched with agent/workflow metadata.
    assert rows["support-agent"]["source"] == "both"
    assert rows["support-agent"]["has_prompt"] is True
    assert rows["support-agent"]["needs_prompt"] is False
    assert rows["support-agent"]["approval_id"] == "APR-123"
    assert rows["support-agent"]["available_aliases"] == ["prod"]

    assert rows["external-prompt"]["has_prompt"] is True
    assert rows["external-prompt"]["needs_prompt"] is False

    # A deployed workflow agent prompt keeps its "Workflow / Node" name.
    workflow_ref = rows["wf-custom-registry"]
    assert workflow_ref["source"] == "mlflow"
    assert workflow_ref["has_prompt"] is True
    assert workflow_ref["needs_prompt"] is False
    assert workflow_ref["agent_name"] == "Travel Booking Pipeline / Router Agent"
    assert workflow_ref["available_aliases"] == ["prod"]

    # The full inventory now surfaces the promptless backlog too: a registered
    # agent without a deployed prompt (orders-agent) and a workflow node whose
    # prompt lives inline in the manifest (wf-demo-travel-booking-triage_agent)
    # appear as ``needs_prompt`` rows so the page is the canonical backlog.
    assert rows["orders-agent"]["has_prompt"] is False
    assert rows["orders-agent"]["needs_prompt"] is True
    inline_node = rows["wf-demo-travel-booking-triage_agent"]
    assert inline_node["has_prompt"] is False
    assert inline_node["needs_prompt"] is True
    assert inline_node["agent_name"] == "Travel Booking Pipeline / Triage Agent"

    # Every item carries the explicit needs_prompt flag, the inverse of has_prompt.
    for row in rows.values():
        assert "needs_prompt" in row
        assert row["needs_prompt"] is (not row["has_prompt"])

    # Deterministic ordering: deployed prompts come before the needs-prompt backlog.
    ordered = [r["agent_id"] for r in response.json()["data"]]
    deployed_idx = [i for i, name in enumerate(ordered) if rows[name]["has_prompt"]]
    needs_idx = [i for i, name in enumerate(ordered) if rows[name]["needs_prompt"]]
    assert max(deployed_idx) < min(needs_idx)


def test_list_prompts_uses_top_level_mlflow_functions_when_genai_missing(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    _insert_agent(db_session)
    _install_mlflow(
        monkeypatch,
        use_genai=False,
        search_items=[
            SimpleNamespace(
                name="support-agent", description=None, creation_timestamp=None, tags={}
            )
        ],
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent",
                version=5,
                template="Top-level API",
                tags={},
            )
        },
    )

    response = client.get(PREFIX)
    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["agent_id"] == "support-agent"
    assert row["version"] == 5


def test_list_prompts_prefers_staging_when_prod_missing(
    client: TestClient,
    db_session: Session,
    monkeypatch,
    multi_alias: None,
) -> None:
    _insert_agent(db_session)
    _install_mlflow(
        monkeypatch,
        search_items=[
            SimpleNamespace(
                name="support-agent", description="support", creation_timestamp=1, tags={}
            )
        ],
        load_refs={
            "prompts:/support-agent@prod": None,
            "prompts:/support-agent@staging": SimpleNamespace(
                name="support-agent",
                version=6,
                template="Stage template",
                tags={},
            ),
            "prompts:/support-agent@dev": None,
        },
    )

    response = client.get(PREFIX)
    assert response.status_code == 200
    row = response.json()["data"][0]
    assert row["alias"] == "staging"
    assert row["available_aliases"] == ["staging"]
    assert row["artifact_ref"] == "prompts:/support-agent@staging"


def test_create_prompt_validation_and_success(
    client: TestClient, monkeypatch, multi_alias: None
) -> None:
    calls = _install_mlflow(monkeypatch)

    missing_name = client.post(PREFIX, json={"template": "x"})
    assert missing_name.status_code == 400

    bad_name = client.post(PREFIX, json={"name": "bad name", "template": "x"})
    assert bad_name.status_code == 400

    bad_tags = client.post(PREFIX, json={"name": "good_name", "template": "x", "tags": []})
    assert bad_tags.status_code == 400

    ok = client.post(
        PREFIX,
        json={
            "name": "good_name",
            "template": "hello",
            "commit_message": "create",
            "tags": {"x": "y"},
        },
    )
    assert ok.status_code == 201
    payload = ok.json()["data"]
    assert payload["name"] == "good_name"
    assert payload["version"] == 7
    assert payload["active_alias"] == "prod"
    assert len(calls["register_calls"]) == 1
    assert calls["alias_calls"] == [("good_name", "prod", 7)]

    staging = client.post(
        PREFIX,
        json={
            "name": "staging_name",
            "template": "hello",
            "target_alias": "staging",
        },
    )
    assert staging.status_code == 201
    assert staging.json()["data"]["active_alias"] == "staging"
    assert calls["alias_calls"][-1] == ("staging_name", "staging", 7)


def test_create_prompt_returns_502_when_register_fails(client: TestClient, monkeypatch) -> None:
    def _raise_register(**_kwargs: object) -> object:
        raise RuntimeError("register failed")

    _install_mlflow(monkeypatch, register_impl=_raise_register)

    response = client.post(PREFIX, json={"name": "x", "template": "hello"})
    assert response.status_code == 502
    assert "failed to register prompt" in response.json()["detail"]


def test_get_prompt_success_and_not_found(client: TestClient, monkeypatch) -> None:
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent",
                version=4,
                template="full template",
            ),
            "prompts:/missing@prod": None,
        },
    )

    ok = client.get(f"{PREFIX}/support-agent")
    assert ok.status_code == 200
    assert ok.json()["data"]["template"] == "full template"

    missing = client.get(f"{PREFIX}/missing")
    assert missing.status_code == 404


def test_create_prompt_version_success_and_503_when_api_missing(
    client: TestClient,
    monkeypatch,
    multi_alias: None,
) -> None:
    calls = _install_mlflow(monkeypatch)
    ok = client.post(
        f"{PREFIX}/support-agent/versions",
        json={"template": "v2", "tags": {}, "target_alias": "staging"},
    )
    assert ok.status_code == 201
    assert ok.json()["data"]["version"] == 7
    assert ok.json()["data"]["active_alias"] == "staging"
    assert calls["alias_calls"] == [("support-agent", "staging", 7)]

    _install_mlflow(monkeypatch, include_register=False)
    unavailable = client.post(f"{PREFIX}/support-agent/versions", json={"template": "v3"})
    assert unavailable.status_code == 503


def test_create_prompt_version_defaults_to_promoting_live(
    client: TestClient,
    monkeypatch,
) -> None:
    """Backward compatibility: omitting ``promote`` rotates the live ``prod`` alias."""
    calls = _install_mlflow(monkeypatch)
    ok = client.post(f"{PREFIX}/support-agent/versions", json={"template": "v2"})
    assert ok.status_code == 201
    data = ok.json()["data"]
    assert data["active_alias"] == "prod"
    assert data["alias_changed"] is True
    assert calls["alias_calls"] == [("support-agent", "prod", 7)]


def test_create_prompt_version_draft_does_not_rotate_alias(
    client: TestClient,
    monkeypatch,
) -> None:
    """``promote: false`` registers the version but leaves the live alias untouched."""
    calls = _install_mlflow(monkeypatch)
    ok = client.post(
        f"{PREFIX}/support-agent/versions",
        json={"template": "candidate draft", "promote": False},
    )
    assert ok.status_code == 201
    data = ok.json()["data"]
    # The version is registered…
    assert data["version"] == 7
    assert len(calls["register_calls"]) == 1
    # …but no alias is rotated, so a developer can evaluate before going live.
    assert data["alias_changed"] is False
    assert data["active_alias"] == ""
    assert calls["alias_calls"] == []


def test_create_prompt_version_draft_ignores_target_alias(
    client: TestClient,
    monkeypatch,
    multi_alias: None,
) -> None:
    """When not promoting, a supplied ``target_alias`` must not trigger a rotation."""
    calls = _install_mlflow(monkeypatch)
    ok = client.post(
        f"{PREFIX}/support-agent/versions",
        json={"template": "v9", "promote": False, "target_alias": "staging"},
    )
    assert ok.status_code == 201
    assert ok.json()["data"]["active_alias"] == ""
    assert calls["alias_calls"] == []


def test_create_prompt_version_rejects_non_boolean_promote(
    client: TestClient,
    monkeypatch,
) -> None:
    _install_mlflow(monkeypatch)
    bad = client.post(
        f"{PREFIX}/support-agent/versions",
        json={"template": "v2", "promote": "yes"},
    )
    assert bad.status_code == 400


def test_list_prompt_versions_reads_aliases_from_mlflow_client(
    client: TestClient,
    monkeypatch,
) -> None:
    class FakeClient:
        def search_prompt_versions(self, name: str) -> list[object]:
            assert name == "support-agent"
            return [
                SimpleNamespace(
                    version=3,
                    source=None,
                    tags={"mlflow.prompt.commit_message": "old"},
                    description=None,
                    commit_message="old",
                    creation_timestamp=100,
                    last_updated_timestamp=120,
                    run_id=None,
                    aliases=[],
                ),
                SimpleNamespace(
                    version=4,
                    source=None,
                    tags={},
                    description="new",
                    commit_message="new",
                    creation_timestamp=200,
                    last_updated_timestamp=220,
                    run_id=None,
                    aliases=[],
                ),
            ]

        def get_prompt_version_by_alias(self, name: str, alias: str) -> object:
            assert name == "support-agent"
            mapping = {
                "prod": 4,
                "canary": 3,
            }
            if alias not in mapping:
                raise RuntimeError(f"alias {alias} not found")
            return SimpleNamespace(version=mapping[alias])

    _install_mlflow(monkeypatch, client_cls=FakeClient)

    response = client.get(f"{PREFIX}/support-agent/versions")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["version"] for row in data] == [4, 3]
    assert data[0]["aliases"] == ["prod"]
    assert data[0]["current"] is True
    assert data[0]["commit_message"] == "new"
    assert data[0]["source"] == "prompts:/support-agent/4"
    assert data[1]["aliases"] == []


def test_list_prompt_versions_falls_back_to_model_registry_metadata(
    client: TestClient,
    monkeypatch,
) -> None:
    class FakeClient:
        def get_registered_model(self, name: str) -> object:
            return SimpleNamespace(aliases={"prod": "4", "canary": "3"})

        def search_model_versions(self, _query: str) -> list[object]:
            return [
                SimpleNamespace(
                    version=3,
                    source="models:/support-agent/3",
                    tags={"mlflow.prompt.commit_message": "old"},
                    description=None,
                    creation_timestamp=100,
                    last_updated_timestamp=120,
                    run_id="run-3",
                ),
                SimpleNamespace(
                    version=4,
                    source="models:/support-agent/4",
                    tags={},
                    description="new",
                    creation_timestamp=200,
                    last_updated_timestamp=220,
                    run_id="run-4",
                ),
            ]

    _install_mlflow(monkeypatch, client_cls=FakeClient)

    response = client.get(f"{PREFIX}/support-agent/versions")
    assert response.status_code == 200
    data = response.json()["data"]
    assert [row["version"] for row in data] == [4, 3]
    assert data[0]["aliases"] == ["prod"]
    assert data[0]["current"] is True
    assert data[1]["aliases"] == ["canary"]


def test_list_prompt_versions_falls_back_to_live_alias_when_model_registry_fails(
    client: TestClient,
    monkeypatch,
) -> None:
    class FailingClient:
        def get_registered_model(self, _name: str) -> object:
            raise RuntimeError("model registry unavailable")

        def search_model_versions(self, _query: str) -> list[object]:
            raise RuntimeError("model registry unavailable")

    _install_mlflow(
        monkeypatch,
        client_cls=FailingClient,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent",
                version=2,
                template="fallback",
                tags={},
            )
        },
    )

    response = client.get(f"{PREFIX}/support-agent/versions")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data == [
        {
            "name": "support-agent",
            "version": 2,
            "aliases": ["prod"],
            "creation_timestamp": None,
            "updated_timestamp": None,
            "run_id": None,
            "source": "prompts:/support-agent@prod",
            "commit_message": None,
            "current": True,
        }
    ]


def test_get_prompt_version_and_set_alias_validation_paths(
    client: TestClient,
    monkeypatch,
) -> None:
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent/2": SimpleNamespace(
                name="support-agent", version=2, template="v2"
            )
        },
    )

    ok = client.get(f"{PREFIX}/support-agent/versions/2")
    assert ok.status_code == 200
    assert ok.json()["data"]["template"] == "v2"

    bad_version = client.get(f"{PREFIX}/support-agent/versions/not-an-int")
    assert bad_version.status_code == 400

    bad_alias = client.post(f"{PREFIX}/support-agent/aliases/bad!alias", json={"version": 2})
    assert bad_alias.status_code == 400

    bad_body = client.post(f"{PREFIX}/support-agent/aliases/prod", json={"version": None})
    assert bad_body.status_code == 400


def test_set_alias_and_version_errors(client: TestClient, monkeypatch) -> None:
    _install_mlflow(monkeypatch, include_set_alias=False)
    missing_api = client.post(f"{PREFIX}/support-agent/aliases/prod", json={"version": 1})
    assert missing_api.status_code == 503

    def _raise_alias(_name: str, _alias: str, _version: int) -> None:
        raise RuntimeError("alias failed")

    _install_mlflow(monkeypatch, set_alias_impl=_raise_alias)
    failed = client.post(f"{PREFIX}/support-agent/aliases/prod", json={"version": 1})
    assert failed.status_code == 502

    called = _install_mlflow(monkeypatch)
    ok = client.post(f"{PREFIX}/support-agent/aliases/prod", json={"version": "3"})
    assert ok.status_code == 200
    assert called["alias_calls"] == [("support-agent", "prod", 3)]


def _audit_rows(session_factory: sessionmaker[Session], entity_id: str) -> list[CaliberAuditLog]:
    with session_factory() as session:
        return list(
            session.execute(
                select(CaliberAuditLog)
                .where(CaliberAuditLog.entity_type == "prompt")
                .where(CaliberAuditLog.entity_id == entity_id)
                .order_by(CaliberAuditLog.log_id.asc())
            )
            .scalars()
            .all()
        )


def test_promote_records_audit_with_exact_previous_live_and_gate(
    client: TestClient,
    monkeypatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Promoting an alias writes an auditable ``promote_prompt`` row that captures
    the exact outgoing version and the advisory-gate override."""
    calls = _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=4, template="live v4"
            )
        },
    )
    resp = client.post(
        f"{PREFIX}/support-agent/aliases/prod",
        json={
            "version": 5,
            "gate_state": "fail",
            "gate_score": 0.71,
            "overridden": True,
            "override_reason": "urgent hotfix",
        },
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["version"] == 5
    assert data["previous_live_version"] == 4
    assert ("support-agent", "prod", 5) in calls["alias_calls"]

    rows = _audit_rows(session_factory, "support-agent")
    promote = [r for r in rows if r.action == "promote_prompt"]
    assert len(promote) == 1
    details = promote[0].details or {}
    assert details["from_version"] == 4
    assert details["to_version"] == 5
    assert details["gate_state"] == "fail"
    assert details["gate_score"] == 0.71
    assert details["overridden"] is True
    assert details["override_reason"] == "urgent hotfix"

    # The advisory verdict is persisted keyed by the now-live version so the
    # Version panel can read it back.
    verdict = client.get("/ajax-api/2.0/mlflow/caliber/gate-verdicts/prompt/5")
    assert verdict.status_code == 200
    verdict_data = verdict.json()["data"]
    assert verdict_data["state"] == "fail"
    assert verdict_data["score"] == 0.71


def test_promote_expected_version_guard(client: TestClient, monkeypatch) -> None:
    """A stale ``expected_version`` blocks the rotation with 409; a matching one
    lets it through."""
    calls = _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=4, template="live v4"
            )
        },
    )
    stale = client.post(
        f"{PREFIX}/support-agent/aliases/prod",
        json={"version": 5, "expected_version": 2},
    )
    assert stale.status_code == 409
    assert calls["alias_calls"] == []  # no rotation happened

    ok = client.post(
        f"{PREFIX}/support-agent/aliases/prod",
        json={"version": 5, "expected_version": 4},
    )
    assert ok.status_code == 200
    assert ("support-agent", "prod", 5) in calls["alias_calls"]


def test_promote_rejects_bad_gate_fields(client: TestClient, monkeypatch) -> None:
    _install_mlflow(monkeypatch)
    bad = client.post(
        f"{PREFIX}/support-agent/aliases/prod",
        json={"version": 2, "overridden": "yes"},
    )
    assert bad.status_code == 400


def test_rollback_prompt_restores_exact_previous_live(
    client: TestClient,
    monkeypatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Rollback rotates the alias back to the exact version recorded as live
    before the current one, and writes a ``rollback_prompt`` audit row."""
    # Current live is v5; an audited promotion recorded v4 -> v5.
    calls = _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=5, template="live v5"
            )
        },
    )
    with session_factory() as session:
        session.add(
            CaliberAuditLog(
                actor="@op",
                action="promote_prompt",
                entity_type="prompt",
                entity_id="support-agent",
                details={"alias": "prod", "from_version": 4, "to_version": 5},
            )
        )
        session.commit()

    resp = client.post(f"{PREFIX}/support-agent/rollback", json={})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["version"] == 4
    assert data["rolled_back_from"] == 5
    assert ("support-agent", "prod", 4) in calls["alias_calls"]

    rows = _audit_rows(session_factory, "support-agent")
    rollback = [r for r in rows if r.action == "rollback_prompt"]
    assert len(rollback) == 1
    assert (rollback[0].details or {})["to_version"] == 4


def test_rollback_prompt_twice_walks_backward_not_oscillating(
    client: TestClient,
    monkeypatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Two consecutive rollbacks step strictly backward through the promote
    chain (v3->v2 then v2->v1), never oscillating v3<->v2. Regression for the
    rollback rows shadowing the promote chain."""
    # Promote history: 1->2 then 2->3, live is v3.
    with session_factory() as session:
        for frm, to in ((1, 2), (2, 3)):
            session.add(
                CaliberAuditLog(
                    actor="@op",
                    action="promote_prompt",
                    entity_type="prompt",
                    entity_id="support-agent",
                    details={"alias": "prod", "from_version": frm, "to_version": to},
                )
            )
        session.commit()

    # Rollback #1: live v3 -> v2.
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=3, template="v3"
            )
        },
    )
    first = client.post(f"{PREFIX}/support-agent/rollback", json={})
    assert first.status_code == 200
    assert first.json()["data"]["version"] == 2

    # Rollback #2: live is now v2 -> must go to v1 (NOT back to v3).
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=2, template="v2"
            )
        },
    )
    second = client.post(f"{PREFIX}/support-agent/rollback", json={})
    assert second.status_code == 200
    assert second.json()["data"]["version"] == 1  # walked back, not oscillated to 3


def test_rollback_prompt_409_when_no_prior_promotion(
    client: TestClient,
    monkeypatch,
    session_factory: sessionmaker[Session],
) -> None:
    """A live version with no recorded prior promotion cannot be rolled back."""
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent", version=5, template="live v5"
            )
        },
    )
    resp = client.post(f"{PREFIX}/support-agent/rollback", json={})
    assert resp.status_code == 409


def test_rollback_prompt_409_when_no_live_version(
    client: TestClient,
    monkeypatch,
) -> None:
    """No live version on the alias -> nothing to roll back."""
    _install_mlflow(monkeypatch)  # load_prompt returns None for every ref
    resp = client.post(f"{PREFIX}/support-agent/rollback", json={})
    assert resp.status_code == 409


def test_test_render_prompt_uses_loaded_template_and_fallback(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")

    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@prod": SimpleNamespace(
                name="support-agent",
                version=8,
                template="Hi {{name}} from {{team}}",
                tags={},
            )
        },
    )

    rendered = client.post(
        f"{PREFIX}/support-agent/test-render",
        json={"variables": {"name": "Alex"}},
    )
    assert rendered.status_code == 200
    data = rendered.json()["data"]
    assert data["rendered_content"] == "Hi Alex from {{team}}"
    assert data["detected_variables"] == ["name", "team"]
    assert data["unresolved_variables"] == ["team"]
    assert data["version"] == 8

    _install_mlflow(monkeypatch, load_refs={"prompts:/support-agent@prod": None})
    fallback = client.post(
        f"{PREFIX}/support-agent/test-render",
        json={"variables": {"agent_name": "Support Agent"}},
    )
    assert fallback.status_code == 200
    assert "a helpful AI assistant" in fallback.json()["data"]["original_template"]


def test_test_render_prompt_rejects_invalid_variables_and_unknown_agent(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    _install_mlflow(monkeypatch, load_refs={"prompts:/support-agent@prod": None})

    bad_variables = client.post(f"{PREFIX}/support-agent/test-render", json={"variables": []})
    assert bad_variables.status_code == 400

    missing_agent = client.post(
        f"{PREFIX}/missing-agent/test-render",
        json={"variables": {"x": "y"}},
    )
    assert missing_agent.status_code == 404


def test_prompt_template_library_and_preview_routes(client: TestClient) -> None:
    library = client.get(f"{PREFIX}/template-library")
    assert library.status_code == 200
    payload = library.json()["data"]
    assert payload["catalog_version"] == "2.0.0"
    assert len(payload["base_templates"]) >= 1
    assert len(payload["modifiers"]) >= 1
    assert len(payload["starter_recipes"]) >= 1
    assert "custom-prompt" in {item["id"] for item in payload["base_templates"]}
    recipe_map = {item["id"]: item for item in payload["starter_recipes"]}
    assert recipe_map["rag-grounded-qa"]["base_template_id"] == "rag-grounded-qa"
    assert recipe_map["rag-grounded-qa"]["support_level"] == "builder"
    assert recipe_map["rag-grounded-qa"]["title"] == "rag-grounded-qa"
    assert recipe_map["react-tool-loop"]["support_level"] == "builder"
    assert recipe_map["react-tool-loop"]["title"] == "react-tool-loop"

    preview = client.post(
        f"{PREFIX}/template-library/preview",
        json={
            "base_template_id": "rag-grounded-qa",
            "preview_variables": {
                "retrieved_docs": "Policy doc excerpt",
                "question": "How long do refunds take after approval?",
            },
        },
    )
    assert preview.status_code == 200
    compiled = preview.json()["data"]
    assert "Retrieved context:\n{{retrieved_docs}}" in compiled["compiled_template"]
    assert compiled["validation_report"]["valid"] is True
    assert "retrieved_docs" in {item["name"] for item in compiled["runtime_variables"]}
    assert compiled["recommended_scorers"]

    extraction_preview = client.post(
        f"{PREFIX}/template-library/preview",
        json={
            "base_template_id": "extract-structured-data",
        },
    )
    assert extraction_preview.status_code == 200
    extraction = extraction_preview.json()["data"]
    values = {item["name"]: item["value"] for item in extraction["builder_variables"]}
    assert extraction["validation_report"]["valid"] is True
    assert '"invoice_number": "string | null"' in values["schema"]

    hallucination_recipe = recipe_map["check-hallucination"]
    hallucination_preview = client.post(
        f"{PREFIX}/template-library/preview",
        json={
            "base_template_id": hallucination_recipe["base_template_id"],
            "modifier_ids": hallucination_recipe["modifier_ids"],
            "builder_values": hallucination_recipe["builder_values"],
            "preview_variables": hallucination_recipe["preview_variables"],
            "runtime_variables": hallucination_recipe["runtime_variables"],
            "template_override": hallucination_recipe["template_override"],
        },
    )
    assert hallucination_preview.status_code == 200
    hallucination = hallucination_preview.json()["data"]
    assert hallucination["validation_report"]["valid"] is True
    assert "unsupported_claims" in hallucination["rendered_preview"]

    override_preview = client.post(
        f"{PREFIX}/template-library/preview",
        json={
            "base_template_id": "grounded-answer",
            "builder_values": {"task_description": "Answer support questions."},
            "section_overrides": {"context": "Only use the attached contract."},
        },
    )
    assert override_preview.status_code == 200
    overridden = override_preview.json()["data"]
    assert overridden["overridden_sections"] == ["context"]
    assert "Only use the attached contract." in overridden["compiled_template"]
    assert "Answering goal:" not in overridden["compiled_template"]
    assert "Answering goal:" in overridden["composed_sections"]["context"]


def test_prompt_helpers_handle_missing_or_failing_mlflow_apis(monkeypatch) -> None:
    no_api_mod = types.ModuleType("mlflow")
    no_api_mod.genai = SimpleNamespace()
    monkeypatch.setattr(prompt_routes, "_get_mlflow_module", lambda: no_api_mod)
    assert prompt_routes._load_prompt_info("x") is None
    assert prompt_routes._search_mlflow_prompts() == []

    def _search_raises() -> list[object]:
        raise RuntimeError("search failed")

    search_failing = types.ModuleType("mlflow")
    search_failing.genai = SimpleNamespace(search_prompts=_search_raises)
    monkeypatch.setattr(prompt_routes, "_get_mlflow_module", lambda: search_failing)
    assert prompt_routes._search_mlflow_prompts() == []

    def _load_raises(_ref: str, allow_missing: bool = False) -> object | None:
        raise RuntimeError("load failed")

    load_failing = types.ModuleType("mlflow")
    load_failing.genai = SimpleNamespace(load_prompt=_load_raises)
    monkeypatch.setattr(prompt_routes, "_get_mlflow_module", lambda: load_failing)
    assert prompt_routes._load_prompt_info("x") is None

    load_none = types.ModuleType("mlflow")
    load_none.genai = SimpleNamespace(load_prompt=lambda _ref, allow_missing=False: None)
    monkeypatch.setattr(prompt_routes, "_get_mlflow_module", lambda: load_none)
    assert prompt_routes._load_prompt_info("x") is None


def test_load_prompt_infos_for_names_bounds_slow_lookups(monkeypatch) -> None:
    """A slow/hung registry call must not stall the whole batch.

    Without the deadline this is the 6-7 min "stuck pending" page-load freeze.
    """
    import time as _time

    def _fake_load(agent_id: str, alias: str = "prod") -> dict[str, Any] | None:
        if agent_id == "slow":
            _time.sleep(2.0)  # would hang the route without a deadline
            return {"agent_id": agent_id, "alias": alias}
        if alias == "prod":
            return {"agent_id": agent_id, "alias": alias, "version": 1}
        return None

    monkeypatch.setattr(prompt_routes, "_load_prompt_info", _fake_load)
    monkeypatch.setattr(prompt_routes, "_PROMPT_LOOKUP_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(prompt_routes, "_PROMPT_INFO_CACHE_TTL_SECONDS", 0)

    start = _time.monotonic()
    result = prompt_routes._load_prompt_infos_for_names(["fast", "slow"])
    elapsed = _time.monotonic() - start

    # Bounded by the ~0.3s deadline, not the 2s sleep.
    assert elapsed < 1.5
    # The healthy prompt resolved; the lagging one is simply omitted.
    assert result["fast"]["prod"]["agent_id"] == "fast"
    assert result["slow"] == {}


def test_load_prompt_info_cached_reuses_within_ttl(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def _fake_load(agent_id: str, alias: str = "prod") -> dict[str, Any]:
        calls.append((agent_id, alias))
        return {"agent_id": agent_id, "alias": alias}

    monkeypatch.setattr(prompt_routes, "_load_prompt_info", _fake_load)
    monkeypatch.setattr(prompt_routes, "_PROMPT_INFO_CACHE_TTL_SECONDS", 60)
    prompt_routes._reset_prompt_info_cache()

    first = prompt_routes._load_prompt_info_cached("x", "prod")
    second = prompt_routes._load_prompt_info_cached("x", "prod")

    assert first == second
    # The second read is served from cache — MLflow is hit only once.
    assert calls == [("x", "prod")]


def test_get_prompt_optimization_options(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prompt_routes,
        "_module_available",
        lambda module_name: module_name != "deepeval",
    )

    response = client.get(f"{PREFIX}/optimization/options")
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["optimizers"] == ["MetaPrompt", "GEPA"]
    assert payload["default_optimizer"] == "MetaPrompt"
    assert len(payload["scorers"]) >= 7
    assert payload["default_gate"]["min_aggregate_score"] == 0.85
    assert payload["runtime"]["deepeval"]["available"] is False
    assert payload["runtime"]["deepeval"]["install_command"] == "pip install -U deepeval"

    deepeval_faithfulness = next(
        scorer for scorer in payload["scorers"] if scorer["name"] == "DeepEval.Faithfulness"
    )
    assert deepeval_faithfulness["available"] is False
    assert deepeval_faithfulness["install_command"] == "pip install -U deepeval"


def test_get_prompt_calibration_options_alias(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        prompt_routes,
        "_module_available",
        lambda module_name: module_name != "deepeval",
    )

    response = client.get(f"{PREFIX}/calibration/options")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["optimizers"] == ["MetaPrompt", "GEPA"]
    assert payload["default_optimizer"] == "MetaPrompt"
    assert payload["runtime"]["deepeval"]["available"] is False


def test_create_prompt_optimization_run_creates_verified_item_and_job(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    _insert_eval_dataset(db_session, "EDS-PROMPT-1")

    response = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-PROMPT-1",
            "optimizer_type": "GEPA",
            "scorers": [
                {"name": "Correctness", "weight": 0.7, "config": {}},
                {
                    "name": "Guidelines",
                    "weight": 0.3,
                    "config": {"guidelines": ["stay factual", "avoid PII"]},
                },
            ],
            "gate": {"min_aggregate_score": 0.78, "max_regression_delta": 0.08},
            "notes": "Run from prompts UI",
        },
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["job"]["optimizer_type"] == "GEPA"
    assert payload["job"]["status"] == "queued"
    assert payload["item"]["status"] == "verified"
    assert payload["item"]["category"] == "prompt_optimization"

    item = db_session.get(CaliberVerificationItem, payload["item"]["item_id"])
    assert item is not None
    assert item.status == "verified"
    assert isinstance(item.submitted_context, dict)
    ctx = item.submitted_context["prompt_optimization"]
    assert ctx["eval_dataset_id"] == "EDS-PROMPT-1"
    assert ctx["optimizer_type"] == "GEPA"
    assert len(ctx["scorers"]) == 2
    assert ctx["gate"]["min_aggregate_score"] == 0.78

    job = db_session.get(CaliberRefinementJob, payload["job"]["job_id"])
    assert job is not None
    assert job.primary_item_id == item.item_id
    assert job.optimizer_type == "GEPA"


def test_prompt_optimization_run_pins_current_version_and_stays_reproducible(
    client: TestClient,
    db_session: Session,
    session_factory: sessionmaker[Session],
) -> None:
    """A run pins the dataset's version at launch; a later append can't change it.

    Reproducibility guarantee: launch pins v1, then we append an example (which
    bumps the dataset to v2). The run's recorded ``eval_dataset_version`` must
    still be 1, and resolving the dataset as of v1 must return the v1-only set
    (not the v2 set).
    """
    from caliber.eval.predict import build_db_load_dataset

    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    dataset = _insert_eval_dataset(db_session, "EDS-REPRO")
    # Seed one example at v1 (the dataset already starts at version 1).
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="EDS-REPRO-EX1",
            dataset_id=dataset.dataset_id,
            dataset_version=1,
            input={"question": "v1"},
            expected={"answer": "a1"},
        )
    )
    db_session.commit()

    # Launch: no explicit version → pin the dataset's current version (1).
    response = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-REPRO",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert response.status_code == 201
    payload = response.json()["data"]
    item = db_session.get(CaliberVerificationItem, payload["item"]["item_id"])
    assert item is not None
    assert item.submitted_context["prompt_optimization"]["eval_dataset_version"] == 1

    # Now mutate the dataset AFTER the run: append a v2 example (bumps to v2).
    append = client.post(
        f"/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset.dataset_id}/examples",
        json={"input": {"question": "v2"}, "expected": {"answer": "a2"}},
    )
    assert append.status_code == 201
    db_session.expire_all()
    refreshed = db_session.get(CaliberEvalDataset, dataset.dataset_id)
    assert refreshed is not None
    assert refreshed.version == 2  # dataset moved on

    # The run still says v1, and resolving as of v1 yields the v1-only set.
    assert item.submitted_context["prompt_optimization"]["eval_dataset_version"] == 1
    load = build_db_load_dataset(session_factory)
    as_of_v1 = load("EDS-REPRO", 1)
    assert as_of_v1 == [{"inputs": {"question": "v1"}, "expectations": {"answer": "a1"}}]
    # And the current (unpinned) set now has both — proving the edit DID land,
    # so the v1 pin is genuinely protecting the run from it.
    current = load("EDS-REPRO")
    assert len(current) == 2


def test_prompt_optimization_run_accepts_explicit_in_range_version(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    dataset = _insert_eval_dataset(db_session, "EDS-EXPLICIT")
    dataset.version = 3
    db_session.commit()

    response = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-EXPLICIT",
            "eval_dataset_version": 2,
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert response.status_code == 201
    payload = response.json()["data"]
    item = db_session.get(CaliberVerificationItem, payload["item"]["item_id"])
    assert item is not None
    assert item.submitted_context["prompt_optimization"]["eval_dataset_version"] == 2


def test_prompt_optimization_run_rejects_out_of_range_version(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    dataset = _insert_eval_dataset(db_session, "EDS-OOR")
    dataset.version = 2
    db_session.commit()

    # Version 5 exceeds the dataset's current version (2) → 400.
    response = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-OOR",
            "eval_dataset_version": 5,
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert response.status_code == 400
    assert "eval_dataset_version must be between 1 and 2" in response.json()["detail"]


def test_create_prompt_calibration_run_alias_creates_verified_item_and_job(
    client: TestClient,
    db_session: Session,
    monkeypatch,
    multi_alias: None,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    _insert_eval_dataset(db_session, "EDS-PROMPT-CAL")
    _install_mlflow(
        monkeypatch,
        load_refs={
            "prompts:/support-agent@staging": SimpleNamespace(
                name="support-agent",
                version=4,
                template="Staging baseline prompt",
                tags={},
            )
        },
    )

    response = client.post(
        f"{PREFIX}/calibration/runs",
        json={
            "agent_id": "support-agent",
            "prompt_alias": "staging",
            "eval_dataset_id": "EDS-PROMPT-CAL",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
            "notes": "Run from prompt calibration UI",
        },
    )

    assert response.status_code == 201
    payload = response.json()["data"]
    assert payload["job"]["optimizer_type"] == "MetaPrompt"
    assert payload["job"]["status"] == "queued"
    assert payload["item"]["category"] == "prompt_optimization"

    item = db_session.get(CaliberVerificationItem, payload["item"]["item_id"])
    assert item is not None
    assert item.submitted_context["prompt_optimization"]["eval_dataset_id"] == "EDS-PROMPT-CAL"
    assert item.submitted_context["prompt_optimization"]["prompt_alias"] == "staging"
    assert (
        item.submitted_context["prompt_optimization"]["baseline_content"]
        == "Staging baseline prompt"
    )
    assert item.artifact_ref == "prompts:/support-agent@staging"


def test_create_prompt_optimization_run_rejects_invalid_input(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    _insert_eval_dataset(db_session, "EDS-PROMPT-2")

    # An unknown agent_id no longer 404s — calibration auto-provisions a hidden
    # prompt target so prompt testing/calibration never requires an agent.
    unknown_agent = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "missing-agent",
            "eval_dataset_id": "EDS-PROMPT-2",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert unknown_agent.status_code == 201
    db_session.expire_all()
    provisioned = db_session.get(CaliberAgentConfig, "missing-agent")
    assert provisioned is not None
    assert provisioned.optimizer_config["source_type"] == "prompt_target"

    unknown_dataset = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-missing",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert unknown_dataset.status_code == 404

    bad_optimizer = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-PROMPT-2",
            "optimizer_type": "TextGrad",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert bad_optimizer.status_code == 400

    bad_scorer = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-PROMPT-2",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "UnknownScorer", "weight": 1.0, "config": {}}],
        },
    )
    assert bad_scorer.status_code == 400

    missing_guidelines_config = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-PROMPT-2",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Guidelines", "weight": 1.0, "config": {}}],
        },
    )
    assert missing_guidelines_config.status_code == 400


def test_create_prompt_optimization_run_rejects_unavailable_deepeval_scorer(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    monkeypatch.setattr(prompt_routes, "_module_available", lambda _module_name: False)
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")
    _insert_eval_dataset(db_session, "EDS-PROMPT-3")

    response = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "support-agent",
            "eval_dataset_id": "EDS-PROMPT-3",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "DeepEval.Faithfulness", "weight": 1.0, "config": {}}],
        },
    )

    assert response.status_code == 400
    assert "unavailable" in response.json()["detail"]
    assert "pip install -U deepeval" in response.json()["detail"]


def test_prompt_routes_return_503_when_mlflow_is_unavailable(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(prompt_routes, "_get_mlflow_module", lambda: None)

    assert client.post(PREFIX, json={"name": "x", "template": "t"}).status_code == 503
    assert client.get(f"{PREFIX}/x").status_code == 503
    assert client.post(f"{PREFIX}/x/versions", json={"template": "t"}).status_code == 503
    assert client.get(f"{PREFIX}/x/versions/1").status_code == 503
    assert client.post(f"{PREFIX}/x/aliases/prod", json={"version": 1}).status_code == 503


def test_prompt_routes_return_502_when_load_fails_and_404_for_fallback_miss(
    client: TestClient,
    monkeypatch,
) -> None:
    class FailingClient:
        def get_registered_model(self, _name: str) -> object:
            raise RuntimeError("registry down")

        def search_model_versions(self, _query: str) -> list[object]:
            raise RuntimeError("registry down")

    _install_mlflow(
        monkeypatch,
        client_cls=FailingClient,
        load_refs={
            "prompts:/support-agent@prod": RuntimeError("load prompt failed"),
            "prompts:/support-agent/9": RuntimeError("load version failed"),
            "prompts:/unknown@prod": None,
        },
    )

    assert client.get(f"{PREFIX}/support-agent").status_code == 502
    assert client.get(f"{PREFIX}/support-agent/versions/9").status_code == 502
    assert client.get(f"{PREFIX}/unknown/versions").status_code == 404


def test_test_render_prompt_falls_back_when_mlflow_load_api_missing(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    _insert_agent(db_session, agent_id="support-agent", experiment_id="exp-support")

    _install_mlflow(monkeypatch, include_load=False)
    response = client.post(
        f"{PREFIX}/support-agent/test-render",
        json={"variables": {"agent_name": "Support Agent"}},
    )
    assert response.status_code == 200
    assert "Your role is to assist users" in response.json()["data"]["original_template"]


def test_delete_prompt_admin_success(client: TestClient, monkeypatch) -> None:
    """An admin can permanently delete a prompt via the registry client."""
    deleted: list[str] = []

    class _FakeClient:
        def delete_prompt(self, name: str) -> None:
            deleted.append(name)

    monkeypatch.setattr(prompt_routes, "_build_mlflow_client", lambda: _FakeClient())

    response = client.delete(f"{PREFIX}/support-agent")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"deleted": "support-agent"}
    assert deleted == ["support-agent"]


def test_delete_prompt_forbidden_for_non_admin(client: TestClient) -> None:
    """Deleting a prompt requires the admin scope."""
    response = client.delete(
        f"{PREFIX}/support-agent",
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Ad-hoc prompt-test runs (durable run history)
# ---------------------------------------------------------------------------


def _test_run_body(**overrides: Any) -> dict[str, Any]:
    """A minimal POST body for ``/prompts/test-runs`` with two pass + one fail."""
    body: dict[str, Any] = {
        "agent_id": "support-agent",
        "prompt_name": "support-agent",
        "prompt_alias": "prod",
        "prompt_version": 3,
        "model": "gpt-4o-mini",
        "eval_dataset_id": None,
        "results": [
            {
                "testCaseId": "tc-1",
                "input": "hello",
                "expectedBehavior": "greet",
                "actualResponse": "hi there",
                "verdict": "pass",
                "score": 1.0,
                "reasoning": "greeted",
            },
            {
                "testCaseId": "tc-2",
                "input": "refund?",
                "expectedBehavior": "explain policy",
                "actualResponse": "see policy",
                "verdict": "pass",
                "score": 0.8,
                "reasoning": "ok",
            },
            {
                "testCaseId": "tc-3",
                "input": "boom",
                "expectedBehavior": "stay safe",
                "actualResponse": "",
                "verdict": "fail",
                "score": 0.0,
                "reasoning": "errored",
            },
        ],
    }
    body.update(overrides)
    return body


def test_create_list_and_get_prompt_test_run_roundtrip(
    client: TestClient,
    db_session: Session,
) -> None:
    """POST a run → list shows summary (no results) → detail returns per-case data."""
    create = client.post(f"{PREFIX}/test-runs", json=_test_run_body())
    assert create.status_code == 201
    saved = create.json()["data"]
    test_run_id = saved["test_run_id"]
    assert test_run_id.startswith("PTR-")
    # Server-recomputed aggregates (2 pass + 1 fail, mean of 1.0/0.8/0.0).
    assert saved["test_set_size"] == 3
    assert saved["passed_count"] == 2
    assert saved["failed_count"] == 1
    assert saved["partial_count"] == 0
    assert saved["overall_score"] == pytest.approx(0.6)
    # The summary must NOT carry the heavy per-case array.
    assert "results" not in saved
    # The default ``client`` fixture authenticates as ``@test``.
    assert saved["created_by"] == "@test"

    # The row was persisted with the per-case payload.
    row = db_session.get(CaliberPromptTestRun, test_run_id)
    assert row is not None
    assert len(row.results) == 3
    assert row.completed_at is not None

    # List returns the summary, newest-first, without ``results``.
    listing = client.get(f"{PREFIX}/test-runs", params={"agent_id": "support-agent"})
    assert listing.status_code == 200
    rows = listing.json()["data"]
    assert len(rows) == 1
    assert rows[0]["test_run_id"] == test_run_id
    assert "results" not in rows[0]

    # Detail returns the full per-case array.
    detail = client.get(f"{PREFIX}/test-runs/{test_run_id}")
    assert detail.status_code == 200
    detail_data = detail.json()["data"]
    assert len(detail_data["results"]) == 3
    assert detail_data["results"][0]["testCaseId"] == "tc-1"
    assert detail_data["results"][2]["verdict"] == "fail"


def test_create_prompt_test_run_recomputes_partials_and_ignores_client_aggregates(
    client: TestClient,
) -> None:
    """The server computes counts/score from ``results`` and rejects stray aggregates.

    We can't even send aggregates (``extra='forbid'``), so sending one is a 400 —
    that, plus the recompute below, proves the client can't desync the summary.
    """
    # Stray client aggregate is rejected outright (schema forbids extras).
    rejected = client.post(
        f"{PREFIX}/test-runs",
        json=_test_run_body(passed_count=999, overall_score=0.99),
    )
    assert rejected.status_code == 400

    # A run with a partial verdict; assert the server-side recompute.
    body = _test_run_body(
        results=[
            {
                "testCaseId": "a",
                "input": "i",
                "expectedBehavior": "e",
                "actualResponse": "r",
                "verdict": "partial",
                "score": 0.5,
                "reasoning": "meh",
            },
            {
                "testCaseId": "b",
                "input": "i",
                "expectedBehavior": "e",
                "actualResponse": "r",
                "verdict": "pass",
                "score": 1.0,
                "reasoning": "good",
            },
        ]
    )
    created = client.post(f"{PREFIX}/test-runs", json=body)
    assert created.status_code == 201
    data = created.json()["data"]
    assert data["test_set_size"] == 2
    assert data["passed_count"] == 1
    assert data["partial_count"] == 1
    assert data["failed_count"] == 0
    assert data["overall_score"] == pytest.approx(0.75)


def test_create_prompt_test_run_rejects_empty_results(client: TestClient) -> None:
    """An empty per-case array is a 400 (nothing durable to persist)."""
    response = client.post(f"{PREFIX}/test-runs", json=_test_run_body(results=[]))
    assert response.status_code == 400


def test_create_prompt_test_run_rejects_invalid_verdict_and_score(
    client: TestClient,
) -> None:
    """Out-of-vocabulary verdict and out-of-range score are rejected (schema)."""
    bad_verdict = client.post(
        f"{PREFIX}/test-runs",
        json=_test_run_body(
            results=[
                {
                    "testCaseId": "a",
                    "input": "i",
                    "expectedBehavior": "e",
                    "actualResponse": "r",
                    "verdict": "maybe",
                    "score": 0.5,
                    "reasoning": "",
                }
            ]
        ),
    )
    assert bad_verdict.status_code == 400

    bad_score = client.post(
        f"{PREFIX}/test-runs",
        json=_test_run_body(
            results=[
                {
                    "testCaseId": "a",
                    "input": "i",
                    "expectedBehavior": "e",
                    "actualResponse": "r",
                    "verdict": "pass",
                    "score": 1.5,
                    "reasoning": "",
                }
            ]
        ),
    )
    assert bad_score.status_code == 400


def test_get_prompt_test_run_unknown_id_returns_404(client: TestClient) -> None:
    response = client.get(f"{PREFIX}/test-runs/PTR-deadbeef")
    assert response.status_code == 404


def test_list_prompt_test_runs_filter_order_and_limit(
    client: TestClient,
) -> None:
    """agent_id filter, newest-first ordering, default limit, and the 100 cap."""
    # Two runs for support-agent, one for billing-agent.
    first = client.post(f"{PREFIX}/test-runs", json=_test_run_body())
    second = client.post(f"{PREFIX}/test-runs", json=_test_run_body())
    other = client.post(f"{PREFIX}/test-runs", json=_test_run_body(agent_id="billing-agent"))
    assert first.status_code == second.status_code == other.status_code == 201

    # Filtered to support-agent → exactly the two support runs, newest first.
    support = client.get(f"{PREFIX}/test-runs", params={"agent_id": "support-agent"}).json()["data"]
    assert [r["test_run_id"] for r in support] == [
        second.json()["data"]["test_run_id"],
        first.json()["data"]["test_run_id"],
    ]
    assert all(r["agent_id"] == "support-agent" for r in support)

    # No agent_id → all three runs.
    everything = client.get(f"{PREFIX}/test-runs").json()["data"]
    assert len(everything) == 3

    # limit honored.
    limited = client.get(f"{PREFIX}/test-runs", params={"limit": "1"}).json()["data"]
    assert len(limited) == 1

    # Over-cap and invalid limit handling.
    capped = client.get(f"{PREFIX}/test-runs", params={"limit": "9999"})
    assert capped.status_code == 200  # silently capped at 100, not an error
    bad_limit = client.get(f"{PREFIX}/test-runs", params={"limit": "0"})
    assert bad_limit.status_code == 400


def test_create_prompt_test_run_requires_operator_scope(client: TestClient) -> None:
    """Persisting a run is an operator write."""
    response = client.post(
        f"{PREFIX}/test-runs",
        json=_test_run_body(),
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Auto-provisioned hidden prompt targets ("pytest for prompts")
# ---------------------------------------------------------------------------


def _select_targets(session: Session) -> list[CaliberAgentConfig]:
    return list(session.execute(select(CaliberAgentConfig)).scalars().all())


def test_create_prompt_auto_provisions_one_hidden_target(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    """Creating a prompt makes exactly ONE hidden target (agent_id == name)."""
    _install_mlflow(monkeypatch)

    created = client.post(
        PREFIX, json={"name": "checkout-prompt", "template": "Help {{user}}", "model": "gpt-4o"}
    )
    assert created.status_code == 201

    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "checkout-prompt")
    assert target is not None
    assert target.agent_id == "checkout-prompt"
    assert target.optimizer_config["source_type"] == "prompt_target"
    assert target.optimizer_config["model"] == "gpt-4o"
    assert target.experiment_id.startswith("prompt-target-")

    # A second create of the same prompt must NOT duplicate the target.
    again = client.post(PREFIX, json={"name": "checkout-prompt", "template": "Help {{user}} more"})
    assert again.status_code == 201
    db_session.expire_all()
    matches = [t for t in _select_targets(db_session) if t.agent_id == "checkout-prompt"]
    assert len(matches) == 1


def test_prompt_test_run_auto_provisions_target(
    client: TestClient,
    db_session: Session,
) -> None:
    """A test run with NO pre-existing agent provisions the target (no 404)."""
    response = client.post(f"{PREFIX}/test-runs", json=_test_run_body(agent_id="brand-new"))
    assert response.status_code == 201

    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "brand-new")
    assert target is not None
    assert target.optimizer_config["source_type"] == "prompt_target"


def test_prompt_optimization_run_auto_provisions_without_preexisting_agent(
    client: TestClient,
    db_session: Session,
) -> None:
    """Calibration auto-provisions instead of demanding a pre-existing agent."""
    _insert_eval_dataset(db_session, "EDS-AUTO")

    response = client.post(
        f"{PREFIX}/optimization/runs",
        json={
            "agent_id": "never-registered",
            "eval_dataset_id": "EDS-AUTO",
            "optimizer_type": "MetaPrompt",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
    )
    assert response.status_code == 201

    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "never-registered")
    assert target is not None
    assert target.optimizer_config["source_type"] == "prompt_target"
    # The calibration dataset is recorded on the target for the "Has test set" signal.
    assert target.optimizer_config["dataset_id"] == "EDS-AUTO"


def test_hidden_target_absent_from_agents_and_prompt_backlog(
    client: TestClient,
    db_session: Session,
) -> None:
    """A hidden target appears neither in GET /agents nor as a needs_prompt row."""
    # Provision a hidden target via a test run.
    assert (
        client.post(f"{PREFIX}/test-runs", json=_test_run_body(agent_id="ghost")).status_code == 201
    )

    agents = client.get("/ajax-api/2.0/mlflow/caliber/agents").json()["data"]
    assert all(a["agent_id"] != "ghost" for a in agents)

    prompts = client.get(PREFIX).json()["data"]
    assert all(p["agent_id"] != "ghost" for p in prompts)


def test_prompt_workspace_status_lifecycle_and_bind(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    """Draft → Tested (test run) → Bound (bind), with model/version/last_run surfaced."""
    _install_mlflow(monkeypatch)

    # Author the prompt (auto-provisions the hidden target, pins model).
    created = client.post(
        PREFIX, json={"name": "ws-prompt", "template": "Answer {{q}}", "model": "claude-3"}
    )
    assert created.status_code == 201

    # Draft: target exists, no test run, no bind.
    ws = client.get(f"{PREFIX}/ws-prompt/workspace")
    assert ws.status_code == 200
    data = ws.json()["data"]
    assert data["status"] == "Draft"
    assert data["model"] == "claude-3"
    assert data["bound_to"] is None
    assert data["last_run"] is None

    # Tested: record a test run for this prompt.
    run = client.post(
        f"{PREFIX}/test-runs", json=_test_run_body(agent_id="ws-prompt", prompt_name="ws-prompt")
    )
    assert run.status_code == 201
    ws_tested = client.get(f"{PREFIX}/ws-prompt/workspace").json()["data"]
    assert ws_tested["status"] == "Tested"
    assert ws_tested["last_run"] is not None
    assert ws_tested["last_run"]["test_set_size"] == 3
    assert ws_tested["last_run"]["passed_count"] == 2

    # Bound: bind takes precedence over Tested.
    bind = client.post(f"{PREFIX}/ws-prompt/bind", json={"kind": "standalone"})
    assert bind.status_code == 200
    assert bind.json()["data"]["bound_to"] == {"kind": "standalone"}
    assert bind.json()["data"]["status"] == "Bound"
    ws_bound = client.get(f"{PREFIX}/ws-prompt/workspace").json()["data"]
    assert ws_bound["status"] == "Bound"
    assert ws_bound["bound_to"] == {"kind": "standalone"}


def test_prompt_workspace_status_calibrated_when_job_applied(
    client: TestClient,
    db_session: Session,
) -> None:
    """An ``applied`` (terminal-success) refinement job → Calibrated (over Tested)."""
    from caliber.prompt_targets import ensure_prompt_target

    # Provision the hidden target and seed a verified item + applied job for it.
    ensure_prompt_target(db_session, "cal-prompt", owner="@test")
    item = CaliberVerificationItem(
        item_id="FB-cal01",
        agent_id="cal-prompt",
        category="prompt_optimization",
        free_text="x",
        severity="standard",
        status="verified",
    )
    db_session.add(item)
    db_session.flush()
    db_session.add(
        CaliberRefinementJob(
            job_id="RFN-cal01",
            agent_id="cal-prompt",
            primary_item_id=item.item_id,
            artifact_type="prompt",
            status="applied",
            current_stage="done",
        )
    )
    # Also record a test run, to prove Calibrated outranks Tested.
    db_session.commit()
    assert (
        client.post(f"{PREFIX}/test-runs", json=_test_run_body(agent_id="cal-prompt")).status_code
        == 201
    )

    ws = client.get(f"{PREFIX}/cal-prompt/workspace").json()["data"]
    assert ws["status"] == "Calibrated"


def test_prompt_bind_agent_links_real_agent(
    client: TestClient,
    db_session: Session,
) -> None:
    """Binding kind=agent records bound_to and points the real agent at the prompt."""
    _insert_agent(db_session, agent_id="real-agent", experiment_id="exp-real")

    bind = client.post(
        f"{PREFIX}/bound-prompt/bind",
        json={"kind": "agent", "agent_id": "real-agent"},
    )
    assert bind.status_code == 200
    assert bind.json()["data"]["bound_to"] == {"kind": "agent", "agent_id": "real-agent"}

    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "bound-prompt")
    assert target is not None
    assert target.optimizer_config["bound_to"] == {"kind": "agent", "agent_id": "real-agent"}
    agent = db_session.get(CaliberAgentConfig, "real-agent")
    assert agent is not None
    assert agent.optimizer_config["prompt"] == "bound-prompt"


def test_prompt_bind_rejects_invalid_kind_and_missing_ids(client: TestClient) -> None:
    """Invalid kind / missing required ids → 400."""
    bad_kind = client.post(f"{PREFIX}/p/bind", json={"kind": "nonsense"})
    assert bad_kind.status_code == 400

    missing_agent_id = client.post(f"{PREFIX}/p/bind", json={"kind": "agent"})
    assert missing_agent_id.status_code == 400

    missing_node = client.post(
        f"{PREFIX}/p/bind", json={"kind": "workflow_node", "workflow_id": "WF-1"}
    )
    assert missing_node.status_code == 400


def test_prompt_bind_unknown_agent_returns_404(client: TestClient) -> None:
    """kind=agent pointing at a non-existent agent is a 404."""
    response = client.post(f"{PREFIX}/p/bind", json={"kind": "agent", "agent_id": "no-such-agent"})
    assert response.status_code == 404


def test_prompt_bind_requires_operator_scope(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/p/bind",
        json={"kind": "standalone"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert response.status_code == 403


def test_prompt_set_baseline_reflected_in_workspace(
    client: TestClient,
    db_session: Session,
) -> None:
    """Pinning a run as baseline records it and the workspace surfaces it."""
    run = client.post(
        f"{PREFIX}/test-runs", json=_test_run_body(agent_id="bl-prompt", prompt_name="bl-prompt")
    )
    assert run.status_code == 201
    test_run_id = run.json()["data"]["test_run_id"]

    # No baseline yet.
    ws = client.get(f"{PREFIX}/bl-prompt/workspace").json()["data"]
    assert ws["baseline_run_id"] is None
    assert ws["baseline_run"] is None

    set_baseline = client.post(f"{PREFIX}/bl-prompt/baseline", json={"test_run_id": test_run_id})
    assert set_baseline.status_code == 200
    assert set_baseline.json()["data"]["baseline_run_id"] == test_run_id

    # The id is recorded on the hidden target's optimizer_config.
    db_session.expire_all()
    target = db_session.get(CaliberAgentConfig, "bl-prompt")
    assert target is not None
    assert target.optimizer_config["baseline_run_id"] == test_run_id

    # The workspace reflects the baseline plus a cheap summary.
    ws_after = client.get(f"{PREFIX}/bl-prompt/workspace").json()["data"]
    assert ws_after["baseline_run_id"] == test_run_id
    assert ws_after["baseline_run"] is not None
    assert ws_after["baseline_run"]["test_run_id"] == test_run_id
    assert ws_after["baseline_run"]["test_set_size"] == 3
    assert ws_after["baseline_run"]["passed_count"] == 2


def test_prompt_set_baseline_wrong_prompt_returns_400(
    client: TestClient,
    db_session: Session,
) -> None:
    """A run that belongs to a different prompt cannot be its baseline (400)."""
    run = client.post(
        f"{PREFIX}/test-runs", json=_test_run_body(agent_id="owner-prompt", prompt_name="owner")
    )
    assert run.status_code == 201
    test_run_id = run.json()["data"]["test_run_id"]

    # Try to pin owner-prompt's run as the baseline of a different prompt.
    response = client.post(f"{PREFIX}/other-prompt/baseline", json={"test_run_id": test_run_id})
    assert response.status_code == 400


def test_prompt_set_baseline_missing_run_returns_404(client: TestClient) -> None:
    """An unknown run id → 404."""
    response = client.post(f"{PREFIX}/p/baseline", json={"test_run_id": "PTR-deadbeef"})
    assert response.status_code == 404


def test_prompt_set_baseline_requires_operator_scope(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/p/baseline",
        json={"test_run_id": "PTR-x"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert response.status_code == 403


def test_list_prompts_includes_status_and_model(
    client: TestClient,
    db_session: Session,
    monkeypatch,
) -> None:
    """Prompt list rows carry the computed status + model badge fields.

    The hidden prompt target supplies the model + status for the prompt row even
    though the target itself is filtered out of the backlog: the row is sourced
    from the MLflow registry, and its status/model are read off the hidden
    target keyed on the same name.
    """
    _install_mlflow(
        monkeypatch,
        search_items=[
            SimpleNamespace(
                name="badge-prompt", description="badge", creation_timestamp=1, tags={}
            ),
        ],
        load_refs={
            "prompts:/badge-prompt@prod": SimpleNamespace(
                name="badge-prompt", version=2, template="Hi {{x}}", tags={}
            ),
        },
    )
    created = client.post(
        PREFIX, json={"name": "badge-prompt", "template": "Hi {{x}}", "model": "gpt-4o-mini"}
    )
    assert created.status_code == 201

    prompts = client.get(PREFIX).json()["data"]
    badge = next((p for p in prompts if p["agent_id"] == "badge-prompt"), None)
    assert badge is not None
    assert badge["status"] == "Draft"
    assert badge["model"] == "gpt-4o-mini"
    # The hidden target is NOT emitted as its own separate backlog row.
    assert sum(1 for p in prompts if p["agent_id"] == "badge-prompt") == 1
