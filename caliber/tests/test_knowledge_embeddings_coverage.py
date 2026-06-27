"""Coverage for the embedding/reranker backends in ``caliber.knowledge.embeddings``.

No real model is ever downloaded: the LangChain embeddings class and the
``sentence_transformers.CrossEncoder`` boundary are replaced with in-process
fakes, exactly like the sibling tests fake the backend (see
``tests/test_routes_knowledge_bases.py`` ``_DummyEmbedder`` and
``tests/test_knowledge_rerank.py`` ``_FakeReranker``). This exercises the
backend wiring, dimension probing, batching, vector coercion and the
empty-input guards.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

import caliber.knowledge.embeddings as knowledge_embeddings
from caliber.knowledge.embeddings import (
    CrossEncoderRerankerBackend,
    HuggingFaceEmbeddingBackend,
    _build_cross_encoder,
    _build_langchain_backend,
    _coerce_vector,
    build_embedding_backend,
    build_reranker_backend,
)


class _FakeLangChainEmbeddings:
    """Stand-in for ``langchain_huggingface.HuggingFaceEmbeddings``."""

    def __init__(self, *, model_name: str, model_kwargs: dict, encode_kwargs: dict) -> None:
        self.model_name = model_name
        self.model_kwargs = model_kwargs
        self.encode_kwargs = encode_kwargs

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        # Return a 3-d vector per text so ``dimension`` is deterministic, and
        # deliberately yield non-``float`` (int) members so ``_coerce_vector``
        # has real work to do.
        return [[len(text), 0, 1] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return [len(text), 0, 1]


class _FakeCrossEncoder:
    """Stand-in for ``sentence_transformers.CrossEncoder``."""

    def __init__(self, model_id: str, device: str = "cpu") -> None:
        self.model_id = model_id
        self.device = device

    def predict(self, pairs: list[tuple[str, str]]) -> list[Any]:
        # Return numpy-free, non-``float`` scores (ints) so ``rerank_scores``
        # exercises its ``float(score)`` coercion.
        return [len(passage) for _query, passage in pairs]


@pytest.fixture
def _fake_langchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the HuggingFace embeddings class with the in-process fake."""

    monkeypatch.setattr(
        knowledge_embeddings,
        "_import_huggingface_embeddings_class",
        lambda: _FakeLangChainEmbeddings,
    )


