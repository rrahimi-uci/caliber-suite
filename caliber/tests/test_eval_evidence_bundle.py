"""Regression tests for the immutable evidence bundle on an evaluation run.

The review's finding was that a run persisted its rows but recorded "no
cryptographic content/run digest, full pre-truncation inventory or sampling
decision, or a resolved bundle of skill content/version, prompt content/alias,
draft workflow manifest, judge definition/model, and provider configuration", so
"a pinned run is reproducible by convention, not by proof". It separately recorded
that per-scorer aggregates "do not persist valid-row/weight denominators" and that
"grouped tag/slice metrics" were absent.

Each of those is a distinct claim, tested separately:

* the digests distinguish "same data" from "same result";
* a truncated sample says so, and says how much it left out;
* every aggregate mean carries the denominator it was computed over; and
* the resolved bundle pins *content*, not just a mutable reference.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import caliber.routes.evaluations as evaluations_route
from caliber.db.models import CaliberEvalDataset, CaliberEvalDatasetExample, CaliberEvalRun
from caliber.eval.evidence import (
    dataset_digest,
    scorer_denominators,
    tag_slices,
)
from caliber.routes.evaluations import LIST_PATH

# ---------------------------------------------------------------------------
# Unit level — the derivations, without a route
# ---------------------------------------------------------------------------


def _row(
    *,
    example_id: str,
    score: float,
    passed: bool,
    weight: float = 1.0,
    tags: list[str] | None = None,
    error: str | None = None,
    scores: dict[str, float] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        example_id=example_id,
        score=score,
        passed=passed,
        weight=weight,
        tags=tags or [],
        error=error,
        scores=scores if scores is not None else {"exact_match": score},
    )


def test_the_dataset_digest_ignores_predictions() -> None:
    """ "Was this the same data?" has to be answerable across two runs of
    *different* subjects over one dataset, so the digest must not include the
    prediction."""
    rows = [{"example_id": "a", "input": {"q": 1}, "expected": {"expected": "x"}, "weight": 1.0}]
    with_prediction = [{**rows[0], "prediction": "x"}]
    assert dataset_digest(rows) == dataset_digest(with_prediction)


def test_the_dataset_digest_changes_with_data_weight_or_tags() -> None:
    base = [{"example_id": "a", "input": {"q": 1}, "expected": {"expected": "x"}, "weight": 1.0}]
    assert dataset_digest(base) != dataset_digest([{**base[0], "expected": {"expected": "y"}}])
    # Weight is part of the graded evidence: a re-weighted dataset produces a
    # different verdict from the same rows.
    assert dataset_digest(base) != dataset_digest([{**base[0], "weight": 3.0}])
    assert dataset_digest(base) != dataset_digest([{**base[0], "tags": ["p0"]}])


def test_the_dataset_digest_is_insensitive_to_tag_order() -> None:
    """Tag order is not evidence; treating it as such would make the digest
    spuriously unstable."""
    left = [{"example_id": "a", "input": {}, "expected": {}, "tags": ["b", "a"]}]
    right = [{"example_id": "a", "input": {}, "expected": {}, "tags": ["a", "b"]}]
    assert dataset_digest(left) == dataset_digest(right)


def test_denominators_exclude_errored_rows_and_record_the_weight_sum() -> None:
    """A row with any scorer error is excluded from per-scorer aggregates. That
    policy is only auditable if the resulting denominator is recorded."""
    rows = [
        _row(example_id="a", score=1.0, passed=True, weight=2.0),
        _row(example_id="b", score=0.0, passed=False, weight=3.0),
        _row(example_id="c", score=0.0, passed=False, weight=5.0, error="judge exploded"),
    ]
    denominators = scorer_denominators(rows)
    assert denominators == {"exact_match": {"valid_rows": 2, "weight_sum": 5.0}}


def test_denominators_are_per_scorer() -> None:
    rows = [
        _row(example_id="a", score=1.0, passed=True, scores={"exact_match": 1.0, "token_f1": 0.9}),
        _row(example_id="b", score=1.0, passed=True, scores={"exact_match": 1.0}),
    ]
    denominators = scorer_denominators(rows)
    assert denominators["exact_match"]["valid_rows"] == 2
    assert denominators["token_f1"]["valid_rows"] == 1


def test_tag_slices_are_weighted_and_report_their_own_denominator() -> None:
    rows = [
        _row(example_id="a", score=1.0, passed=True, weight=1.0, tags=["geo"]),
        _row(example_id="b", score=0.0, passed=False, weight=3.0, tags=["math", "p0"]),
        _row(example_id="c", score=1.0, passed=True, weight=1.0, tags=["math"]),
    ]
    slices = tag_slices(rows)
    assert slices["geo"] == {
        "n_examples": 1,
        "weight_sum": 1.0,
        "passed_count": 1,
        "errored_count": 0,
        "overall": 1.0,
        "pass_rate": 1.0,
    }
    # math: weights 3 (score 0) and 1 (score 1) → weighted overall 0.25.
    assert slices["math"]["overall"] == 0.25
    assert slices["math"]["weight_sum"] == 4.0
    assert slices["p0"]["n_examples"] == 1


def test_a_zero_weight_slice_has_no_mean_rather_than_a_zero_one() -> None:
    """Zero weight means *excluded*. Reporting 0.0 would read as "this slice
    scored badly", which is the opposite of what the curator asked for."""
    slices = tag_slices([_row(example_id="a", score=1.0, passed=True, weight=0.0, tags=["skip"])])
    assert slices["skip"]["overall"] is None
    assert slices["skip"]["pass_rate"] is None
    assert slices["skip"]["n_examples"] == 1


