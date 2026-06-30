"""Integration tests for ``POST /eval-datasets/{id}/examples/from-trace``.

The trace fetch is faked by monkeypatching
``caliber.routes.eval_datasets.fetch_trace_detail`` so no MLflow is needed.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.eval_datasets as eval_datasets_route
from caliber.db.models import CaliberEvalDataset
from caliber.trace_client import TraceDetail

FROM_TRACE = "/ajax-api/2.0/mlflow/caliber/eval-datasets/{dataset_id}/examples/from-trace"


def _seed(session: Session, dataset_id: str = "ED-1", version: int = 2) -> None:
    session.add(
        CaliberEvalDataset(
            dataset_id=dataset_id,
            name=f"ds-{dataset_id}",
            description="",
            owner="@sarah",
            tags=[],
            status="active",
            version=version,
        )
    )
    session.commit()


def _fake_detail(request: object, response: object):
    def fetch(trace_id: str) -> TraceDetail:
        return TraceDetail(trace_id=trace_id, request=request, response=response)

    return fetch


def test_from_trace_captures_request_and_response(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, version=2)
    monkeypatch.setattr(
        eval_datasets_route,
        "fetch_trace_detail",
        _fake_detail("What is the capital of France?", "Paris"),
    )
    resp = client.post(
        FROM_TRACE.format(dataset_id="ED-1"),
        json={"trace_id": "tr-123", "tags": ["smoke"]},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["input"]["input"] == "What is the capital of France?"
    assert data["expected"]["expected"] == "Paris"
    # Lineage + caller tags, de-duplicated and ordered.
    assert data["tags"] == ["from-trace", "trace:tr-123", "smoke"]
    # Dataset version bumped from 2 → 3 (append-only).
    assert data["dataset_version"] == 3


def test_from_trace_serializes_structured_payloads(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(
        eval_datasets_route,
        "fetch_trace_detail",
        _fake_detail({"messages": [{"role": "user", "content": "hi"}]}, {"answer": "hello"}),
    )
    resp = client.post(FROM_TRACE.format(dataset_id="ED-1"), json={"trace_id": "tr-1"})
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    # Non-string trace payloads are rendered as stable JSON, nothing dropped.
    assert "messages" in data["input"]["input"]
    assert "hello" in data["expected"]["expected"]


def test_from_trace_overrides_take_precedence(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(
        eval_datasets_route, "fetch_trace_detail", _fake_detail("raw q", "wrong answer")
    )
    resp = client.post(
        FROM_TRACE.format(dataset_id="ED-1"),
        json={
            "trace_id": "tr-1",
            "input": {"question": "corrected q"},
            "expected": {"expected": "corrected answer"},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["input"] == {"question": "corrected q"}
    assert data["expected"] == {"expected": "corrected answer"}


def test_from_trace_empty_capture_404(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(eval_datasets_route, "fetch_trace_detail", _fake_detail(None, None))
    resp = client.post(FROM_TRACE.format(dataset_id="ED-1"), json={"trace_id": "tr-missing"})
    assert resp.status_code == 404
    assert "no request to capture" in resp.json()["detail"]


def test_from_trace_empty_capture_with_input_override_succeeds(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(eval_datasets_route, "fetch_trace_detail", _fake_detail(None, None))
    resp = client.post(
        FROM_TRACE.format(dataset_id="ED-1"),
        json={"trace_id": "tr-x", "input": {"question": "manual"}},
    )
    assert resp.status_code == 201, resp.text


def test_from_trace_unknown_dataset_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(eval_datasets_route, "fetch_trace_detail", _fake_detail("q", "a"))
    resp = client.post(FROM_TRACE.format(dataset_id="ED-missing"), json={"trace_id": "tr-1"})
    assert resp.status_code == 404
