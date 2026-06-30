"""Unit tests for :mod:`caliber.eval.dataset_sync`.

These cover the *non-MLflow* mapping/transform logic plus the
``MLflowDatasetSyncClient`` with the ``mlflow.genai.datasets`` boundary
injected via a fake module (the same ``monkeypatch.setitem(sys.modules, ...)``
trick used in ``test_eval_mlflow_runner.py``). No real MLflow is ever touched.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from caliber.eval.dataset_sync import (
    DatasetRecord,
    DatasetSyncResult,
    FakeDatasetSyncClient,
    MLflowDatasetSyncClient,
    _optional_str,
)


class _FakeDataset:
    """Minimal stand-in for an ``mlflow.genai`` dataset handle."""

    def __init__(self, *, dataset_id: str, name: str, digest: Any = None) -> None:
        self.dataset_id = dataset_id
        self.name = name
        self.digest = digest
        self.merged: list[list[dict[str, Any]]] = []

    def merge_records(self, records: list[dict[str, Any]]) -> None:
        self.merged.append(records)


def _install_fake_datasets(
    monkeypatch: pytest.MonkeyPatch, namespace: types.SimpleNamespace
) -> None:
    """Install ``mlflow.genai.datasets`` so the lazy import inside
    :meth:`MLflowDatasetSyncClient.sync_dataset` resolves to ``namespace``."""
    mlflow_stub = types.ModuleType("mlflow")
    genai_stub = types.ModuleType("mlflow.genai")
    genai_stub.datasets = namespace  # type: ignore[attr-defined]
    mlflow_stub.genai = genai_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai", genai_stub)
    monkeypatch.setitem(sys.modules, "mlflow.genai.datasets", namespace)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# DatasetRecord.to_mlflow (line 39)
# --------------------------------------------------------------------------- #


def test_dataset_record_to_mlflow_copies_into_plain_dicts() -> None:
    inputs = {"question": "hi"}
    expectations = {"answer": "hello"}
    record = DatasetRecord(inputs=inputs, expectations=expectations)

    mapped = record.to_mlflow()

    assert mapped == {
        "inputs": {"question": "hi"},
        "expectations": {"answer": "hello"},
    }
    # Returned dicts must be independent copies, not aliases of the source.
    mapped["inputs"]["question"] = "mutated"
    assert inputs["question"] == "hi"
    assert isinstance(mapped["inputs"], dict)
    assert isinstance(mapped["expectations"], dict)


# --------------------------------------------------------------------------- #
# MLflowDatasetSyncClient.sync_dataset (lines 78-88) + _get_or_create (104-117)
# --------------------------------------------------------------------------- #


def test_sync_dataset_creates_new_and_merges_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _FakeDataset(dataset_id="d-new", name="golden", digest="dg-pre")
    # After merge, sync_dataset re-fetches by id; return a fresh handle whose
    # digest reflects the merge so the result carries the post-merge state.
    refetched = _FakeDataset(dataset_id="d-new", name="golden", digest="dg-post")
    create_calls: list[dict[str, Any]] = []
    get_by_id_calls: list[str] = []

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        if dataset_id is not None:
            get_by_id_calls.append(dataset_id)
            return refetched
        # Lookup by name during _get_or_create: nothing exists yet.
        return None

    def create_dataset(*, name: str, experiment_id: str | None, tags: Any) -> Any:
        create_calls.append({"name": name, "experiment_id": experiment_id, "tags": tags})
        return created

    ns = types.SimpleNamespace(get_dataset=get_dataset, create_dataset=create_dataset)
    _install_fake_datasets(monkeypatch, ns)

    records = [
        DatasetRecord(inputs={"q": "1"}, expectations={"a": "x"}),
        DatasetRecord(inputs={"q": "2"}, expectations={"a": "y"}),
    ]
    result = MLflowDatasetSyncClient().sync_dataset(
        name="golden",
        records=records,
        experiment_id="exp-7",
        tags={"team": "qa"},
    )

    # Created (not found by name) with experiment + tags threaded through.
    assert create_calls == [{"name": "golden", "experiment_id": "exp-7", "tags": {"team": "qa"}}]
    # merge_records was called on the created handle with the mapped shape.
    assert created.merged == [
        [
            {"inputs": {"q": "1"}, "expectations": {"a": "x"}},
            {"inputs": {"q": "2"}, "expectations": {"a": "y"}},
        ]
    ]
    # Re-fetch happened by the created dataset's id.
    assert get_by_id_calls == ["d-new"]

    assert isinstance(result, DatasetSyncResult)
    assert result.mlflow_dataset_id == "d-new"
    assert result.name == "golden"
    assert result.record_count == 2
    # Digest comes from the re-fetched (post-merge) handle.
    assert result.digest == "dg-post"


def test_sync_dataset_empty_records_skips_merge_and_refetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty-dataset path: no merge, no re-fetch — the created handle is
    used directly and record_count is 0 (exercises the 83->88 false branch)."""
    created = _FakeDataset(dataset_id="d-empty", name="empty-ds", digest="dg-0")
    get_by_id_calls: list[str] = []

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        if dataset_id is not None:
            get_by_id_calls.append(dataset_id)
            return created
        return None

    def create_dataset(*, name: str, experiment_id: str | None, tags: Any) -> Any:
        return created

    ns = types.SimpleNamespace(get_dataset=get_dataset, create_dataset=create_dataset)
    _install_fake_datasets(monkeypatch, ns)

    result = MLflowDatasetSyncClient().sync_dataset(name="empty-ds", records=[])

    assert created.merged == []  # merge_records never invoked
    assert get_by_id_calls == []  # no re-fetch
    assert result.record_count == 0
    assert result.mlflow_dataset_id == "d-empty"
    assert result.name == "empty-ds"
    assert result.digest == "dg-0"


