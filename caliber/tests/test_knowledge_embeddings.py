from __future__ import annotations

import os

import pytest

import caliber.knowledge.embeddings as knowledge_embeddings
from caliber.knowledge.embeddings import (
    KnowledgeDependencyError,
    _prepare_transformers_runtime_env,
    ensure_embedding_backend_runtime_available,
)


def test_prepare_transformers_runtime_env_sets_torchvision_opt_out(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TRANSFORMERS_NO_TORCHVISION", raising=False)

    _prepare_transformers_runtime_env()

    assert os.environ["TRANSFORMERS_NO_TORCHVISION"] == "1"


def test_prepare_transformers_runtime_env_preserves_existing_value(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TRANSFORMERS_NO_TORCHVISION", "0")

    _prepare_transformers_runtime_env()

    assert os.environ["TRANSFORMERS_NO_TORCHVISION"] == "0"


def test_embedding_runtime_availability_raises_when_local_stack_is_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        knowledge_embeddings,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: None if allow_flagged else "local embeddings blocked",
    )

    with pytest.raises(KnowledgeDependencyError, match="local embeddings blocked"):
        ensure_embedding_backend_runtime_available()


def test_embedding_runtime_availability_honors_override(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        knowledge_embeddings,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: None if allow_flagged else "local embeddings blocked",
    )
    monkeypatch.setattr(
        knowledge_embeddings,
        "_import_huggingface_embeddings_class",
        lambda: object,
    )

    ensure_embedding_backend_runtime_available(allow_flagged_local_embeddings=True)
