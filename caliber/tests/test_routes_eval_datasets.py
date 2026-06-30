"""Integration tests for ``/caliber/eval-datasets``.

Covers dataset CRUD, example append, version bumping, and the
supersede flow that retires examples without deleting them.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
)
from caliber.eval.dataset_sync import FakeDatasetSyncClient
from caliber.routes.eval_datasets import (
    DETAIL_PATH,
    EXAMPLES_PATH,
    LIST_PATH,
    RESTORE_PATH,
    REVISE_PATH,
    SUPERSEDE_PATH,
    SYNC_PATH,
)


def _seed_dataset(session: Session, **overrides: object) -> CaliberEvalDataset:
    defaults: dict[str, object] = {
        "dataset_id": "ED-test",
        "name": "factual-checks",
        "description": "Q&A factual accuracy",
        "owner": "@sarah",
        "tags": ["factual"],
        "status": "active",
        "version": 1,
    }
    defaults.update(overrides)
    ds = CaliberEvalDataset(**defaults)
    session.add(ds)
    session.commit()
    return ds


def test_create_dataset_happy_path(client: TestClient, db_session: Session) -> None:
    response = client.post(
        LIST_PATH,
        json={"name": "tone-checks", "description": "tone", "owner": "@sarah"},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "tone-checks"
    assert data["dataset_id"].startswith("ED-")
    assert data["version"] == 1


def test_create_dataset_409_on_duplicate_name(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session)
    response = client.post(
        LIST_PATH,
        json={"name": "factual-checks", "owner": "@alice"},
    )
    assert response.status_code == 409


def test_list_datasets_filters_status(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-a", name="active-ds", status="active")
    _seed_dataset(db_session, dataset_id="ED-b", name="archived-ds", status="archived")

    response = client.get(LIST_PATH)
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"active-ds"}

    response = client.get(LIST_PATH, params={"status": "all"})
    names = {item["name"] for item in response.json()["data"]}
    assert names == {"active-ds", "archived-ds"}


def test_list_datasets_invalid_status_returns_400(client: TestClient) -> None:
    """``?status=foo`` rejected with 400 instead of silently matching
    nothing (deep-review consistency note #1)."""
    response = client.get(LIST_PATH, params={"status": "trash"})
    assert response.status_code == 400


def test_get_dataset_404(client: TestClient) -> None:
    response = client.get(DETAIL_PATH.replace("{dataset_id}", "ED-missing"))
    assert response.status_code == 404


def test_update_dataset_archive(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1")
    response = client.patch(
        DETAIL_PATH.replace("{dataset_id}", "ED-1"),
        json={"status": "archived"},
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "archived"


def test_append_example_bumps_version(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=3)
    response = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-1"),
        json={"input": {"q": "hi"}, "expected": {"a": "hi back"}, "weight": 2.0},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["dataset_version"] == 4
    assert data["weight"] == 2.0

    # Dataset version bumped.
    get_response = client.get(DETAIL_PATH.replace("{dataset_id}", "ED-1"))
    assert get_response.json()["data"]["version"] == 4


def test_list_examples_excludes_superseded_by_default(
    client: TestClient, db_session: Session
) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    # Add two examples, supersede one.
    r1 = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 1}})
    r2 = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 2}})
    ex1 = r1.json()["data"]["example_id"]
    ex2 = r2.json()["data"]["example_id"]

    supersede_response = client.post(
        SUPERSEDE_PATH.replace("{dataset_id}", "ED-1").replace("{example_id}", ex1)
    )
    assert supersede_response.status_code == 200
    assert supersede_response.json()["data"]["superseded_at"] is not None

    listed = client.get(EXAMPLES_PATH.replace("{dataset_id}", "ED-1")).json()["data"]
    assert [item["example_id"] for item in listed] == [ex2]

    all_listed = client.get(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-1"),
        params={"include_superseded": "true"},
    ).json()["data"]
    assert {item["example_id"] for item in all_listed} == {ex1, ex2}


