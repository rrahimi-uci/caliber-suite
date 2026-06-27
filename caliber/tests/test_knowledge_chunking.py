from __future__ import annotations

import math
import re

import pytest

from caliber.knowledge.chunking import chunk_text, list_chunking_strategies


class _DummyEmbedder:
    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            slot = sum(ord(char) for char in token) % self.dimension
            vector[slot] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.mark.parametrize(
    "strategy_id",
    [strategy.strategy_id for strategy in list_chunking_strategies()],
)
def test_chunk_text_supports_every_catalogued_strategy(strategy_id: str) -> None:
    text = """
# Product Guide

Retries happen three times before an alert is sent.

## Storage

Chunk outputs are versioned and written under a reserved knowledge-base prefix.

## UX

Users can compare versions in the playground and inspect the retrieved chunks.
""".strip()

    fragments = chunk_text(
        text,
        strategy_id,
        {
            "chunk_size": 110,
            "chunk_overlap": 12,
            "semantic_similarity_threshold": 0.72,
        },
        base_metadata={"document_id": "doc-1", "source_key": "guide.md"},
        embedder=_DummyEmbedder() if strategy_id == "semantic" else None,
    )

    assert fragments, f"{strategy_id} did not produce any fragments"
    assert all(fragment.content.strip() for fragment in fragments)
    assert all(fragment.metadata.get("document_id") == "doc-1" for fragment in fragments)
