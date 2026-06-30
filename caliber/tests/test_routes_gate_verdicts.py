"""Route tests for /caliber/gate-verdicts/{artifact_type}/{version_key}."""

from __future__ import annotations

from starlette.testclient import TestClient

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