def test_sync_dataset_reuses_existing_dataset_and_sets_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a dataset of the same name already exists, _get_or_create reuses it
    and pushes tags via set_dataset_tags rather than creating a new one."""
    existing = _FakeDataset(dataset_id="d-existing", name="reuse", digest="dg-x")
    refetched = _FakeDataset(dataset_id="d-existing", name="reuse", digest="dg-x2")
    set_tag_calls: list[dict[str, Any]] = []
    create_calls: list[Any] = []

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        if dataset_id is not None:
            return refetched
        return existing  # found by name

    def set_dataset_tags(*, dataset_id: str, tags: dict[str, str]) -> None:
        set_tag_calls.append({"dataset_id": dataset_id, "tags": tags})

    def create_dataset(**kw: Any) -> Any:
        create_calls.append(kw)
        raise AssertionError("create_dataset must not be called when reusing")

    ns = types.SimpleNamespace(
        get_dataset=get_dataset,
        set_dataset_tags=set_dataset_tags,
        create_dataset=create_dataset,
    )
    _install_fake_datasets(monkeypatch, ns)

    result = MLflowDatasetSyncClient().sync_dataset(
        name="reuse",
        records=[DatasetRecord(inputs={"q": "1"}, expectations={})],
        tags={"env": "prod"},
    )

    assert create_calls == []
    assert set_tag_calls == [{"dataset_id": "d-existing", "tags": {"env": "prod"}}]
    # merge ran on the existing handle.
    assert existing.merged == [[{"inputs": {"q": "1"}, "expectations": {}}]]
    assert result.mlflow_dataset_id == "d-existing"
    assert result.digest == "dg-x2"


def test_sync_dataset_get_existing_failure_falls_back_to_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When get_dataset(name=...) raises, _get_or_create swallows it and
    creates a fresh dataset (exercises the except->existing=None branch)."""
    created = _FakeDataset(dataset_id="d-after-error", name="boom", digest=None)

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        if dataset_id is not None:
            return created
        raise RuntimeError("registry lookup down")

    def create_dataset(*, name: str, experiment_id: str | None, tags: Any) -> Any:
        return created

    ns = types.SimpleNamespace(get_dataset=get_dataset, create_dataset=create_dataset)
    _install_fake_datasets(monkeypatch, ns)

    result = MLflowDatasetSyncClient().sync_dataset(
        name="boom",
        records=[DatasetRecord(inputs={"q": "1"}, expectations={"a": "z"})],
    )

    assert result.mlflow_dataset_id == "d-after-error"
    # digest=None on the handle maps to None on the result.
    assert result.digest is None