def test_list_examples_filter_by_version(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"v": 1}})
    # Now version is 2. Append again → version 3.
    client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"v": 2}})

    response = client.get(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), params={"version": "3"})
    items = response.json()["data"]
    assert len(items) == 1
    assert items[0]["input"]["v"] == 2


def test_list_examples_rejects_out_of_range_version(
    client: TestClient, db_session: Session
) -> None:
    """Phase 1 audit (#5): a giant integer like ``2**62`` parses
    cleanly with ``int()`` but no row will ever match it. We now 400
    the request rather than silently returning ``[]``, which would
    look identical to "no examples at this version" and mislead the
    caller."""
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    response = client.get(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-1"),
        params={"version": str(2**62)},
    )
    assert response.status_code == 400
    assert "version must be between" in response.json()["detail"]


def test_list_examples_rejects_zero_or_negative_version(
    client: TestClient, db_session: Session
) -> None:
    """Versions are 1-indexed; 0 and negative inputs are nonsense."""
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    for bad in ("0", "-1", "-999"):
        response = client.get(
            EXAMPLES_PATH.replace("{dataset_id}", "ED-1"),
            params={"version": bad},
        )
        assert response.status_code == 400, f"expected 400 for version={bad!r}"


def test_supersede_idempotent(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    created = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 1}})
    ex_id = created.json()["data"]["example_id"]
    first = client.post(
        SUPERSEDE_PATH.replace("{dataset_id}", "ED-1").replace("{example_id}", ex_id)
    )
    assert first.status_code == 200
    assert first.json()["data"]["superseded_at"] is not None
    # Second call returns 200 with the row already retired — no
    # additional version bump, no new timestamp.
    second = client.post(
        SUPERSEDE_PATH.replace("{dataset_id}", "ED-1").replace("{example_id}", ex_id)
    )
    assert second.status_code == 200
    assert second.json()["data"]["superseded_at"] is not None

    # The dataset version reflects exactly one bump from the create
    # + one from the first supersede; the second is a no-op.
    detail = client.get(DETAIL_PATH.replace("{dataset_id}", "ED-1"))
    assert detail.json()["data"]["version"] == 3


def test_dataset_audit_rows(client: TestClient, db_session: Session) -> None:
    r = client.post(LIST_PATH, json={"name": "x", "owner": "@x"})
    dataset_id = r.json()["data"]["dataset_id"]
    rows = (
        db_session.execute(select(CaliberAuditLog).where(CaliberAuditLog.entity_id == dataset_id))
        .scalars()
        .all()
    )
    assert [row.action for row in rows] == ["create_eval_dataset"]


def test_example_create_404_for_unknown_dataset(client: TestClient) -> None:
    response = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-missing"),
        json={"input": {"q": 1}},
    )
    assert response.status_code == 404


def test_example_list_404_for_unknown_dataset(client: TestClient) -> None:
    response = client.get(EXAMPLES_PATH.replace("{dataset_id}", "ED-missing"))
    assert response.status_code == 404


def test_supersede_404_for_wrong_dataset(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1")
    _seed_dataset(db_session, dataset_id="ED-2", name="other")
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="EX-belongs-to-1",
            dataset_id="ED-1",
            dataset_version=1,
            input={},
            expected={},
            weight=1.0,
            tags=[],
        )
    )
    db_session.commit()
    response = client.post(
        SUPERSEDE_PATH.replace("{dataset_id}", "ED-2").replace("{example_id}", "EX-belongs-to-1")
    )
    assert response.status_code == 404


# --- Revise (edit a row: supersede old + append replacement atomically) -----


def _revise_path(dataset_id: str, example_id: str) -> str:
    return REVISE_PATH.replace("{dataset_id}", dataset_id).replace("{example_id}", example_id)


