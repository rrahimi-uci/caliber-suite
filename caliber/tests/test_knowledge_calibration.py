"""Tests for knowledge-base calibration (Phase K1).

Two layers:

* **Deterministic metric math** — :func:`recall_at_k` and :func:`ndcg_at_k`
  against hand-computed expectations (perfect / partial / zero / order-matters),
  with no LLM, DB, or HTTP. The order-matters case proves nDCG ≠ precision.
* **End-to-end calibrate run** through the HTTP route with the LLM judge mocked
  (a deterministic fake completion fn) and a tiny real KB version built from the
  ``_DummyEmbedder`` fixture pattern. Covers persistence, the summary-vs-detail
  split, the baseline route, and the 404/400 error branches.
"""

from __future__ import annotations

import math

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

import caliber.knowledge.service as knowledge_service
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberKnowledgeBaseTestRun,
)
from caliber.knowledge import calibration

from .test_routes_knowledge_bases import (  # reuse the moto/embedder helpers
    KB,
    PREFIX,
    _DummyEmbedder,
    _put_text,
    _wire_moto,
    mock_aws,
)

CALIBRATE = PREFIX + "/knowledge-bases/{kb}/calibrate"
TEST_RUNS = PREFIX + "/knowledge-bases/{kb}/test-runs"
BASELINE = PREFIX + "/knowledge-bases/{kb}/baseline"
TEST_RUN_DETAIL = PREFIX + "/knowledge/test-runs/{run}"


def _sources(*keys: str) -> list[set[str]]:
    """Shape a top-k ranking as the calibration math expects: one set per rank."""
    return [{key} for key in keys]


# ---------------------------------------------------------------------------
# Deterministic Recall@k — pure, no LLM.
# ---------------------------------------------------------------------------


def test_recall_at_k_perfect() -> None:
    # Both gold sources retrieved within the top-3 → full recall.
    retrieved = _sources("a", "b", "c")
    assert calibration.recall_at_k(retrieved, ["a", "b"], 3) == 1.0


def test_recall_at_k_partial() -> None:
    # 1 of 2 gold sources present → 0.5, regardless of the non-gold noise.
    retrieved = _sources("a", "x", "y")
    assert calibration.recall_at_k(retrieved, ["a", "b"], 3) == 0.5


def test_recall_at_k_zero_when_none_retrieved() -> None:
    retrieved = _sources("x", "y", "z")
    assert calibration.recall_at_k(retrieved, ["a", "b"], 3) == 0.0


def test_recall_at_k_respects_cutoff() -> None:
    # Gold "b" sits at rank 4, outside top_k=3 → only "a" counts → 0.5.
    retrieved = _sources("a", "x", "y", "b")
    assert calibration.recall_at_k(retrieved, ["a", "b"], 3) == 0.5
    # Widen the cutoff and both are covered.
    assert calibration.recall_at_k(retrieved, ["a", "b"], 4) == 1.0


def test_recall_at_k_dedupes_gold_and_repeats() -> None:
    # Duplicate gold entries collapse; the denominator is the distinct gold set.
    retrieved = _sources("a", "a", "x")
    assert calibration.recall_at_k(retrieved, ["a", "a"], 3) == 1.0


def test_recall_at_k_empty_gold_raises() -> None:
    with pytest.raises(ValueError, match="non-empty gold set"):
        calibration.recall_at_k(_sources("a"), [], 3)


# ---------------------------------------------------------------------------
# Deterministic nDCG@k — pure, no LLM. Ranking-sensitive (≠ precision).
# ---------------------------------------------------------------------------


def test_ndcg_at_k_perfect_ranking() -> None:
    # Relevant chunks first → DCG == IDCG → 1.0.
    retrieved = _sources("a", "b", "x")
    assert calibration.ndcg_at_k(retrieved, ["a", "b"], 3) == pytest.approx(1.0)


