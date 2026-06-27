from __future__ import annotations

import json
import math
import re

import pytest
from starlette.testclient import TestClient

import caliber.knowledge.service as knowledge_service
from caliber.knowledge.age import (
    AgeChunkCandidate,
    AgeGraphEntity,
    AgeGraphExploreResult,
    AgeGraphRelationship,
    AgeRetrievalResult,
    AgeSyncResult,
)
from caliber.knowledge.worker import KnowledgeBaseWorker
from caliber.server import create_app

boto3 = pytest.importorskip("boto3")
mock_aws = pytest.importorskip("moto").mock_aws

PREFIX = "/ajax-api/2.0/mlflow/caliber"
KB = PREFIX + "/knowledge-bases"
QUERY = PREFIX + "/knowledge/query"


class _DummyEmbedder:
    def __init__(self, model_id: str, dimension: int = 8) -> None:
        self.model_id = model_id
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


def _wire_moto(client: TestClient):
    s3 = boto3.client("s3", region_name="us-east-1")
    client.app.state.object_store_client = s3
    return s3


def _put_text(s3, bucket: str, key: str, body: str, content_type: str = "text/plain") -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType=content_type)


def _put_bytes(
    s3, bucket: str, key: str, body: bytes, content_type: str = "application/octet-stream"
) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)