def test_revise_example_supersedes_old_and_appends_replacement(
    client: TestClient, db_session: Session
) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    created = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-1"),
        json={"input": {"q": "old"}, "expected": {"a": "old"}, "weight": 1.0},
    )
    old_id = created.json()["data"]["example_id"]
    assert created.json()["data"]["dataset_version"] == 2  # create bumped 1 -> 2

    revised = client.post(
        _revise_path("ED-1", old_id),
        json={"input": {"q": "new"}, "expected": {"a": "new"}, "weight": 3.0, "tags": ["edited"]},
    )
    assert revised.status_code == 201, revised.text
    new_row = revised.json()["data"]
    # A brand-new row at the bumped version carrying the edited content.
    assert new_row["example_id"] != old_id
    assert new_row["dataset_version"] == 3
    assert new_row["input"] == {"q": "new"}
    assert new_row["weight"] == 3.0
    assert new_row["tags"] == ["edited"]
    assert new_row["superseded_at"] is None

    # Dataset bumped exactly once (2 -> 3).
    detail = client.get(DETAIL_PATH.replace("{dataset_id}", "ED-1"))
    assert detail.json()["data"]["version"] == 3

    # The default (current) view shows only the replacement; the old row is retired.
    current = client.get(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"))
    current_ids = {row["example_id"] for row in current.json()["data"]}
    assert current_ids == {new_row["example_id"]}

    # The old row still exists (append-only) and is marked superseded at v3.
    with_retired = client.get(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-1"),
        params={"include_superseded": "true"},
    )
    old = next(r for r in with_retired.json()["data"] if r["example_id"] == old_id)
    assert old["superseded_at"] is not None
    assert old["superseded_version"] == 3


def test_revise_example_409_when_already_superseded(
    client: TestClient, db_session: Session
) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    created = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 1}})
    ex_id = created.json()["data"]["example_id"]
    client.post(SUPERSEDE_PATH.replace("{dataset_id}", "ED-1").replace("{example_id}", ex_id))
    response = client.post(_revise_path("ED-1", ex_id), json={"input": {"q": 2}})
    assert response.status_code == 409


def test_revise_example_404_for_wrong_dataset(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1")
    _seed_dataset(db_session, dataset_id="ED-2", name="other")
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="EX-in-1",
            dataset_id="ED-1",
            dataset_version=1,
            input={},
            expected={},
            weight=1.0,
            tags=[],
        )
    )
    db_session.commit()
    response = client.post(_revise_path("ED-2", "EX-in-1"), json={"input": {"q": 1}})
    assert response.status_code == 404


def test_revise_example_writes_audit_row(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1", version=1)
    created = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 1}})
    old_id = created.json()["data"]["example_id"]
    revised = client.post(_revise_path("ED-1", old_id), json={"input": {"q": 2}})
    new_id = revised.json()["data"]["example_id"]
    rows = (
        db_session.execute(
            select(CaliberAuditLog)
            .where(CaliberAuditLog.entity_id == "ED-1")
            .where(CaliberAuditLog.action == "revise_eval_example")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].details["superseded_example_id"] == old_id
    assert rows[0].details["replacement_example_id"] == new_id


# --- MLflow GenAI dataset sync (MLflow 3.14) --------------------------------


class _RaisingSyncClient:
    """Sync client that always fails — exercises the 502 degrade path."""

    def sync_dataset(self, **_kwargs: object) -> object:
        raise RuntimeError("mlflow unreachable")


def test_sync_dataset_pushes_current_examples(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1")
    # One live example + one superseded (the superseded one must NOT be pushed).
    client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 1}})
    created = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-1"), json={"input": {"q": 2}})
    example_id = created.json()["data"]["example_id"]
    client.post(SUPERSEDE_PATH.replace("{dataset_id}", "ED-1").replace("{example_id}", example_id))

    fake = FakeDatasetSyncClient(dataset_id="d-123", digest="abc")
    client.app.state.dataset_sync_client = fake
    try:
        response = client.post(SYNC_PATH.replace("{dataset_id}", "ED-1"))
    finally:
        client.app.state.dataset_sync_client = None

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mlflow_dataset_id"] == "d-123"
    assert data["mlflow_digest"] == "abc"
    # Only the single live example is pushed (superseded one excluded).
    assert data["mlflow_record_count"] == 1
    assert data["mlflow_synced_version"] == data["version"]
    assert data["mlflow_synced_at"] is not None
    # The fake captured exactly one live record + the lineage tags.
    assert fake.calls[0]["record_count"] == 1
    assert fake.calls[0]["tags"]["caliber.dataset_id"] == "ED-1"