def test_sync_dataset_existing_set_tags_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing set_dataset_tags must be logged-and-ignored; the existing
    dataset is still reused (exercises the set_dataset_tags except branch)."""
    existing = _FakeDataset(dataset_id="d-keep", name="keep", digest="dg")

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        if dataset_id is not None:
            return existing
        return existing

    def set_dataset_tags(*, dataset_id: str, tags: dict[str, str]) -> None:
        raise RuntimeError("tagging unavailable")

    ns = types.SimpleNamespace(get_dataset=get_dataset, set_dataset_tags=set_dataset_tags)
    _install_fake_datasets(monkeypatch, ns)

    result = MLflowDatasetSyncClient().sync_dataset(
        name="keep",
        records=[DatasetRecord(inputs={"q": "1"}, expectations={})],
        tags={"team": "qa"},
    )

    assert result.mlflow_dataset_id == "d-keep"
    assert result.record_count == 1


def test_sync_dataset_existing_without_tags_skips_set_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reusing an existing dataset with no tags must not call set_dataset_tags
    at all (exercises the `if tags:` false branch inside _get_or_create)."""
    existing = _FakeDataset(dataset_id="d-notag", name="notag", digest="dg")

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        return existing

    def set_dataset_tags(**_kw: Any) -> None:
        raise AssertionError("set_dataset_tags must not be called without tags")

    ns = types.SimpleNamespace(get_dataset=get_dataset, set_dataset_tags=set_dataset_tags)
    _install_fake_datasets(monkeypatch, ns)

    result = MLflowDatasetSyncClient().sync_dataset(name="notag", records=[])

    assert result.mlflow_dataset_id == "d-notag"
    assert result.record_count == 0


def test_sync_dataset_name_falls_back_when_handle_missing_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the dataset handle lacks a ``name`` attribute, the result name falls
    back to the requested name (the getattr default path on line 90)."""

    class _NoName:
        dataset_id = "d-x"
        digest = "dg"

    handle = _NoName()

    def get_dataset(*, name: str | None = None, dataset_id: str | None = None) -> Any:
        return handle

    ns = types.SimpleNamespace(get_dataset=get_dataset)
    _install_fake_datasets(monkeypatch, ns)

    result = MLflowDatasetSyncClient().sync_dataset(name="requested-name", records=[])

    assert result.name == "requested-name"
    assert result.mlflow_dataset_id == "d-x"


# --------------------------------------------------------------------------- #
# _optional_str (lines 157-160)
# --------------------------------------------------------------------------- #


def test_optional_str_returns_none_for_none() -> None:
    assert _optional_str(None) is None


def test_optional_str_returns_none_for_empty_string() -> None:
    # str("") is falsy -> normalized back to None.
    assert _optional_str("") is None


def test_optional_str_stringifies_non_empty_values() -> None:
    assert _optional_str("digest-abc") == "digest-abc"
    assert _optional_str(123) == "123"


# --------------------------------------------------------------------------- #
# FakeDatasetSyncClient sanity (records calls, deterministic result)
# --------------------------------------------------------------------------- #


def test_fake_client_records_calls_and_returns_deterministic_result() -> None:
    client = FakeDatasetSyncClient(dataset_id="d-fake", digest="dg-fake")
    records = [DatasetRecord(inputs={"q": "1"}, expectations={"a": "x"})]

    result = client.sync_dataset(
        name="ds",
        records=records,
        experiment_id="exp-1",
        tags={"k": "v"},
    )

    assert client.calls == [
        {
            "name": "ds",
            "record_count": 1,
            "experiment_id": "exp-1",
            "tags": {"k": "v"},
        }
    ]
    assert result.mlflow_dataset_id == "d-fake"
    assert result.name == "ds"
    assert result.record_count == 1
    assert result.digest == "dg-fake"
