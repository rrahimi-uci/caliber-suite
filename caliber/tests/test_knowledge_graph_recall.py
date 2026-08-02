"""Graph retrieval must be able to surface chunks the ANN pool did not return.

Under pgvector, ``_load_dense_candidates`` materializes only the ANN top-k.
Graph retrieval then scores chunks through ``entity.source_chunks`` and
``relationship.evidence_chunk_ids`` — but a chunk outside the ANN pool is not in
the working set, so no amount of graph boost can surface it. The graph could
identify exactly the right passage and the query would still miss it, which
defeats the reason to run graph retrieval: it exists to find what vector
similarity does not.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from caliber.db.models import CaliberKnowledgeBaseChunk
from caliber.knowledge.service import KnowledgeBaseService


class _Version:
    knowledge_base_version_id = "KBV-1"


def _chunk(chunk_id: str) -> CaliberKnowledgeBaseChunk:
    return CaliberKnowledgeBaseChunk(
        knowledge_base_chunk_id=chunk_id,
        knowledge_base_version_id="KBV-1",
        document_id="DOC-1",
        source_bucket="b",
        source_key="k",
        source_name="n",
        chunk_index=0,
        ordinal=0,
    )


class _Session:
    """Returns the requested chunks, standing in for the version-scoped query."""

    def __init__(self, available: dict[str, CaliberKnowledgeBaseChunk]) -> None:
        self._available = available
        self.queried = False

    def execute(self, _stmt: Any) -> Any:
        self.queried = True
        rows = list(self._available.values())
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))


def _service() -> KnowledgeBaseService:
    return KnowledgeBaseService.__new__(KnowledgeBaseService)


def test_a_graph_referenced_chunk_outside_the_ann_pool_is_materialized() -> None:
    """The defect: the graph points at a chunk the working set does not contain."""
    in_pool = _chunk("CH-in-pool")
    outside = _chunk("CH-graph-only")
    session = _Session({"CH-graph-only": outside})
    entity = SimpleNamespace(source_chunks=["CH-in-pool", "CH-graph-only"])

    result = _service()._augment_with_graph_chunks(
        session, _Version(), [in_pool], [entity], []
    )

    ids = {chunk.knowledge_base_chunk_id for chunk in result}
    assert ids == {"CH-in-pool", "CH-graph-only"}, (
        "a chunk the graph references must join the working set, or graph "
        "retrieval cannot surface anything the ANN pool missed"
    )


def test_relationship_evidence_chunks_are_materialized_too() -> None:
    """Relationships carry their own evidence, on the same reasoning as entities."""
    outside = _chunk("CH-evidence")
    session = _Session({"CH-evidence": outside})
    relationship = SimpleNamespace(evidence_chunk_ids=["CH-evidence"])

    result = _service()._augment_with_graph_chunks(
        session, _Version(), [], [], [relationship]
    )

    assert [chunk.knowledge_base_chunk_id for chunk in result] == ["CH-evidence"]


def test_no_extra_query_when_the_graph_adds_nothing() -> None:
    """Every referenced chunk already present must not trigger a second query."""
    present = _chunk("CH-1")
    session = _Session({})
    entity = SimpleNamespace(source_chunks=["CH-1"])

    result = _service()._augment_with_graph_chunks(
        session, _Version(), [present], [entity], []
    )

    assert result == [present]
    assert session.queried is False, "an unnecessary round trip on every graph query"


def test_no_graph_context_is_a_no_op() -> None:
    """Non-graph modes must not pay for this at all."""
    present = _chunk("CH-1")
    session = _Session({})

    assert _service()._augment_with_graph_chunks(session, _Version(), [present], [], []) == [
        present
    ]
    assert session.queried is False
