"""Routes that make one model call per row must not run them on the event loop.

``POST /evaluations``, ``POST /judges/{id}/alignment`` (and the judge test run),
``POST /knowledge-bases/{id}/calibrate``, ``POST /knowledge/query`` and the deploy gate behind
``POST /workflows/{id}/deployments/{alias}/promote`` (one workflow run per test-set row) each call a model
synchronously, one call per example. On a real provider that is minutes. Called inline from an
``async`` handler it holds the event loop for the whole time: the web app, ``/readiness`` and every
other user stall until the request ends (found running the cookbook labs against a real provider).

The probes below run in place of the model call and report whether an event loop is running on the
calling thread. In a worker thread there is none; inline in the handler there is.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.evaluations as evaluations_route
import caliber.routes.judges as judges_route
import caliber.routes.knowledge_bases as knowledge_bases_route
import caliber.routes.workflow_deployments as workflow_deployments_route
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberJudge,
    CaliberKnowledgeBase,
)
from caliber.knowledge.schemas import KnowledgeCalibrationRunSummary, KnowledgeQueryResultSchema


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _seed_judge(session: Session) -> None:
    session.add(
        CaliberJudge(
            judge_id="JDG-loop",
            name="loop-judge",
            description="d",
            instructions="Rate {{ outputs }}.",
            model="openai:/gpt-4o-mini",
            owner="@sarah",
            tags=[],
            status="active",
        )
    )
    session.commit()


def test_evaluation_model_calls_run_off_the_event_loop(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        CaliberEvalDataset(
            dataset_id="ED-loop",
            name="loop",
            description="",
            owner="@sarah",
            tags=[],
            status="active",
            version=2,
        )
    )
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="EX-loop",
            dataset_id="ED-loop",
            dataset_version=2,
            input={"question": "2+2"},
            expected={"expected": "4"},
            weight=1.0,
            tags=[],
        )
    )
    db_session.commit()
    seen: list[bool] = []

    def complete_factory(_config: object):  # type: ignore[no-untyped-def]
        def complete(_system: str, _user: str) -> str:
            seen.append(_on_the_event_loop())
            return "4"

        return complete

    monkeypatch.setattr(evaluations_route, "build_completion_fn", complete_factory)
    response = client.post(
        evaluations_route.LIST_PATH,
        json={"dataset_id": "ED-loop", "scorers": ["exact_match"], "label": "loop"},
    )
    assert response.status_code == 201, response.text
    assert seen == [False]


def test_judge_alignment_and_test_run_call_the_model_off_the_event_loop(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_judge(db_session)
    seen: list[bool] = []

    def score(_judge: object, **_kwargs: object) -> SimpleNamespace:
        seen.append(_on_the_event_loop())
        return SimpleNamespace(score=1.0, value=True, rationale="fine")

    monkeypatch.setattr(judges_route, "build_judge", lambda *a, **k: object())
    monkeypatch.setattr(judges_route, "score_with_judge", score)

    aligned = client.post(
        judges_route.ALIGNMENT_PATH.replace("{judge_id}", "JDG-loop"),
        json={"examples": [{"outputs": "a", "label": True}, {"outputs": "b", "label": False}]},
    )
    assert aligned.status_code == 200, aligned.text
    assert aligned.json()["data"]["scored"] == 2
    tried = client.post(
        judges_route.TEST_RUN_PATH.replace("{judge_id}", "JDG-loop"), json={"outputs": "x"}
    )
    assert tried.status_code == 200, tried.text
    assert seen == [False, False, False]


class _FakeKnowledgeService:
    """Stands in for KnowledgeBaseService and records where each call ran."""

    def __init__(self) -> None:
        self.on_loop: dict[str, bool] = {}

    def query(self, _payload: object, **_kwargs: object) -> KnowledgeQueryResultSchema:
        self.on_loop["query"] = _on_the_event_loop()
        # Built without validation: only where the call ran matters here, not the payload.
        return KnowledgeQueryResultSchema.model_construct(question="q", versions=[])

    def calibrate(self, knowledge_base_id: str, **kwargs: Any) -> KnowledgeCalibrationRunSummary:
        self.on_loop["calibrate"] = _on_the_event_loop()
        return KnowledgeCalibrationRunSummary(
            test_run_id="KBTR-loop",
            knowledge_base_id=knowledge_base_id,
            knowledge_base_version_id=kwargs["version_id"],
            eval_dataset_id=kwargs["eval_dataset_id"],
            eval_dataset_version=None,
            retrieval_mode=kwargs["retrieval_mode"],
            top_k=kwargs["top_k"],
            test_set_size=1,
            metrics={},
            created_by="@sarah",
            status="completed",
            created_at=datetime.now(timezone.utc),
            completed_at=None,
        )


def test_knowledge_query_and_calibrate_run_off_the_event_loop(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-loop",
            name="loop-kb",
            owner="@sarah",
            source_bucket="b",
            visibility="user",
        )
    )
    db_session.commit()
    service = _FakeKnowledgeService()
    monkeypatch.setattr(knowledge_bases_route, "_service", lambda _request: service)
    prefix = "/ajax-api/2.0/mlflow/caliber"

    queried = client.post(
        f"{prefix}/knowledge/query", json={"version_ids": ["KBV-1"], "question": "q"}
    )
    assert queried.status_code == 200, queried.text
    calibrated = client.post(
        f"{prefix}/knowledge-bases/KB-loop/calibrate",
        json={"version_id": "KBV-1", "eval_dataset_id": "ED-1", "top_k": 1},
    )
    assert calibrated.status_code == 201, calibrated.text
    assert service.on_loop == {"query": False, "calibrate": False}


def test_deploy_gate_replays_run_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A promotion replays the gate's test set through the workflow: minutes with a real model.

    Found when a ``prod`` promotion over 41 incidents froze the whole server (readiness
    included) for the length of the replay."""
    from tests.workflow_helpers import PREFIX, create_and_publish

    wid, vid = create_and_publish(client)
    seen: list[bool] = []
    real_promote = workflow_deployments_route.promote

    def probe(*args: Any, **kwargs: Any) -> Any:
        seen.append(_on_the_event_loop())
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow_deployments_route, "promote", probe)

    response = client.post(
        f"{PREFIX}/workflows/{wid}/deployments/dev/promote", json={"version_id": vid}
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["rotated"] is True
    assert seen == [False]