@pytest.fixture
def _fake_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind ``from sentence_transformers import CrossEncoder`` to the fake."""

    monkeypatch.setattr("sentence_transformers.CrossEncoder", _FakeCrossEncoder)


# --------------------------------------------------------------------------- #
# _build_langchain_backend / HuggingFaceEmbeddingBackend
# --------------------------------------------------------------------------- #


def test_build_langchain_backend_constructs_with_cpu_normalized_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    _fake_langchain: None,
) -> None:
    # Force the env-prep branch to run from a clean slate.
    monkeypatch.delenv("TRANSFORMERS_NO_TORCHVISION", raising=False)

    backend = _build_langchain_backend("BAAI/bge-m3")

    assert isinstance(backend, _FakeLangChainEmbeddings)
    assert backend.model_name == "BAAI/bge-m3"
    assert backend.model_kwargs == {"device": "cpu"}
    assert backend.encode_kwargs == {"normalize_embeddings": True}
    # The runtime env opt-out is set as a side effect.
    assert os.environ["TRANSFORMERS_NO_TORCHVISION"] == "1"


def test_embedding_backend_init_stores_model_id_and_builds_backend(
    _fake_langchain: None,
) -> None:
    backend = HuggingFaceEmbeddingBackend(model_id="intfloat/e5-large-v2")

    assert backend.model_id == "intfloat/e5-large-v2"
    # ``langchain_backend`` property returns the underlying built backend.
    assert isinstance(backend.langchain_backend, _FakeLangChainEmbeddings)
    assert backend.langchain_backend.model_name == "intfloat/e5-large-v2"


def test_embedding_backend_dimension_probes_query_length(
    _fake_langchain: None,
) -> None:
    backend = HuggingFaceEmbeddingBackend(model_id="m")

    # The fake returns a 3-element vector for any query.
    assert backend.dimension == 3
    # ``dimension`` is a cached_property: a second access returns the cached value.
    assert backend.dimension == 3


def test_embedding_backend_embed_texts_coerces_each_row(
    _fake_langchain: None,
) -> None:
    backend = HuggingFaceEmbeddingBackend(model_id="m")

    vectors = backend.embed_texts(["ab", "abcd"])

    assert vectors == [[2.0, 0.0, 1.0], [4.0, 0.0, 1.0]]
    # Every member is a real ``float`` after coercion (the fake emitted ints).
    assert all(isinstance(value, float) for row in vectors for value in row)


def test_embedding_backend_embed_texts_short_circuits_on_empty(
    _fake_langchain: None,
) -> None:
    backend = HuggingFaceEmbeddingBackend(model_id="m")

    assert backend.embed_texts([]) == []


def test_embedding_backend_embed_query_coerces_vector(
    _fake_langchain: None,
) -> None:
    backend = HuggingFaceEmbeddingBackend(model_id="m")

    vector = backend.embed_query("abc")

    assert vector == [3.0, 0.0, 1.0]
    assert all(isinstance(value, float) for value in vector)


def test_build_embedding_backend_factory_returns_backend(
    _fake_langchain: None,
) -> None:
    backend = build_embedding_backend("sentence-transformers/all-MiniLM-L6-v2")

    assert isinstance(backend, HuggingFaceEmbeddingBackend)
    assert backend.model_id == "sentence-transformers/all-MiniLM-L6-v2"


# --------------------------------------------------------------------------- #
# _build_cross_encoder / CrossEncoderRerankerBackend
# --------------------------------------------------------------------------- #


def test_build_cross_encoder_constructs_on_cpu(
    monkeypatch: pytest.MonkeyPatch,
    _fake_cross_encoder: None,
) -> None:
    monkeypatch.delenv("TRANSFORMERS_NO_TORCHVISION", raising=False)

    model = _build_cross_encoder("BAAI/bge-reranker-base")

    assert isinstance(model, _FakeCrossEncoder)
    assert model.model_id == "BAAI/bge-reranker-base"
    assert model.device == "cpu"
    assert os.environ["TRANSFORMERS_NO_TORCHVISION"] == "1"


def test_reranker_backend_init_stores_model_id_and_builds_model(
    _fake_cross_encoder: None,
) -> None:
    backend = CrossEncoderRerankerBackend(model_id="reranker/x")

    assert backend.model_id == "reranker/x"
    assert isinstance(backend._model, _FakeCrossEncoder)


def test_reranker_backend_scores_each_passage(
    _fake_cross_encoder: None,
) -> None:
    backend = CrossEncoderRerankerBackend(model_id="reranker/x")

    scores = backend.rerank_scores("query", ["ab", "abcde"])

    # The fake scores by passage length; values are coerced to real floats.
    assert scores == [2.0, 5.0]
    assert all(isinstance(score, float) for score in scores)


def test_reranker_backend_short_circuits_on_empty_passages(
    _fake_cross_encoder: None,
) -> None:
    backend = CrossEncoderRerankerBackend(model_id="reranker/x")

    assert backend.rerank_scores("query", []) == []


def test_build_reranker_backend_factory_returns_backend(
    _fake_cross_encoder: None,
) -> None:
    backend = build_reranker_backend("reranker/x")

    assert isinstance(backend, CrossEncoderRerankerBackend)
    assert backend.model_id == "reranker/x"


# --------------------------------------------------------------------------- #
# _coerce_vector
# --------------------------------------------------------------------------- #


def test_coerce_vector_converts_mixed_iterable_to_floats() -> None:
    # Accepts any iterable (here a tuple of mixed int/float) and yields floats.
    result = _coerce_vector((1, 2.5, 3))

    assert result == [1.0, 2.5, 3.0]
    assert all(isinstance(value, float) for value in result)


def test_coerce_vector_handles_empty_iterable() -> None:
    assert _coerce_vector([]) == []
