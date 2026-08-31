"""Dataset, judge, and evaluation resource modules."""

from __future__ import annotations

from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import EvalDataset, decode

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def test_is_synced_reports_history_not_freshness() -> None:
    """A synced dataset can still be behind.

    ``mlflow_synced_version`` lags ``version`` the moment a row is added, so
    conflating "has been synced" with "is in sync" would let a caller trust
    stale evidence.
    """
    stale = decode(
        EvalDataset, {"version": 5, "mlflow_synced_version": 2, "mlflow_synced_at": "2026-01-01"}
    )
    assert stale.is_synced
    assert stale.mlflow_synced_version != stale.version

    assert not decode(EvalDataset, {"version": 1}).is_synced


def test_capturing_a_trace_as_an_example_posts_the_trace_id() -> None:
    """The path from an observed failure to evidence."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        sent.update(jsonlib.loads(request.content))
        assert request.url.path.endswith("/examples/from-trace")
        return envelope({"example_id": "EX-1", "source_trace_id": "tr-1"})

    with client_with(handler) as caliber:
        example = caliber.eval_datasets.add_from_trace("ED-1", "tr-1")

    assert sent["trace_id"] == "tr-1"
    assert example.source_trace_id == "tr-1"


def test_judge_creation_defaults_to_a_boolean_verdict() -> None:
    """A bool judge and a numeric one are not interchangeable downstream."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as jsonlib

        sent.update(jsonlib.loads(request.content))
        return envelope({"judge_id": "J-1", "feedback_value_type": "bool"})

    with client_with(handler) as caliber:
        judge = caliber.judges.create("json-valid", instructions="Is it valid JSON?")

    assert sent["feedback_value_type"] == "bool"
    assert judge.feedback_value_type == "bool"


def test_alignment_exposes_kappa_separately_from_agreement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"judge_id": "J-1", "agreement": 0.9, "kappa": 0.1, "sample_size": 40})

    with client_with(handler) as caliber:
        alignment = caliber.judges.alignment("J-1")

    # High agreement, near-zero kappa: the judge is agreeing by answering the
    # same way every time, which is exactly what kappa is for.
    assert alignment.agreement == 0.9
    assert alignment.kappa == 0.1


def test_waiting_on_an_evaluation_returns_a_low_score_rather_than_raising() -> None:
    """The score is the measurement; a bad one is not a failed call."""
    states = iter(["running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {"evaluation_id": "EV-1", "status": next(states), "metrics": {"pass_rate": 0.1}}
        )

    with client_with(handler) as caliber:
        evaluation = caliber.evaluations.wait("EV-1", interval=0.001, max_interval=0.001, timeout=5)

    assert evaluation.status == "succeeded"
    assert evaluation.metrics == {"pass_rate": 0.1}


def test_evaluation_terminal_states_include_failure() -> None:
    from caliber_sdk.models import Evaluation

    assert decode(Evaluation, {"status": "failed"}).is_terminal
    assert not decode(Evaluation, {"status": "queued"}).is_terminal


def test_dataset_and_judge_lists_hit_the_documented_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/caliber", 1)[-1])
        return envelope([])

    with client_with(handler) as caliber:
        caliber.eval_datasets.list()
        caliber.judges.list()
        caliber.evaluations.list(dataset_id="ED-1")

    assert seen == ["/eval-datasets", "/judges", "/evaluations"]


def test_running_a_judge_hits_the_real_test_run_route() -> None:
    """Regression test: this method previously POSTed to
    ``/judges/{id}/test``, which no server route serves -- the real route is
    ``POST /judges/{id}/test-run`` (``routes/judges.py``'s ``TEST_RUN_PATH``).
    The SDK<->API coverage gate (``test_sdk_api_coverage.py``) is what caught
    the mismatch; this pins the fix so it cannot silently regress.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path.rsplit('/caliber', 1)[-1]}")
        assert request.read()  # a body was sent, not a bare POST
        return envelope({"score": 1.0, "value": True, "rationale": "matches"})

    with client_with(handler) as caliber:
        result = caliber.judges.test("JDG-1", inputs={"q": "x"}, outputs={"a": "y"})

    assert seen == ["POST /judges/JDG-1/test-run"]
    assert result == {"score": 1.0, "value": True, "rationale": "matches"}
