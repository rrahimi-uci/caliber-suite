"""Unit tests for the cross-encoder rerank stage (``KnowledgeBaseService._rerank``).

The reranker model is faked (no torch/sentence-transformers needed): the runtime
guard and ``build_reranker_backend`` are monkeypatched so only the reorder + score
bookkeeping logic is under test.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

import caliber.knowledge.service as knowledge_service
from caliber.config import CaliberConfig
from caliber.knowledge.service import KnowledgeBaseService, _RetrievedChunk


class _FakeReranker:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def rerank_scores(self, _query: str, passages: list[str]) -> list[float]:
        return self._scores[: len(passages)]


class _Chunk:
    """Stand-in for a CaliberKnowledgeBaseChunk (``_rerank`` only reads two attrs)."""

    def __init__(self, chunk_id: str, content: str) -> None:
        self.knowledge_base_chunk_id = chunk_id
        self.content = content


def _item(chunk_id: str, content: str, score: float) -> _RetrievedChunk:
    return _RetrievedChunk(
        chunk=_Chunk(chunk_id, content),  # type: ignore[arg-type]
        score=score,
        dense_score=score,
        score_breakdown={"dense": score},
    )


def _service(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    **overrides: Any,
) -> KnowledgeBaseService:
    cfg = app_config.model_copy(update=overrides)
    return KnowledgeBaseService(config=cfg, session_factory=session_factory)


def test_rerank_reorders_by_cross_encoder_and_records_scores(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service, "ensure_embedding_backend_runtime_available", lambda **_kw: None
    )
    # First-stage order [a, b, c]; reranker prefers b > c > a.
    monkeypatch.setattr(
        knowledge_service,
        "build_reranker_backend",
        lambda _model: _FakeReranker([0.1, 0.9, 0.5]),
    )
    svc = _service(app_config, session_factory, knowledge_rerank_enabled=True)
    items = [
        _item("a", "alpha", 0.9),
        _item("b", "beta", 0.5),
        _item("c", "gamma", 0.3),
    ]
    out = svc._rerank("q", items, top_k=2)

    # Reordered by the reranker and narrowed to top_k.
    assert [it.chunk.knowledge_base_chunk_id for it in out] == ["b", "c"]
    # Top item's score is the min-max-normalized rerank score; breakdown carries
    # both the raw rerank score and the preserved first-stage retrieval score.
    assert out[0].score == 1.0
    assert out[0].score_breakdown["rerank"] == 0.9
    assert out[0].score_breakdown["retrieval"] == 0.5


def test_rerank_is_noop_when_disabled(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
) -> None:
    svc = _service(app_config, session_factory, knowledge_rerank_enabled=False)
    items = [_item("a", "x", 0.5), _item("b", "y", 0.4)]
    # No reranker built (disabled) → returns the first-stage order, sliced to top_k.
    assert svc._rerank("q", items, top_k=5) == items
    assert [it.chunk.knowledge_base_chunk_id for it in svc._rerank("q", items, top_k=1)] == ["a"]


def test_reranker_backend_none_when_disabled(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    svc = _service(app_config, session_factory, knowledge_rerank_enabled=False)
    assert svc._reranker_backend() is None
