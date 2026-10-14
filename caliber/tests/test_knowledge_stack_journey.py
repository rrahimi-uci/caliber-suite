"""The knowledge-base journey against a real MinIO and a real PostgreSQL (alpha gap P1-23).

Every other knowledge-base test uses moto and SQLite, so pgvector-adjacent storage, the S3
client against a real S3-compatible server, and PostgreSQL's stricter types had never run
together with the build. This runs create, build, version, query, activate and roll back
against the docker stack's own MinIO and PostgreSQL.

**The embeddings are simulated.** A real model needs the ``knowledge-local`` extra and a
download, which this test deliberately avoids; ``build_embedding_backend`` is replaced by a
deterministic bag-of-words embedder. So the retrieval ranking here is a property of the test
double, not evidence about a model, and a green run says the storage, the build pipeline and
the routes work on the real services, not that retrieval quality is good.

It needs two environment variables and skips without them, so a plain ``pytest`` run is
unaffected. ``scripts/verify-knowledge-stack.sh`` starts both services and sets them:

    CALIBER_KB_STACK_S3_ENDPOINT   e.g. http://127.0.0.1:9000 (MinIO, minioadmin/minioadmin)
    CALIBER_KB_STACK_POSTGRES_URL  e.g. postgresql+psycopg://caliber:caliber@127.0.0.1:5432/caliber
"""

from __future__ import annotations

import math
import os
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

S3_ENDPOINT = os.environ.get("CALIBER_KB_STACK_S3_ENDPOINT", "")
POSTGRES_URL = os.environ.get("CALIBER_KB_STACK_POSTGRES_URL", "")

pytestmark = [
    pytest.mark.skipif(
        not (S3_ENDPOINT and POSTGRES_URL),
        reason="needs CALIBER_KB_STACK_S3_ENDPOINT and CALIBER_KB_STACK_POSTGRES_URL "
        "(scripts/verify-knowledge-stack.sh starts both)",
    ),
    pytest.mark.slow,
]

PREFIX = "/ajax-api/2.0/mlflow/caliber"
KB = f"{PREFIX}/knowledge-bases"
QUERY = f"{PREFIX}/knowledge/query"


