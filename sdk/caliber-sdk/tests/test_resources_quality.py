"""Dataset, judge, evaluation, and verification-queue resource modules."""

from __future__ import annotations

import json as jsonlib
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


# ---------------------------------------------------------------------------
# Verification queue -- Stage ① Verify
# ---------------------------------------------------------------------------


def test_verification_queue_list_sends_only_provided_filters() -> None:
    """``status`` defaults to ``pending``; omitted filters are not sent as
    empty query params, matching the server's ``request.query_params.get``
    handling (an empty string would not equal ``None`` there)."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return envelope([])

    with client_with(handler) as caliber:
        caliber.verification_queue.list()

    assert seen == {"status": "pending"}


def test_verification_queue_list_decodes_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/verification-queue")
        return envelope(
            [{"item_id": "FB-1", "agent_id": "support-agent", "status": "pending"}]
        )

    with client_with(handler) as caliber:
        items = caliber.verification_queue.list(status="all")

    assert len(items) == 1
    assert items[0].item_id == "FB-1"
    assert items[0].status == "pending"


def test_verification_queue_create_posts_the_flagged_concern() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(jsonlib.loads(request.content))
        assert request.url.path.endswith("/verification-queue")
        return envelope(
            {
                "item_id": "FB-1",
                "agent_id": "support-agent",
                "category": "hallucination",
                "status": "pending",
            }
        )

    with client_with(handler) as caliber:
        item = caliber.verification_queue.create(
            "support-agent", category="hallucination", free_text="Cited a fake policy."
        )

    assert sent["agent_id"] == "support-agent"
    assert sent["free_text"] == "Cited a fake policy."
    assert item.item_id == "FB-1"
    assert item.status == "pending"


def test_verification_queue_verify_decodes_the_nested_item_and_ignores_job() -> None:
    """The server's ``VerifyResponse`` envelope is ``{item, job}``; ``job`` is
    always ``None`` today (verifying an item does not create one -- see
    ``caliber/src/caliber/routes/verification.py``). The SDK method decodes
    ``item`` and does not expose ``job`` at all, rather than surface a field
    that never carries anything yet."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/verify")
        return envelope(
            {
                "item": {
                    "item_id": "FB-1",
                    "status": "verified",
                    "verified_by": "@sarah",
                },
                "job": None,
            }
        )

    with client_with(handler) as caliber:
        item = caliber.verification_queue.verify("FB-1", verification_notes="confirmed")

    assert item.item_id == "FB-1"
    assert item.status == "verified"
    assert item.verified_by == "@sarah"


def test_verification_queue_dismiss_and_mark_duplicate_hit_distinct_routes() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/caliber", 1)[-1])
        return envelope({"item_id": "FB-1", "status": "dismissed"})

    with client_with(handler) as caliber:
        caliber.verification_queue.dismiss("FB-1", reason="not real")
        caliber.verification_queue.mark_duplicate("FB-2", "FB-1")

    assert seen == [
        "/verification-queue/FB-1/dismiss",
        "/verification-queue/FB-2/duplicate",
    ]


def test_verification_queue_batch_reports_per_item_results() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(jsonlib.loads(request.content))
        assert request.url.path.endswith("/verification-queue/batch")
        return envelope(
            {
                "action": "verify",
                "requested": 2,
                "succeeded": 1,
                "failed": 1,
                "results": [
                    {
                        "item_id": "FB-1",
                        "status": "succeeded",
                        "reason": None,
                        "linked_job_id": None,
                    },
                    {
                        "item_id": "FB-2",
                        "status": "failed",
                        "reason": "already verified",
                        "linked_job_id": None,
                    },
                ],
            }
        )

    with client_with(handler) as caliber:
        result = caliber.verification_queue.batch("verify", ["FB-1", "FB-2"])

    assert sent == {"action": "verify", "item_ids": ["FB-1", "FB-2"]}
    assert result.succeeded == 1
    assert result.failed == 1
    assert result.results[0]["item_id"] == "FB-1"
    assert result.results[1]["reason"] == "already verified"