def test_sync_dataset_writes_audit_row(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1")
    client.app.state.dataset_sync_client = FakeDatasetSyncClient()
    try:
        client.post(SYNC_PATH.replace("{dataset_id}", "ED-1"))
    finally:
        client.app.state.dataset_sync_client = None
    rows = (
        db_session.execute(
            select(CaliberAuditLog).where(CaliberAuditLog.action == "sync_eval_dataset_mlflow")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].entity_id == "ED-1"


def test_sync_dataset_404_for_unknown_dataset(client: TestClient) -> None:
    response = client.post(SYNC_PATH.replace("{dataset_id}", "ED-missing"))
    assert response.status_code == 404


def test_sync_dataset_502_on_mlflow_failure(client: TestClient, db_session: Session) -> None:
    _seed_dataset(db_session, dataset_id="ED-1")
    client.app.state.dataset_sync_client = _RaisingSyncClient()
    try:
        response = client.post(SYNC_PATH.replace("{dataset_id}", "ED-1"))
    finally:
        client.app.state.dataset_sync_client = None
    assert response.status_code == 502
    # CALIBER state stays clean — nothing recorded as synced.
    detail = client.get(DETAIL_PATH.replace("{dataset_id}", "ED-1")).json()["data"]
    assert detail["mlflow_dataset_id"] is None


def test_restore_dataset_version_re_adds_retired_examples(
    client: TestClient, db_session: Session
) -> None:
    """Restoring a prior version re-adds examples retired since then, as a new
    head version, leaving the still-active examples in place."""
    _seed_dataset(db_session, dataset_id="ED-R", version=1)
    a = client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-R"), json={"input": {"q": "A"}})
    client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-R"), json={"input": {"q": "B"}})
    ex_a = a.json()["data"]["example_id"]
    target_version = client.get(DETAIL_PATH.replace("{dataset_id}", "ED-R")).json()["data"][
        "version"
    ]
    # target_version is 3 here: active set = {A, B}.

    # Retire A -> head becomes {B}.
    client.post(SUPERSEDE_PATH.replace("{dataset_id}", "ED-R").replace("{example_id}", ex_a))

    restored = client.post(
        RESTORE_PATH.replace("{dataset_id}", "ED-R"), json={"version": target_version}
    )
    assert restored.status_code == 200, restored.text
    # Version advanced past the supersede (4) to the restore head.
    assert restored.json()["data"]["version"] == 5

    active = client.get(EXAMPLES_PATH.replace("{dataset_id}", "ED-R")).json()["data"]
    assert sorted(e["input"]["q"] for e in active) == ["A", "B"]  # A re-added, B kept


def test_restore_dataset_version_rejects_current_or_future(
    client: TestClient, db_session: Session
) -> None:
    _seed_dataset(db_session, dataset_id="ED-R2", version=3)
    bad = client.post(RESTORE_PATH.replace("{dataset_id}", "ED-R2"), json={"version": 3})
    assert bad.status_code == 400
    future = client.post(RESTORE_PATH.replace("{dataset_id}", "ED-R2"), json={"version": 9})
    assert future.status_code == 400


def test_restore_dataset_version_409_when_set_unchanged(
    client: TestClient, db_session: Session
) -> None:
    """Restoring a prior version whose active set already equals the current
    active set is a no-op -> 409."""
    _seed_dataset(db_session, dataset_id="ED-R3", version=1)
    client.post(EXAMPLES_PATH.replace("{dataset_id}", "ED-R3"), json={"input": {"q": "A"}})  # v2
    b = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-R3"), json={"input": {"q": "B"}}
    )  # v3
    ex_b = b.json()["data"]["example_id"]
    # Retire B (v4) -> active set is {A}, identical to the set as-of v2.
    client.post(SUPERSEDE_PATH.replace("{dataset_id}", "ED-R3").replace("{example_id}", ex_b))

    same = client.post(RESTORE_PATH.replace("{dataset_id}", "ED-R3"), json={"version": 2})
    assert same.status_code == 409