def test_slices_count_errored_rows_separately() -> None:
    slices = tag_slices([_row(example_id="a", score=0.0, passed=False, tags=["p0"], error="boom")])
    assert slices["p0"]["errored_count"] == 1
    assert slices["p0"]["passed_count"] == 0


# ---------------------------------------------------------------------------
# Through the route
# ---------------------------------------------------------------------------


def _fake_completion(_config):
    def complete(_system: str, user: str) -> str:
        return {"capital of France": "Paris"}.get(user.strip(), "I don't know")

    return complete


def _seed(session: Session, *, count: int = 3) -> None:
    session.add(
        CaliberEvalDataset(
            dataset_id="ED-ev",
            name="evidence",
            owner="@test",
            status="active",
            version=1,
        )
    )
    session.add(
        CaliberEvalDatasetExample(
            example_id="EX-hit",
            dataset_id="ED-ev",
            dataset_version=1,
            input={"question": "capital of France"},
            expected={"expected": "Paris"},
            weight=1.0,
            tags=["geo", "p0"],
        )
    )
    for index in range(count - 1):
        session.add(
            CaliberEvalDatasetExample(
                example_id=f"EX-miss-{index}",
                dataset_id="ED-ev",
                dataset_version=1,
                input={"question": f"unknown {index}"},
                expected={"expected": "something"},
                weight=1.0,
                tags=["math"],
            )
        )
    session.commit()


def _run(client: TestClient, **overrides: object) -> dict:
    body: dict[str, object] = {"dataset_id": "ED-ev", "scorers": ["exact_match"]}
    body.update(overrides)
    response = client.post(LIST_PATH, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_a_run_persists_its_evidence_bundle(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    data = _run(client)
    evidence = data["evidence"]
    assert evidence is not None
    assert evidence["schema_version"] >= 1
    assert evidence["digests"]["dataset"].startswith("sha256:")
    assert evidence["digests"]["content"].startswith("sha256:")
    assert evidence["sampling"]["available_examples"] == 3
    assert evidence["sampling"]["evaluated_examples"] == 3
    assert evidence["sampling"]["truncated"] is False
    assert evidence["sampling"]["order"]
    assert evidence["denominators"]["exact_match"]["valid_rows"] == 3
    assert set(evidence["slices"]) == {"geo", "p0", "math"}
    assert evidence["policy"]["scorers"] == ["exact_match"]
    assert evidence["resolved"]["dataset_id"] == "ED-ev"
    assert evidence["resolved"]["dataset_version"] == 1
    # Latency is now joined into the run record; it existed only in observability.
    assert evidence["cost"]["avg_latency_ms"] is not None
    assert evidence["cost"]["total_latency_ms"] >= evidence["cost"]["avg_latency_ms"]

    # ...and it is durable, not just returned.
    stored = db_session.get(CaliberEvalRun, data["run_id"])
    assert stored is not None
    assert stored.evidence == evidence


def test_a_truncated_run_discloses_what_it_left_out(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The specific gap: no "full pre-truncation inventory or sampling decision",
    so a bounded run was indistinguishable from an exhaustive one."""
    _seed(db_session, count=5)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)

    data = _run(client, max_examples=2)
    sampling = data["evidence"]["sampling"]
    assert sampling["available_examples"] == 5
    assert sampling["evaluated_examples"] == 2
    assert sampling["cap"] == 2
    assert sampling["truncated"] is True


def test_the_content_digest_moves_when_predictions_change_but_the_dataset_digest_does_not(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This split is what makes the digests useful: it separates "the subject
    changed" from "the data changed"."""
    _seed(db_session, count=2)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    first = _run(client)["evidence"]["digests"]

    def _different(_config):
        def complete(_system: str, _user: str) -> str:
            return "a completely different answer"

        return complete

    monkeypatch.setattr(evaluations_route, "build_completion_fn", _different)
    second = _run(client)["evidence"]["digests"]

    assert first["dataset"] == second["dataset"]  # same data...
    assert first["content"] != second["content"]  # ...different result


def test_the_dataset_digest_moves_when_an_expected_answer_is_edited(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, count=2)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    first = _run(client)["evidence"]["digests"]["dataset"]

    example = db_session.get(CaliberEvalDatasetExample, "EX-hit")
    assert example is not None
    example.expected = {"expected": "Lyon"}
    db_session.commit()

    second = _run(client)["evidence"]["digests"]["dataset"]
    assert first != second


def test_the_resolved_bundle_pins_a_judges_definition_not_just_its_id(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Judges are mutable and unversioned, so a stored ``Judge.<id>`` token cannot
    prove which definition graded a historical run."""
    from caliber.db.models import CaliberJudge

    _seed(db_session, count=1)
    db_session.add(
        CaliberJudge(
            judge_id="JD-1",
            name="tone",
            instructions="Rate the tone of {{ outputs }} from 0 to 1.",
            model="openai:/gpt-4o-mini",
            owner="@test",
            status="active",
        )
    )
    db_session.commit()
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    monkeypatch.setattr(
        evaluations_route,
        "_make_judge_runner",
        lambda _judge: lambda **_kwargs: 1.0,
    )

    data = _run(client, scorers=["exact_match", "Judge.JD-1"])
    judges = data["evidence"]["resolved"]["judges"]
    assert judges is not None
    entry = judges["Judge.JD-1"]
    assert entry["judge_id"] == "JD-1"
    assert entry["model"] == "openai:/gpt-4o-mini"
    assert entry["instructions_digest"].startswith("sha256:")


def test_the_resolved_bundle_records_the_provider(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db_session, count=1)
    monkeypatch.setattr(evaluations_route, "build_completion_fn", _fake_completion)
    resolved = _run(client)["evidence"]["resolved"]
    assert "llm_provider" in resolved
    assert resolved["predict_target"] == "llm"
