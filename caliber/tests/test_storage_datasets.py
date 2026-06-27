"""Phase 4: artifact comparison scorers + dataset file register/materialize (§7)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import caliber.db.models  # noqa: F401
from caliber.config import WorkflowStorageConfig
from caliber.db.base import Base
from caliber.db.models import CaliberEvalDatasetFile
from caliber.storage import (
    LocalStorageBackend,
    StorageValidationError,
    WorkingDirectoryService,
    compare_artifact,
)


# ----- comparison scorers -------------------------------------------------- #
def test_exact_hash() -> None:
    assert compare_artifact({"type": "exact_hash"}, b"abc", b"abc")["passed"] is True
    assert compare_artifact({"type": "exact_hash"}, b"abc", b"abd")["passed"] is False


def test_text_contains() -> None:
    r = compare_artifact(
        {"type": "text_contains", "contains": ["total", "tax"]}, None, b"total: 5\ntax: 1"
    )
    assert r["passed"] is True
    r = compare_artifact({"type": "text_contains", "contains": ["missing"]}, None, b"nope")
    assert r["passed"] is False


def test_regex() -> None:
    assert compare_artifact({"type": "regex", "pattern": r"\d{3}-\d{4}"}, None, b"call 555-1234")[
        "passed"
    ]
    assert not compare_artifact({"type": "regex", "pattern": r"^\d+$"}, None, b"abc")["passed"]


def test_json_exact_and_subset() -> None:
    assert compare_artifact({"type": "json_exact"}, b'{"a":1}', b'{"a":1}')["passed"]
    assert not compare_artifact({"type": "json_exact"}, b'{"a":1}', b'{"a":2}')["passed"]
    # subset: expected fields present in actual
    r = compare_artifact({"type": "json_field_subset"}, b'{"a":1}', b'{"a":1,"b":2}')
    assert r["passed"] is True
    # field/value form
    r = compare_artifact(
        {"type": "json_field_subset", "field": "sources", "value": "policy.pdf"},
        None,
        b'{"sources":["policy.pdf","faq.md"]}',
    )
    assert r["passed"] is True


def test_csv_rows_equal_with_tolerance_and_order() -> None:
    expected = b"invoice,total\n1,10.00\n2,20.00\n"
    actual = b"invoice,total\n2,20.001\n1,10.00\n"
    spec = {
        "type": "csv_rows_equal",
        "ignore_row_order": True,
        "numeric_tolerance": 0.01,
        "required_columns": ["invoice", "total"],
    }
    assert compare_artifact(spec, expected, actual)["passed"] is True
    # row count mismatch fails
    assert not compare_artifact({"type": "csv_rows_equal"}, b"a\n1\n", b"a\n1\n2\n")["passed"]


def test_unknown_match_type_is_failure_not_crash() -> None:
    r = compare_artifact({"type": "nonsense"}, b"x", b"x")
    assert r["passed"] is False and "unknown match type" in r["detail"]


def test_no_actual_artifact() -> None:
    assert compare_artifact({"type": "text_exact"}, b"x", None)["passed"] is False


# ----- dataset file register + materialize --------------------------------- #
@pytest.fixture
def service(tmp_path: Path) -> Iterator[tuple[WorkingDirectoryService, Session]]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    cfg = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/ws")
    svc = WorkingDirectoryService(LocalStorageBackend(cfg.base_uri), cfg)
    try:
        with sessionmaker(engine)() as s:
            yield svc, s
    finally:
        engine.dispose()


def test_register_dataset_file_creates_row_and_join(service) -> None:
    svc, s = service
    rec = svc.register_dataset_file(
        s,
        dataset_id="ED-1",
        example_id="EX-1",
        role="invoice",
        kind="input",
        filename="invoice.pdf",
        data=b"%PDF-1.7",
        media_type="application/pdf",
        actor="@me",
    )
    s.flush()
    assert rec.file_ref == "caliber://datasets/ED-1/examples/EX-1/input/invoice.pdf"
    joins = s.query(CaliberEvalDatasetFile).all()
    assert len(joins) == 1 and joins[0].file_id == rec.file_id and joins[0].role == "invoice"


def test_register_dataset_rejects_non_dataset_kind(service) -> None:
    svc, s = service
    with pytest.raises(StorageValidationError, match="dataset kind"):
        svc.register_dataset_file(
            s,
            dataset_id="ED-1",
            example_id="EX-1",
            role="x",
            kind="work",
            filename="f.txt",
            data=b"x",
            media_type=None,
            actor="@me",
        )


def test_dataset_file_materializes_into_run(service) -> None:
    svc, s = service
    ds = svc.register_dataset_file(
        s,
        dataset_id="ED-1",
        example_id="EX-1",
        role="invoice",
        kind="input",
        filename="invoice.pdf",
        data=b"%PDF",
        media_type="application/pdf",
        actor="@me",
    )
    s.flush()
    run = svc.create_run_workspace(workflow_id="WF-1", workflow_run_id="WR-EVAL")
    bound = svc.materialize_input_files(s, run, [{"file_ref": ds.file_ref}], actor="@eval")
    assert len(bound) == 1
    assert bound[0].file_ref == "caliber://workflow-runs/WR-EVAL/input/invoice.pdf"
    assert svc.read_bytes(svc.get_row(s, bound[0].file_id)) == b"%PDF"
