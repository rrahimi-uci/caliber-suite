"""Coverage tests for ``/caliber/review-queues`` — list, update, and the
add-item / submit error branches not exercised by ``test_routes_review_queues``.

Targets the previously-uncovered list route (status filter + invalid-status
400), the create-conflict 409, the whole PATCH update route (empty-body 400,
404, field write + recounted item/pending totals, viewer 403), the add-items
queue-missing 404, the submit queue/item 404s, and the default-writeback-client
resolution. The writeback boundary is injected via the existing
``app.state.review_writeback_client`` fake — no MLflow / network.
"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.testclient import TestClient

from caliber.review.writeback import (
    FakeReviewWriteBackClient,
    MLflowReviewWriteBackClient,
)
from caliber.routes.review_queues import (
    DETAIL_PATH,
    ITEMS_PATH,
    LIST_PATH,
    SUBMIT_PATH,
    _resolve_writeback_client,
)

_QUEUE = {
    "name": "answer-quality",
    "description": "Human review of answer quality.",
    "questions": [
        {"key": "correct", "title": "Is the answer correct?", "type": "pass_fail"},
        {
            "key": "gold",
            "title": "Expected answer",
            "type": "text",
            "target": "expectation",
            "required": False,
        },
    ],
    "reviewers": ["@sarah"],
}


def _create_queue(client: TestClient, name: str = "answer-quality") -> str:
    resp = client.post(LIST_PATH, json={**_QUEUE, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["queue_id"]


# --------------------------------------------------------------------------- #
# list_queues (lines 94-142)
# --------------------------------------------------------------------------- #


def test_list_queues_empty_returns_envelope(client: TestClient) -> None:
    resp = client.get(LIST_PATH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == []


def test_list_queues_reports_item_and_pending_counts(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add = client.post(
        ITEMS_PATH.replace("{queue_id}", queue_id),
        json={"trace_ids": ["tr-1", "tr-2"]},
    )
    item_id = add.json()["data"][0]["item_id"]
    # Complete one item so pending < total exercises the CASE-sum branch.
    client.app.state.review_writeback_client = FakeReviewWriteBackClient()
    try:
        done = client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": True}},
        )
        assert done.status_code == 200, done.text
    finally:
        client.app.state.review_writeback_client = None

    resp = client.get(LIST_PATH)
    assert resp.status_code == 200, resp.text
    rows = {row["queue_id"]: row for row in resp.json()["data"]}
    assert rows[queue_id]["item_count"] == 2
    assert rows[queue_id]["pending_count"] == 1


def test_list_queues_filters_by_status(client: TestClient) -> None:
    active_id = _create_queue(client, name="still-active")
    archived_id = _create_queue(client, name="to-archive")
    patched = client.patch(
        DETAIL_PATH.replace("{queue_id}", archived_id), json={"status": "archived"}
    )
    assert patched.status_code == 200, patched.text

    # Default status filter is "active" — the archived queue is excluded.
    default_ids = {row["queue_id"] for row in client.get(LIST_PATH).json()["data"]}
    assert active_id in default_ids
    assert archived_id not in default_ids

    archived_ids = {
        row["queue_id"] for row in client.get(f"{LIST_PATH}?status=archived").json()["data"]
    }
    assert archived_ids == {archived_id}

    all_ids = {row["queue_id"] for row in client.get(f"{LIST_PATH}?status=all").json()["data"]}
    assert {active_id, archived_id} <= all_ids


def test_list_queues_invalid_status_400(client: TestClient) -> None:
    resp = client.get(f"{LIST_PATH}?status=bogus")
    assert resp.status_code == 400
    assert "status" in resp.text


# --------------------------------------------------------------------------- #
# create_queue conflict (lines 163, 200-201)
# --------------------------------------------------------------------------- #


def test_create_queue_duplicate_name_is_409(client: TestClient) -> None:
    _create_queue(client, name="dupe")
    resp = client.post(LIST_PATH, json={**_QUEUE, "name": "dupe"})
    assert resp.status_code == 409
    assert "already in use" in resp.text


def test_create_queue_viewer_forbidden(client: TestClient) -> None:
    resp = client.post(LIST_PATH, json=_QUEUE, headers={"X-CALIBER-User": "@viewer"})
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# update_queue (lines 242-284)
# --------------------------------------------------------------------------- #


def test_update_queue_empty_body_400(client: TestClient) -> None:
    queue_id = _create_queue(client)
    resp = client.patch(DETAIL_PATH.replace("{queue_id}", queue_id), json={})
    assert resp.status_code == 400
    assert "at least one field" in resp.text


def test_update_queue_nonexistent_404(client: TestClient) -> None:
    resp = client.patch(
        DETAIL_PATH.replace("{queue_id}", "RVQ-missing"),
        json={"description": "x"},
    )
    assert resp.status_code == 404


def test_update_queue_changes_fields_and_recounts(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add = client.post(
        ITEMS_PATH.replace("{queue_id}", queue_id),
        json={"trace_ids": ["tr-1", "tr-2"]},
    )
    item_id = add.json()["data"][0]["item_id"]
    client.app.state.review_writeback_client = FakeReviewWriteBackClient()
    try:
        client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": True}},
        )
    finally:
        client.app.state.review_writeback_client = None

    resp = client.patch(
        DETAIL_PATH.replace("{queue_id}", queue_id),
        json={"description": "updated", "reviewers": ["@alex"], "status": "archived"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["description"] == "updated"
    assert data["reviewers"] == ["@alex"]
    assert data["status"] == "archived"
    # Counts are recomputed post-update: 2 total, 1 still pending.
    assert data["item_count"] == 2
    assert data["pending_count"] == 1


def test_update_queue_viewer_forbidden(client: TestClient) -> None:
    queue_id = _create_queue(client)
    resp = client.patch(
        DETAIL_PATH.replace("{queue_id}", queue_id),
        json={"description": "nope"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# add_items error branch (lines 303, 358-359)
# --------------------------------------------------------------------------- #


def test_add_items_to_missing_queue_404(client: TestClient) -> None:
    resp = client.post(
        ITEMS_PATH.replace("{queue_id}", "RVQ-missing"),
        json={"trace_ids": ["tr-1"]},
    )
    assert resp.status_code == 404
    assert "not found" in resp.text


def test_add_items_viewer_forbidden(client: TestClient) -> None:
    queue_id = _create_queue(client)
    resp = client.post(
        ITEMS_PATH.replace("{queue_id}", queue_id),
        json={"trace_ids": ["tr-1"]},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# submit_item not-found branches (lines 401, 404)
# --------------------------------------------------------------------------- #


def test_submit_item_missing_queue_404(client: TestClient) -> None:
    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", "RVQ-missing").replace("{item_id}", "RVI-x"),
        json={"answers": {}},
    )
    assert resp.status_code == 404
    assert "review queue" in resp.text


def test_submit_item_missing_item_404(client: TestClient) -> None:
    queue_id = _create_queue(client)
    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", "RVI-missing"),
        json={"answers": {}},
    )
    assert resp.status_code == 404
    assert "review item" in resp.text


def test_submit_item_belongs_to_other_queue_404(client: TestClient) -> None:
    queue_a = _create_queue(client, name="queue-a")
    queue_b = _create_queue(client, name="queue-b")
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_a), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    # The item exists, but under queue_a — submitting it under queue_b is a 404.
    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_b).replace("{item_id}", item_id),
        json={"answers": {"correct": True}},
    )
    assert resp.status_code == 404
    assert "review item" in resp.text


# --------------------------------------------------------------------------- #
# _resolve_writeback_client default (line 81)
# --------------------------------------------------------------------------- #


def test_resolve_writeback_client_defaults_to_mlflow_client() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    client = _resolve_writeback_client(request)  # type: ignore[arg-type]
    assert isinstance(client, MLflowReviewWriteBackClient)


def test_resolve_writeback_client_uses_override() -> None:
    fake = FakeReviewWriteBackClient()
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(review_writeback_client=fake))
    )
    assert _resolve_writeback_client(request) is fake  # type: ignore[arg-type]
