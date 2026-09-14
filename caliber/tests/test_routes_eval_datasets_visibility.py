"""`P2` (isolation closure, item 1, slice 3): visibility gaps on
``/caliber/eval-datasets``'s mutation routes.

``create_example``, ``create_example_from_trace``, ``revise_example``,
``sync_dataset_to_mlflow``, and ``restore_dataset_version`` each looked their
dataset up with a bare ``session.get`` -- no visibility check at all --
unlike ``get_dataset``/``list_examples`` (already correct, and the reference
pattern these fixes reuse). A non-member operator could mutate, revise, or
push another project's dataset content to MLflow by id.

``update_dataset``/``supersede_example`` are deliberately untouched:
``SCOPE_ADMIN``-only, and admin already bypasses visibility unconditionally
(``db/scoping.py``), so wrapping them would change nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.eval_datasets as eval_datasets_route
from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample
from caliber.eval.dataset_sync import FakeDatasetSyncClient
from caliber.routes.eval_datasets import (
    EXAMPLES_PATH,
    FROM_TRACE_PATH,
    RESTORE_PATH,
    REVISE_PATH,
    SYNC_PATH,
)
from caliber.trace_client import TraceDetail

# Deliberately not in the test suite's permissive admin list, and not the
# dataset's owner -- a genuine non-member.
STRANGER = "@stranger-eval-ds"


def _grant_operator(client: TestClient, *users: str) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"operator_users": ",".join(users)}
    )


def _seed_hidden_dataset(session: Session, **overrides: object) -> CaliberEvalDataset:
    defaults: dict[str, object] = {
        "dataset_id": "ED-hidden",
        "name": "hidden-dataset",
        "description": "",
        "owner": "@owner",
        "tags": [],
        "status": "active",
        "version": 1,
        "visibility": "user",
        "project_id": None,
    }
    defaults.update(overrides)
    ds = CaliberEvalDataset(**defaults)
    session.add(ds)
    session.commit()
    return ds


def test_create_example_hides_a_hidden_dataset(client: TestClient, db_session: Session) -> None:
    _seed_hidden_dataset(db_session)
    _grant_operator(client, STRANGER)

    resp = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-hidden"),
        json={"input": {"q": "x"}, "expected": {"expected": "y"}},
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text


def test_create_example_from_trace_hides_a_hidden_dataset(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike `create_prompt`'s MLflow *write*, `fetch_trace_detail` is a
    read of a trace id the caller already supplied -- no state to corrupt
    by running it before the dataset check, so (unlike the sync test
    below) this only asserts the final refusal, not call ordering."""
    _seed_hidden_dataset(db_session)
    _grant_operator(client, STRANGER)
    monkeypatch.setattr(
        eval_datasets_route,
        "fetch_trace_detail",
        lambda trace_id: TraceDetail(trace_id=trace_id, request="q", response="a"),
    )

    resp = client.post(
        FROM_TRACE_PATH.replace("{dataset_id}", "ED-hidden"),
        json={"trace_id": "tr-1"},
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text


def test_revise_example_hides_a_hidden_dataset(client: TestClient, db_session: Session) -> None:
    _seed_hidden_dataset(db_session)
    _grant_operator(client, STRANGER)

    resp = client.post(
        REVISE_PATH.replace("{dataset_id}", "ED-hidden").replace("{example_id}", "EX-anything"),
        json={"input": {"q": "x"}, "expected": {"expected": "y"}},
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text


def test_sync_dataset_to_mlflow_hides_a_hidden_dataset(
    client: TestClient, db_session: Session
) -> None:
    """Checked before the MLflow write -- ``fake.calls`` staying empty
    proves the refusal happens before any content reaches MLflow, the same
    ordering ``routes/prompts.py::create_prompt`` established."""
    _seed_hidden_dataset(db_session)
    session_local = db_session
    session_local.add(
        CaliberEvalDatasetExample(
            example_id="EX-hidden",
            dataset_id="ED-hidden",
            dataset_version=1,
            input={"q": "x"},
            expected={"expected": "y"},
            weight=1.0,
            tags=[],
        )
    )
    session_local.commit()
    fake = FakeDatasetSyncClient()
    client.app.state.dataset_sync_client = fake
    _grant_operator(client, STRANGER)

    resp = client.post(
        SYNC_PATH.replace("{dataset_id}", "ED-hidden"),
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text
    assert fake.calls == []


def test_restore_dataset_version_hides_a_hidden_dataset(
    client: TestClient, db_session: Session
) -> None:
    _seed_hidden_dataset(db_session, version=2)
    _grant_operator(client, STRANGER)

    resp = client.post(
        RESTORE_PATH.replace("{dataset_id}", "ED-hidden"),
        json={"version": 1},
        headers={"X-CALIBER-User": STRANGER},
    )
    assert resp.status_code == 404, resp.text


def test_create_example_still_works_for_the_datasets_own_owner(
    client: TestClient, db_session: Session
) -> None:
    """Control case: the fix denies a stranger, not the dataset's own
    owner -- a personal (project_id=None) dataset stays usable by its
    creator, exactly like `get_dataset`/`list_examples` already behave."""
    _seed_hidden_dataset(db_session)
    _grant_operator(client, "@owner")

    resp = client.post(
        EXAMPLES_PATH.replace("{dataset_id}", "ED-hidden"),
        json={"input": {"q": "x"}, "expected": {"expected": "y"}},
        headers={"X-CALIBER-User": "@owner"},
    )
    assert resp.status_code == 201, resp.text