def test_ndcg_at_k_order_matters_differs_from_precision() -> None:
    # Same TWO relevant chunks in both orderings → identical precision@3 (2/3),
    # but nDCG rewards the earlier placement, so the two scores differ.
    good = _sources("a", "b", "x")  # relevant at ranks 1,2
    bad = _sources("x", "a", "b")  # relevant at ranks 2,3
    gold = ["a", "b"]

    score_good = calibration.ndcg_at_k(good, gold, 3)
    score_bad = calibration.ndcg_at_k(bad, gold, 3)

    # Hand-computed: IDCG = 1/log2(2) + 1/log2(3) = 1 + 0.63093 = 1.63093.
    idcg = 1.0 + 1.0 / math.log2(3)
    # good DCG = 1/log2(2) + 1/log2(3) = IDCG → 1.0
    assert score_good == pytest.approx(1.0)
    # bad DCG = 1/log2(3) + 1/log2(4) = 0.63093 + 0.5 = 1.13093.
    bad_dcg = 1.0 / math.log2(3) + 1.0 / math.log2(4)
    assert score_bad == pytest.approx(bad_dcg / idcg)
    # Same relevant set, worse ordering → strictly lower nDCG (precision would tie).
    assert score_bad < score_good


def test_ndcg_at_k_single_relevant_at_rank_two() -> None:
    # One gold source, retrieved at rank 2. DCG = 1/log2(3); IDCG = 1/log2(2)=1.
    retrieved = _sources("x", "a", "y")
    expected = (1.0 / math.log2(3)) / 1.0
    assert calibration.ndcg_at_k(retrieved, ["a"], 3) == pytest.approx(expected)


def test_ndcg_at_k_zero_when_none_relevant() -> None:
    retrieved = _sources("x", "y", "z")
    assert calibration.ndcg_at_k(retrieved, ["a"], 3) == 0.0


def test_ndcg_at_k_idcg_capped_by_topk() -> None:
    # 3 gold sources but only top_k=2 considered → IDCG uses 2 ideal hits, and a
    # perfect-within-cutoff ranking still scores 1.0.
    retrieved = _sources("a", "b")
    assert calibration.ndcg_at_k(retrieved, ["a", "b", "c"], 2) == pytest.approx(1.0)


def test_ndcg_at_k_empty_gold_raises() -> None:
    with pytest.raises(ValueError, match="non-empty gold set"):
        calibration.ndcg_at_k(_sources("a"), [], 3)


def test_ndcg_at_k_stays_bounded_when_chunks_share_a_source() -> None:
    """Regression (#4): when several retrieved chunks match the SAME gold
    source (the normal RAG case — one document, many chunks), nDCG must stay in
    [0, 1]. Previously IDCG was capped at the gold-set size, so DCG > IDCG and
    nDCG climbed above 1.0 (~2.13 for three chunks of one source)."""
    retrieved = [{"a"}, {"a"}, {"a"}]  # three chunks, all from gold source 'a'
    score = calibration.ndcg_at_k(retrieved, ["a"], 3)
    assert score <= 1.0
    # All three relevant chunks are retrieved first → a perfect ranking → 1.0.
    assert score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Source normalization + matching + parsing helpers.
# ---------------------------------------------------------------------------


def test_normalize_source_strips_and_lowercases() -> None:
    assert calibration.normalize_source("  ./Docs/FAQ.md ") == "docs/faq.md"
    assert calibration.normalize_source(None) == ""


def test_chunk_source_identifiers_matches_any_of_three_fields() -> None:
    chunk = {
        "source_key": "Product/Guide.md",
        "source_name": "Guide",
        "object_store_path": "/object-store?bucket=b&key=Product/Guide.md",
    }
    ids = calibration.chunk_source_identifiers(chunk)
    assert "product/guide.md" in ids
    assert "guide" in ids
    # A gold source cited by display name still matches.
    assert calibration.recall_at_k([ids], ["guide"], 1) == 1.0


def test_parse_score_handles_plain_fraction_and_percentage() -> None:
    assert calibration.parse_score("0.8") == pytest.approx(0.8)
    assert calibration.parse_score("score: 0.42/1") == pytest.approx(0.42)
    assert calibration.parse_score("85") == pytest.approx(0.85)  # percentage form
    assert calibration.parse_score("nonsense") is None
    assert calibration.parse_score("") is None
    assert calibration.parse_score("3.0") == 1.0  # clamped


# ---------------------------------------------------------------------------
# Verdict + aggregation over a mixed metric set.
# ---------------------------------------------------------------------------


def test_derive_verdict_thresholds() -> None:
    assert calibration.derive_verdict([0.9, 0.8]) == ("pass", pytest.approx(0.85))
    assert calibration.derive_verdict([0.5, 0.5]) == ("partial", pytest.approx(0.5))
    assert calibration.derive_verdict([0.1, 0.2]) == ("fail", pytest.approx(0.15))
    # No defined metric at all → fail with a None composite (nothing to score).
    assert calibration.derive_verdict([None, None]) == ("fail", None)