class _SimulatedEmbedder:
    """Deterministic bag-of-words vectors. Marked simulated: see the module docstring."""

    def __init__(self, model_id: str, dimension: int = 16) -> None:
        self.model_id = model_id
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            vector[sum(ord(char) for char in token) % self.dimension] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return vector if norm == 0 else [value / norm for value in vector]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def stack_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[TestClient]:
    """The CALIBER app on a fresh PostgreSQL database, with MinIO as its object store."""
    boto3 = pytest.importorskip("boto3")
    sqlalchemy = pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg")
    from alembic import command

    import caliber.knowledge.service as knowledge_service
    from caliber.config import CaliberConfig
    from caliber.db.migrate import build_config
    from caliber.server import create_app

    database = f"caliber_kb_{uuid.uuid4().hex[:10]}"
    admin_engine = sqlalchemy.create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(sqlalchemy.text(f'CREATE DATABASE "{database}"'))
    database_url = sqlalchemy.engine.make_url(POSTGRES_URL).set(database=database)
    rendered_url = database_url.render_as_string(hide_password=False)

    monkeypatch.setenv("CALIBER_DATABASE_URL", rendered_url)
    command.upgrade(build_config(), "head")

    monkeypatch.setattr(
        knowledge_service, "build_embedding_backend", lambda model_id: _SimulatedEmbedder(model_id)
    )
    monkeypatch.setattr(
        knowledge_service, "ensure_embedding_backend_runtime_available", lambda **kwargs: None
    )
    config = CaliberConfig.load(
        environ={
            "CALIBER_DATABASE_URL": rendered_url,
            "CALIBER_AUTH_MODE": "trusted_header",
            "CALIBER_ADMIN_USERS": "@admin",
            "CALIBER_BACKGROUND_TASKS_ENABLED": "false",
            "CALIBER_ASSISTANT_ENGINE": "fake",
            "CALIBER_WORKFLOW_STORAGE_BASE_URI": f"file://{tmp_path / 'workspaces'}",
        }
    )
    app = create_app(config=config)
    app.state.object_store_client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=os.environ.get("MINIO_ROOT_USER", "minioadmin"),
        aws_secret_access_key=os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"),
        region_name="us-east-1",
        config=boto3.session.Config(s3={"addressing_style": "path"}),
    )
    try:
        with TestClient(app, headers={"X-CALIBER-User": "@admin"}) as client:
            yield client
    finally:
        app.state.engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(
                sqlalchemy.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
        admin_engine.dispose()


def test_the_knowledge_base_journey_on_minio_and_postgres_with_simulated_embeddings(
    stack_client: TestClient,
) -> None:
    s3 = stack_client.app.state.object_store_client
    bucket = f"kb-stack-{uuid.uuid4().hex[:8]}"
    s3.create_bucket(Bucket=bucket)
    s3.put_object(
        Bucket=bucket,
        Key="product/guide.md",
        Body=(
            b"# Product Guide\n\nRetries happen three times before an alert is sent.\n\n"
            b"Dark mode applies consistently across linked tools.\n"
        ),
        ContentType="text/markdown",
    )
    s3.put_object(
        Bucket=bucket,
        Key="faq.txt",
        Body=b"Chunk outputs are written under a reserved prefix for traceable reuse.\n",
        ContentType="text/plain",
    )

    try:
        # ---- Create and build: the sources are read from MinIO ----------------------
        created = stack_client.post(
            KB,
            json={
                "name": "Stack Docs",
                "description": "Journey on MinIO and PostgreSQL",
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
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        kb_id = data["knowledge_base"]["knowledge_base_id"]
        version_one = data["version"]
        assert version_one["status"] == "completed", version_one
        assert version_one["summary"]["processed_sources"] == 2
        assert version_one["summary"]["chunk_count"] >= 2
        assert version_one["embedding_dimension"] == 16
        assert (
            data["knowledge_base"]["active_version_id"] == version_one["knowledge_base_version_id"]
        )

        # ---- The build wrote its artifacts back to MinIO ---------------------------
        keys = {
            row["Key"]
            for row in s3.list_objects_v2(Bucket=bucket, Prefix=version_one["output_prefix"]).get(
                "Contents", []
            )
        }
        assert f"{version_one['output_prefix']}/chunks.jsonl" in keys
        assert f"{version_one['output_prefix']}/manifest.json" in keys

        # ---- The chunks and their vectors landed in PostgreSQL ---------------------
        chunks = stack_client.get(
            f"{PREFIX}/knowledge-base-versions/{version_one['knowledge_base_version_id']}/chunks",
            params={"limit": "50"},
        )
        assert chunks.status_code == 200, chunks.text
        rows = chunks.json()["data"]
        assert rows
        assert all(len(row["embedding"]) == 16 for row in rows)

        # ---- A second version, then query both ------------------------------------
        second = stack_client.post(
            f"{KB}/{kb_id}/versions",
            json={
                "chunking_strategy": "markdown",
                "embedding_model": "BAAI/bge-m3",
                "chunking_config": {"chunk_size": 85, "chunk_overlap": 10},
            },
        )
        assert second.status_code == 201, second.text
        version_two = second.json()["data"]["version"]
        assert version_two["version_number"] == 2

        answer = stack_client.post(
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
        assert answer.status_code == 200, answer.text
        results = answer.json()["data"]["versions"]
        assert len(results) == 2
        for result in results:
            assert result["retrieved_chunks"], result
            assert result["citations"], result
            assert any(
                "Retries happen three times" in chunk["content"]
                for chunk in result["retrieved_chunks"]
            )

        # ---- Roll back to version one ---------------------------------------------
        rolled = stack_client.post(
            f"{KB}/{kb_id}/versions/{version_one['knowledge_base_version_id']}/activate", json={}
        )
        assert rolled.status_code == 200, rolled.text
        detail = stack_client.get(f"{KB}/{kb_id}")
        assert (
            detail.json()["data"]["active_version_id"] == version_one["knowledge_base_version_id"]
        )
    finally:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for item in page.get("Contents", []):
                s3.delete_object(Bucket=bucket, Key=item["Key"])
        s3.delete_bucket(Bucket=bucket)
