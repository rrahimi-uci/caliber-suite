"""pgvector SQL-side ANN candidate generation (Postgres-only).

On Postgres with the ``vector`` extension, dense retrieval gathers its candidate
pool with a ``ORDER BY embedding_vec <=> :q LIMIT :pool`` over the chunk vector
column — the database computes the cosine distances in C and returns only the
top-``pool`` rows, instead of CALIBER materializing every chunk and running an
O(n·d) Python cosine scan. That's the difference between demo-scale and millions
of chunks.

On SQLite (tests, local dev) there is no pgvector, so :func:`is_postgres` is
``False`` and the service falls back to the in-memory Python path — keeping the
exact behaviour the existing tests pin. The ``embedding_vec`` column itself is
created (and populated at build time) only on Postgres, via migration ``0060``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

CHUNK_TABLE = "caliber_knowledge_base_chunks"
VECTOR_COLUMN = "embedding_vec"
_PK = "knowledge_base_chunk_id"
_VERSION_FK = "knowledge_base_version_id"


def is_postgres(session: Any) -> bool:
    """Whether ``session`` is bound to a Postgres engine (so pgvector may exist)."""
    try:
        return bool(session.get_bind().dialect.name == "postgresql")
    except Exception:  # pragma: no cover - defensive; missing bind
        return False


def to_vector_literal(vector: Sequence[float]) -> str:
    """Render a vector as a pgvector text literal, e.g. ``[0.1,0.2,0.3]``."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def dense_candidate_ids(
    session: Any,
    *,
    version_id: str,
    query_vector: Sequence[float],
    pool_size: int,
) -> list[tuple[str, float]]:
    """Return ``[(chunk_id, cosine_distance)]`` for the ``pool_size`` nearest chunks.

    Cosine distance (``<=>``) is in ``[0, 2]``; the caller turns it into a
    similarity (``1 - distance``). Postgres-only — guard with :func:`is_postgres`
    (and the ``knowledge_pgvector_enabled`` flag) before calling.
    """
    # Table/column names are module constants (not user input); every value is a
    # bound parameter — so the f-string interpolation is not an injection vector.
    stmt = text(
        f"SELECT {_PK}, ({VECTOR_COLUMN} <=> CAST(:q AS vector)) AS dist "  # noqa: S608
        f"FROM {CHUNK_TABLE} "
        f"WHERE {_VERSION_FK} = :vid AND {VECTOR_COLUMN} IS NOT NULL "
        "ORDER BY dist ASC LIMIT :pool"
    )
    rows = session.execute(
        stmt,
        {"q": to_vector_literal(query_vector), "vid": version_id, "pool": int(pool_size)},
    ).all()
    return [(str(row[0]), float(row[1])) for row in rows]


def populate_chunk_vectors(
    session: Any,
    rows: Sequence[tuple[str, Sequence[float]]],
    *,
    expected_dim: int,
) -> None:
    """Set the ``embedding_vec`` column for freshly-built chunks (Postgres-only).

    ``rows`` is ``[(chunk_id, embedding)]``. The ``embedding_vec`` column is a
    fixed-dimension ``vector(expected_dim)`` (so it can carry an HNSW index), so
    chunks whose embedding dimension differs are **skipped** rather than inserted
    — a dimension mismatch would otherwise error the whole build. Those chunks
    keep their JSON ``embedding`` and fall back to the in-memory cosine scan at
    query time. A no-op when nothing matches. The caller guards on
    :func:`is_postgres` + the pgvector flag.
    """
    payload = [
        {"id": chunk_id, "v": to_vector_literal(embedding)}
        for chunk_id, embedding in rows
        if embedding and len(embedding) == expected_dim
    ]
    if not payload:
        return
    # Constant table/column names + bound values — not an injection vector.
    stmt = text(
        f"UPDATE {CHUNK_TABLE} SET {VECTOR_COLUMN} = CAST(:v AS vector) WHERE {_PK} = :id"  # noqa: S608
    )
    session.execute(stmt, payload)