def test_aggregate_metrics_averages_only_defined() -> None:
    runner_outcome = calibration.RetrievalOutcome(
        answer="x",
        retrieved_sources=_sources("a"),
        retrieved_chunk_texts=["a"],
        retrieved_source_keys=["a"],
    )
    s1 = calibration.score_question(
        question="q1",
        expected={"sources": ["a"]},
        outcome=runner_outcome,
        top_k=3,
        judge=None,
    )
    s2 = calibration.score_question(
        question="q2",
        expected=None,  # no gold → recall/ndcg both None
        outcome=runner_outcome,
        top_k=3,
        judge=None,
    )
    agg = calibration.aggregate_metrics([s1, s2])
    # recall/ndcg defined only on q1 (== 1.0); averaged over the 1 defined question.
    assert agg["recall_at_k"] == pytest.approx(1.0)
    assert agg["ndcg_at_k"] == pytest.approx(1.0)
    # No judge → answer metrics undefined across the board.
    assert agg["faithfulness"] is None
    assert agg["answer_correctness"] is None
    assert agg["question_count"] == 2


# ---------------------------------------------------------------------------
# run_calibration with a fully stubbed runner + judge (no DB / HTTP).
# ---------------------------------------------------------------------------


def test_run_calibration_with_stub_runner_and_judge() -> None:
    def runner(question: str, top_k: int, mode: str) -> calibration.RetrievalOutcome:
        return calibration.RetrievalOutcome(
            answer=f"answer to {question}",
            retrieved_sources=_sources("faq.txt", "guide.md"),
            retrieved_chunk_texts=["context one", "context two"],
            retrieved_source_keys=["faq.txt", "guide.md"],
        )

    # Faithfulness scores 0.9; correctness scores 0.6 — via the unified KbJudge.
    judge = calibration.KbJudge(
        faithfulness_judge=lambda **_kw: _FakeFeedback(0.9),
        correctness_judge=lambda **_kw: _FakeFeedback(0.6),
    )

    outcome = calibration.run_calibration(
        questions=[
            ("q1", {"sources": ["faq.txt"], "answer": "gold"}),
            ("q2", {"sources": ["missing.txt"]}),
        ],
        runner=runner,
        top_k=3,
        retrieval_mode="dense",
        judge=judge,
    )
    assert len(outcome.results) == 2
    # q1: recall=1.0 (faq retrieved), faithfulness=0.9, correctness=0.6.
    q1 = outcome.results[0]
    assert q1["recall_at_k"] == pytest.approx(1.0)
    assert q1["faithfulness"] == pytest.approx(0.9)
    assert q1["answer_correctness"] == pytest.approx(0.6)
    # q2: gold source not retrieved → recall 0; no gold answer → correctness None.
    q2 = outcome.results[1]
    assert q2["recall_at_k"] == pytest.approx(0.0)
    assert q2["answer_correctness"] is None
    assert outcome.metrics["question_count"] == 2


# ---------------------------------------------------------------------------
# Eval-dataset seeding for the route tests.
# ---------------------------------------------------------------------------


def _seed_dataset(
    session: Session,
    *,
    dataset_id: str = "ED-kbcal",
    examples: list[dict[str, object]] | None = None,
    version: int = 1,
) -> None:
    session.add(
        CaliberEvalDataset(
            dataset_id=dataset_id,
            name=f"kb-calibration-{dataset_id}",
            owner="@test",
            version=version,
        )
    )
    for index, example in enumerate(examples or []):
        session.add(
            CaliberEvalDatasetExample(
                example_id=f"{dataset_id}-EX{index}",
                dataset_id=dataset_id,
                dataset_version=int(example.get("dataset_version", 1)),  # type: ignore[arg-type]
                input=example["input"],
                expected=example.get("expected", {}),
            )
        )
    session.commit()


