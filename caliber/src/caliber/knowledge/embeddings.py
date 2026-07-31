"""Embedding backends and curated model catalog for knowledge bases."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from caliber.runtime_advisories import local_embedding_block_reason


class KnowledgeDependencyError(RuntimeError):
    """Raised when an optional knowledge-base dependency is unavailable."""


@dataclass(frozen=True)
class EmbeddingModelSpec:
    """One curated Hugging Face embedding model exposed in the UI."""

    model_id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()


EMBEDDING_MODEL_SPECS: tuple[EmbeddingModelSpec, ...] = (
    EmbeddingModelSpec(
        model_id="BAAI/bge-m3",
        name="BGE-M3",
        description="High-quality multilingual retrieval with strong dense and hybrid search performance.",
        tags=("recommended", "multilingual", "high-recall"),
    ),
    EmbeddingModelSpec(
        model_id="intfloat/e5-large-v2",
        name="E5 Large v2",
        description="Instruction-tuned retrieval model with strong English semantic search quality.",
        tags=("recommended", "english", "balanced"),
    ),
    EmbeddingModelSpec(
        model_id="Qwen/Qwen3-Embedding-0.6B",
        name="Qwen3 Embedding 0.6B",
        description="Modern open embedding model with strong retrieval quality and efficient footprint.",
        tags=("modern", "balanced"),
    ),
    EmbeddingModelSpec(
        model_id="sentence-transformers/all-MiniLM-L6-v2",
        name="MiniLM L6 v2",
        description="Lightweight local model for fast iteration and smaller developer machines.",
        tags=("fast", "lightweight"),
    ),
)


class HuggingFaceEmbeddingBackend:
    """Local Hugging Face embeddings via LangChain's integration layer."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self._backend = _build_langchain_backend(model_id)

    @cached_property
    def dimension(self) -> int:
        probe = self.embed_query("dimension probe")
        return len(probe)

    @property
    def langchain_backend(self) -> Any:
        return self._backend

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [_coerce_vector(row) for row in self._backend.embed_documents(texts)]

    def embed_query(self, text: str) -> list[float]:
        return _coerce_vector(self._backend.embed_query(text))


class CrossEncoderRerankerBackend:
    """Local cross-encoder reranker (``sentence_transformers.CrossEncoder``).

    First-stage retrieval (bi-encoder cosine / ANN) is fast but coarse; a
    cross-encoder re-scores each ``(query, passage)`` pair *jointly* — far more
    accurate — to reorder a candidate pool down to the final top-k. Raw scores
    are used only for ordering (they need not be calibrated to ``[0, 1]``). Same
    local/torch runtime + advisory guard as the embedding backend, and no new
    dependency: ``sentence-transformers`` ships in the ``knowledge-local`` extra.
    """

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self._model = _build_cross_encoder(model_id)

    def rerank_scores(self, query: str, passages: list[str]) -> list[float]:
        """Relevance score per passage against ``query`` (higher = more relevant)."""
        if not passages:
            return []
        pairs = [(query, passage) for passage in passages]
        return [float(score) for score in self._model.predict(pairs)]


def list_embedding_model_specs() -> list[EmbeddingModelSpec]:
    return list(EMBEDDING_MODEL_SPECS)


def build_embedding_backend(model_id: str) -> HuggingFaceEmbeddingBackend:
    return HuggingFaceEmbeddingBackend(model_id=model_id)


def build_reranker_backend(model_id: str) -> CrossEncoderRerankerBackend:
    return CrossEncoderRerankerBackend(model_id=model_id)


def _build_cross_encoder(model_id: str) -> Any:
    _prepare_transformers_runtime_env()
    try:
        from sentence_transformers import CrossEncoder  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without optional deps
        raise KnowledgeDependencyError(
            "Cross-encoder reranking needs the optional package "
            "'sentence-transformers'. Install caliber-suite[knowledge,knowledge-local]."
        ) from exc
    return CrossEncoder(model_id, device="cpu")


def ensure_embedding_backend_runtime_available(
    *,
    allow_flagged_local_embeddings: bool = False,
) -> None:
    """Fail fast when the local embedding runtime is unavailable or blocked."""

    block_reason = local_embedding_block_reason(allow_flagged=allow_flagged_local_embeddings)
    if block_reason is not None:
        raise KnowledgeDependencyError(block_reason)
    _prepare_transformers_runtime_env()
    _import_huggingface_embeddings_class()


def _prepare_transformers_runtime_env() -> None:
    # Text embeddings do not need torchvision. Opting out keeps the local
    # Hugging Face stack from hanging in torchvision/PyTorch fake-op setup on
    # newer Python runtimes during backend construction.
    os.environ.setdefault("TRANSFORMERS_NO_TORCHVISION", "1")


def _build_langchain_backend(model_id: str) -> Any:
    _prepare_transformers_runtime_env()
    hugging_face_embeddings_cls = _import_huggingface_embeddings_class()

    return hugging_face_embeddings_cls(
        model_name=model_id,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def _import_huggingface_embeddings_class() -> Any:
    try:
        from langchain_huggingface import HuggingFaceEmbeddings  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without optional deps
        raise KnowledgeDependencyError(
            "Knowledge-base embeddings need the optional packages "
            "'langchain-huggingface' and its runtime dependencies. Install "
            "caliber-suite[knowledge,knowledge-local]."
        ) from exc
    return HuggingFaceEmbeddings


def _coerce_vector(values: Any) -> list[float]:
    return [float(value) for value in list(values)]
