"""Route tests for /caliber/gate-verdicts/{artifact_type}/{version_key}."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber import gate_verdicts
from caliber.db.models import CaliberGateVerdict

BASE = "/ajax-api/2.0/mlflow/caliber/gate-verdicts"


def test_get_verdict_returns_none_state_when_absent(client: TestClient) -> None:
    """A version with no recorded verdict reads as ``state: none`` (honest, not faked)."""
    resp = client.get(f"{BASE}/prompt/7")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"state": "none"}


def test_post_then_get_roundtrips_full_verdict(client: TestClient) -> None:
    resp = client.post(
        f"{BASE}/prompt/7",
        json={
            "state": "fail",
            "score": 0.91,
            "baseline_score": 0.89,
            "min_aggregate_score": 0.85,
            "worst_regression": 0.04,
            "max_regression_delta": 0.02,
            "eval_run_id": "ER-1",
        },
    )
    assert resp.status_code == 200, resp.text
    got = client.get(f"{BASE}/prompt/7").json()["data"]
    # Aggregate clears 0.85 but a dimension regressed past 0.02 -> authoritative FAIL.
    assert got["state"] == "fail"
    assert got["score"] == 0.91
    assert got["worst_regression"] == 0.04
    assert got["max_regression_delta"] == 0.02
    assert got["eval_run_id"] == "ER-1"


def test_post_upserts_latest_verdict(client: TestClient) -> None:
    client.post(f"{BASE}/prompt/9", json={"state": "fail", "score": 0.7})
    client.post(f"{BASE}/prompt/9", json={"state": "pass", "score": 0.95})
    got = client.get(f"{BASE}/prompt/9").json()["data"]
    assert got["state"] == "pass"
    assert got["score"] == 0.95


def test_post_rejects_unknown_state(client: TestClient) -> None:
    resp = client.post(f"{BASE}/prompt/7", json={"state": "great"})
    assert resp.status_code == 400


def test_rejects_unsupported_artifact_type(client: TestClient) -> None:
    assert client.get(f"{BASE}/judge/7").status_code == 400
    assert client.post(f"{BASE}/judge/7", json={"state": "pass"}).status_code == 400


def test_post_rejects_non_numeric_score(client: TestClient) -> None:
    resp = client.post(f"{BASE}/prompt/7", json={"state": "pass", "score": "high"})
    assert resp.status_code == 400


def test_post_requires_operator_scope(client: TestClient) -> None:
    resp = client.post(
        f"{BASE}/prompt/7",
        json={"state": "pass"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


def test_record_gate_verdict_recovers_from_concurrent_insert_race(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two evaluators racing the same version must not 500 on the unique constraint.

    Both concurrent writers read ``None`` from the pre-insert existence check and
    race to INSERT the same ``(artifact_type, version_key)``. The loser hits
    ``uq_gate_verdict_artifact_version``; the savepoint fallback must recover by
    updating the winner's row instead of failing the caller's transaction — and
    exactly one row must persist. We simulate the race deterministically by
    forcing the first existence check to miss a row that is already committed.
    """
    with session_factory() as session:
        gate_verdicts.record_gate_verdict(
            session,
            artifact_type="prompt",
            version_key="AG-race@prod#1",
            state="pass",
            score=0.9,
        )
        session.commit()

    real_get = gate_verdicts.get_gate_verdict
    calls = {"n": 0}

    def racing_miss(session: Session, artifact_type: str, version_key: str):
        # First lookup misses (as a concurrent inserter would); later lookups
        # (the fallback) see the committed row.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_get(session, artifact_type, version_key)

    monkeypatch.setattr(gate_verdicts, "get_gate_verdict", racing_miss)

    with session_factory() as session:
        row = gate_verdicts.record_gate_verdict(
            session,
            artifact_type="prompt",
            version_key="AG-race@prod#1",
            state="fail",
            score=0.1,
        )
        session.commit()
        assert row.state == "fail"
        assert row.score == 0.1

    with session_factory() as session:
        count = session.execute(
            select(func.count())
            .select_from(CaliberGateVerdict)
            .where(CaliberGateVerdict.version_key == "AG-race@prod#1")
        ).scalar_one()
        assert count == 1
        persisted = real_get(session, "prompt", "AG-race@prod#1")
        assert persisted is not None
        assert persisted.state == "fail"
