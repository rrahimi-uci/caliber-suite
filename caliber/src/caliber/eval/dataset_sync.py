"""Sync CALIBER eval datasets into MLflow's native GenAI dataset registry.

CALIBER keeps test sets in Postgres as the source of truth (versioned,
project-scoped, append-only examples). MLflow 3.14 shipped a first-class
``mlflow.genai.datasets`` registry plus a revamped dataset UI with
source-trace lineage. Rather than migrate the data out of Postgres, we *push*
the current example set to MLflow on demand so those native surfaces light up
while CALIBER stays authoritative.

The boundary mirrors :mod:`caliber.trace_client` / :mod:`caliber.mlflow_client`:
a :class:`DatasetSyncClient` Protocol with a real MLflow implementation and a
fake for tests. Every MLflow call is the caller's concern to guard — the real
client lets exceptions propagate so the route can degrade to a clean error.

``merge_records`` upserts by the record's ``inputs`` hash, so re-syncing the same
example set is idempotent (no duplicate records) and changed expectations update
in place.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetRecord:
    """One example mapped to MLflow's record shape (inputs + expectations)."""

    inputs: Mapping[str, Any]
    expectations: Mapping[str, Any]

    def to_mlflow(self) -> dict[str, Any]:
        return {"inputs": dict(self.inputs), "expectations": dict(self.expectations)}


@dataclass(frozen=True)
class DatasetSyncResult:
    """Outcome of a push to MLflow's dataset registry."""

    mlflow_dataset_id: str
    name: str
    record_count: int
    digest: str | None = None
    synced_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DatasetSyncClient(Protocol):
    """Boundary for pushing a CALIBER test set to MLflow's dataset registry."""

    def sync_dataset(
        self,
        *,
        name: str,
        records: Sequence[DatasetRecord],
        experiment_id: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> DatasetSyncResult: ...


class MLflowDatasetSyncClient:
    """Production client backed by ``mlflow.genai.datasets`` (MLflow 3.14+)."""

    def sync_dataset(
        self,
        *,
        name: str,
        records: Sequence[DatasetRecord],
        experiment_id: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> DatasetSyncResult:
        # Lazy import — keeps mlflow optional for unit tests that inject a fake.
        from mlflow.genai import datasets as mlflow_datasets  # noqa: PLC0415

        tag_dict = dict(tags or {})
        dataset = self._get_or_create(mlflow_datasets, name, experiment_id, tag_dict)

        if records:
            dataset.merge_records([record.to_mlflow() for record in records])
            # Re-fetch so digest / record state reflect the merge.
            dataset = mlflow_datasets.get_dataset(dataset_id=dataset.dataset_id)

        return DatasetSyncResult(
            mlflow_dataset_id=str(dataset.dataset_id),
            name=str(getattr(dataset, "name", name)),
            record_count=len(records),
            digest=_optional_str(getattr(dataset, "digest", None)),
        )

    @staticmethod
    def _get_or_create(
        mlflow_datasets: Any,
        name: str,
        experiment_id: str | None,
        tags: dict[str, str],
    ) -> Any:
        # Re-use an existing dataset of the same name so re-syncs keep one
        # stable id (and thus stable trace lineage); otherwise create it.
        try:
            existing = mlflow_datasets.get_dataset(name=name)
        except Exception:
            existing = None
        if existing is not None:
            if tags:
                try:
                    mlflow_datasets.set_dataset_tags(dataset_id=existing.dataset_id, tags=tags)
                except Exception as exc:
                    logger.debug("set_dataset_tags failed for %s (%s)", name, exc)
            return existing
        return mlflow_datasets.create_dataset(
            name=name,
            experiment_id=experiment_id,
            tags=tags or None,
        )


class FakeDatasetSyncClient:
    """In-memory test double — records calls, returns a deterministic result."""

    def __init__(self, *, dataset_id: str = "d-fake", digest: str = "digest-1") -> None:
        self._dataset_id = dataset_id
        self._digest = digest
        self.calls: list[dict[str, Any]] = []

    def sync_dataset(
        self,
        *,
        name: str,
        records: Sequence[DatasetRecord],
        experiment_id: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> DatasetSyncResult:
        self.calls.append(
            {
                "name": name,
                "record_count": len(records),
                "experiment_id": experiment_id,
                "tags": dict(tags or {}),
            }
        )
        return DatasetSyncResult(
            mlflow_dataset_id=self._dataset_id,
            name=name,
            record_count=len(records),
            digest=self._digest,
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
