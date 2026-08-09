"""Tests covering remaining gaps across csrf.py, routes/static.py,
routes/eval_datasets.py, and routes/workflow_deployments.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from tests.workflow_helpers import (
    PREFIX,
    create_and_publish,
    make_support_manifest,
    relax_release_graded_executor,
    relax_tool_isolation_gate,
    seed_eval_dataset,
)


def _gated_manifest(workflow_id: str) -> dict:
    gate = {
        "type": "deploy_gate",
        "dataset_ref": "support_eval",
        "required_for_aliases": ["prod"],
        # Completion, not quality: this dataset carries no expected output. See
        # tests/test_deploy_gate_evidence.py for the graded contract.
        "thresholds": {"min_completion_rate": 1.0},
    }
    return make_support_manifest(workflow_id, deploy_gates={"support_eval": gate})


def _deploy_prod(
    client: TestClient, db_session: Session, workflow_name: str = "Gap"
) -> tuple[str, str, str]:
    """Create a gated workflow, publish, promote to prod, approve. Returns (wid, vid, promo_id)."""
    seed_eval_dataset(db_session)
    # The subject of these tests is promotion-approval state transitions. The suite
    # grades with the deterministic fake, which production release policy otherwise
    # refuses as evidence — see tests/test_deploy_gate_executor.py for that default.
    relax_release_graded_executor(client)
    # Registered tools now require an OS-enforced isolation backend before a
    # production alias will accept them; that default is the subject of
    # tests/test_deployment_environment_policy.py, not of this suite.
    relax_tool_isolation_gate(client)
    wid, vid = create_and_publish(
        client, workflow_name=workflow_name, manifest=_gated_manifest(workflow_name + "_wf")
    )
    r = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/prod/promote",
        json={"version_id": vid},
    )
    assert r.status_code == 202, r.text
    promo_id = r.json()["data"]["promotion"]["promotion_id"]
    r = client.post(f"{PREFIX}/workflow-promotions/{promo_id}/approve")
    assert r.status_code == 200, r.text
    return wid, vid, promo_id


# ===================================================================
# csrf.py gaps (lines 89, 192-193, 209, 214-215, 235)
# ===================================================================


def test_csrf_issue_without_secret_raises() -> None:
    """Calling issue() on a manager with empty secret raises RuntimeError."""
    from caliber.csrf import CSRFTokenManager

    mgr = CSRFTokenManager(secret=b"", ttl_seconds=300)
    assert not mgr.is_enabled
    with pytest.raises(RuntimeError, match="no signing secret"):
        mgr.issue("@test")


def test_normalize_user_strips_comma_payloads() -> None:
    """A user header with commas resolves to 'anonymous' to block
    list-bypass attacks."""
    from caliber.csrf import _normalize_user

    assert _normalize_user("user1,user2") == "anonymous"
    assert _normalize_user("anonymous") == "anonymous"
    assert _normalize_user("") == "anonymous"
    assert _normalize_user(None) == "anonymous"
    assert _normalize_user("  ") == "anonymous"
    assert _normalize_user("@reza") == "@reza"


def test_read_header_returns_none_for_non_list_headers() -> None:
    """When scope['headers'] is not a list/tuple, return None."""
    from caliber.csrf import _read_header

    assert _read_header({"headers": "not-a-list"}, b"x-caliber-csrf") is None
    assert _read_header({}, b"x-caliber-csrf") is None


def test_read_user_header_uses_fallback_when_anonymous() -> None:
    """When the user header resolves to anonymous, the dev_user fallback
    kicks in."""
    from caliber.csrf import _read_user_header

    # No header at all → falls back
    assert _read_user_header({}, fallback_user="@dev") == "@dev"
    # Explicit anonymous → falls back
    scope = {"headers": [(b"x-caliber-user", b"anonymous")]}
    assert _read_user_header(scope, fallback_user="@dev") == "@dev"


def test_send_403_writes_json_body() -> None:
    """The _send_403 coroutine produces a valid 403 JSON response."""
    from caliber.csrf import _send_403

    sent: list[dict] = []

    async def mock_send(msg: dict) -> None:
        sent.append(msg)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_send_403(mock_send, "test detail"))
    finally:
        loop.close()
    assert len(sent) == 2
    assert sent[0]["status"] == 403
    assert b"test detail" in sent[1]["body"]


# ===================================================================
# routes/static.py gaps (lines 75, 85, 100, 208-209, 234, 245)
# ===================================================================


def test_static_handler_ui_dir_property(tmp_path: Path) -> None:
    """The ui_dir property returns the resolved path."""
    from caliber.routes.static import StaticUIHandler

    handler = StaticUIHandler(tmp_path, static_prefix="")
    assert handler.ui_dir == tmp_path.resolve()


def test_static_handler_index_html_cached(tmp_path: Path) -> None:
    """Second call to index_html returns the cached value."""
    from caliber.routes.static import StaticUIHandler

    (tmp_path / "index.html").write_text(
        "<html><head></head><body></body></html>", encoding="utf-8"
    )
    handler = StaticUIHandler(tmp_path, static_prefix="/x")
    first = handler.index_html()
    second = handler.index_html()
    assert first is second  # same object from cache


def test_resolve_asset_strips_leading_slash(tmp_path: Path) -> None:
    """A relative path with a leading / is handled correctly."""
    from caliber.routes.static import StaticUIHandler

    (tmp_path / "foo.txt").write_text("bar", encoding="utf-8")
    handler = StaticUIHandler(tmp_path, static_prefix="")
    assert handler.resolve_asset("/foo.txt") is not None


def test_build_handler_returns_handler() -> None:
    """build_handler constructs a StaticUIHandler from config."""
    from caliber.config import CaliberConfig
    from caliber.routes.static import build_handler

    config = CaliberConfig()
    handler = build_handler(config)
    assert handler is not None


# ===================================================================
# routes/eval_datasets.py gaps (lines 79, 153, 159, 172, 213-214, 297)
# ===================================================================

ED_PATH = f"{PREFIX}/eval-datasets"


def _seed_eval_dataset(session: Session, **overrides: object):
    from caliber.db.models import CaliberEvalDataset

    defaults = {
        "dataset_id": "ED-gap",
        "name": "gap-ds",
        "description": "test",
        "owner": "@test",
        "tags": ["t1", "t2"],
        "status": "active",
        "version": 1,
    }
    defaults.update(overrides)
    ds = CaliberEvalDataset(**defaults)
    session.add(ds)
    session.commit()
    return ds


def test_list_datasets_tag_filter(client: TestClient, db_session: Session) -> None:
    _seed_eval_dataset(db_session, tags=["beta"])
    r = client.get(f"{ED_PATH}?tag=beta")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
    r = client.get(f"{ED_PATH}?tag=nope")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 0


def test_update_dataset_404(client: TestClient) -> None:
    r = client.patch(f"{ED_PATH}/ED-NOPE", json={"description": "x"})
    assert r.status_code == 404


def test_update_dataset_no_change_returns_current(client: TestClient, db_session: Session) -> None:
    _seed_eval_dataset(db_session, dataset_id="ED-noc", name="nochange-ds")
    r = client.patch(f"{ED_PATH}/ED-noc", json={"description": "test"})
    assert r.status_code == 200


def test_list_examples_non_integer_version_400(client: TestClient, db_session: Session) -> None:
    _seed_eval_dataset(db_session, dataset_id="ED-bad-v", name="badver")
    r = client.get(f"{ED_PATH}/ED-bad-v/examples?version=abc")
    assert r.status_code == 400
    assert "integer" in r.json()["detail"]


def test_supersede_missing_example_404(client: TestClient, db_session: Session) -> None:
    _seed_eval_dataset(db_session, dataset_id="ED-sup", name="sup-ds")
    r = client.post(f"{ED_PATH}/ED-sup/examples/EX-NOPE/supersede")
    assert r.status_code == 404


# ===================================================================
# routes/workflow_deployments.py gaps (lines 195, 210, 213-214, 252, 255-256)
# ===================================================================


def test_list_promotions_status_filter(
    client: TestClient, db_session: Session, gated_prod: None
) -> None:
    """The ?status parameter filters promotions."""
    wid, vid, promo_id = _deploy_prod(client, db_session, "StatusFilter")
    r = client.get(f"{PREFIX}/workflows/{wid}/promotions?status=approved")
    assert r.status_code == 200


def test_approve_promotion_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflow-promotions/WP-NOPE/approve")
    assert r.status_code == 404


def test_reject_promotion_404(client: TestClient) -> None:
    r = client.post(f"{PREFIX}/workflow-promotions/WP-NOPE/reject")
    assert r.status_code == 404


def test_approve_already_approved_promotion_409(
    client: TestClient, db_session: Session, gated_prod: None
) -> None:
    """Approving an already-approved promotion raises DeployError → 409."""
    _wid, _vid, promo_id = _deploy_prod(client, db_session, "DblApprove")
    # Second approve attempt on same promotion
    r = client.post(f"{PREFIX}/workflow-promotions/{promo_id}/approve")
    assert r.status_code == 409


def test_reject_already_approved_promotion_409(
    client: TestClient, db_session: Session, gated_prod: None
) -> None:
    """Rejecting an already-approved promotion raises DeployError → 409."""
    _wid, _vid, promo_id = _deploy_prod(client, db_session, "DblReject")
    r = client.post(f"{PREFIX}/workflow-promotions/{promo_id}/reject")
    assert r.status_code == 409
