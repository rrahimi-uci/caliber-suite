"""Dataset, judge, evaluation, and verification-queue resource modules."""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models import EvalDataset, Evaluation, decode

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
        return envelope([{"item_id": "FB-1", "agent_id": "support-agent", "status": "pending"}])

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


def test_evaluation_decodes_the_scorecard_run_the_server_returns() -> None:
    """The server names a scorecard run ``run_id`` and returns its scores inline."""
    run = decode(
        Evaluation,
        {
            "run_id": "EVR-1",
            "status": "completed",
            "n_examples": 44,
            "pass_rate": 0.5,
            "overall_score": 0.4,
            "aggregate": {"token_f1": 0.4},
            "results": [{"prediction": "{}"}],
        },
    )
    assert run.run_id == "EVR-1"
    assert run.is_terminal
    assert run.aggregate == {"token_f1": 0.4}
    assert run.extra == {}


def test_audit_list_reads_the_servers_entries_key() -> None:
    """``GET /audit-log`` answers ``{"entries": [...], "total": ...}``; it used to read ``items``
    and so always returned an empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["entity_id"] == "p1"
        return envelope(
            {
                "entries": [
                    {
                        "log_id": 7,
                        "timestamp": "2026-10-16T18:44:34",
                        "actor": "admin",
                        "action": "promote_prompt",
                        "entity_type": "prompt",
                        "entity_id": "p1",
                        "severity": "standard",
                        "details": {"to_version": 2},
                    }
                ],
                "total": 1,
                "limit": 50,
                "offset": 0,
            }
        )

    entries = client_with(handler).audit.list(entity_id="p1")
    assert [(e.log_id, e.actor, e.action, e.details["to_version"]) for e in entries] == [
        (7, "admin", "promote_prompt", 2)
    ]
    assert entries[0].extra == {}


def test_judge_alignment_reads_the_servers_agreement_and_kappa() -> None:
    """``POST /judges/{id}/alignment`` answers ``agreement_rate``/``cohen_kappa`` plus how many
    rows were scored; the old ``agreement``/``kappa`` names left every metric empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {
                "n": 50,
                "scored": 50,
                "agreement_rate": 0.82,
                "cohen_kappa": 0.6365,
                "threshold": 0.5,
                "confusion": {"true_pos": 23, "false_pos": 5, "true_neg": 18, "false_neg": 4},
                "per_example": [{"outputs": "a", "human_label": True, "agree": True}],
            }
        )

    report = client_with(handler).judges.alignment(
        "J-1", examples=[{"outputs": "a", "label": True}]
    )
    assert (report.n, report.scored) == (50, 50)
    assert (report.agreement_rate, report.cohen_kappa) == (0.82, 0.6365)
    assert report.confusion["false_pos"] == 5
    assert report.per_example[0]["agree"] is True
    assert report.extra == {}


def test_eval_example_and_review_queue_decode_the_servers_field_names() -> None:
    from caliber_sdk.models import EvalExample, ReviewQueue

    example = decode(EvalExample, {"example_id": "EX-1", "input": {"q": "a"}, "expected": {}})
    assert example.input == {"q": "a"} and example.extra == {}
    queue = decode(
        ReviewQueue,
        {"queue_id": "RVQ-1", "questions": [{"key": "faithful"}], "reviewers": [], "item_count": 3},
    )
    assert queue.questions == [{"key": "faithful"}]
    assert queue.item_count == 3


def test_review_queue_get_unwraps_the_servers_queue_and_items() -> None:
    """``GET /review-queues/{id}`` answers ``{"queue": {...}, "items": [...]}``; decoding the whole
    payload as a queue left every field empty (name, status and questions all blank)."""
    payload = {
        "queue": {
            "queue_id": "RVQ-1",
            "name": "agent-reply-review",
            "status": "active",
            "questions": [{"key": "safety"}],
            "item_count": 2,
        },
        "items": [{"item_id": "RVI-1", "status": "pending"}, {"item_id": "RVI-2"}],
    }
    queue = client_with(lambda request: envelope(payload)).review_queues.get("RVQ-1")
    assert (queue.queue_id, queue.name, queue.status) == ("RVQ-1", "agent-reply-review", "active")
    assert queue.questions == [{"key": "safety"}] and queue.item_count == 2
    assert [item["item_id"] for item in queue.extra["items"]] == ["RVI-1", "RVI-2"]


def test_review_queue_get_still_decodes_a_flat_queue() -> None:
    queue = client_with(
        lambda request: envelope({"queue_id": "RVQ-2", "name": "flat", "status": "active"})
    ).review_queues.get("RVQ-2")
    assert (queue.queue_id, queue.name) == ("RVQ-2", "flat") and "items" not in queue.extra


def test_observability_traces_reads_the_servers_traces_key() -> None:
    """``GET /observability/traces`` answers ``{"traces": [...]}``; reading ``items`` meant the list
    was always empty."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope(
            {"traces": [{"trace_id": "tr-1", "status": "OK", "name": "recorded-output"}]}
        )

    traces = client_with(handler).observability.traces(limit=1)
    assert [t.trace_id for t in traces] == ["tr-1"]
    assert traces[0].extra["name"] == "recorded-output"
