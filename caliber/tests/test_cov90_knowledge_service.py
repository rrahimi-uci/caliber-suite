"""Coverage tests for :mod:`caliber.knowledge.service`.

These target previously-uncovered branches without running a real build for
most cases:

* module-level pure helpers (cosine, etag stripping, prompt builders, fallback
  answer truncation, event-timestamp parsing);
* in-memory scoring helpers driven with unpersisted ORM rows (lexical/BM25,
  hybrid/dense/graph_hybrid fusion, entity matching, AGE candidate scoring,
  AGE fallback);
* config / graph-target guard clauses (AGE availability reasons, unsupported
  graph target, query-override finalisation);
* CRUD + error branches called directly on the service with seeded rows
  (update no-op / diff, run-events 404, version-create no-sources, activation
  404/409, rollback 409 variants, AGE-sync 400, not-ready query, build-row 500,
  execute-run guards);
* object-store + calibration-question helpers, and a build that fails fast so
  the failure-persistence path runs end to end.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

import caliber.knowledge.service as knowledge_service
from caliber.audit import record as audit_record
from caliber.auth import CaliberIdentity
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseChunk,
    CaliberKnowledgeBaseEntity,
    CaliberKnowledgeBaseRelationship,
    CaliberKnowledgeBaseRun,
    CaliberKnowledgeBaseVersion,
)
from caliber.knowledge.age import (
    AgeChunkCandidate,
    AgeGraphExploreResult,
    AgeRetrievalResult,
)
from caliber.knowledge.schemas import (
    KnowledgeBaseUpdateRequest,
    KnowledgeBaseVersionCreateRequest,
    KnowledgeGraphConfigSchema,
    KnowledgeQueryChunkSchema,
    KnowledgeQueryGraphOverridesSchema,
    KnowledgeQueryRequest,
)
from caliber.knowledge.service import (
    KnowledgeBaseService,
    _cosine_similarity,
    _event_created_at,
    _fallback_answer,
    _looks_supported,
    _rag_user_prompt,
    _strip_etag,
)

boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _identity() -> CaliberIdentity:
    return CaliberIdentity(user_id="@test", scopes=frozenset({"caliber.admin"}))


def _service(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    *,
    object_store_client: object | None = None,
) -> KnowledgeBaseService:
    return KnowledgeBaseService(
        config=app_config,
        session_factory=session_factory,
        object_store_client=object_store_client,
    )


def _chunk(
    chunk_id: str,
    *,
    content: str = "",
    ordinal: int = 1,
    embedding: list[float] | None = None,
) -> CaliberKnowledgeBaseChunk:
    return CaliberKnowledgeBaseChunk(
        knowledge_base_chunk_id=chunk_id,
        knowledge_base_version_id="KBV-1",
        document_id="DOC-1",
        source_bucket="docs",
        source_key="guide.md",
        source_name="guide.md",
        chunk_index=0,
        ordinal=ordinal,
        content=content,
        embedding=embedding if embedding is not None else [],
    )


def _entity(
    entity_id: str,
    *,
    entity_key: str,
    label: str,
    source_chunks: list[str] | None = None,
    mention_count: int = 1,
    aliases: list[str] | None = None,
    entity_type: str = "concept",
) -> CaliberKnowledgeBaseEntity:
    return CaliberKnowledgeBaseEntity(
        knowledge_base_entity_id=entity_id,
        knowledge_base_version_id="KBV-1",
        entity_key=entity_key,
        label=label,
        entity_type=entity_type,
        aliases=aliases if aliases is not None else [],
        mention_count=mention_count,
        source_documents=["DOC-1"],
        source_keys=["guide.md"],
        source_chunks=source_chunks if source_chunks is not None else [],
    )


def _seed_kb(
    session: Session,
    *,
    kb_id: str = "KB-1",
    active_version_id: str | None = None,
    source_manifest: list[dict[str, str]] | None = None,
    description: str = "",
    status: str = "active",
) -> None:
    session.add(
        CaliberKnowledgeBase(
            knowledge_base_id=kb_id,
            name=f"KB {kb_id}",
            description=description,
            owner="@test",
            project_id=None,
            visibility="user",
            status=status,
            source_bucket="docs",
            source_manifest=source_manifest if source_manifest is not None else [],
            active_version_id=active_version_id,
        )
    )


def _seed_version(
    session: Session,
    *,
    version_id: str = "KBV-1",
    kb_id: str = "KB-1",
    status: str = "completed",
    version_number: int = 1,
    summary: dict[str, object] | None = None,
    graph_config: dict[str, object] | None = None,
) -> None:
    session.add(
        CaliberKnowledgeBaseVersion(
            knowledge_base_version_id=version_id,
            knowledge_base_id=kb_id,
            version_number=version_number,
            status=status,
            chunking_strategy="recursive",
            chunking_config={},
            graph_config=graph_config or {},
            embedding_model="sentence-transformers/all-MiniLM-L6-v2",
            source_manifest=[],
            output_bucket="docs",
            output_prefix=f".caliber/knowledge-bases/{kb_id}/versions/0001-{version_id}",
            summary=summary or {},
        )
    )


def _version_obj(
    *,
    summary: dict[str, object] | None = None,
    status: str = "completed",
) -> CaliberKnowledgeBaseVersion:
    return CaliberKnowledgeBaseVersion(
        knowledge_base_version_id="KBV-1",
        knowledge_base_id="KB-1",
        version_number=1,
        status=status,
        chunking_strategy="recursive",
        chunking_config={},
        graph_config={},
        embedding_model="m",
        source_manifest=[],
        output_bucket="docs",
        output_prefix="p",
        summary=summary or {},
    )


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def test_strip_etag_handles_none_and_quotes() -> None:
    assert _strip_etag(None) is None
    assert _strip_etag('"abc123"') == "abc123"


def test_cosine_similarity_edge_cases() -> None:
    assert _cosine_similarity([], []) == 0.0
    assert _cosine_similarity([1.0], [1.0, 2.0]) == 0.0  # length mismatch
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero norm
    assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_looks_supported_by_extension_and_media_hint() -> None:
    assert _looks_supported("notes.pdf", None) is True
    assert _looks_supported("blob.bin", "text/plain") is True
    assert _looks_supported("blob.bin", "application/octet-stream") is False


def test_rag_user_prompt_lists_numbered_sources() -> None:
    chunks = [
        KnowledgeQueryChunkSchema(
            chunk_id="c1",
            source_bucket="b",
            source_key="guide.md",
            source_name="guide.md",
            score=0.9,
            content="Retries happen three times.",
            chunk_index=0,
            ordinal=1,
            document_id="d",
            metadata={},
            object_store_path="/p",
            score_breakdown={},
            matched_entity_labels=[],
        )
    ]
    prompt = _rag_user_prompt("How many retries?", chunks)
    assert "Question: How many retries?" in prompt
    assert "[1] source=guide.md" in prompt
    assert "Retries happen three times." in prompt


def test_fallback_answer_truncates_long_previews() -> None:
    long_content = "x" * 400
    chunks = [
        KnowledgeQueryChunkSchema(
            chunk_id="c1",
            source_bucket="b",
            source_key="k",
            source_name="guide.md",
            score=0.5,
            content=long_content,
            chunk_index=0,
            ordinal=1,
            document_id="d",
            metadata={},
            object_store_path="/p",
            score_breakdown={},
            matched_entity_labels=[],
        )
    ]
    answer = _fallback_answer("Q?", chunks)
    assert "..." in answer
    assert "guide.md" in answer


def test_event_created_at_parses_iso_and_falls_back() -> None:
    parsed = _event_created_at({"at": "2026-01-02T03:04:05+00:00"})
    assert parsed.year == 2026
    # Bad ISO string -> _utcnow fallback (still a datetime).
    assert isinstance(_event_created_at({"at": "not-a-date"}), datetime)
    # No "at" key -> _utcnow fallback.
    assert isinstance(_event_created_at({}), datetime)


# ---------------------------------------------------------------------------
# In-memory scoring helpers
# ---------------------------------------------------------------------------


def test_lexical_scores_empty_query_and_empty_docs(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    # Query is only stopwords -> no query terms -> {}.
    assert service._lexical_scores([_chunk("c1", content="hello")], "the and of") == {}
    # Real query but all chunks empty -> no non-empty documents -> {}.
    assert service._lexical_scores([_chunk("c1", content="")], "hello world") == {}


def test_retrieve_hybrid_skips_scores_for_unknown_chunks(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    chunks = [_chunk("c1", content="alpha", ordinal=1)]
    dense = {"c1": 0.8, "ghost": 0.9}  # "ghost" has no chunk -> continue branch
    lexical = {"c1": 1.2}
    top, context = service._retrieve_hybrid(
        chunks=chunks, dense_scores=dense, lexical_scores=lexical, top_k=5
    )
    assert [item.chunk.knowledge_base_chunk_id for item in top] == ["c1"]
    assert context["fused_chunk_count"] == 2


def test_retrieve_dense_orders_by_score(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    chunks = [_chunk("c1", ordinal=1), _chunk("c2", ordinal=2)]
    top, context = service._retrieve_dense(
        chunks=chunks, dense_scores={"c1": 0.2, "c2": 0.9}, top_k=5
    )
    assert [item.chunk.knowledge_base_chunk_id for item in top] == ["c2", "c1"]
    assert context == {"matched_entities": [], "expanded_entities": []}


def test_retrieve_graph_hybrid_boosts_and_skips(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    graph_config = service._resolve_graph_config(None)
    chunks = [
        _chunk("CH-1", content="platform reliability guide", ordinal=1),
        _chunk("CH-2", content="oncall rotation notes", ordinal=2),
        _chunk("CH-3", content="", ordinal=3),  # no dense/graph/lexical -> skipped
    ]
    entities = [
        _entity(
            "E1",
            entity_key="platform-reliability",
            label="Platform Reliability",
            source_chunks=["CH-1"],
        ),
        _entity("E2", entity_key="oncall", label="Oncall", source_chunks=["CH-2"]),
    ]
    relationships = [
        CaliberKnowledgeBaseRelationship(
            knowledge_base_relationship_id="R-ghost",
            knowledge_base_version_id="KBV-1",
            source_entity_id="E1",
            target_entity_id="GHOST",  # neighbor not in entity map -> continue
            relationship_type="related",
            weight=1.0,
            evidence_chunk_ids=[],
            source_documents=[],
        ),
        CaliberKnowledgeBaseRelationship(
            knowledge_base_relationship_id="R-1",
            knowledge_base_version_id="KBV-1",
            source_entity_id="E1",
            target_entity_id="E2",
            relationship_type="related",
            weight=2.0,
            evidence_chunk_ids=[],
            source_documents=[],
        ),
    ]
    top, context = service._retrieve_graph_hybrid(
        question="tell me about platform reliability",
        chunks=chunks,
        dense_scores={"CH-1": 0.5},
        entities=entities,
        relationships=relationships,
        graph_config=graph_config,
        top_k=10,
    )
    ids = {item.chunk.knowledge_base_chunk_id for item in top}
    assert "CH-1" in ids
    assert "CH-3" not in ids  # zero-everything chunk skipped
    assert "Platform Reliability" in context["matched_entities"]


def test_retrieve_graph_hybrid_degrades_without_matches(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    graph_config = service._resolve_graph_config(None)
    chunks = [_chunk("CH-1", content="unrelated text", ordinal=1)]
    top, context = service._retrieve_graph_hybrid(
        question="a totally different question",
        chunks=chunks,
        dense_scores={"CH-1": 0.4},
        entities=[_entity("E9", entity_key="widget", label="Widget", source_chunks=["CH-9"])],
        relationships=[],
        graph_config=graph_config,
        top_k=5,
    )
    assert context["matched_entities"] == []
    assert context["boosted_chunk_count"] == 0


def test_match_query_entities_empty_returns_empty(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    assert service._match_query_entities(question="q", entities=[], query_entity_keys=set()) == []


def test_entity_match_score_skips_unresolvable_alias(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    entity = _entity("E1", entity_key="unrelated", label="!!!", aliases=["!!!"])
    # No key match, aliases resolve to empty keys -> skipped -> score 0.
    score = service._entity_match_score(
        entity=entity,
        padded_question=" nothing here ",
        question_terms={"nothing", "here"},
        query_entity_keys=set(),
    )
    assert score == 0.0


def test_score_age_chunk_candidates_skips_absent_chunks(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    graph_config = service._resolve_graph_config(None)
    age_result = AgeRetrievalResult(
        status="ok",
        graph_name="g",
        chunk_candidates=[
            AgeChunkCandidate(chunk_id="CH-1", graph_score=4.0, matched_entities=("Bob",)),
            AgeChunkCandidate(chunk_id="GHOST", graph_score=2.0),  # not in chunks -> skip
        ],
    )
    scored = service._score_age_chunk_candidates(
        chunks=[_chunk("CH-1", content="x", ordinal=1)],
        dense_scores={"CH-1": 0.5},
        age_result=age_result,
        graph_config=graph_config,
    )
    assert [item.chunk.knowledge_base_chunk_id for item in scored] == ["CH-1"]
    assert scored[0].graph_boost == pytest.approx(1.0)


def test_apply_age_retrieval_context_copies_result_fields(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    base: dict[str, object] = {}
    age_result = AgeRetrievalResult(
        status="ok",
        graph_name="graph_x",
        matched_entities=("Alpha",),
        expanded_entities=("Beta",),
        traversal_hops=2,
        matched_chunk_count=3,
        seed_strategy="query_text",
    )
    service._apply_age_retrieval_context(base, age_result)
    assert base["age_status"] == "ok"
    assert base["age_graph_name"] == "graph_x"
    assert base["age_seed_strategy"] == "query_text"
    assert base["matched_entities"] == ["Alpha"]
    assert base["expanded_entities"] == ["Beta"]


def test_age_graph_fallback_uses_graph_hybrid_then_dense(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    graph_config = service._resolve_graph_config(None)
    chunks = [_chunk("CH-1", content="platform reliability", ordinal=1)]
    entities = [
        _entity(
            "E1",
            entity_key="platform-reliability",
            label="Platform Reliability",
            source_chunks=["CH-1"],
        )
    ]
    # With entities present -> graph_hybrid fallback path.
    _top, ctx = service._age_graph_fallback(
        question="platform reliability",
        chunks=chunks,
        dense_scores={"CH-1": 0.5},
        entities=entities,
        relationships=[],
        graph_config=graph_config,
        top_k=5,
        graph_context={"fallback_reason": None},
        reason="age offline",
    )
    assert ctx["fallback_retrieval_mode"] == "graph_hybrid"
    assert ctx["age_fallback_reason"] == "age offline"

    # No entities -> dense fallback path.
    _top2, ctx2 = service._age_graph_fallback(
        question="platform reliability",
        chunks=chunks,
        dense_scores={"CH-1": 0.5},
        entities=[],
        relationships=[],
        graph_config=graph_config,
        top_k=5,
        graph_context={"fallback_reason": None},
        reason="age offline",
    )
    assert ctx2["fallback_retrieval_mode"] == "dense"


# ---------------------------------------------------------------------------
# Config / graph-target guard clauses
# ---------------------------------------------------------------------------


def test_age_unavailable_reason_branches_when_age_available(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_age_available", lambda: True)
    default_cfg = service._default_graph_config()
    age_cfg = KnowledgeGraphConfigSchema.model_validate(
        {**default_cfg.model_dump(), "output_target": "object_store_and_age"}
    )
    local_cfg = KnowledgeGraphConfigSchema.model_validate(
        {**default_cfg.model_dump(), "output_target": "object_store"}
    )

    not_synced = service._age_unavailable_reason(_version_obj(summary={}), graph_config=local_cfg)
    assert "not synced to Apache AGE" in not_synced

    pending = service._age_unavailable_reason(
        _version_obj(summary={"age_sync_status": "pending"}), graph_config=age_cfg
    )
    assert "still syncing" in pending

    default_reason = service._age_unavailable_reason(
        _version_obj(summary={"age_sync_status": "synced"}), graph_config=age_cfg
    )
    assert "not available for this version" in default_reason


def test_assert_graph_target_supported_rejects_age_when_unavailable(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    age_cfg = KnowledgeGraphConfigSchema.model_validate(
        {**service._default_graph_config().model_dump(), "output_target": "object_store_and_age"}
    )
    with pytest.raises(HTTPException) as exc:
        service._assert_graph_target_supported(age_cfg)
    assert exc.value.status_code == 400


def test_finalize_query_graph_context_with_active_overrides(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    cfg = service._resolve_graph_config(None)
    overrides = KnowledgeQueryGraphOverridesSchema(retrieval_strength="balanced")
    resolved = service._finalize_query_graph_context(
        {},
        graph_config=cfg,
        graph_overrides=overrides,
        retrieval_mode="graph_hybrid",
        age_ready=False,
    )
    assert resolved["query_override_active"] is True
    assert "strict_age_retrieval" not in resolved.get("query_overrides", {})


class _FakeAgeStore:
    def __init__(self) -> None:
        self.available = False
        self.unavailable_reason = ""
        self.graph_name = "knowledge_graph"


def test_age_unavailable_deployment_reason_config_fallbacks(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    disabled = _service(app_config, session_factory)
    disabled._age_store = _FakeAgeStore()  # type: ignore[assignment]
    assert (
        disabled._age_unavailable_deployment_reason() == "Apache AGE is disabled by configuration."
    )

    enabled_config = app_config.model_copy(update={"knowledge_age_enabled": True})
    enabled = _service(enabled_config, session_factory)
    enabled._age_store = _FakeAgeStore()  # type: ignore[assignment]
    reason = enabled._age_unavailable_deployment_reason()
    assert "PostgreSQL" in reason


# ---------------------------------------------------------------------------
# CRUD + error branches (direct service calls)
# ---------------------------------------------------------------------------


def test_update_knowledge_base_requires_a_field(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.update_knowledge_base(
            "KB-1", KnowledgeBaseUpdateRequest(), identity=_identity(), actor="@test"
        )
    assert exc.value.status_code == 400


def test_update_knowledge_base_no_diff_and_real_diff(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, description="original")
    db_session.commit()
    service = _service(app_config, session_factory)

    # Same value -> no diff -> returns without an audit row.
    unchanged = service.update_knowledge_base(
        "KB-1",
        KnowledgeBaseUpdateRequest(description="original"),
        identity=_identity(),
        actor="@test",
    )
    assert unchanged.description == "original"

    # Real change -> diff -> audit + commit.
    changed = service.update_knowledge_base(
        "KB-1",
        KnowledgeBaseUpdateRequest(description="updated"),
        identity=_identity(),
        actor="@test",
    )
    assert changed.description == "updated"


def test_list_run_events_missing_run_404(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.list_run_events("KBR-missing", identity=_identity())
    assert exc.value.status_code == 404


def test_create_version_without_sources_400(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "ensure_embedding_backend_runtime_available",
        lambda *, allow_flagged_local_embeddings=False: None,
    )
    _seed_kb(db_session, source_manifest=[])  # no sources to inherit
    db_session.commit()
    service = _service(app_config, session_factory)
    payload = KnowledgeBaseVersionCreateRequest(
        chunking_strategy="recursive",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
    )
    with pytest.raises(HTTPException) as exc:
        service.create_version("KB-1", payload, identity=_identity(), actor="@test")
    assert exc.value.status_code == 400


def test_activate_version_missing_and_not_completed(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-queued", status="queued")
    db_session.commit()
    service = _service(app_config, session_factory)

    with pytest.raises(HTTPException) as missing:
        service.activate_version("KB-1", "KBV-nope", identity=_identity(), actor="@test")
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as not_done:
        service.activate_version("KB-1", "KBV-queued", identity=_identity(), actor="@test")
    assert not_done.value.status_code == 409


def test_rollback_without_active_version_409(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, active_version_id=None)
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.rollback_version("KB-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 409
    assert "no active version" in exc.value.detail


def test_rollback_without_recorded_prior_409(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, active_version_id="KBV-1")
    _seed_version(db_session, version_id="KBV-1")
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.rollback_version("KB-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 409
    assert "no recorded prior active version" in exc.value.detail


def _seed_activation_audit(session: Session, *, current: str, prior: str) -> None:
    audit_record(
        session,
        actor="@test",
        action="activate_knowledge_base_version",
        entity_type="knowledge_base",
        entity_id="KB-1",
        details={"version_id": current, "previous_active_version_id": prior},
    )


def test_rollback_prior_version_missing_409(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, active_version_id="KBV-2")
    _seed_version(db_session, version_id="KBV-2", version_number=2)
    _seed_activation_audit(db_session, current="KBV-2", prior="KBV-gone")
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.rollback_version("KB-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 409
    assert "no longer exists" in exc.value.detail


def test_rollback_prior_version_not_completed_409(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, active_version_id="KBV-2")
    _seed_version(db_session, version_id="KBV-2", version_number=2)
    _seed_version(db_session, version_id="KBV-1", version_number=1, status="queued")
    _seed_activation_audit(db_session, current="KBV-2", prior="KBV-1")
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.rollback_version("KB-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 409
    assert "not in a completed state" in exc.value.detail


def test_sync_version_to_age_rejected_when_age_unavailable(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.sync_version_to_age("KBV-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 400


def test_query_reports_not_ready_for_incomplete_version(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-Q", status="queued")
    db_session.commit()
    service = _service(app_config, session_factory)
    result = service.query(
        KnowledgeQueryRequest(version_ids=["KBV-Q"], question="hello", top_k=3),
        identity=_identity(),
    )
    assert len(result.versions) == 1
    assert result.versions[0].answer_error == "Version is not ready for retrieval."


def test_build_result_missing_rows_500(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session)
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service._build_result("KB-1", "KBV-missing", "KBR-missing", _identity())
    assert exc.value.status_code == 500


def test_execute_run_returns_for_unknown_run(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    # No row -> guard returns without touching anything.
    service.execute_run("KBR-none")


def test_execute_run_marks_failed_when_version_missing(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    db_session.add(
        CaliberKnowledgeBaseRun(
            knowledge_base_run_id="KBR-orphan",
            knowledge_base_id="KB-missing",
            knowledge_base_version_id="KBV-missing",
            status="queued",
            source_manifest=[],
            queued_at=_utcnow(),
        )
    )
    db_session.commit()
    service = _service(app_config, session_factory)
    service.execute_run("KBR-orphan")

    db_session.expire_all()
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-orphan")
    assert run is not None
    assert run.status == "failed"
    assert run.error_summary == "knowledge base or version not found"


# ---------------------------------------------------------------------------
# Object-store + calibration-question helpers
# ---------------------------------------------------------------------------


def test_object_store_builds_boto3_client_lazily(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory, object_store_client=None)
    client = service._object_store()
    assert client is not None
    # Cached: a second call returns the same client.
    assert service._object_store() is client


def test_best_effort_drop_age_version_swallows_errors(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_age_available", lambda: True)

    def _boom(*, version_id: str) -> bool:
        raise RuntimeError("age down")

    monkeypatch.setattr(service._age_store, "drop_version", _boom)
    # Must not raise — the except branch logs and swallows.
    service._best_effort_drop_age_version("KBV-1")


def test_best_effort_delete_output_prefix_guards(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    # Empty bucket / prefix -> early return, no client access.
    service._best_effort_delete_output_prefix("", "prefix")

    # Client resolves to None -> early return.
    monkeypatch.setattr(service, "_object_store", lambda: None)
    service._best_effort_delete_output_prefix("bucket", "prefix")


@mock_aws
def test_best_effort_delete_output_prefix_removes_objects(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-out")
    s3.put_object(Bucket="kb-out", Key="prefix/a.json", Body=b"{}")
    s3.put_object(Bucket="kb-out", Key="prefix/b.json", Body=b"{}")
    service = _service(app_config, session_factory, object_store_client=s3)

    service._best_effort_delete_output_prefix("kb-out", "prefix")

    remaining = s3.list_objects_v2(Bucket="kb-out").get("Contents", [])
    assert remaining == []


def test_best_effort_delete_output_prefix_swallows_client_errors(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenClient:
        def list_objects_v2(self, **_kwargs: object) -> dict[str, object]:
            raise RuntimeError("s3 down")

    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_object_store", lambda: _BrokenClient())
    # Failure is logged and swallowed.
    service._best_effort_delete_output_prefix("bucket", "prefix")


def test_load_calibration_questions_version_pinned_and_skips_blank(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    db_session.add(CaliberEvalDataset(dataset_id="ED-1", name="cal", owner="@test", version=1))
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="ED-1-EX0",
            dataset_id="ED-1",
            dataset_version=1,
            input={"question": "How many retries?"},
            expected={"sources": ["guide.md"]},
        )
    )
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="ED-1-EX1",
            dataset_id="ED-1",
            dataset_version=1,
            input={"question": "   "},  # blank -> skipped
            expected={},
        )
    )
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="ED-1-EX2",
            dataset_id="ED-1",
            dataset_version=1,
            input={"note": "no question key"},  # missing question -> skipped
            expected={},
        )
    )
    db_session.commit()

    service = _service(app_config, session_factory)
    with session_factory() as session:
        pinned = service._load_calibration_questions(session, "ED-1", 1)
        active = service._load_calibration_questions(session, "ED-1", None)
    assert [q for q, _ in pinned] == ["How many retries?"]
    assert [q for q, _ in active] == ["How many retries?"]


# ---------------------------------------------------------------------------
# Answer generation dispatch + provider helpers
# ---------------------------------------------------------------------------


def _kb_and_version() -> tuple[CaliberKnowledgeBase, CaliberKnowledgeBaseVersion]:
    kb = CaliberKnowledgeBase(
        knowledge_base_id="KB-1",
        name="Docs",
        owner="@test",
        source_bucket="docs",
    )
    version = _version_obj()
    return kb, version


def _one_chunk() -> list[KnowledgeQueryChunkSchema]:
    return [
        KnowledgeQueryChunkSchema(
            chunk_id="c1",
            source_bucket="b",
            source_key="guide.md",
            source_name="guide.md",
            score=0.9,
            content="Retries happen three times.",
            chunk_index=0,
            ordinal=1,
            document_id="d",
            metadata={},
            object_store_path="/p",
            score_breakdown={},
            matched_entity_labels=[],
        )
    ]


def test_generate_answer_dispatches_to_providers(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
) -> None:
    kb, version = _kb_and_version()
    chunks = _one_chunk()

    openai_config = app_config.model_copy(update={"assistant_engine": "openai"})
    openai_service = _service(openai_config, session_factory)
    openai_service._generate_openai_answer = lambda **_kw: "openai answer"  # type: ignore[method-assign]
    answer, error = openai_service._generate_answer(
        question="q",
        history=[],
        knowledge_base=kb,
        version=version,
        retrieved_chunks=chunks,
        chat_model=None,
    )
    assert answer == "openai answer"
    assert error is None

    anthropic_config = app_config.model_copy(update={"assistant_engine": "anthropic"})
    anthropic_service = _service(anthropic_config, session_factory)
    anthropic_service._generate_anthropic_answer = lambda **_kw: "claude answer"  # type: ignore[method-assign]
    answer2, error2 = anthropic_service._generate_answer(
        question="q",
        history=[],
        knowledge_base=kb,
        version=version,
        retrieved_chunks=chunks,
        chat_model=None,
    )
    assert answer2 == "claude answer"
    assert error2 is None


def test_generate_answer_without_chunks_reports_error(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    kb, version = _kb_and_version()
    service = _service(app_config, session_factory)
    answer, error = service._generate_answer(
        question="q",
        history=[],
        knowledge_base=kb,
        version=version,
        retrieved_chunks=[],
        chat_model=None,
    )
    assert answer is None
    assert "No relevant chunks" in (error or "")


def test_generate_openai_answer_requires_key_then_uses_client(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb, version = _kb_and_version()
    service = _service(app_config, session_factory)

    monkeypatch.setattr(knowledge_service, "resolve_secret", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="OpenAI API key"):
        service._generate_openai_answer(
            model="gpt-x",
            question="q",
            history=[{"role": "system", "content": "ignored"}],
            knowledge_base=kb,
            version=version,
            retrieved_chunks=_one_chunk(),
        )

    class _Msg:
        content = "grounded answer"

    class _Choice:
        message = _Msg()

    class _Completions:
        def create(self, *, model: str, messages: object) -> object:
            return type("_Resp", (), {"choices": [_Choice()]})()

    class _Chat:
        completions = _Completions()

    class _FakeOpenAI:
        def __init__(self, *, api_key: str) -> None:
            self.chat = _Chat()

    monkeypatch.setattr(knowledge_service, "resolve_secret", lambda *_a, **_k: "sk-test")
    monkeypatch.setattr("openai.OpenAI", _FakeOpenAI)
    answer = service._generate_openai_answer(
        model="gpt-x",
        question="q",
        history=[{"role": "user", "content": "prior"}, {"role": "bot", "content": "x"}],
        knowledge_base=kb,
        version=version,
        retrieved_chunks=_one_chunk(),
    )
    assert answer == "grounded answer"


def test_generate_anthropic_answer_uses_injected_sdk(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb, version = _kb_and_version()
    service = _service(app_config, session_factory)

    class _Block:
        text = "claude grounded answer"

    class _Messages:
        def create(self, *, model: str, max_tokens: int, system: str, messages: object) -> object:
            return type("_Resp", (), {"content": [_Block()]})()

    class _FakeAnthropic:
        def __init__(self, *, api_key: str) -> None:
            self.messages = _Messages()

    fake_module = type(sys)("anthropic")
    fake_module.Anthropic = _FakeAnthropic  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)

    # No key configured -> raises before the SDK is used.
    monkeypatch.setattr(knowledge_service, "resolve_secret", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="Anthropic API key"):
        service._generate_anthropic_answer(
            model="claude-x",
            question="q",
            history=[],
            knowledge_base=kb,
            version=version,
            retrieved_chunks=_one_chunk(),
        )

    monkeypatch.setattr(knowledge_service, "resolve_secret", lambda *_a, **_k: "key")
    answer = service._generate_anthropic_answer(
        model="claude-x",
        question="q",
        history=[{"role": "assistant", "content": "prior"}, {"role": "tool", "content": "x"}],
        knowledge_base=kb,
        version=version,
        retrieved_chunks=_one_chunk(),
    )
    assert answer == "claude grounded answer"


# ---------------------------------------------------------------------------
# Build failure path (persist_failure end to end)
# ---------------------------------------------------------------------------


@mock_aws
def test_process_build_persists_failure_when_no_sources(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-fail")

    _seed_kb(db_session, kb_id="KB-F")
    _seed_version(db_session, version_id="KBV-F", kb_id="KB-F", status="processing")
    db_session.add(
        CaliberKnowledgeBaseRun(
            knowledge_base_run_id="KBR-F",
            knowledge_base_id="KB-F",
            knowledge_base_version_id="KBV-F",
            status="running",
            source_manifest=[],
            started_at=_utcnow(),
        )
    )
    db_session.commit()

    service = _service(app_config, session_factory, object_store_client=s3)
    graph_config = service._resolve_graph_config(None)
    service._process_build(
        knowledge_base_id="KB-F",
        knowledge_base_name="KB KB-F",
        version_id="KBV-F",
        version_number=1,
        run_id="KBR-F",
        source_bucket="kb-fail",
        source_manifest=[{"kind": "folder", "path": "missing/"}],
        chunking_strategy="recursive",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        chunking_config={},
        graph_config=graph_config,
    )

    db_session.expire_all()
    version = db_session.get(CaliberKnowledgeBaseVersion, "KBV-F")
    run = db_session.get(CaliberKnowledgeBaseRun, "KBR-F")
    assert version is not None and version.status == "failed"
    assert version.error_summary is not None
    assert run is not None and run.status == "failed"


# ---------------------------------------------------------------------------
# Cheap direct-call branches
# ---------------------------------------------------------------------------


def test_list_chunks_applies_query_and_source_filters(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-1")
    db_session.add(_chunk("CH-1", content="alpha keyword"))
    db_session.commit()
    service = _service(app_config, session_factory)
    rows = service.list_chunks(
        "KBV-1", identity=_identity(), query="keyword", source_key="guide.md", limit=10
    )
    assert [row.knowledge_base_chunk_id for row in rows] == ["CH-1"]


def test_dense_scores_skips_chunks_without_embeddings(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    scores = service._dense_scores(
        [_chunk("c1", embedding=[]), _chunk("c2", embedding=[1.0, 0.0])],
        [1.0, 0.0],
    )
    assert "c1" not in scores
    assert scores["c2"] == pytest.approx(1.0)


def test_library_version_map_empty(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    with session_factory() as session:
        assert service._library_version_map(session, []) == {}


def test_assert_unique_name_rejects_duplicate_in_project(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id="KB-P",
            name="Shared",
            owner="@test",
            project_id="P1",
            visibility="project",
            source_bucket="docs",
            source_manifest=[],
        )
    )
    db_session.commit()
    service = _service(app_config, session_factory)
    identity = CaliberIdentity(
        user_id="@test", scopes=frozenset({"caliber.admin"}), active_project_id="P1"
    )
    with session_factory() as session, pytest.raises(HTTPException) as exc:
        service._assert_unique_name(session, "Shared", identity, exclude_id=None)
    assert exc.value.status_code == 409


def test_put_json_and_jsonl_return_none_without_client(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_object_store", lambda: None)
    assert service._put_json("bucket", "key", {"a": 1}) is None
    assert service._put_jsonl("bucket", "key", [{"a": 1}]) is None


def test_calibrate_rejects_version_from_other_kb(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, kb_id="KB-A")
    _seed_kb(db_session, kb_id="KB-B")
    _seed_version(db_session, version_id="KBV-B", kb_id="KB-B")
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.calibrate(
            "KB-A",
            version_id="KBV-B",
            eval_dataset_id="ED-x",
            identity=_identity(),
            actor="@test",
        )
    assert exc.value.status_code == 404
    assert "does not belong" in exc.value.detail


def test_get_calibration_run_missing_404(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.get_calibration_run("KBTR-missing", identity=_identity())
    assert exc.value.status_code == 404


def test_query_runner_handles_missing_version_result(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    runner = service._build_query_runner(version_id="KBV-1", identity=_identity())

    class _EmptyResult:
        versions: list[object] = []

    monkeypatch.setattr(service, "query", lambda *_a, **_k: _EmptyResult())
    outcome = runner("q", 3, "dense")
    assert outcome.answer_error == "no version result returned"


# ---------------------------------------------------------------------------
# explore_graph — strict AGE branch + local graph traversal
# ---------------------------------------------------------------------------


def test_explore_graph_strict_age_returns_fallback_shape(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-1")
    db_session.commit()
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_version_age_ready", lambda *a, **k: True)
    monkeypatch.setattr(
        service._age_store,
        "explore",
        lambda **kw: AgeGraphExploreResult(
            status="fallback", graph_name="g", fallback_reason="nothing matched"
        ),
    )

    strict = service.explore_graph(
        "KBV-1", identity=_identity(), source="age", strict_age_retrieval=True
    )
    assert strict.served_source == "age"
    assert strict.fallback_reason == "nothing matched"
    assert strict.entities == []

    # Non-strict AGE miss falls through to the local-graph response.
    lenient = service.explore_graph(
        "KBV-1", identity=_identity(), source="age", strict_age_retrieval=False
    )
    assert lenient.served_source == "local"


def _seed_local_graph(session: Session, *, version_id: str = "KBV-L", kb_id: str = "KB-L") -> None:
    _seed_kb(session, kb_id=kb_id)
    _seed_version(session, version_id=version_id, kb_id=kb_id)
    specs = [
        ("E1", "alpha", "Alpha", ["CH-1"], 9),
        ("E2", "beta", "Beta", ["CH-2"], 5),
        ("E3", "gamma", "Gamma", ["CH-3"], 3),
    ]
    for eid, key, label, chunks, mentions in specs:
        session.add(
            CaliberKnowledgeBaseEntity(
                knowledge_base_entity_id=eid,
                knowledge_base_version_id=version_id,
                entity_key=key,
                label=label,
                entity_type="concept",
                aliases=[],
                mention_count=mentions,
                source_documents=["DOC"],
                source_keys=["guide.md"],
                source_chunks=chunks,
            )
        )
    session.add(
        CaliberKnowledgeBaseRelationship(
            knowledge_base_relationship_id="R1",
            knowledge_base_version_id=version_id,
            source_entity_id="E1",
            target_entity_id="E2",
            relationship_type="mentions",
            weight=2.0,
            evidence_chunk_ids=[],
            source_documents=[],
        )
    )
    session.add(
        CaliberKnowledgeBaseRelationship(
            knowledge_base_relationship_id="R2",
            knowledge_base_version_id=version_id,
            source_entity_id="E2",
            target_entity_id="E3",
            relationship_type="mentions",
            weight=1.0,
            evidence_chunk_ids=[],
            source_documents=[],
        )
    )


def test_explore_graph_local_expands_two_hops(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_local_graph(db_session)
    db_session.commit()
    service = _service(app_config, session_factory)
    result = service.explore_graph(
        "KBV-L", identity=_identity(), query="alpha", traversal_hops=2, node_limit=12
    )
    labels = {entity.label for entity in result.entities}
    assert "Alpha" in labels
    assert "Beta" in labels  # one hop
    assert result.matched_entity_labels == ["Alpha"]
    assert "Beta" in result.expanded_entity_labels


def test_explore_graph_local_relationship_seed_and_no_query(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_local_graph(db_session)
    db_session.commit()
    service = _service(app_config, session_factory)
    # Query matches only a relationship_type, not an entity label -> relationship
    # seed path pulls in the endpoints.
    via_rel = service.explore_graph(
        "KBV-L", identity=_identity(), query="mentions", traversal_hops=1
    )
    assert via_rel.entities
    # No query -> seed the whole eligible set.
    all_nodes = service.explore_graph("KBV-L", identity=_identity(), query="", traversal_hops=1)
    assert len(all_nodes.entities) >= 3


def test_explore_graph_local_no_match_and_type_filter(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_local_graph(db_session)
    db_session.commit()
    service = _service(app_config, session_factory)
    # Query matches nothing -> empty result.
    empty = service.explore_graph("KBV-L", identity=_identity(), query="zzznomatch")
    assert empty.entities == []
    # entity_type filter excludes every entity -> empty result.
    filtered = service.explore_graph("KBV-L", identity=_identity(), entity_type="person")
    assert filtered.entities == []


# ---------------------------------------------------------------------------
# _expand_sources + _refresh_version_age_sync_artifacts (moto)
# ---------------------------------------------------------------------------


@mock_aws
def test_expand_sources_files_folders_and_reserved(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-src")
    s3.put_object(Bucket="kb-src", Key="docs/a.md", Body=b"alpha")
    s3.put_object(Bucket="kb-src", Key="docs/b.txt", Body=b"beta")
    s3.put_object(Bucket="kb-src", Key="docs/sub/", Body=b"")  # folder marker -> skipped
    s3.put_object(Bucket="kb-src", Key=".caliber/knowledge-bases/x.json", Body=b"{}")
    service = _service(app_config, session_factory, object_store_client=s3)

    expanded = service._expand_sources(
        "kb-src",
        [
            {"kind": "file", "path": ""},  # blank path -> skipped
            {"kind": "file", "path": "docs/a.md"},
            {"kind": "file", "path": ".caliber/knowledge-bases/x.json"},  # reserved -> skipped
            {"kind": "folder", "path": "docs/"},  # a.md already seen; sub/ marker skipped
        ],
    )
    keys = sorted(item.object_key for item in expanded)
    assert keys == ["docs/a.md", "docs/b.txt"]


@mock_aws
def test_refresh_age_sync_artifacts_builds_missing_graph(
    app_config: CaliberConfig, session_factory: sessionmaker[Session]
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-refresh")
    service = _service(app_config, session_factory, object_store_client=s3)
    graph_config = service._resolve_graph_config(None)
    # No graph.json present -> get_object raises -> payload rebuilt from summary.
    service._refresh_version_age_sync_artifacts(
        knowledge_base_id="KB-1",
        version_id="KBV-1",
        version_status="completed",
        version_error_summary=None,
        source_bucket="kb-refresh",
        source_manifest=[],
        chunking_strategy="recursive",
        embedding_model="m",
        chunking_config={},
        output_bucket="kb-refresh",
        output_prefix="prefix",
        graph_config=graph_config,
        summary={"entity_count": 2, "relationship_count": 1},
        age_sync_summary={"status": "synced", "graph_name": "g"},
    )
    written = json_load(s3, "kb-refresh", "prefix/graph.json")
    assert written["metadata"]["age"]["status"] == "synced"


def json_load(s3: object, bucket: str, key: str) -> dict[str, object]:
    import json

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()  # type: ignore[attr-defined]
    return json.loads(body)


def test_refresh_age_sync_artifacts_noop_without_client(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_object_store", lambda: None)
    graph_config = service._resolve_graph_config(None)
    # Client None -> early return, no error.
    service._refresh_version_age_sync_artifacts(
        knowledge_base_id="KB-1",
        version_id="KBV-1",
        version_status="completed",
        version_error_summary=None,
        source_bucket="b",
        source_manifest=[],
        chunking_strategy="recursive",
        embedding_model="m",
        chunking_config={},
        output_bucket="b",
        output_prefix="p",
        graph_config=graph_config,
        summary={},
        age_sync_summary={},
    )


# ---------------------------------------------------------------------------
# create_version queued path + rollback self-referential audit skip
# ---------------------------------------------------------------------------


def test_create_version_queues_build_when_enabled(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "ensure_embedding_backend_runtime_available",
        lambda *, allow_flagged_local_embeddings=False: None,
    )
    _seed_kb(db_session, source_manifest=[{"kind": "file", "path": "a.md"}])
    db_session.commit()
    queued_config = app_config.model_copy(
        update={"background_tasks_enabled": True, "knowledge_build_queue_enabled": True}
    )
    service = _service(queued_config, session_factory)
    payload = KnowledgeBaseVersionCreateRequest(
        chunking_strategy="recursive",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
    )
    result = service.create_version("KB-1", payload, identity=_identity(), actor="@test")
    assert result.version.status == "queued"
    assert result.run.status == "queued"


def test_rollback_skips_self_referential_activation_rows(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, active_version_id="KBV-2")
    _seed_version(db_session, version_id="KBV-2", version_number=2)
    _seed_version(db_session, version_id="KBV-1", version_number=1, status="queued")
    # Oldest -> valid prior; newest -> self-referential (must be skipped).
    _seed_activation_audit(db_session, current="KBV-2", prior="KBV-1")
    db_session.commit()
    _seed_activation_audit(db_session, current="KBV-2", prior="KBV-2")
    db_session.commit()
    service = _service(app_config, session_factory)
    with pytest.raises(HTTPException) as exc:
        service.rollback_version("KB-1", identity=_identity(), actor="@test")
    # Self-ref row skipped, valid prior resolved to KBV-1 which is not completed.
    assert exc.value.status_code == 409
    assert "not in a completed state" in exc.value.detail


# ---------------------------------------------------------------------------
# Reranker cache + length-mismatch guard
# ---------------------------------------------------------------------------


def test_reranker_backend_caches_built_model(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "ensure_embedding_backend_runtime_available",
        lambda *, allow_flagged_local_embeddings=False: None,
    )
    sentinel = object()
    monkeypatch.setattr(knowledge_service, "build_reranker_backend", lambda _model: sentinel)
    rerank_config = app_config.model_copy(
        update={"knowledge_rerank_enabled": True, "knowledge_rerank_model": "cross-x"}
    )
    service = _service(rerank_config, session_factory)
    first = service._reranker_backend()
    second = service._reranker_backend()
    assert first is sentinel
    assert second is sentinel  # cache hit


def test_rerank_returns_pool_on_score_length_mismatch(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)

    class _FakeReranker:
        def rerank_scores(self, _question: str, _texts: list[str]) -> list[float]:
            return [1.0]  # deliberately wrong length

    monkeypatch.setattr(service, "_reranker_backend", lambda: _FakeReranker())
    items = [
        knowledge_service._RetrievedChunk(
            chunk=_chunk("c1", ordinal=1), score=0.5, dense_score=0.5
        ),
        knowledge_service._RetrievedChunk(
            chunk=_chunk("c2", ordinal=2), score=0.4, dense_score=0.4
        ),
    ]
    result = service._rerank("q", items, top_k=1)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# _persist_artifacts without a client + local-graph node-limit break
# ---------------------------------------------------------------------------


def test_persist_artifacts_without_client_returns_empty(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_object_store", lambda: None)
    artifacts = service._persist_artifacts(
        source_bucket="b",
        output_prefix="p",
        source_rows=[],
        chunk_exports=[],
        entity_exports=[],
        relationship_exports=[],
        graph_export=None,
        events=[],
        manifest={},
        stats={},
    )
    assert artifacts.logs_uri is None
    assert artifacts.chunks_uri is None


def test_explore_graph_local_respects_node_limit(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
) -> None:
    _seed_kb(db_session, kb_id="KB-STAR")
    _seed_version(db_session, version_id="KBV-STAR", kb_id="KB-STAR")
    for eid, key, label in [
        ("S0", "hub", "Hub"),
        ("S1", "one", "One"),
        ("S2", "two", "Two"),
        ("S3", "three", "Three"),
        ("S4", "four", "Four"),
        ("S5", "five", "Five"),
    ]:
        db_session.add(
            CaliberKnowledgeBaseEntity(
                knowledge_base_entity_id=eid,
                knowledge_base_version_id="KBV-STAR",
                entity_key=key,
                label=label,
                entity_type="concept",
                aliases=[],
                mention_count=5,
                source_documents=["DOC"],
                source_keys=["guide.md"],
                source_chunks=[f"CH-{eid}"],
            )
        )
    for idx, target in enumerate(("S1", "S2", "S3", "S4", "S5")):
        db_session.add(
            CaliberKnowledgeBaseRelationship(
                knowledge_base_relationship_id=f"SR{idx}",
                knowledge_base_version_id="KBV-STAR",
                source_entity_id="S0",
                target_entity_id=target,
                relationship_type="mentions",
                weight=1.0,
                evidence_chunk_ids=[],
                source_documents=[],
            )
        )
    db_session.commit()
    service = _service(app_config, session_factory)
    # node_limit is clamped to a minimum of 4; with 5 candidate neighbors the
    # per-neighbor break fires once the visible set reaches the cap.
    result = service.explore_graph(
        "KBV-STAR", identity=_identity(), query="hub", traversal_hops=1, node_limit=4
    )
    assert len(result.entities) <= 4
    assert "Hub" in {entity.label for entity in result.entities}


# ---------------------------------------------------------------------------
# sync_version_to_age internals (AGE forced available)
# ---------------------------------------------------------------------------


def test_sync_version_to_age_requires_completed_version(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-1", status="queued")
    db_session.commit()
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_age_available", lambda: True)
    with pytest.raises(HTTPException) as exc:
        service.sync_version_to_age("KBV-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 409
    assert "completed" in exc.value.detail


def test_sync_version_to_age_requires_chunks(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-1", status="completed")
    db_session.commit()
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_age_available", lambda: True)
    with pytest.raises(HTTPException) as exc:
        service.sync_version_to_age("KBV-1", identity=_identity(), actor="@test")
    assert exc.value.status_code == 409
    assert "no chunks" in exc.value.detail


def test_sync_version_to_age_persists_failure_summary(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_kb(db_session)
    _seed_version(db_session, version_id="KBV-1", status="completed")
    db_session.add(_chunk("CH-1", content="alpha"))
    db_session.commit()
    service = _service(app_config, session_factory)
    monkeypatch.setattr(service, "_age_available", lambda: True)
    # Force the artifact-refresh to raise so the except branch is exercised.
    monkeypatch.setattr(
        service,
        "_refresh_version_age_sync_artifacts",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("refresh boom")),
    )
    version = service.sync_version_to_age("KBV-1", identity=_identity(), actor="@test")
    # The real (disabled) AGE store returns a failed sync -> persisted on summary.
    assert version.knowledge_base_version_id == "KBV-1"

    db_session.expire_all()
    row = db_session.get(CaliberKnowledgeBaseVersion, "KBV-1")
    assert row is not None
    assert row.summary.get("age_sync_status") == "failed"
    assert row.summary.get("age_sync_error")


# ---------------------------------------------------------------------------
# Build source-loop skips + no-chunk failure (moto + dummy embedder)
# ---------------------------------------------------------------------------


def _seed_build_triple(session: Session, *, suffix: str) -> None:
    _seed_kb(session, kb_id=f"KB-{suffix}")
    _seed_version(session, version_id=f"KBV-{suffix}", kb_id=f"KB-{suffix}", status="processing")
    session.add(
        CaliberKnowledgeBaseRun(
            knowledge_base_run_id=f"KBR-{suffix}",
            knowledge_base_id=f"KB-{suffix}",
            knowledge_base_version_id=f"KBV-{suffix}",
            status="running",
            source_manifest=[],
            started_at=_utcnow(),
        )
    )


def _run_build(service: KnowledgeBaseService, *, suffix: str, bucket: str) -> None:
    service._process_build(
        knowledge_base_id=f"KB-{suffix}",
        knowledge_base_name=f"KB {suffix}",
        version_id=f"KBV-{suffix}",
        version_number=1,
        run_id=f"KBR-{suffix}",
        source_bucket=bucket,
        source_manifest=[{"kind": "file", "path": "good.md"}],
        chunking_strategy="recursive",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        chunking_config={},
        graph_config=service._resolve_graph_config(None),
    )


@mock_aws
def test_process_build_ingestion_error_source(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_routes_knowledge_bases import _DummyEmbedder

    monkeypatch.setattr(
        knowledge_service, "build_embedding_backend", lambda mid: _DummyEmbedder(mid)
    )

    def _boom(_path: str, *, max_chars: int) -> dict[str, object]:
        raise knowledge_service.IngestionError("cannot parse")

    monkeypatch.setattr(knowledge_service, "extract_document", _boom)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-ie")
    s3.put_object(Bucket="kb-ie", Key="good.md", Body=b"content", ContentType="text/markdown")
    _seed_build_triple(db_session, suffix="IE")
    db_session.commit()
    service = _service(app_config, session_factory, object_store_client=s3)
    _run_build(service, suffix="IE", bucket="kb-ie")

    db_session.expire_all()
    version = db_session.get(CaliberKnowledgeBaseVersion, "KBV-IE")
    assert version is not None and version.status == "failed"


@mock_aws
def test_process_build_empty_text_source(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_routes_knowledge_bases import _DummyEmbedder

    monkeypatch.setattr(
        knowledge_service, "build_embedding_backend", lambda mid: _DummyEmbedder(mid)
    )
    monkeypatch.setattr(
        knowledge_service,
        "extract_document",
        lambda _path, *, max_chars: {"text": "   ", "format": "md", "chars": 0, "ocr_used": False},
    )
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-empty")
    s3.put_object(Bucket="kb-empty", Key="good.md", Body=b"content", ContentType="text/markdown")
    _seed_build_triple(db_session, suffix="EM")
    db_session.commit()
    service = _service(app_config, session_factory, object_store_client=s3)
    _run_build(service, suffix="EM", bucket="kb-empty")

    db_session.expire_all()
    version = db_session.get(CaliberKnowledgeBaseVersion, "KBV-EM")
    assert version is not None and version.status == "failed"


@mock_aws
def test_process_build_zero_chunks_source(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from .test_routes_knowledge_bases import _DummyEmbedder

    monkeypatch.setattr(
        knowledge_service, "build_embedding_backend", lambda mid: _DummyEmbedder(mid)
    )
    monkeypatch.setattr(
        knowledge_service,
        "extract_document",
        lambda _path, *, max_chars: {"text": "real content", "format": "md", "chars": 12},
    )
    monkeypatch.setattr(knowledge_service, "chunk_text", lambda *a, **k: [])
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-zero")
    s3.put_object(Bucket="kb-zero", Key="good.md", Body=b"content", ContentType="text/markdown")
    _seed_build_triple(db_session, suffix="ZC")
    db_session.commit()
    service = _service(app_config, session_factory, object_store_client=s3)
    _run_build(service, suffix="ZC", bucket="kb-zero")

    db_session.expire_all()
    version = db_session.get(CaliberKnowledgeBaseVersion, "KBV-ZC")
    assert version is not None and version.status == "failed"


@mock_aws
def test_process_build_swallows_artifact_persist_failure(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-art")
    _seed_build_triple(db_session, suffix="ART")
    db_session.commit()
    service = _service(app_config, session_factory, object_store_client=s3)

    def _boom(**_kw: object) -> object:
        raise RuntimeError("artifact write failed")

    monkeypatch.setattr(service, "_persist_artifacts", _boom)
    # Empty folder -> no sources -> ValueError -> persist_failure; artifact write
    # raises inside persist_failure's try and is swallowed, DB still updated.
    service._process_build(
        knowledge_base_id="KB-ART",
        knowledge_base_name="KB ART",
        version_id="KBV-ART",
        version_number=1,
        run_id="KBR-ART",
        source_bucket="kb-art",
        source_manifest=[{"kind": "folder", "path": "missing/"}],
        chunking_strategy="recursive",
        embedding_model="sentence-transformers/all-MiniLM-L6-v2",
        chunking_config={},
        graph_config=service._resolve_graph_config(None),
    )

    db_session.expire_all()
    version = db_session.get(CaliberKnowledgeBaseVersion, "KBV-ART")
    assert version is not None and version.status == "failed"
