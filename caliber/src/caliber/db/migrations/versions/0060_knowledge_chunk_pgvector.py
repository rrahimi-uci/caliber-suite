"""Knowledge chunk pgvector column + HNSW index for ANN retrieval

Revision ID: 0060
Revises: 0059
Create Date: 2026-06-24

Adds a pgvector ``embedding_vec`` column + an HNSW index to
``caliber_knowledge_base_chunks`` so dense retrieval can gather its candidate
pool with a sub-linear nearest-neighbour search instead of an in-memory O(n)
Python cosine scan — the path that scales to millions of chunks.

**Postgres-only.** The ``vector`` type + ``hnsw`` access method ship with the
pgvector extension (already present in the deploy Postgres image). On SQLite
(tests / local dev) this whole migration is a no-op and the service falls back to
the Python scan, so the SQLite schema is unchanged.

**Dimension.** pgvector's HNSW index requires a fixed dimension and caps at 2000.
The column is therefore declared ``vector(N)`` where ``N`` is
``CALIBER_KNOWLEDGE_EMBEDDING_DIMENSION`` (default 1024 — the dimension of the
recommended BGE-M3 / E5-large / Qwen3 models; MiniLM is 384). Knowledge bases
built at a different dimension simply leave ``embedding_vec`` NULL and fall back
to the in-memory scan, so a mismatch degrades gracefully. The index uses cosine
distance (``vector_cosine_ops``) since CALIBER stores L2-normalized embeddings.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0060"
down_revision: str | Sequence[str] | None = "0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HNSW_MAX_DIM = 2000
_DEFAULT_DIM = 1024


def _embedding_dimension() -> int:
    """Resolve the configured embedding dimension, clamped to pgvector's HNSW max."""
    raw = os.environ.get("CALIBER_KNOWLEDGE_EMBEDDING_DIMENSION", str(_DEFAULT_DIM))
    try:
        dim = int(raw)
    except ValueError:
        dim = _DEFAULT_DIM
    return max(1, min(dim, _HNSW_MAX_DIM))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    dim = _embedding_dimension()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        f"ALTER TABLE caliber_knowledge_base_chunks "
        f"ADD COLUMN IF NOT EXISTS embedding_vec vector({dim})"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_base_chunks_embedding_vec "
        "ON caliber_knowledge_base_chunks "
        "USING hnsw (embedding_vec vector_cosine_ops)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_knowledge_base_chunks_embedding_vec")
    op.execute("ALTER TABLE caliber_knowledge_base_chunks DROP COLUMN IF EXISTS embedding_vec")
