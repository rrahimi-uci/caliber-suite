"""Tests for the real evidence stage — trace-fetch client + similar feedback.

Covers the wiring that turns evidence collection from a row-echo into a real
summary: the MLflow trace-fetch client (normalizing a Trace into a compact
summary), and the evidence stage enriching with the trace + similar past
feedback for the same agent + category.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.orchestrator.evidence import _collect, run_evidence
from caliber.trace_client import FakeTraceClient, MLflowTraceClient, TraceSummary


def _agent(session: Session, agent_id: str = "a1") -> None:
    session.add(
        CaliberAgentConfig(
            agent_id=agent_id,
            experiment_id="e1",
            name="A",
            owner="@o",
            artifact_types=["prompt"],
            eval_thresholds={},
            optimizer_config={},
            approval_policy={},
        )
    )
    session.flush()


def _item(
    session: Session,
    item_id: str,
    *,
    agent_id: str = "a1",
    category: str = "tool_use",
    trace_id: str | None = None,
    free_text: str = "x",
    severity: str = "standard",
) -> None:
    session.add(
        CaliberVerificationItem(
            item_id=item_id,
            agent_id=agent_id,
            category=category,
            free_text=free_text,
            severity=severity,
            status="verified",
            trace_id=trace_id,
        )
    )
    session.flush()


def _job(session: Session, *, primary_item_id: str, agent_id: str = "a1") -> CaliberRefinementJob:
    job = CaliberRefinementJob(
        job_id="J1",
        agent_id=agent_id,
        primary_item_id=primary_item_id,
        artifact_type="prompt",
        status="running",
        current_stage="evidence",
        bundle_targets=[],
    )
    session.add(job)
    session.commit()
    return job


# ───────────────────── trace client ─────────────────────


class TestTraceClient:
    def test_fake_trace_client(self) -> None:
        client = FakeTraceClient(
            {"T1": TraceSummary(status="OK", span_count=3, tool_calls=["search"])}
        )
        summary = client.get_trace_summary("T1")
        assert summary is not None
        assert summary.status == "OK"
        assert summary.tool_calls == ["search"]
        assert client.get_trace_summary("missing") is None

    def test_mlflow_trace_client_summarizes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mlflow

        def _span(name: str, span_type: str, status_code: str = "") -> object:
            status = type("S", (), {"status_code": status_code})()
            return type("Span", (), {"name": name, "span_type": span_type, "status": status})()

        data = type(
            "Data",
            (),
            {
                "request": '{"q":"hi"}',
                "response": '{"a":"yo"}',
                "spans": [
                    _span("agent", "AGENT"),
                    _span("search", "TOOL"),
                    _span("bad", "TOOL", "ERROR"),
                ],
            },
        )()
        info = type("Info", (), {"status": "OK", "request_preview": "", "response_preview": ""})()
        trace = type("Trace", (), {"data": data, "info": info})()
        monkeypatch.setattr(mlflow, "get_trace", lambda _tid, silent=False: trace)

        summary = MLflowTraceClient().get_trace_summary("T1")

        assert summary is not None
        assert summary.span_count == 3
        assert summary.tool_calls == ["search", "bad"]  # both TOOL spans
        assert summary.error is not None  # the ERROR-status span
        assert summary.request_preview == '{"q":"hi"}'

    def test_mlflow_trace_client_none_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mlflow

        monkeypatch.setattr(mlflow, "get_trace", lambda _tid, silent=False: None)
        assert MLflowTraceClient().get_trace_summary("T1") is None

    def test_mlflow_trace_client_swallows_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import mlflow

        def _boom(_tid: str, silent: bool = False) -> object:
            raise RuntimeError("tracking server down")

        monkeypatch.setattr(mlflow, "get_trace", _boom)
        assert MLflowTraceClient().get_trace_summary("T1") is None

    def test_empty_trace_id_returns_none(self) -> None:
        assert MLflowTraceClient().get_trace_summary("") is None


# ───────────────────── evidence enrichment ─────────────────────


class TestEvidenceEnrichment:
    def test_collect_includes_trace_summary(self, db_session: Session) -> None:
        _agent(db_session)
        _item(db_session, "I1", trace_id="T1")
        job = _job(db_session, primary_item_id="I1")
        item = db_session.get(CaliberVerificationItem, "I1")
        client = FakeTraceClient(
            {"T1": TraceSummary(status="OK", span_count=2, tool_calls=["search"])}
        )

        base = _collect(db_session, item, job, trace_client=client)

        assert base["trace"]["status"] == "OK"
        assert base["trace"]["tool_calls"] == ["search"]
        assert base["trace"]["span_count"] == 2

    def test_collect_omits_trace_without_client(self, db_session: Session) -> None:
        _agent(db_session)
        _item(db_session, "I1", trace_id="T1")
        job = _job(db_session, primary_item_id="I1")
        item = db_session.get(CaliberVerificationItem, "I1")

        base = _collect(db_session, item, job)  # no trace_client

        assert "trace" not in base

    def test_collect_includes_similar_feedback(self, db_session: Session) -> None:
        _agent(db_session)
        _item(db_session, "I1", category="tool_use")
        _item(db_session, "I2", category="tool_use", free_text="also broke")
        _item(db_session, "I3", category="tool_use", free_text="again")
        _item(db_session, "I4", category="hallucination")  # different category → excluded
        job = _job(db_session, primary_item_id="I1")
        item = db_session.get(CaliberVerificationItem, "I1")

        base = _collect(db_session, item, job)

        assert base["similar_feedback"]["count"] == 2  # I2 + I3 (not self, not other category)
        ids = {e["item_id"] for e in base["similar_feedback"]["examples"]}
        assert ids == {"I2", "I3"}

    def test_run_evidence_with_trace_client_advances_to_diagnosis(
        self, db_session: Session
    ) -> None:
        _agent(db_session)
        _item(db_session, "I1", trace_id="T1")
        _job(db_session, primary_item_id="I1")
        client = FakeTraceClient({"T1": TraceSummary(status="OK")})

        job = run_evidence(db_session, "J1", trace_client=client)

        assert job.current_stage == "diagnosis"
