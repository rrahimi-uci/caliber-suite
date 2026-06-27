"""Integration tests for ``/caliber/llm-pricing`` + the cost-engine override.

Covers the CRUD resource (scoping, audit, scope gates) and the wiring that makes
a DB pricing row override CALIBER's built-in ``DEFAULT_MODEL_PRICING`` for cost
attribution.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog, CaliberLlmModelPricing
from caliber.observability import mlflow_tracing as mt
from caliber.routes.llm_pricing import DETAIL_PATH, LIST_PATH

_GOOD = {
    "provider": "openai",
    "model_id": "gpt-4o",
    "prompt_price": 0.0025,
    "completion_price": 0.01,
    "cached_prompt_price": 0.00125,
    "tags": ["openai"],
}


# --- CRUD --------------------------------------------------------------------


def test_create_pricing_happy_path(client: TestClient) -> None:
    resp = client.post(LIST_PATH, json=_GOOD)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["pricing_id"].startswith("LPRC-")
    assert data["provider"] == "openai" and data["model_id"] == "gpt-4o"
    assert data["prompt_price"] == 0.0025
    assert data["status"] == "active"


def test_create_duplicate_provider_model_conflicts(client: TestClient) -> None:
    assert client.post(LIST_PATH, json=_GOOD).status_code == 201
    dup = client.post(LIST_PATH, json=_GOOD)
    assert dup.status_code == 409, dup.text


def test_create_requires_operator(client: TestClient) -> None:
    resp = client.post(LIST_PATH, json=_GOOD, headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


def test_list_filters_by_status(client: TestClient, db_session: Session) -> None:
    db_session.add(
        CaliberLlmModelPricing(
            pricing_id="LPRC-arch",
            provider="anthropic",
            model_id="claude-3-5-haiku",
            prompt_price=0.0008,
            completion_price=0.004,
            owner="@test",
            tags=[],
            status="archived",
        )
    )
    db_session.commit()
    client.post(LIST_PATH, json=_GOOD)  # one active
    active = client.get(LIST_PATH).json()["data"]
    assert {r["model_id"] for r in active} == {"gpt-4o"}
    every = client.get(f"{LIST_PATH}?status=all").json()["data"]
    assert {"gpt-4o", "claude-3-5-haiku"} <= {r["model_id"] for r in every}


def test_get_404(client: TestClient) -> None:
    assert client.get(DETAIL_PATH.replace("{pricing_id}", "LPRC-missing")).status_code == 404


def test_get_scoped_to_visibility(client: TestClient, db_session: Session) -> None:
    # A project-scoped row owned by @other is invisible to a non-admin @viewer,
    # visible to admin (@test).
    db_session.add(
        CaliberLlmModelPricing(
            pricing_id="LPRC-scoped",
            provider="openai",
            model_id="gpt-4o-mini",
            prompt_price=0.00015,
            completion_price=0.0006,
            owner="@other",
            project_id="PRJ-x",
            visibility="project",
            tags=[],
            status="active",
        )
    )
    db_session.commit()
    path = DETAIL_PATH.replace("{pricing_id}", "LPRC-scoped")
    assert client.get(path).status_code == 200  # admin
    assert client.get(path, headers={"X-CALIBER-User": "@viewer"}).status_code == 404


def test_update_admin_and_audits(client: TestClient, db_session: Session) -> None:
    pricing_id = client.post(LIST_PATH, json=_GOOD).json()["data"]["pricing_id"]
    path = DETAIL_PATH.replace("{pricing_id}", pricing_id)
    resp = client.patch(path, json={"prompt_price": 0.009, "status": "archived"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["prompt_price"] == 0.009
    assert resp.json()["data"]["status"] == "archived"
    row = (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "update_llm_pricing")
        )
        .scalars()
        .first()
    )
    assert row is not None and row.entity_id == pricing_id


def test_update_requires_admin(client: TestClient) -> None:
    pricing_id = client.post(LIST_PATH, json=_GOOD).json()["data"]["pricing_id"]
    path = DETAIL_PATH.replace("{pricing_id}", pricing_id)
    resp = client.patch(path, json={"prompt_price": 0.5}, headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


# --- Cost-engine override -----------------------------------------------------
# (the autouse _reset_pricing_source fixture in conftest clears the global
# pricing source + cache after each test, so these never leak forward)


def test_resolve_model_pricing_merges_over_defaults(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        session.add(
            CaliberLlmModelPricing(
                pricing_id="LPRC-o1",
                provider="openai",
                model_id="gpt-4o",
                prompt_price=9.9,
                completion_price=9.9,
                owner="@test",
                tags=[],
                status="active",
            )
        )
        session.commit()
    table = mt.resolve_model_pricing(session_factory)
    assert table["gpt-4o"] == (9.9, 9.9)  # DB row overrides the built-in gpt-4o rate
    assert "claude-3-5-sonnet" in table  # defaults still present


def test_db_pricing_overrides_default_cost(session_factory: sessionmaker[Session]) -> None:
    # An unknown model costs 0 against the defaults (never guessed).
    assert mt.model_cost_usd("acme-llm-1", prompt_tokens=1000, completion_tokens=1000) == 0.0
    with session_factory() as session:
        session.add(
            CaliberLlmModelPricing(
                pricing_id="LPRC-acme",
                provider="acme",
                model_id="acme-llm-1",
                prompt_price=1.0,
                completion_price=2.0,
                owner="@test",
                tags=[],
                status="active",
            )
        )
        session.commit()
    mt.register_pricing_source(session_factory)
    mt.invalidate_pricing_cache()
    # 1000 prompt @ $1.0/1K + 1000 completion @ $2.0/1K = 1.0 + 2.0 = 3.0
    assert mt.model_cost_usd("acme-llm-1", prompt_tokens=1000, completion_tokens=1000) == 3.0