def _build_kb(client: TestClient) -> str:
    """Build a tiny completed KB version with two known source keys; return KB id."""
    s3 = _wire_moto(client)
    bucket = "kb-cal-docs"
    s3.create_bucket(Bucket=bucket)
    _put_text(
        s3,
        bucket,
        "guide.md",
        "Retries happen three times before an alert is sent to the on-call engineer.\n",
        content_type="text/markdown",
    )
    _put_text(
        s3,
        bucket,
        "faq.txt",
        "Dark mode applies consistently across every linked tool in the workspace.\n",
    )
    create = client.post(
        KB,
        json={
            "name": "Calibration Docs",
            "description": "KB for calibration tests",
            "source_bucket": bucket,
            "sources": [
                {"kind": "file", "path": "guide.md"},
                {"kind": "file", "path": "faq.txt"},
            ],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 200, "chunk_overlap": 20},
        },
    )
    assert create.status_code == 201, create.text
    data = create.json()["data"]
    return data["knowledge_base"]["knowledge_base_id"]


def _kb_version_id(client: TestClient, kb_id: str) -> str:
    versions = client.get(f"{KB}/{kb_id}/versions")
    assert versions.status_code == 200, versions.text
    return versions.json()["data"][0]["knowledge_base_version_id"]


class _FakeFeedback:
    """Minimal stand-in for an mlflow ``Feedback`` (carries ``.value``)."""

    def __init__(self, value: object) -> None:
        self.value = value
        self.rationale = None


