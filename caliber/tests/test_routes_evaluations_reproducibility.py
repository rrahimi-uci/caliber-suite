"""Dataset-reproducibility contract for ``POST /caliber/evaluations``.

From the repository review (``ui-complete-report.md`` §4, "Dataset
reproducibility" and "Dataset weights and slices"):

* ``dataset_version`` was only validated as ``>= 1``. Pinning a version the
  dataset never reached persisted that number on the run while the "as of N"
  load returned the *current* set — a run record that reads reproducible but
  isn't.
* The generic loader dropped ``weight`` and ``tags``, so a deliberately
  weighted dataset aggregated as an unweighted row mean.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.evaluations as evaluations_route
from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample
from caliber.routes.evaluations import LIST_PATH


def _fake_completion(_config):
    def complete(_system: str, user: str) -> str:
        return {"capital of France": "Paris"}.get(user.strip(), "I don't know")

    return complete


def _seed_weighted_dataset(session: Session) -> None:
    """One passing example at weight 1, one failing example at weight 3."""
    session.add(
        CaliberEvalDataset(
            dataset_id="ED-w",
            name="weighted",
            description="",
            owner="@test",
            tags=[],
            status="active",
            version=2,
        )
    )
    session.add_all(
        [
            CaliberEvalDatasetExample(
                example_id="EX-hit",
                dataset_id="ED-w",
                dataset_version=1,
                input={"question": "capital of France"},
                expected={"expected": "Paris"},
                weight=1.0,
                tags=["geo"],
            ),
            CaliberEvalDatasetExample(
                example_id="EX-miss",
                dataset_id="ED-w",
                dataset_version=1,
                input={"question": "2+2"},
                expected={"expected": "4"},
                weight=3.0,
                tags=["math", "p0"],
            ),
        ]
    )
    session.commit()


def test_pinning_a_version_beyond_the_dataset_is_rejected(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_weighted_dataset(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-w", "dataset_version": 99, "scorers": ["exact_match"]},
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "no version 99" in detail
    assert "current version is 2" in detail


def test_pinning_an_existing_version_still_works(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new guard must reject only versions the dataset never reached."""
    _seed_weighted_dataset(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(
        LIST_PATH,
        json={"dataset_id": "ED-w", "dataset_version": 1, "scorers": ["exact_match"]},
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["dataset_version"] == 1


def test_example_weights_drive_the_aggregate(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 3x-weighted failing example must outweigh a 1x-weighted passing one.

    Before the loader carried ``weight`` through, this scored 0.5 (one of two
    rows passed) regardless of how the dataset was curated.
    """
    _seed_weighted_dataset(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(LIST_PATH, json={"dataset_id": "ED-w", "scorers": ["exact_match"]})

    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    # (1.0*1 + 0.0*3) / 4 — not the unweighted 0.5.
    assert data["aggregate"]["exact_match"] == 0.25
    assert data["overall_score"] == 0.25
    # Row-level pass/fail counts stay per-row (they are counts, not means).
    assert data["passed_count"] == 1
    assert data["failed_count"] == 1


def test_example_tags_survive_into_the_row_results(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_weighted_dataset(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    resp = client.post(LIST_PATH, json={"dataset_id": "ED-w", "scorers": ["exact_match"]})

    assert resp.status_code == 201, resp.text
    by_id = {row["example_id"]: row for row in resp.json()["data"]["results"]}
    assert by_id["EX-miss"]["tags"] == ["math", "p0"]
    assert by_id["EX-miss"]["weight"] == 3.0
    assert by_id["EX-hit"]["tags"] == ["geo"]
