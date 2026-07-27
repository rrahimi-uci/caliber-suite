"""Integration tests for ``/caliber/review-queues`` — structured human review."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog, CaliberReviewItem
from caliber.review.writeback import FakeReviewWriteBackClient
from caliber.routes.review_queues import (
    DETAIL_PATH,
    ITEMS_PATH,
    LIST_PATH,
    SUBMIT_PATH,
)

_QUEUE = {
    "name": "answer-quality",
    "description": "Human review of answer quality.",
    "questions": [
        {"key": "correct", "title": "Is the answer correct?", "type": "pass_fail"},
        {
            "key": "category",
            "title": "Failure category",
            "type": "categorical",
            "options": ["none", "hallucination", "refusal"],
            "required": False,
        },
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


def _create_queue(client: TestClient) -> str:
    resp = client.post(LIST_PATH, json=_QUEUE)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["queue_id"]


def test_create_queue_and_counts(client: TestClient) -> None:
    resp = client.post(LIST_PATH, json=_QUEUE)
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["queue_id"].startswith("RVQ-")
    assert data["item_count"] == 0
    assert len(data["questions"]) == 3


def test_create_queue_requires_at_least_one_question(client: TestClient) -> None:
    resp = client.post(LIST_PATH, json={**_QUEUE, "questions": []})
    assert resp.status_code == 400


def test_categorical_question_requires_options(client: TestClient) -> None:
    bad = {
        **_QUEUE,
        "questions": [{"key": "c", "title": "Cat", "type": "categorical"}],
    }
    resp = client.post(LIST_PATH, json=bad)
    assert resp.status_code == 400


def test_add_items_is_idempotent_per_trace(client: TestClient) -> None:
    queue_id = _create_queue(client)
    path = ITEMS_PATH.replace("{queue_id}", queue_id)
    first = client.post(path, json={"trace_ids": ["tr-1", "tr-2"]})
    assert first.status_code == 201
    assert len(first.json()["data"]) == 2
    # Re-adding tr-1 plus a new tr-3 only creates tr-3.
    second = client.post(path, json={"trace_ids": ["tr-1", "tr-3"]})
    assert len(second.json()["data"]) == 1

    detail = client.get(DETAIL_PATH.replace("{queue_id}", queue_id)).json()["data"]
    assert detail["queue"]["item_count"] == 3
    assert detail["queue"]["pending_count"] == 3


def test_submit_writes_answers_back_and_completes_item(
    client: TestClient, db_session: Session
) -> None:
    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]

    fake = FakeReviewWriteBackClient()
    client.app.state.review_writeback_client = fake
    try:
        resp = client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": True, "gold": "42"}},
        )
    finally:
        client.app.state.review_writeback_client = None

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "completed"
    assert data["completed_by"] == "@test"
    assert len(data["assessment_ids"]) == 2
    # The fake captured the write-back: a feedback answer + an expectation answer.
    call = fake.calls[0]
    assert call["trace_id"] == "tr-1"
    targets = {a["name"]: a["target"] for a in call["answers"]}
    assert targets == {"correct": "feedback", "gold": "expectation"}


def test_submit_missing_required_answer_is_400(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    # 'correct' is required but omitted.
    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": {"category": "none"}},
    )
    assert resp.status_code == 400
    assert "correct" in resp.text


def test_submit_unknown_answer_key_is_400(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": {"correct": True, "bogus": 1}},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    ("answers", "detail"),
    [
        ({"correct": "yes"}, "boolean"),
        ({"correct": True, "category": "other"}, "one of"),
        ({"correct": True, "gold": 42}, "text"),
    ],
)
def test_submit_validates_answer_types(
    client: TestClient, answers: dict[str, object], detail: str
) -> None:
    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]

    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": answers},
    )

    assert resp.status_code == 400
    assert detail in resp.text


def test_submit_validates_numeric_answer_type(client: TestClient) -> None:
    payload = {
        **_QUEUE,
        "name": "numeric-review",
        "questions": [{"key": "score", "title": "Score", "type": "numeric"}],
    }
    created = client.post(LIST_PATH, json=payload)
    queue_id = created.json()["data"]["queue_id"]
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]

    resp = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": {"score": True}},
    )

    assert resp.status_code == 400
    assert "numeric" in resp.text


def test_submit_rejects_duplicate_completion(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    path = SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id)
    fake = FakeReviewWriteBackClient()
    client.app.state.review_writeback_client = fake
    try:
        assert client.post(path, json={"answers": {"correct": True}}).status_code == 200
        duplicate = client.post(path, json={"answers": {"correct": False}})
    finally:
        client.app.state.review_writeback_client = None

    assert duplicate.status_code == 409
    assert len(fake.calls) == 1


def test_failed_writeback_releases_item_for_retry(client: TestClient) -> None:
    class FailingWriteBack:
        def write_answers(self, **_kwargs: object) -> list[str]:
            raise RuntimeError("writeback unavailable")

    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    path = SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id)
    client.app.state.review_writeback_client = FailingWriteBack()
    try:
        failed = client.post(path, json={"answers": {"correct": True}})
    finally:
        client.app.state.review_writeback_client = FakeReviewWriteBackClient()

    assert failed.status_code == 502
    retry = client.post(path, json={"answers": {"correct": True}})
    client.app.state.review_writeback_client = None
    assert retry.status_code == 200


def test_archived_queue_rejects_add_and_submit(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add_path = ITEMS_PATH.replace("{queue_id}", queue_id)
    add = client.post(add_path, json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    archived = client.patch(
        DETAIL_PATH.replace("{queue_id}", queue_id), json={"status": "archived"}
    )
    assert archived.status_code == 200

    assert client.post(add_path, json={"trace_ids": ["tr-2"]}).status_code == 409
    submit = client.post(
        SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
        json={"answers": {"correct": True}},
    )
    assert submit.status_code == 409


def test_hidden_queue_cannot_be_mutated_by_unrelated_user(client: TestClient) -> None:
    queue_id = _create_queue(client)
    add = client.post(
        ITEMS_PATH.replace("{queue_id}", queue_id),
        json={"trace_ids": ["tr-1"]},
    )
    item_id = add.json()["data"][0]["item_id"]
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test", "operator_users": "@sarah"}
    )
    other_user = {"X-CALIBER-User": "@sarah"}

    assert (
        client.post(
            ITEMS_PATH.replace("{queue_id}", queue_id),
            json={"trace_ids": ["tr-2"]},
            headers=other_user,
        ).status_code
        == 404
    )
    assert (
        client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": True}},
            headers=other_user,
        ).status_code
        == 404
    )


def test_get_queue_404(client: TestClient) -> None:
    assert client.get(DETAIL_PATH.replace("{queue_id}", "RVQ-x")).status_code == 404


def test_submit_writes_audit_row(client: TestClient, db_session: Session) -> None:
    queue_id = _create_queue(client)
    add = client.post(ITEMS_PATH.replace("{queue_id}", queue_id), json={"trace_ids": ["tr-1"]})
    item_id = add.json()["data"][0]["item_id"]
    client.app.state.review_writeback_client = FakeReviewWriteBackClient()
    try:
        client.post(
            SUBMIT_PATH.replace("{queue_id}", queue_id).replace("{item_id}", item_id),
            json={"answers": {"correct": False}},
        )
    finally:
        client.app.state.review_writeback_client = None
    actions = {row.action for row in db_session.execute(select(CaliberAuditLog)).scalars().all()}
    assert {"create_review_queue", "add_review_items", "submit_review_item"} <= actions
    # The item carries the recorded answer + completion.
    item = db_session.get(CaliberReviewItem, item_id)
    assert item is not None and item.status == "completed"