@pytest.fixture
def _fake_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the unified judge builder to a deterministic fake ``KbJudge``.

    Faithfulness scores 0.9; answer-correctness scores 0.5. The service now
    builds judges via :func:`caliber.knowledge.calibration.build_kb_judge` (the
    unified ``make_judge`` path), so we patch that to return fake judge objects
    rather than faking a raw completion fn.
    """
    fake = calibration.KbJudge(
        faithfulness_judge=lambda **_kw: _FakeFeedback(0.9),
        correctness_judge=lambda **_kw: _FakeFeedback(0.5),
    )
    monkeypatch.setattr(calibration, "build_kb_judge", lambda model=None: fake)


# ---------------------------------------------------------------------------
# End-to-end calibrate run through the HTTP route.
# ---------------------------------------------------------------------------


@mock_aws
def test_calibrate_persists_run_and_lists_and_details(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    version_id = _kb_version_id(client, kb_id)

    with session_factory() as session:
        _seed_dataset(
            session,
            examples=[
                {
                    "input": {"question": "How many retries before an alert?"},
                    "expected": {"sources": ["guide.md"], "answer": "Three retries."},
                },
                {
                    "input": {"question": "Where does dark mode apply?"},
                    "expected": {"sources": ["faq.txt"], "answer": "Across linked tools."},
                },
            ],
        )

    calibrate = client.post(
        CALIBRATE.format(kb=kb_id),
        json={
            "version_id": version_id,
            "eval_dataset_id": "ED-kbcal",
            "retrieval_mode": "dense",
            "top_k": 3,
        },
    )
    assert calibrate.status_code == 201, calibrate.text
    summary = calibrate.json()["data"]

    assert summary["knowledge_base_id"] == kb_id
    assert summary["knowledge_base_version_id"] == version_id
    assert summary["eval_dataset_id"] == "ED-kbcal"
    assert summary["test_set_size"] == 2
    assert summary["retrieval_mode"] == "dense"
    assert summary["top_k"] == 3
    metrics = summary["metrics"]
    # All four aggregate metrics present; answer metrics come from the fake judge.
    assert set(metrics) >= {
        "recall_at_k",
        "ndcg_at_k",
        "faithfulness",
        "answer_correctness",
    }
    assert metrics["faithfulness"] == pytest.approx(0.9)
    assert metrics["answer_correctness"] == pytest.approx(0.5)
    assert metrics["recall_at_k"] is not None
    assert metrics["ndcg_at_k"] is not None
    # Summary carries no heavy per-question array.
    assert "results" not in summary

    run_id = summary["test_run_id"]
    assert run_id.startswith("KBTR-")

    # Durable row persisted.
    with session_factory() as session:
        row = session.get(CaliberKnowledgeBaseTestRun, run_id)
        assert row is not None
        assert row.test_set_size == 2
        assert len(row.results) == 2

    # List endpoint: newest-first summaries, no results.
    listing = client.get(TEST_RUNS.format(kb=kb_id))
    assert listing.status_code == 200, listing.text
    rows = listing.json()["data"]
    assert len(rows) == 1
    assert rows[0]["test_run_id"] == run_id
    assert "results" not in rows[0]

    # Detail endpoint: full per-question results.
    detail = client.get(TEST_RUN_DETAIL.format(run=run_id))
    assert detail.status_code == 200, detail.text
    detail_data = detail.json()["data"]
    assert len(detail_data["results"]) == 2
    first = detail_data["results"][0]
    assert set(first) >= {
        "question",
        "recall_at_k",
        "ndcg_at_k",
        "faithfulness",
        "answer_correctness",
        "verdict",
        "retrieved_sources",
    }


@mock_aws
def test_baseline_set_and_reflected(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    version_id = _kb_version_id(client, kb_id)
    with session_factory() as session:
        _seed_dataset(
            session,
            examples=[
                {
                    "input": {"question": "How many retries before an alert?"},
                    "expected": {"sources": ["guide.md"], "answer": "Three."},
                }
            ],
        )
    calibrate = client.post(
        CALIBRATE.format(kb=kb_id),
        json={"version_id": version_id, "eval_dataset_id": "ED-kbcal"},
    )
    assert calibrate.status_code == 201, calibrate.text
    run_id = calibrate.json()["data"]["test_run_id"]

    baseline = client.post(BASELINE.format(kb=kb_id), json={"test_run_id": run_id})
    assert baseline.status_code == 200, baseline.text
    assert baseline.json()["data"]["baseline_run_id"] == run_id

    # Reflected on the KB detail.
    detail = client.get(f"{KB}/{kb_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["baseline_run_id"] == run_id


@mock_aws
def test_baseline_rejects_run_from_other_kb(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    version_id = _kb_version_id(client, kb_id)
    with session_factory() as session:
        _seed_dataset(
            session,
            examples=[
                {
                    "input": {"question": "How many retries before an alert?"},
                    "expected": {"sources": ["guide.md"]},
                }
            ],
        )
    calibrate = client.post(
        CALIBRATE.format(kb=kb_id),
        json={"version_id": version_id, "eval_dataset_id": "ED-kbcal"},
    )
    run_id = calibrate.json()["data"]["test_run_id"]

    # Insert a second KB row, then try to pin the first KB's run onto it.
    other_kb_id = "KB-otherkb"
    with session_factory() as session:
        from caliber.db.models import CaliberKnowledgeBase

        session.add(
            CaliberKnowledgeBase(
                knowledge_base_id=other_kb_id,
                name="Other KB",
                owner="@test",
                source_bucket="kb-cal-docs",
            )
        )
        session.commit()

    wrong = client.post(BASELINE.format(kb=other_kb_id), json={"test_run_id": run_id})
    assert wrong.status_code == 400, wrong.text


# ---------------------------------------------------------------------------
# Error branches: missing dataset/version (404), empty dataset (400).
# ---------------------------------------------------------------------------


@mock_aws
def test_calibrate_missing_dataset_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    version_id = _kb_version_id(client, kb_id)
    resp = client.post(
        CALIBRATE.format(kb=kb_id),
        json={"version_id": version_id, "eval_dataset_id": "ED-missing"},
    )
    assert resp.status_code == 404, resp.text


@mock_aws
def test_calibrate_missing_version_404(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    with session_factory() as session:
        _seed_dataset(
            session,
            examples=[{"input": {"question": "q"}, "expected": {"sources": ["guide.md"]}}],
        )
    resp = client.post(
        CALIBRATE.format(kb=kb_id),
        json={"version_id": "KBV-missing", "eval_dataset_id": "ED-kbcal"},
    )
    assert resp.status_code == 404, resp.text


@mock_aws
def test_calibrate_empty_dataset_400(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    version_id = _kb_version_id(client, kb_id)
    with session_factory() as session:
        _seed_dataset(session, examples=[])  # dataset exists but has no examples
    resp = client.post(
        CALIBRATE.format(kb=kb_id),
        json={"version_id": version_id, "eval_dataset_id": "ED-kbcal"},
    )
    assert resp.status_code == 400, resp.text


@mock_aws
def test_calibrate_requires_operator_scope(
    client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    _fake_judge: None,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    kb_id = _build_kb(client)
    version_id = _kb_version_id(client, kb_id)
    with session_factory() as session:
        _seed_dataset(
            session,
            examples=[{"input": {"question": "q"}, "expected": {"sources": ["guide.md"]}}],
        )
    # A non-admin user lacks the operator scope → 403.
    resp = client.post(
        CALIBRATE.format(kb=kb_id),
        json={"version_id": version_id, "eval_dataset_id": "ED-kbcal"},
        headers={"X-CALIBER-User": "@viewer"},
    )
    assert resp.status_code == 403, resp.text