@mock_aws
def test_knowledge_base_options_expose_chunkers_and_models(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_moto(client)
    monkeypatch.setattr(
        knowledge_service,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: None,
    )
    response = client.get(f"{KB}/options")
    assert response.status_code == 200, response.text

    data = response.json()["data"]
    strategies = {item["id"] for item in data["chunking_strategies"]}
    assert {"recursive", "semantic", "markdown", "token", "character"} <= strategies

    embedding_models = {item["id"] for item in data["embedding_models"]}
    assert {
        "BAAI/bge-m3",
        "intfloat/e5-large-v2",
        "Qwen/Qwen3-Embedding-0.6B",
        "sentence-transformers/all-MiniLM-L6-v2",
    } <= embedding_models
    minilm = next(
        item
        for item in data["embedding_models"]
        if item["id"] == "sentence-transformers/all-MiniLM-L6-v2"
    )
    assert minilm["available"] is True
    assert minilm["unavailable_reason"] is None
    assert minilm["requires_override"] is False
    retrieval_modes = {item["id"] for item in data["retrieval_modes"]}
    assert {"dense", "hybrid", "graph_hybrid"} <= retrieval_modes
    hybrid_mode = next(item for item in data["retrieval_modes"] if item["id"] == "hybrid")
    assert hybrid_mode["name"] == "Hybrid (keyword + vector)"
    assert set(hybrid_mode["tags"]) == {"hybrid", "keyword", "vector"}
    graph_presets = {item["id"] for item in data["graph_build_presets"]}
    assert graph_presets == {"portable", "balanced"}
    assert {item["id"] for item in data["graph_query_presets"]} == {
        "hybrid_precision",
        "hybrid_balanced",
    }

    assert data["age_enabled"] is False
    assert data["age_viewer_url"] is None
    assert data["age_unavailable_reason"] == "Apache AGE is disabled by configuration."
    assert data["reserved_output_prefix"] == ".caliber/knowledge-bases"


@mock_aws
def test_knowledge_base_options_flag_embedding_models_when_runtime_is_blocked(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_moto(client)
    monkeypatch.setattr(
        knowledge_service,
        "local_embedding_block_reason",
        lambda *, allow_flagged=False: (
            "Local Hugging Face embedding builds are blocked because the current runtime "
            "includes flagged dependencies: torch 2.12.0 (CVE-2025-3000). Set "
            "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true only if you explicitly accept "
            "the risk for this deployment."
        ),
    )

    response = client.get(f"{KB}/options")
    assert response.status_code == 200, response.text

    data = response.json()["data"]
    minilm = next(
        item
        for item in data["embedding_models"]
        if item["id"] == "sentence-transformers/all-MiniLM-L6-v2"
    )
    assert minilm["available"] is False
    assert "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true" in minilm["unavailable_reason"]
    assert minilm["requires_override"] is True


def test_create_knowledge_base_returns_503_when_embedding_runtime_is_blocked(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"allow_flagged_local_embeddings": False}
    )
    monkeypatch.setattr(
        knowledge_service,
        "ensure_embedding_backend_runtime_available",
        lambda *, allow_flagged_local_embeddings=False: (_ for _ in ()).throw(
            knowledge_service.KnowledgeDependencyError("local embedding runtime blocked")
        ),
    )

    response = client.post(
        KB,
        json={
            "name": "Blocked Docs",
            "description": "Should fail fast before creating a run",
            "source_bucket": "knowledge-docs",
            "sources": [{"kind": "folder", "path": "docs/"}],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
        },
    )

    assert response.status_code == 503
    assert "local embedding runtime blocked" in response.text


@mock_aws
def test_create_version_returns_503_when_embedding_runtime_is_blocked(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )
    monkeypatch.setattr(
        knowledge_service,
        "ensure_embedding_backend_runtime_available",
        lambda *, allow_flagged_local_embeddings=False: None,
    )

    s3 = _wire_moto(client)
    s3.create_bucket(Bucket="knowledge-docs")
    _put_text(
        s3,
        "knowledge-docs",
        "docs/guide.md",
        "# Product Guide\n\nDark mode applies consistently across linked tools.\n",
        content_type="text/markdown",
    )

    create = client.post(
        KB,
        json={
            "name": "Versioned Docs",
            "description": "Baseline knowledge base",
            "source_bucket": "knowledge-docs",
            "sources": [{"kind": "folder", "path": "docs/"}],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
        },
    )
    assert create.status_code == 201, create.text
    knowledge_base_id = create.json()["data"]["knowledge_base"]["knowledge_base_id"]

    client.app.state.config = client.app.state.config.model_copy(
        update={"allow_flagged_local_embeddings": False}
    )
    monkeypatch.setattr(
        knowledge_service,
        "ensure_embedding_backend_runtime_available",
        lambda *, allow_flagged_local_embeddings=False: (_ for _ in ()).throw(
            knowledge_service.KnowledgeDependencyError("local embedding runtime blocked")
        ),
    )

    response = client.post(
        f"{KB}/{knowledge_base_id}/versions",
        json={
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
        },
    )

    assert response.status_code == 503
    assert "local embedding runtime blocked" in response.text


@mock_aws
def test_knowledge_base_build_versions_query_and_rollback(  # noqa: PLR0915
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    s3 = _wire_moto(client)
    bucket = "knowledge-docs"
    s3.create_bucket(Bucket=bucket)
    _put_text(
        s3,
        bucket,
        "product/guide.md",
        """# Product Guide

Retries happen three times before an alert is sent.

Dark mode applies consistently across linked tools.
""",
        content_type="text/markdown",
    )
    _put_text(
        s3,
        bucket,
        "faq.txt",
        "Chunk outputs are written under a reserved knowledge-base prefix for traceable reuse.\n",
    )
    _put_bytes(s3, bucket, "product/blob.bin", b"\x00\x01\x02\x03")

    create = client.post(
        KB,
        json={
            "name": "Product Docs",
            "description": "Support and ops documentation",
            "source_bucket": bucket,
            "sources": [
                {"kind": "folder", "path": "product/"},
                {"kind": "file", "path": "faq.txt"},
            ],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 90, "chunk_overlap": 12},
        },
    )
    assert create.status_code == 201, create.text
    create_data = create.json()["data"]

    knowledge_base = create_data["knowledge_base"]
    version_one = create_data["version"]
    run_one = create_data["run"]

    assert knowledge_base["name"] == "Product Docs"
    assert knowledge_base["active_version_id"] == version_one["knowledge_base_version_id"]
    assert knowledge_base["last_run_status"] == "completed"
    assert (
        knowledge_base["active_version_summary"]["knowledge_base_version_id"]
        == version_one["knowledge_base_version_id"]
    )
    assert knowledge_base["active_version_summary"]["graph_target"] == "object_store"
    assert knowledge_base["active_version_summary"]["graph_profile_label"] == "Balanced GraphRAG"
    assert knowledge_base["active_version_summary"]["age_ready"] is False
    assert version_one["version_number"] == 1
    assert version_one["status"] == "completed"
    assert version_one["graph_profile_label"] == "Balanced GraphRAG"
    assert version_one["summary"]["processed_sources"] == 2
    assert version_one["summary"]["skipped_sources"] == 1
    assert version_one["summary"]["chunk_count"] >= 2
    assert version_one["summary"]["entity_count"] >= 1
    assert version_one["summary"]["graph_profile_label"] == "Balanced GraphRAG"
    assert version_one["embedding_dimension"] == 8
    assert run_one["status"] == "completed"
    assert run_one["metrics"]["chunk_count"] == version_one["summary"]["chunk_count"]

    versions_initial = client.get(f"{KB}/{knowledge_base['knowledge_base_id']}/versions")
    assert versions_initial.status_code == 200, versions_initial.text
    assert len(versions_initial.json()["data"]) == 1

    sources = client.get(
        f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/sources"
    )
    assert sources.status_code == 200, sources.text
    source_rows = sources.json()["data"]
    assert len(source_rows) == 3
    processed = [row for row in source_rows if row["status"] == "processed"]
    skipped = [row for row in source_rows if row["status"] == "skipped"]
    assert len(processed) == 2
    assert len(skipped) == 1
    assert skipped[0]["error_summary"] == "unsupported or binary file type"
    assert all(
        row["object_store_path"].startswith(f"/object-store?bucket={bucket}&key=")
        for row in source_rows
    )

    chunks = client.get(
        f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/chunks",
        params={"limit": "50"},
    )
    assert chunks.status_code == 200, chunks.text
    chunk_rows = chunks.json()["data"]
    assert chunk_rows
    assert all(len(row["embedding"]) == 8 for row in chunk_rows)
    assert {row["source_key"] for row in chunk_rows} <= {"product/guide.md", "faq.txt"}

    entities = client.get(
        f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/entities"
    )
    assert entities.status_code == 200, entities.text
    entity_rows = entities.json()["data"]
    assert entity_rows
    assert entity_rows[0]["mention_count"] >= 1

    relationships = client.get(
        f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/relationships"
    )
    assert relationships.status_code == 200, relationships.text
    relationship_rows = relationships.json()["data"]
    assert relationship_rows
    assert relationship_rows[0]["weight"] >= 1

    graph_view = client.get(
        f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/graph"
    )
    assert graph_view.status_code == 200, graph_view.text
    graph_view_data = graph_view.json()["data"]
    assert graph_view_data["requested_source"] == "local"
    assert graph_view_data["served_source"] == "local"
    assert graph_view_data["entities"]
    assert graph_view_data["relationships"]
    assert graph_view_data["entities"][0]["graph_source"] == "local"

    age_fallback = client.get(
        f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/graph",
        params={"source": "age"},
    )
    assert age_fallback.status_code == 200, age_fallback.text
    age_fallback_data = age_fallback.json()["data"]
    assert age_fallback_data["requested_source"] == "age"
    assert age_fallback_data["served_source"] == "local"
    assert age_fallback_data["strict_age_retrieval"] is False
    assert age_fallback_data["fallback_reason"]

    runs = client.get(f"{KB}/{knowledge_base['knowledge_base_id']}/runs")
    assert runs.status_code == 200, runs.text
    run_rows = runs.json()["data"]
    assert len(run_rows) == 1
    assert run_rows[0]["knowledge_base_run_id"] == run_one["knowledge_base_run_id"]
    assert run_rows[0]["log_line_count"] >= 4

    run_events = client.get(f"{PREFIX}/knowledge-runs/{run_one['knowledge_base_run_id']}/events")
    assert run_events.status_code == 200, run_events.text
    event_types = [item["event_type"] for item in run_events.json()["data"]]
    assert "build_started" in event_types
    assert "sources_expanded" in event_types
    assert "source_completed" in event_types
    assert "source_skipped" in event_types
    assert "build_completed" in event_types

    objects = s3.list_objects_v2(
        Bucket=bucket,
        Prefix=f"{version_one['output_prefix']}/",
    )
    keys = {item["Key"] for item in objects.get("Contents", []) or []}
    assert f"{version_one['output_prefix']}/chunks.jsonl" in keys
    assert f"{version_one['output_prefix']}/entities.jsonl" in keys
    assert f"{version_one['output_prefix']}/relationships.jsonl" in keys
    assert f"{version_one['output_prefix']}/graph.json" in keys
    assert f"{version_one['output_prefix']}/manifest.json" in keys
    assert f"{version_one['output_prefix']}/logs.jsonl" in keys
    assert f"{version_one['output_prefix']}/sources.jsonl" in keys
    assert f"{version_one['output_prefix']}/stats.json" in keys

    manifest_payload = json.loads(
        s3.get_object(Bucket=bucket, Key=f"{version_one['output_prefix']}/manifest.json")[
            "Body"
        ].read()
    )
    assert manifest_payload["knowledge_base_id"] == knowledge_base["knowledge_base_id"]
    assert manifest_payload["summary"]["chunk_count"] == version_one["summary"]["chunk_count"]
    assert manifest_payload["summary"]["entity_count"] == version_one["summary"]["entity_count"]

    second = client.post(
        f"{KB}/{knowledge_base['knowledge_base_id']}/versions",
        json={
            "chunking_strategy": "markdown",
            "embedding_model": "BAAI/bge-m3",
            "chunking_config": {"chunk_size": 85, "chunk_overlap": 10},
        },
    )
    assert second.status_code == 201, second.text
    second_data = second.json()["data"]
    version_two = second_data["version"]

    assert version_two["version_number"] == 2
    assert version_two["status"] == "completed"
    assert version_two["output_prefix"] != version_one["output_prefix"]
    assert (
        second_data["knowledge_base"]["active_version_id"]
        == version_two["knowledge_base_version_id"]
    )
    assert (
        second_data["knowledge_base"]["active_version_summary"]["knowledge_base_version_id"]
        == version_two["knowledge_base_version_id"]
    )

    versions = client.get(f"{KB}/{knowledge_base['knowledge_base_id']}/versions")
    assert versions.status_code == 200, versions.text
    version_rows = versions.json()["data"]
    assert [row["version_number"] for row in version_rows] == [2, 1]

    from caliber.observability.mlflow_tracing import Tracer, set_tracer

    from .test_mlflow_tracing import FakeMlflow

    _rag_trace = FakeMlflow()
    set_tracer(Tracer(mlflow_module=_rag_trace))
    compare = client.post(
        QUERY,
        json={
            "version_ids": [
                version_one["knowledge_base_version_id"],
                version_two["knowledge_base_version_id"],
            ],
            "question": "How many retries happen before an alert is sent?",
            "top_k": 3,
            "retrieval_modes": ["dense"],
        },
    )
    assert compare.status_code == 200, compare.text
    compare_data = compare.json()["data"]
    assert compare_data["question"] == "How many retries happen before an alert is sent?"
    assert len(compare_data["versions"]) == 2
    for result in compare_data["versions"]:
        assert result["retrieved_chunks"]
        assert result["citations"]
        assert any(
            "Retries happen three times" in chunk["content"] for chunk in result["retrieved_chunks"]
        )
        assert result["timing_ms"]["total"] >= 0
        assert result["retrieval_mode"] == "dense"

    # RAG retrieval now emits one RETRIEVER span per completed version queried.
    set_tracer(None)
    rag_spans = [s for s in _rag_trace.spans if s.name == "rag.query.dense"]
    assert len(rag_spans) == 2
    rag_span = rag_spans[0]
    assert rag_span.span_type == "RETRIEVER"
    assert rag_span.attributes["caliber.rag.mode"] == "dense"
    assert rag_span.attributes["caliber.rag.top_k"] == 3
    assert rag_span.attributes["caliber.rag.retrieved_count"] >= 1
    assert "caliber.rag.scores" in rag_span.attributes
    assert "caliber.rag.retrieval_ms" in rag_span.attributes
    assert "caliber.rag.generation_ms" in rag_span.attributes
    assert rag_span.attributes["caliber.status"] == "completed"

    graph_compare = client.post(
        QUERY,
        json={
            "version_ids": [version_one["knowledge_base_version_id"]],
            "question": "What does Product Guide say about Dark mode?",
            "top_k": 3,
            "retrieval_modes": ["dense", "graph_hybrid"],
        },
    )
    assert graph_compare.status_code == 200, graph_compare.text
    graph_compare_data = graph_compare.json()["data"]
    assert len(graph_compare_data["versions"]) == 2
    graph_result = next(
        item for item in graph_compare_data["versions"] if item["retrieval_mode"] == "graph_hybrid"
    )
    assert graph_result["graph_context"]["matched_entities"]
    assert graph_result["graph_context"]["boosted_chunk_count"] >= 1
    assert all("score_breakdown" in chunk for chunk in graph_result["retrieved_chunks"])
    # graph_hybrid is a tri-hybrid: a matched-entity result fuses dense + graph + lexical.
    boosted_chunk = next(
        chunk
        for chunk in graph_result["retrieved_chunks"]
        if chunk["score_breakdown"].get("graph_boost", 0.0) > 0.0
    )
    assert {"dense", "graph_boost", "lexical"} <= set(boosted_chunk["score_breakdown"])

    rollback = client.post(
        f"{KB}/{knowledge_base['knowledge_base_id']}/versions/{version_one['knowledge_base_version_id']}/activate",
        json={},
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["data"]["active_version_id"] == version_one["knowledge_base_version_id"]

    detail = client.get(f"{KB}/{knowledge_base['knowledge_base_id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["active_version_id"] == version_one["knowledge_base_version_id"]
    assert (
        detail.json()["data"]["active_version_summary"]["knowledge_base_version_id"]
        == version_one["knowledge_base_version_id"]
    )


@mock_aws
def test_knowledge_base_queue_worker_processes_queued_runs(
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    queue_config = app_config.model_copy(
        update={
            "background_tasks_enabled": True,
            "knowledge_build_queue_enabled": True,
            "knowledge_build_worker_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=queue_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as client:
        s3 = _wire_moto(client)
        bucket = "queued-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Ops Guide

Caliber keeps object-store lineage for every build version.
The worker should resume queued runs after restarts.
""",
            content_type="text/markdown",
        )

        create = client.post(
            KB,
            json={
                "name": "Queued Docs",
                "description": "Queue-backed build",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
            },
        )
        assert create.status_code == 201, create.text
        create_data = create.json()["data"]
        knowledge_base = create_data["knowledge_base"]
        version = create_data["version"]
        run = create_data["run"]

        assert knowledge_base["last_run_status"] == "queued"
        assert version["status"] == "queued"
        assert run["status"] == "queued"
        assert run["queued_at"] is not None

        worker = KnowledgeBaseWorker(
            session_factory,
            config=queue_config,
            object_store_client=s3,
        )
        worker._tick()

        versions = client.get(f"{KB}/{knowledge_base['knowledge_base_id']}/versions")
        assert versions.status_code == 200, versions.text
        version_row = versions.json()["data"][0]
        assert version_row["status"] == "completed"
        assert version_row["summary"]["entity_count"] >= 1

        runs = client.get(f"{KB}/{knowledge_base['knowledge_base_id']}/runs")
        assert runs.status_code == 200, runs.text
        run_row = runs.json()["data"][0]
        assert run_row["status"] == "completed"
        assert run_row["claimed_by"]
        assert run_row["last_heartbeat_at"] is not None

        events = client.get(f"{PREFIX}/knowledge-runs/{run['knowledge_base_run_id']}/events")
        assert events.status_code == 200, events.text
        event_types = [item["event_type"] for item in events.json()["data"]]
        assert "build_queued" in event_types
        assert "build_started" in event_types
        assert "build_completed" in event_types


@mock_aws
def test_knowledge_base_age_graph_sync_and_retrieval(  # noqa: PLR0915
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    class _FakeAgeStore:
        graph_name = "knowledge_graph"

        def __init__(self) -> None:
            self.version_id = ""
            self.chunk_ids: list[str] = []
            self.retrieve_kwargs: dict[str, object] | None = None
            self.explore_kwargs: dict[str, object] | None = None

        def sync_version(self, **kwargs) -> AgeSyncResult:
            self.version_id = str(kwargs["version_id"])
            self.chunk_ids = [str(item["chunk_id"]) for item in kwargs["chunks"]]
            return AgeSyncResult(
                status="synced",
                graph_name=self.graph_name,
                node_count=17,
                edge_count=23,
            )

        def retrieve(self, **kwargs) -> AgeRetrievalResult:
            self.retrieve_kwargs = kwargs
            assert kwargs["version_id"] == self.version_id
            assert kwargs["query_entity_keys"]
            assert kwargs["query_text"] == "What does Product Guide say about Dark mode?"
            assert kwargs["traversal_hops"] == 2
            assert kwargs["candidate_pool_size"] == 40
            return AgeRetrievalResult(
                status="ok",
                graph_name=self.graph_name,
                chunk_candidates=[
                    AgeChunkCandidate(
                        chunk_id=self.chunk_ids[0],
                        graph_score=4.5,
                        matched_entities=("Product Guide",),
                        expanded_entities=("Dark mode",),
                    )
                ],
                matched_entities=("Product Guide",),
                expanded_entities=("Dark mode",),
                traversal_hops=1,
                matched_chunk_count=1,
                seed_strategy="query_entities",
            )

        def explore(self, **kwargs) -> AgeGraphExploreResult:
            self.explore_kwargs = kwargs
            assert kwargs["version_id"] == self.version_id
            assert kwargs["query"] == "Product Guide"
            assert kwargs["query_entity_keys"]
            assert kwargs["traversal_hops"] == 2
            assert kwargs["seed_mode"] == "query_text_only"
            assert kwargs["node_limit"] == 10
            return AgeGraphExploreResult(
                status="ok",
                graph_name=self.graph_name,
                entities=[
                    AgeGraphEntity(
                        entity_id="KBE-1",
                        entity_key="product-guide",
                        label="Product Guide",
                        entity_type="heading",
                        mention_count=4,
                        source_documents=("DOC-1",),
                        source_keys=("docs/guide.md",),
                        distance=0,
                        highlighted=True,
                    ),
                    AgeGraphEntity(
                        entity_id="KBE-2",
                        entity_key="dark-mode",
                        label="Dark mode",
                        entity_type="concept",
                        mention_count=2,
                        source_documents=("DOC-1",),
                        source_keys=("docs/guide.md",),
                        distance=1,
                        highlighted=False,
                    ),
                ],
                relationships=[
                    AgeGraphRelationship(
                        relationship_id="REL-1",
                        source_entity_id="KBE-1",
                        source_entity_key="product-guide",
                        source_entity_label="Product Guide",
                        target_entity_id="KBE-2",
                        target_entity_key="dark-mode",
                        target_entity_label="Dark mode",
                        relationship_type="co_occurs",
                        weight=3.0,
                        evidence_chunk_ids=("CH-1",),
                        source_documents=("DOC-1",),
                        hop_distance=1,
                    )
                ],
                matched_entities=("Product Guide",),
                expanded_entities=("Dark mode",),
                seed_strategy="query_text",
            )

    fake_age_store = _FakeAgeStore()
    monkeypatch.setattr(
        knowledge_service,
        "build_age_store",
        lambda **_kwargs: fake_age_store,
    )

    age_config = app_config.model_copy(
        update={
            "knowledge_age_enabled": True,
            "knowledge_age_viewer_url": "http://127.0.0.1:8082/workbench",
            "background_tasks_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        s3 = _wire_moto(age_client)
        bucket = "age-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Product Guide

Dark mode applies consistently across linked tools.
""",
            content_type="text/markdown",
        )

        options = age_client.get(f"{KB}/options")
        assert options.status_code == 200, options.text
        option_data = options.json()["data"]
        assert "age_graph" in {item["id"] for item in option_data["retrieval_modes"]}
        assert option_data["default_graph_config"]["output_target"] == "object_store_and_age"
        assert option_data["default_graph_config"]["default_retrieval_mode"] == "age_graph"
        assert option_data["age_graph_name"] == "knowledge_graph"
        assert option_data["age_viewer_url"] == "http://127.0.0.1:8082/workbench"
        assert option_data["age_unavailable_reason"] is None
        assert option_data["default_graph_config"]["age_seed_mode"] == "entity_then_text"
        assert option_data["default_graph_config"]["age_traversal_hops"] == 1
        assert option_data["default_graph_config"]["age_candidate_pool_size"] == 24
        assert option_data["default_graph_config"]["age_dense_rerank_weight"] == 0.35
        assert option_data["default_graph_config"]["strict_age_retrieval_default"] is False
        assert {item["id"] for item in option_data["graph_build_presets"]} == {
            "portable",
            "balanced",
            "age_native",
            "age_strict",
        }
        assert {item["id"] for item in option_data["graph_query_presets"]} == {
            "hybrid_precision",
            "hybrid_balanced",
            "age_balanced",
            "age_native",
            "age_strict",
        }
        strict_preset = next(
            item for item in option_data["graph_query_presets"] if item["id"] == "age_strict"
        )
        assert strict_preset["retrieval_mode"] == "age_graph"
        assert strict_preset["patch"]["strict_age_retrieval"] is True
        strict_build_preset = next(
            item for item in option_data["graph_build_presets"] if item["id"] == "age_strict"
        )
        assert strict_build_preset["patch"]["strict_age_retrieval_default"] is True

        create = age_client.post(
            KB,
            json={
                "name": "AGE Docs",
                "description": "AGE-synced knowledge base",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
                "graph_config": {
                    "output_target": "object_store_and_age",
                    "default_retrieval_mode": "age_graph",
                    "retrieval_strength": "balanced",
                    "age_traversal_hops": 2,
                    "age_candidate_pool_size": 40,
                    "age_dense_rerank_weight": 0.25,
                },
            },
        )
        assert create.status_code == 201, create.text
        create_data = create.json()["data"]
        version = create_data["version"]
        run = create_data["run"]
        knowledge_base = create_data["knowledge_base"]

        assert version["graph_config"]["output_target"] == "object_store_and_age"
        assert version["graph_config"]["default_retrieval_mode"] == "age_graph"
        assert version["graph_config"]["age_seed_mode"] == "entity_then_text"
        assert version["graph_config"]["age_traversal_hops"] == 2
        assert version["graph_config"]["age_candidate_pool_size"] == 40
        assert version["graph_config"]["age_dense_rerank_weight"] == 0.25
        assert version["graph_config"]["strict_age_retrieval_default"] is False
        assert version["summary"]["age_sync_status"] == "synced"
        assert version["summary"]["age_graph_name"] == "knowledge_graph"
        assert version["summary"]["age_synced_nodes"] == 17
        assert version["summary"]["age_synced_edges"] == 23
        assert version["summary"]["graph_default_retrieval_mode"] == "age_graph"
        assert version["summary"]["graph_age_seed_mode"] == "entity_then_text"
        assert version["summary"]["graph_age_traversal_hops"] == 2
        assert version["summary"]["graph_age_candidate_pool_size"] == 40
        assert version["summary"]["graph_age_dense_rerank_weight"] == 0.25
        assert version["summary"]["graph_strict_age_retrieval_default"] is False
        assert version["graph_profile_label"] == "Custom graph profile"
        assert version["summary"]["graph_profile_label"] == "Custom graph profile"
        assert knowledge_base["active_version_summary"]["graph_target"] == "object_store_and_age"
        assert knowledge_base["active_version_summary"]["default_retrieval_mode"] == "age_graph"
        assert (
            knowledge_base["active_version_summary"]["graph_profile_label"]
            == "Custom graph profile"
        )
        assert knowledge_base["active_version_summary"]["age_ready"] is True

        events = age_client.get(f"{PREFIX}/knowledge-runs/{run['knowledge_base_run_id']}/events")
        assert events.status_code == 200, events.text
        event_types = [item["event_type"] for item in events.json()["data"]]
        assert "age_sync_started" in event_types
        assert "age_sync_completed" in event_types

        compare = age_client.post(
            QUERY,
            json={
                "version_ids": [version["knowledge_base_version_id"]],
                "question": "What does Product Guide say about Dark mode?",
                "top_k": 3,
                "retrieval_modes": ["age_graph"],
            },
        )
        assert compare.status_code == 200, compare.text
        result = compare.json()["data"]["versions"][0]
        assert result["retrieval_mode"] == "age_graph"
        assert result["graph_context"]["age_status"] == "ok"
        assert result["graph_context"]["age_graph_name"] == "knowledge_graph"
        assert fake_age_store.retrieve_kwargs is not None
        assert (
            fake_age_store.retrieve_kwargs["query_text"]
            == "What does Product Guide say about Dark mode?"
        )
        assert fake_age_store.retrieve_kwargs["seed_mode"] == "entity_then_text"
        assert result["graph_context"]["matched_entities"] == ["Product Guide"]
        assert result["graph_context"]["age_configured_seed_mode"] == "entity_then_text"
        assert result["graph_context"]["age_dense_rerank_weight"] == 0.25
        assert result["graph_context"]["age_seed_strategy"] == "query_entities"
        assert result["retrieved_chunks"][0]["score_breakdown"]["age_graph"] > 0
        assert "dense_rerank" in result["retrieved_chunks"][0]["score_breakdown"]

        compare_default = age_client.post(
            QUERY,
            json={
                "version_ids": [version["knowledge_base_version_id"]],
                "question": "What does Product Guide say about Dark mode?",
                "top_k": 3,
                "retrieval_modes": [],
            },
        )
        assert compare_default.status_code == 200, compare_default.text
        default_result = compare_default.json()["data"]["versions"][0]
        assert default_result["retrieval_mode"] == "age_graph"
        assert default_result["graph_context"]["age_status"] == "ok"
        assert default_result["retrieved_chunks"][0]["score_breakdown"]["age_graph"] > 0

        graph_view = age_client.get(
            f"{PREFIX}/knowledge-base-versions/{version['knowledge_base_version_id']}/graph",
            params={
                "source": "age",
                "q": "Product Guide",
                "traversal_hops": "2",
                "age_seed_mode": "query_text_only",
                "node_limit": "10",
            },
        )
        assert graph_view.status_code == 200, graph_view.text
        graph_view_data = graph_view.json()["data"]
        assert graph_view_data["requested_source"] == "age"
        assert graph_view_data["served_source"] == "age"
        assert isinstance(graph_view_data["query_entity_labels"], list)
        assert graph_view_data["age_seed_mode"] == "query_text_only"
        assert graph_view_data["age_seed_strategy"] == "query_text"
        assert graph_view_data["matched_entity_labels"] == ["Product Guide"]
        assert graph_view_data["expanded_entity_labels"] == ["Dark mode"]
        assert graph_view_data["entities"][0]["graph_source"] == "age"
        assert graph_view_data["relationships"][0]["graph_source"] == "age"


@mock_aws
def test_knowledge_query_graph_overrides_can_force_strict_age_retrieval(
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    class _FallbackAgeStore:
        graph_name = "knowledge_graph"

        def __init__(self) -> None:
            self.retrieve_kwargs: dict[str, object] | None = None

        def sync_version(self, **kwargs) -> AgeSyncResult:
            return AgeSyncResult(
                status="synced",
                graph_name=self.graph_name,
                node_count=11,
                edge_count=14,
            )

        def retrieve(self, **kwargs) -> AgeRetrievalResult:
            self.retrieve_kwargs = kwargs
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self.graph_name,
                fallback_reason="age miss",
            )

    fake_age_store = _FallbackAgeStore()
    monkeypatch.setattr(
        knowledge_service,
        "build_age_store",
        lambda **_kwargs: fake_age_store,
    )

    age_config = app_config.model_copy(
        update={
            "knowledge_age_enabled": True,
            "background_tasks_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        s3 = _wire_moto(age_client)
        bucket = "strict-age-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Product Guide

Dark mode applies consistently across linked tools.
Bob owns Platform reliability.
""",
            content_type="text/markdown",
        )

        create = age_client.post(
            KB,
            json={
                "name": "Strict AGE Docs",
                "description": "AGE override validation",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
                "graph_config": {
                    "output_target": "object_store_and_age",
                    "retrieval_strength": "balanced",
                    "age_traversal_hops": 2,
                    "age_candidate_pool_size": 40,
                },
            },
        )
        assert create.status_code == 201, create.text
        version = create.json()["data"]["version"]

        compare = age_client.post(
            QUERY,
            json={
                "version_ids": [version["knowledge_base_version_id"]],
                "question": "What does Product Guide say about Platform reliability?",
                "top_k": 3,
                "retrieval_modes": ["age_graph"],
                "graph_overrides": {
                    "retrieval_strength": "aggressive",
                    "minimum_relationship_weight": 2.5,
                    "age_seed_mode": "query_text_only",
                    "age_traversal_hops": 0,
                    "age_candidate_pool_size": 12,
                    "age_dense_rerank_weight": 0.1,
                    "strict_age_retrieval": True,
                },
            },
        )
        assert compare.status_code == 200, compare.text
        result = compare.json()["data"]["versions"][0]

        assert fake_age_store.retrieve_kwargs is not None
        assert fake_age_store.retrieve_kwargs["retrieval_strength"] == "aggressive"
        assert fake_age_store.retrieve_kwargs["minimum_relationship_weight"] == 2.5
        assert fake_age_store.retrieve_kwargs["seed_mode"] == "query_text_only"
        assert fake_age_store.retrieve_kwargs["traversal_hops"] == 0
        assert fake_age_store.retrieve_kwargs["candidate_pool_size"] == 12

        assert result["retrieval_mode"] == "age_graph"
        assert result["retrieved_chunks"] == []
        assert result["answer"] is None
        assert result["answer_error"] == "No relevant chunks were retrieved for this question."
        assert result["graph_context"]["query_override_active"] is True
        assert result["graph_context"]["query_overrides"] == {
            "retrieval_strength": "aggressive",
            "minimum_relationship_weight": 2.5,
            "age_seed_mode": "query_text_only",
            "age_traversal_hops": 0,
            "age_candidate_pool_size": 12,
            "age_dense_rerank_weight": 0.1,
            "strict_age_retrieval": True,
        }
        assert result["graph_context"]["strict_age_retrieval"] is True
        assert result["graph_context"]["age_configured_seed_mode"] == "query_text_only"
        assert result["graph_context"]["age_dense_rerank_weight"] == 0.1
        assert result["graph_context"]["age_fallback_reason"] == "age miss"
        assert result["graph_context"].get("fallback_retrieval_mode") is None


@mock_aws
def test_knowledge_query_uses_saved_strict_age_default_when_enabled(
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    class _FallbackAgeStore:
        graph_name = "knowledge_graph"

        def __init__(self) -> None:
            self.retrieve_kwargs: dict[str, object] | None = None

        def sync_version(self, **kwargs) -> AgeSyncResult:
            return AgeSyncResult(
                status="synced",
                graph_name=self.graph_name,
                node_count=11,
                edge_count=14,
            )

        def retrieve(self, **kwargs) -> AgeRetrievalResult:
            self.retrieve_kwargs = kwargs
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self.graph_name,
                fallback_reason="age miss",
            )

    fake_age_store = _FallbackAgeStore()
    monkeypatch.setattr(
        knowledge_service,
        "build_age_store",
        lambda **_kwargs: fake_age_store,
    )

    age_config = app_config.model_copy(
        update={
            "knowledge_age_enabled": True,
            "background_tasks_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        s3 = _wire_moto(age_client)
        bucket = "strict-age-default-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Product Guide

Dark mode applies consistently across linked tools.
Bob owns Platform reliability.
""",
            content_type="text/markdown",
        )

        create = age_client.post(
            KB,
            json={
                "name": "Strict AGE Default Docs",
                "description": "Saved strict AGE default validation",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
                "graph_config": {
                    "output_target": "object_store_and_age",
                    "default_retrieval_mode": "age_graph",
                    "retrieval_strength": "aggressive",
                    "age_seed_mode": "query_entities_and_text",
                    "age_traversal_hops": 2,
                    "age_candidate_pool_size": 40,
                    "age_dense_rerank_weight": 0.2,
                    "strict_age_retrieval_default": True,
                },
            },
        )
        assert create.status_code == 201, create.text
        version = create.json()["data"]["version"]

        assert version["graph_config"]["strict_age_retrieval_default"] is True
        assert version["graph_profile_label"] == "Strict AGE default"
        assert version["summary"]["graph_profile_label"] == "Strict AGE default"
        assert version["summary"]["graph_strict_age_retrieval_default"] is True

        compare = age_client.post(
            QUERY,
            json={
                "version_ids": [version["knowledge_base_version_id"]],
                "question": "What does Product Guide say about Platform reliability?",
                "top_k": 3,
                "retrieval_modes": ["age_graph"],
            },
        )
        assert compare.status_code == 200, compare.text
        result = compare.json()["data"]["versions"][0]

        assert fake_age_store.retrieve_kwargs is not None
        assert fake_age_store.retrieve_kwargs["retrieval_strength"] == "aggressive"
        assert fake_age_store.retrieve_kwargs["seed_mode"] == "query_entities_and_text"
        assert fake_age_store.retrieve_kwargs["traversal_hops"] == 2
        assert fake_age_store.retrieve_kwargs["candidate_pool_size"] == 40

        assert result["retrieval_mode"] == "age_graph"
        assert result["retrieved_chunks"] == []
        assert result["answer"] is None
        assert result["answer_error"] == "No relevant chunks were retrieved for this question."
        assert result["graph_context"]["query_override_active"] is False
        assert result["graph_context"]["strict_age_retrieval"] is True
        assert result["graph_context"]["age_fallback_reason"] == "age miss"
        assert result["graph_context"].get("fallback_retrieval_mode") is None


@mock_aws
def test_age_retrieval_requires_successful_sync_before_graph_queries(  # noqa: PLR0915
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    class _FailedAgeStore:
        graph_name = "knowledge_graph"

        def __init__(self) -> None:
            self.retrieve_called = False
            self.explore_called = False

        def sync_version(self, **kwargs) -> AgeSyncResult:
            return AgeSyncResult(
                status="failed",
                graph_name=self.graph_name,
                error="age sync unavailable",
            )

        def retrieve(self, **kwargs) -> AgeRetrievalResult:
            self.retrieve_called = True
            return AgeRetrievalResult(
                status="ok",
                graph_name=self.graph_name,
                chunk_candidates=[],
            )

        def explore(self, **kwargs) -> AgeGraphExploreResult:
            self.explore_called = True
            return AgeGraphExploreResult(
                status="ok",
                graph_name=self.graph_name,
            )

    fake_age_store = _FailedAgeStore()
    monkeypatch.setattr(
        knowledge_service,
        "build_age_store",
        lambda **_kwargs: fake_age_store,
    )

    age_config = app_config.model_copy(
        update={
            "knowledge_age_enabled": True,
            "background_tasks_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        s3 = _wire_moto(age_client)
        bucket = "failed-age-sync-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Product Guide

Bob owns Platform reliability.
Dark mode applies consistently across linked tools.
""",
            content_type="text/markdown",
        )

        create = age_client.post(
            KB,
            json={
                "name": "Failed AGE Sync Docs",
                "description": "AGE should stay unavailable until sync succeeds",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
                "graph_config": {
                    "output_target": "object_store_and_age",
                    "retrieval_strength": "balanced",
                    "age_traversal_hops": 2,
                },
            },
        )
        assert create.status_code == 201, create.text
        version = create.json()["data"]["version"]

        assert version["summary"]["age_sync_status"] == "failed"
        assert version["summary"]["age_sync_error"] == "age sync unavailable"

        compare = age_client.post(
            QUERY,
            json={
                "version_ids": [version["knowledge_base_version_id"]],
                "question": "Who owns Platform reliability?",
                "top_k": 3,
                "retrieval_modes": ["age_graph"],
            },
        )
        assert compare.status_code == 200, compare.text
        result = compare.json()["data"]["versions"][0]

        assert fake_age_store.retrieve_called is False
        assert result["graph_context"]["age_ready"] is False
        assert result["graph_context"]["age_fallback_reason"] == (
            "This knowledge-base version did not finish syncing to Apache AGE."
        )
        assert result["graph_context"]["fallback_retrieval_mode"] in {"dense", "graph_hybrid"}

        compare_default = age_client.post(
            QUERY,
            json={
                "version_ids": [version["knowledge_base_version_id"]],
                "question": "Who owns Platform reliability?",
                "top_k": 3,
                "retrieval_modes": [],
            },
        )
        assert compare_default.status_code == 200, compare_default.text
        default_result = compare_default.json()["data"]["versions"][0]

        assert fake_age_store.retrieve_called is False
        assert default_result["retrieval_mode"] == "graph_hybrid"
        assert default_result["graph_context"]["age_ready"] is False
        assert default_result["graph_context"].get("age_fallback_reason") in {None, ""}
        assert default_result["retrieved_chunks"]

        graph_view = age_client.get(
            f"{PREFIX}/knowledge-base-versions/{version['knowledge_base_version_id']}/graph",
            params={"source": "age"},
        )
        assert graph_view.status_code == 200, graph_view.text
        graph_view_data = graph_view.json()["data"]

        assert fake_age_store.explore_called is False
        assert graph_view_data["requested_source"] == "age"
        assert graph_view_data["served_source"] == "local"
        assert graph_view_data["age_ready"] is False
        assert graph_view_data["strict_age_retrieval"] is False
        assert graph_view_data["fallback_reason"] == (
            "This knowledge-base version did not finish syncing to Apache AGE."
        )

        strict_graph_view = age_client.get(
            f"{PREFIX}/knowledge-base-versions/{version['knowledge_base_version_id']}/graph",
            params={"source": "age", "strict_age_retrieval": "true"},
        )
        assert strict_graph_view.status_code == 200, strict_graph_view.text
        strict_graph_data = strict_graph_view.json()["data"]

        assert fake_age_store.explore_called is False
        assert strict_graph_data["requested_source"] == "age"
        assert strict_graph_data["served_source"] == "age"
        assert strict_graph_data["age_ready"] is False
        assert strict_graph_data["strict_age_retrieval"] is True
        assert strict_graph_data["entities"] == []
        assert strict_graph_data["relationships"] == []
        assert strict_graph_data["fallback_reason"] == (
            "This knowledge-base version did not finish syncing to Apache AGE."
        )


@mock_aws
def test_create_version_defaults_existing_kb_back_to_age_target_when_age_enabled(
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    class _FakeAgeStore:
        graph_name = "knowledge_graph"

        def __init__(self) -> None:
            self.sync_calls: list[dict[str, object]] = []

        def sync_version(self, **kwargs) -> AgeSyncResult:
            self.sync_calls.append(kwargs)
            return AgeSyncResult(
                status="synced",
                graph_name=self.graph_name,
                node_count=9,
                edge_count=12,
            )

        def retrieve(self, **kwargs) -> AgeRetrievalResult:
            return AgeRetrievalResult(
                status="fallback",
                graph_name=self.graph_name,
                fallback_reason="not-needed",
            )

    fake_age_store = _FakeAgeStore()
    monkeypatch.setattr(
        knowledge_service,
        "build_age_store",
        lambda **_kwargs: fake_age_store,
    )

    age_config = app_config.model_copy(
        update={
            "knowledge_age_enabled": True,
            "background_tasks_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        s3 = _wire_moto(age_client)
        bucket = "legacy-graph-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Product Guide

Bob owns Platform reliability.
Graph sync should upgrade on the next build.
""",
            content_type="text/markdown",
        )

        create = age_client.post(
            KB,
            json={
                "name": "Legacy Graph Docs",
                "description": "Starts object-store only, then upgrades to AGE",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
                "graph_config": {
                    "default_retrieval_mode": "age_graph",
                    "output_target": "object_store",
                    "retrieval_strength": "balanced",
                },
            },
        )
        assert create.status_code == 201, create.text
        first_version = create.json()["data"]["version"]
        knowledge_base = create.json()["data"]["knowledge_base"]

        assert first_version["graph_config"]["output_target"] == "object_store"
        assert first_version["graph_config"]["default_retrieval_mode"] == "graph_hybrid"
        assert first_version["summary"]["age_sync_status"] == "skipped"
        assert fake_age_store.sync_calls == []

        second = age_client.post(
            f"{KB}/{knowledge_base['knowledge_base_id']}/versions",
            json={
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
            },
        )
        assert second.status_code == 201, second.text
        second_data = second.json()["data"]
        second_version = second_data["version"]
        second_run = second_data["run"]

        assert second_version["graph_config"]["output_target"] == "object_store_and_age"
        assert second_version["graph_config"]["default_retrieval_mode"] == "age_graph"
        assert second_version["graph_config"]["age_traversal_hops"] == 1
        assert second_version["graph_config"]["age_candidate_pool_size"] == 24
        assert second_version["graph_config"]["age_dense_rerank_weight"] == 0.35
        assert second_version["summary"]["age_sync_status"] == "synced"
        assert second_version["summary"]["age_graph_name"] == "knowledge_graph"
        assert second_version["summary"]["age_synced_nodes"] == 9
        assert second_version["summary"]["age_synced_edges"] == 12
        assert len(fake_age_store.sync_calls) == 1

        events = age_client.get(
            f"{PREFIX}/knowledge-runs/{second_run['knowledge_base_run_id']}/events"
        )
        assert events.status_code == 200, events.text
        event_types = [item["event_type"] for item in events.json()["data"]]
        assert "age_sync_started" in event_types
        assert "age_sync_completed" in event_types


@mock_aws
def test_completed_version_can_sync_into_age_post_build(
    app_config,
    engine,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    class _FakeAgeStore:
        graph_name = "knowledge_graph"

        def __init__(self) -> None:
            self.version_id = ""
            self.chunk_ids: list[str] = []
            self.sync_calls: list[dict[str, object]] = []
            self.retrieve_called = False

        def sync_version(self, **kwargs) -> AgeSyncResult:
            self.sync_calls.append(kwargs)
            self.version_id = str(kwargs["version_id"])
            self.chunk_ids = [str(item["chunk_id"]) for item in kwargs["chunks"]]
            return AgeSyncResult(
                status="synced",
                graph_name=self.graph_name,
                node_count=13,
                edge_count=18,
            )

        def retrieve(self, **kwargs) -> AgeRetrievalResult:
            self.retrieve_called = True
            assert kwargs["version_id"] == self.version_id
            return AgeRetrievalResult(
                status="ok",
                graph_name=self.graph_name,
                chunk_candidates=[
                    AgeChunkCandidate(
                        chunk_id=self.chunk_ids[0],
                        graph_score=3.2,
                        matched_entities=("Product Guide",),
                    )
                ],
                matched_entities=("Product Guide",),
                traversal_hops=1,
                matched_chunk_count=1,
            )

    fake_age_store = _FakeAgeStore()
    monkeypatch.setattr(
        knowledge_service,
        "build_age_store",
        lambda **_kwargs: fake_age_store,
    )

    age_config = app_config.model_copy(
        update={
            "knowledge_age_enabled": True,
            "background_tasks_enabled": False,
            "workflow_run_worker_enabled": False,
            "workflow_scheduler_enabled": False,
        }
    )
    app = create_app(config=age_config)
    app.state.engine = engine
    app.state.session_factory = session_factory

    with TestClient(app, headers={"X-CALIBER-User": "@test"}) as age_client:
        s3 = _wire_moto(age_client)
        bucket = "retro-age-docs"
        s3.create_bucket(Bucket=bucket)
        _put_text(
            s3,
            bucket,
            "docs/guide.md",
            """# Product Guide

Dark mode applies consistently across linked tools.
""",
            content_type="text/markdown",
        )

        create = age_client.post(
            KB,
            json={
                "name": "Retro AGE Docs",
                "description": "Starts without AGE, then syncs later",
                "source_bucket": bucket,
                "sources": [{"kind": "folder", "path": "docs/"}],
                "chunking_strategy": "markdown",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 120, "chunk_overlap": 20},
                "graph_config": {
                    "output_target": "object_store",
                    "retrieval_strength": "balanced",
                },
            },
        )
        assert create.status_code == 201, create.text
        initial_version = create.json()["data"]["version"]
        version_id = initial_version["knowledge_base_version_id"]

        assert initial_version["graph_config"]["output_target"] == "object_store"
        assert initial_version["graph_config"]["default_retrieval_mode"] == "graph_hybrid"
        assert initial_version["summary"]["age_sync_status"] == "skipped"
        assert fake_age_store.sync_calls == []

        sync = age_client.post(f"{PREFIX}/knowledge-base-versions/{version_id}/age-sync")
        assert sync.status_code == 200, sync.text
        synced_version = sync.json()["data"]

        assert synced_version["graph_config"]["output_target"] == "object_store_and_age"
        assert synced_version["graph_config"]["default_retrieval_mode"] == "age_graph"
        assert synced_version["summary"]["age_sync_status"] == "synced"
        assert synced_version["summary"]["age_graph_name"] == "knowledge_graph"
        assert synced_version["summary"]["age_synced_nodes"] == 13
        assert synced_version["summary"]["age_synced_edges"] == 18
        assert synced_version["summary"]["age_sync_attempted_at"] is not None
        assert len(fake_age_store.sync_calls) == 1
        assert fake_age_store.sync_calls[0]["documents"]
        assert fake_age_store.sync_calls[0]["chunks"]

        refreshed = age_client.get(f"{PREFIX}/knowledge-base-versions/{version_id}")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["data"]["summary"]["age_sync_status"] == "synced"

        default_compare = age_client.post(
            QUERY,
            json={
                "version_ids": [version_id],
                "question": "What does Product Guide say about Dark mode?",
                "top_k": 3,
                "retrieval_modes": [],
            },
        )
        assert default_compare.status_code == 200, default_compare.text
        default_result = default_compare.json()["data"]["versions"][0]

        assert default_result["retrieval_mode"] == "age_graph"
        assert default_result["graph_context"]["age_ready"] is True

        compare = age_client.post(
            QUERY,
            json={
                "version_ids": [version_id],
                "question": "What does Product Guide say about Dark mode?",
                "top_k": 3,
                "retrieval_modes": ["age_graph"],
            },
        )
        assert compare.status_code == 200, compare.text
        result = compare.json()["data"]["versions"][0]

        assert fake_age_store.retrieve_called is True
        assert result["retrieval_mode"] == "age_graph"
        assert result["graph_context"]["age_ready"] is True
        assert result["graph_context"]["age_status"] == "ok"
        assert result["retrieved_chunks"]


def _fake_chunk(chunk_id: str, content: str, ordinal: int):
    embedder = _DummyEmbedder("test-model")
    chunk = knowledge_service.CaliberKnowledgeBaseChunk(
        knowledge_base_chunk_id=chunk_id,
        content=content,
        embedding=embedder.embed_query(content),
        ordinal=ordinal,
    )
    return chunk


def _service_only() -> knowledge_service.KnowledgeBaseService:
    return knowledge_service.KnowledgeBaseService.__new__(knowledge_service.KnowledgeBaseService)


def test_lexical_scores_rank_rare_keyword_above_non_matching_chunks() -> None:
    service = _service_only()
    chunks = [
        _fake_chunk("with-keyword", "The zarphidon ledger is reconciled nightly.", 0),
        _fake_chunk("without-keyword", "Operators reconcile the ledger every morning.", 1),
        _fake_chunk("unrelated", "Weather gardening pottery and sculpture notes.", 2),
    ]

    scores = service._lexical_scores(chunks, "How is the zarphidon ledger reconciled?")

    assert scores["with-keyword"] > scores.get("without-keyword", 0.0)
    assert "unrelated" not in scores or scores["unrelated"] < scores["with-keyword"]


def test_lexical_scores_absent_term_yields_empty() -> None:
    service = _service_only()
    chunks = [
        _fake_chunk("a", "Alpha bravo charlie content lines.", 0),
        _fake_chunk("b", "Delta echo foxtrot content lines.", 1),
    ]

    assert service._lexical_scores(chunks, "nonexistentkeywordxyz") == {}
    # An empty/stopword-only query also yields no lexical signal.
    assert service._lexical_scores(chunks, "the of and") == {}


def test_reciprocal_rank_fusion_rewards_top_ranked_across_lists() -> None:
    service = _service_only()
    dense_ranking = [("a", 0.9), ("b", 0.4), ("c", 0.1)]
    lexical_ranking = [("c", 5.0), ("a", 2.0)]

    fused = service._reciprocal_rank_fusion(dense_ranking, lexical_ranking, k=60)

    # "a" appears near the top of both lists, so it must outrank "b" and "c".
    assert fused["a"] > fused["b"]
    assert fused["a"] > fused["c"]
    # "c" is last in dense but first in lexical, so it still beats dense-only "b".
    assert fused["c"] > fused["b"]


def test_retrieve_hybrid_breakdown_contains_dense_lexical_rrf() -> None:
    service = _service_only()
    chunks = [
        _fake_chunk("match", "The zarphidon checkpoint restore runs nightly.", 0),
        _fake_chunk("other", "Unrelated weather gardening pottery notes here.", 1),
    ]
    question = "How is the zarphidon checkpoint restored?"
    dense_scores = service._dense_scores(chunks, _DummyEmbedder("m").embed_query(question))
    lexical_scores = service._lexical_scores(chunks, question)

    top, context = service._retrieve_hybrid(
        chunks=chunks,
        dense_scores=dense_scores,
        lexical_scores=lexical_scores,
        top_k=2,
    )

    assert top
    for item in top:
        assert set(item.score_breakdown) >= {"dense", "lexical", "rrf"}
    match = next(item for item in top if item.chunk.knowledge_base_chunk_id == "match")
    assert match.score_breakdown["lexical"] > 0.0
    assert match.score == match.score_breakdown["rrf"]
    assert context["lexical_matched_chunk_count"] == len(lexical_scores)


@mock_aws
def test_knowledge_base_hybrid_surfaces_exact_keyword_match(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    s3 = _wire_moto(client)
    bucket = "hybrid-docs"
    s3.create_bucket(Bucket=bucket)
    # One short file per chunk so the build yields one chunk per document with
    # fully controlled content (matching the existing build-test pattern).
    # "match" carries the rare keyword but only weak embedding overlap with the
    # question; "semantic" shares question words but lacks the keyword; "decoy"
    # is unrelated yet outranks "match" on the dense (token-hash) vectors.
    _put_text(
        s3,
        bucket,
        "match.txt",
        "Operators consult the zarphidon ledger every morning before opening tickets.\n",
    )
    _put_text(
        s3,
        bucket,
        "semantic.txt",
        "The checkpoint is restored automatically; checkpoint restore runs nightly.\n",
    )
    _put_text(
        s3,
        bucket,
        "decoy.txt",
        "Weather gardening pottery sculpture painting drawing music dancing.\n",
    )

    create = client.post(
        KB,
        json={
            "name": "Hybrid Docs",
            "description": "Keyword + vector fusion fixture",
            "source_bucket": bucket,
            "sources": [
                {"kind": "file", "path": "match.txt"},
                {"kind": "file", "path": "semantic.txt"},
                {"kind": "file", "path": "decoy.txt"},
            ],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 400, "chunk_overlap": 40},
        },
    )
    assert create.status_code == 201, create.text
    version = create.json()["data"]["version"]
    version_id = version["knowledge_base_version_id"]
    assert version["status"] == "completed"

    question = "How is the zarphidon checkpoint restored?"
    compare = client.post(
        QUERY,
        json={
            "version_ids": [version_id],
            "question": question,
            "top_k": 2,
            "retrieval_modes": ["dense", "hybrid"],
        },
    )
    assert compare.status_code == 200, compare.text
    versions = compare.json()["data"]["versions"]

    dense_result = next(item for item in versions if item["retrieval_mode"] == "dense")
    hybrid_result = next(item for item in versions if item["retrieval_mode"] == "hybrid")

    def _contains_keyword(chunks: list[dict]) -> bool:
        return any("zarphidon" in chunk["content"] for chunk in chunks)

    # The exact-keyword chunk is squeezed out of the dense-only top-k...
    assert not _contains_keyword(dense_result["retrieved_chunks"])
    # ...but the lexical leg of hybrid surfaces it.
    assert _contains_keyword(hybrid_result["retrieved_chunks"])

    keyword_chunk = next(
        chunk for chunk in hybrid_result["retrieved_chunks"] if "zarphidon" in chunk["content"]
    )
    assert set(keyword_chunk["score_breakdown"]) >= {"dense", "lexical", "rrf"}
    assert keyword_chunk["score_breakdown"]["lexical"] > 0.0


@mock_aws
def test_knowledge_base_graph_hybrid_surfaces_exact_keyword_match(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # graph_hybrid is a tri-hybrid (dense + BM25 + graph): even with no matching
    # query entity, a strong exact-keyword chunk must surface via the lexical leg
    # where a pure dense ranking would squeeze it out.
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    s3 = _wire_moto(client)
    bucket = "graph-hybrid-docs"
    s3.create_bucket(Bucket=bucket)
    # Same shape as the ``hybrid`` keyword fixture: "match" carries the rare keyword
    # but only weak embedding overlap with the question; "semantic" shares question
    # words but lacks the keyword; "decoy" is unrelated yet outranks "match" on the
    # dense (token-hash) vectors.
    _put_text(
        s3,
        bucket,
        "match.txt",
        "Operators consult the zarphidon ledger every morning before opening tickets.\n",
    )
    _put_text(
        s3,
        bucket,
        "semantic.txt",
        "The checkpoint is restored automatically; checkpoint restore runs nightly.\n",
    )
    _put_text(
        s3,
        bucket,
        "decoy.txt",
        "Weather gardening pottery sculpture painting drawing music dancing.\n",
    )

    create = client.post(
        KB,
        json={
            "name": "Graph Hybrid Docs",
            "description": "Tri-hybrid keyword + vector + graph fixture",
            "source_bucket": bucket,
            "sources": [
                {"kind": "file", "path": "match.txt"},
                {"kind": "file", "path": "semantic.txt"},
                {"kind": "file", "path": "decoy.txt"},
            ],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 400, "chunk_overlap": 40},
        },
    )
    assert create.status_code == 201, create.text
    version = create.json()["data"]["version"]
    version_id = version["knowledge_base_version_id"]
    assert version["status"] == "completed"

    question = "How is the zarphidon checkpoint restored?"
    compare = client.post(
        QUERY,
        json={
            "version_ids": [version_id],
            "question": question,
            "top_k": 2,
            "retrieval_modes": ["dense", "graph_hybrid"],
        },
    )
    assert compare.status_code == 200, compare.text
    versions = compare.json()["data"]["versions"]

    dense_result = next(item for item in versions if item["retrieval_mode"] == "dense")
    graph_result = next(item for item in versions if item["retrieval_mode"] == "graph_hybrid")

    def _contains_keyword(chunks: list[dict]) -> bool:
        return any("zarphidon" in chunk["content"] for chunk in chunks)

    # The exact-keyword chunk is squeezed out of the dense-only top-k...
    assert not _contains_keyword(dense_result["retrieved_chunks"])
    # ...but the lexical leg of the tri-hybrid surfaces it even without an entity match.
    assert graph_result["graph_context"]["matched_entities"] == []
    assert _contains_keyword(graph_result["retrieved_chunks"])

    keyword_chunk = next(
        chunk for chunk in graph_result["retrieved_chunks"] if "zarphidon" in chunk["content"]
    )
    assert "lexical" in keyword_chunk["score_breakdown"]
    assert keyword_chunk["score_breakdown"]["lexical"] > 0.0
    assert graph_result["graph_context"]["lexical_matched_chunk_count"] >= 1


def _build_knowledge_base_for_delete(client: TestClient, s3, bucket: str, name: str) -> str:
    """Build a fully-populated KB via the API and return its id.

    Reuses the build pipeline so the version carries real sources, chunks,
    entities, relationships, a run, and run events — plus object-store artifacts
    under the version's output prefix — exactly like a production build.
    """
    s3.create_bucket(Bucket=bucket)
    _put_text(
        s3,
        bucket,
        "product/guide.md",
        """# Product Guide

Retries happen three times before an alert is sent.

Dark mode applies consistently across linked tools.
""",
        content_type="text/markdown",
    )
    create = client.post(
        KB,
        json={
            "name": name,
            "description": "Knowledge base to hard-delete",
            "source_bucket": bucket,
            "sources": [{"kind": "folder", "path": "product/"}],
            "chunking_strategy": "recursive",
            "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            "chunking_config": {"chunk_size": 90, "chunk_overlap": 12},
        },
    )
    assert create.status_code == 201, create.text
    return create.json()["data"]["knowledge_base"]["knowledge_base_id"]


@mock_aws
def test_delete_knowledge_base_hard_deletes_all_rows_and_artifacts(
    client: TestClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hard DELETE removes the KB and *every* child row in one shot.

    Builds a KB with a completed version (sources + chunks + entities +
    relationships + a run + run events) plus a calibration test run pinned as the
    baseline, then DELETEs it and asserts the KB row is gone and each child table
    has zero rows for that KB / its versions / its runs — while a second,
    untouched KB survives intact (no over-deletion).
    """
    monkeypatch.setattr(
        knowledge_service,
        "build_embedding_backend",
        lambda model_id: _DummyEmbedder(model_id),
    )

    s3 = _wire_moto(client)
    target_id = _build_knowledge_base_for_delete(client, s3, "delete-docs", "Doomed Docs")
    survivor_id = _build_knowledge_base_for_delete(
        client, s3, "keep-docs", "Surviving Docs"
    )

    # Collect the target's version ids and run ids, and confirm the build really
    # populated every child table (otherwise the cascade assertions are vacuous).
    version_ids = [
        row.knowledge_base_version_id
        for row in db_session.query(knowledge_service.CaliberKnowledgeBaseVersion)
        .filter(
            knowledge_service.CaliberKnowledgeBaseVersion.knowledge_base_id == target_id
        )
        .all()
    ]
    run_ids = [
        row.knowledge_base_run_id
        for row in db_session.query(knowledge_service.CaliberKnowledgeBaseRun)
        .filter(knowledge_service.CaliberKnowledgeBaseRun.knowledge_base_id == target_id)
        .all()
    ]
    assert version_ids
    assert run_ids
    version_id = version_ids[0]
    run_id = run_ids[0]

    def _child_count(model, column, values) -> int:
        return db_session.query(model).filter(column.in_(values)).count()

    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseSource)
        .filter(
            knowledge_service.CaliberKnowledgeBaseSource.knowledge_base_version_id == version_id
        )
        .count()
        > 0
    )
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseChunk)
        .filter(
            knowledge_service.CaliberKnowledgeBaseChunk.knowledge_base_version_id == version_id
        )
        .count()
        > 0
    )
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseEntity)
        .filter(
            knowledge_service.CaliberKnowledgeBaseEntity.knowledge_base_version_id == version_id
        )
        .count()
        > 0
    )
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseRelationship)
        .filter(
            knowledge_service.CaliberKnowledgeBaseRelationship.knowledge_base_version_id
            == version_id
        )
        .count()
        > 0
    )
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseRunEvent)
        .filter(knowledge_service.CaliberKnowledgeBaseRunEvent.knowledge_base_run_id == run_id)
        .count()
        > 0
    )

    # Seed a durable calibration test run and pin it as the baseline so the
    # delete also exercises the test-run cascade and the self-FK (baseline_run_id)
    # nulling.
    test_run_id = "KBTR-delete-1"
    db_session.add(
        knowledge_service.CaliberKnowledgeBaseTestRun(
            test_run_id=test_run_id,
            knowledge_base_id=target_id,
            knowledge_base_version_id=version_id,
            metrics={"recall_at_k": 1.0},
            results=[{"question": "q", "verdict": "pass"}],
        )
    )
    target_kb = db_session.get(knowledge_service.CaliberKnowledgeBase, target_id)
    assert target_kb is not None
    target_kb.baseline_run_id = test_run_id
    db_session.commit()

    # The build wrote artifacts under the version's output prefix; capture it so
    # we can assert the best-effort object-store cleanup emptied it.
    output_bucket = target_kb.source_bucket
    output_prefix = (
        db_session.get(knowledge_service.CaliberKnowledgeBaseVersion, version_id).output_prefix
    )
    before = s3.list_objects_v2(Bucket=output_bucket, Prefix=f"{output_prefix}/")
    assert before.get("Contents"), "build should have written version artifacts"

    response = client.delete(f"{KB}/{target_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "knowledge_base_id": target_id,
        "deleted": True,
    }

    db_session.expire_all()

    # The KB row itself is gone, and a follow-up GET 404s.
    assert db_session.get(knowledge_service.CaliberKnowledgeBase, target_id) is None
    assert client.get(f"{KB}/{target_id}").status_code == 404

    # Every child table has zero rows for the deleted KB / its versions / runs.
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseVersion)
        .filter(
            knowledge_service.CaliberKnowledgeBaseVersion.knowledge_base_id == target_id
        )
        .count()
        == 0
    )
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseRun)
        .filter(knowledge_service.CaliberKnowledgeBaseRun.knowledge_base_id == target_id)
        .count()
        == 0
    )
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseTestRun)
        .filter(knowledge_service.CaliberKnowledgeBaseTestRun.knowledge_base_id == target_id)
        .count()
        == 0
    )
    assert (
        _child_count(
            knowledge_service.CaliberKnowledgeBaseRunEvent,
            knowledge_service.CaliberKnowledgeBaseRunEvent.knowledge_base_run_id,
            run_ids,
        )
        == 0
    )
    for model, column in (
        (
            knowledge_service.CaliberKnowledgeBaseSource,
            knowledge_service.CaliberKnowledgeBaseSource.knowledge_base_version_id,
        ),
        (
            knowledge_service.CaliberKnowledgeBaseChunk,
            knowledge_service.CaliberKnowledgeBaseChunk.knowledge_base_version_id,
        ),
        (
            knowledge_service.CaliberKnowledgeBaseEntity,
            knowledge_service.CaliberKnowledgeBaseEntity.knowledge_base_version_id,
        ),
        (
            knowledge_service.CaliberKnowledgeBaseRelationship,
            knowledge_service.CaliberKnowledgeBaseRelationship.knowledge_base_version_id,
        ),
    ):
        assert _child_count(model, column, version_ids) == 0

    # Best-effort object-store cleanup emptied the version's output prefix.
    after = s3.list_objects_v2(Bucket=output_bucket, Prefix=f"{output_prefix}/")
    assert not after.get("Contents")

    # The second KB and all its rows are completely untouched.
    assert db_session.get(knowledge_service.CaliberKnowledgeBase, survivor_id) is not None
    survivor_versions = [
        row.knowledge_base_version_id
        for row in db_session.query(knowledge_service.CaliberKnowledgeBaseVersion)
        .filter(
            knowledge_service.CaliberKnowledgeBaseVersion.knowledge_base_id == survivor_id
        )
        .all()
    ]
    assert survivor_versions
    assert (
        db_session.query(knowledge_service.CaliberKnowledgeBaseChunk)
        .filter(
            knowledge_service.CaliberKnowledgeBaseChunk.knowledge_base_version_id.in_(
                survivor_versions
            )
        )
        .count()
        > 0
    )
    assert client.get(f"{KB}/{survivor_id}").status_code == 200


def test_delete_knowledge_base_returns_404_when_missing(client: TestClient) -> None:
    response = client.delete(f"{KB}/KB-does-not-exist")
    assert response.status_code == 404
    assert "not found" in response.text


def test_delete_knowledge_base_requires_operator_scope(
    client: TestClient,
    db_session,
) -> None:
    """A non-operator (viewer) caller is rejected with 403 and nothing is deleted."""
    db_session.add(
        knowledge_service.CaliberKnowledgeBase(
            knowledge_base_id="KB-rbac-delete",
            name="RBAC KB",
            source_bucket="rbac-bucket",
        )
    )
    db_session.commit()

    response = client.delete(
        f"{KB}/KB-rbac-delete", headers={"X-CALIBER-User": "@viewer"}
    )
    assert response.status_code == 403

    db_session.expire_all()
    assert db_session.get(knowledge_service.CaliberKnowledgeBase, "KB-rbac-delete") is not None
