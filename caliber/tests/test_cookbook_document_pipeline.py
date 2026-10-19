"""Cookbook 04 (Document-to-JSON Pipeline): the installed workflow, its dataset and its docs copies.

Three things are pinned here:

* the documentation assets (the extractor body, the invoice schema) are byte-for-byte what the
  catalog installs, so the lab's README cannot drift from the product;
* every one of the 30 sample documents behaves as ``assets/dataset/extraction-cases.jsonl`` says,
  through the *real* python_code sandbox and the real ``json_schema`` data transform, with the
  ``input_bucket`` rules (UTF-8 decode with replacement, 100 KB cap) applied first;
* the installed manifest, run through the real runtime over a local bucket backend, routes a valid
  invoice to ``invoices/``, an invalid one through the approval gate to ``reviewed/``, and refuses to
  overwrite an invoice that already exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from caliber.tool_sandbox.service import sandbox_from_optional_config
from caliber.workflows import runtime as rt
from caliber.workflows.compiler import build_ir
from caliber.workflows.component_catalog import evaluate_requirements
from caliber.workflows.cookbook_catalog import materialize_cookbook_manifest
from caliber.workflows.cookbook_document_pipeline import (
    EXTRACT_FIELDS_CODE,
    INPUT_BUCKET,
    INVOICE_SCHEMA,
    OUTPUT_BUCKET,
)
from caliber.workflows.data_transform import apply_data_transform
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import RuntimePlan, execute
from tests.workflow_helpers import fake_resolver

LAB = (
    Path(__file__).resolve().parents[2]
    / "docs-site/cookbooks/04-document-extraction-structuring-lab"
)
ASSETS = LAB / "assets"
MAX_BYTES = 100_000


def _cases() -> list[dict[str, Any]]:
    lines = (ASSETS / "dataset/extraction-cases.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


CASES = _cases()


def _read_like_input_bucket(key: str) -> dict[str, Any]:
    """What ``input_bucket`` hands downstream for one object: capped bytes, UTF-8 with replacement."""
    raw = (ASSETS / "dataset/sources" / key.split("/", 1)[1]).read_bytes()
    return {
        "key": key,
        "bytes": min(len(raw), MAX_BYTES),
        "truncated": len(raw) > MAX_BYTES,
        "text": raw[:MAX_BYTES].decode("utf-8", errors="replace"),
    }


def _extract(key: str) -> dict[str, Any]:
    """Run the extractor body in the real python_code sandbox, exactly as the node does."""
    request = rt.ToolSandboxRunRequest(
        source_code=rt._python_node_source(EXTRACT_FIELDS_CODE),
        callable_name=rt._PYTHON_NODE_CALLABLE,
        input={
            "input": None,
            "context": {},
            "inputs": {"files": [_read_like_input_bucket(key)]},
            "run_input": key,
        },
        timeout_seconds=20,
    )
    result = sandbox_from_optional_config(None, default_timeout_seconds=20).run_tool(request)
    assert result.status == "completed", result.error
    return result.output


def test_docs_assets_are_what_the_catalog_installs() -> None:
    shipped = (ASSETS / "tools/extract_invoice_fields.py").read_text(encoding="utf-8")
    assert shipped == EXTRACT_FIELDS_CODE
    assert json.loads((ASSETS / "schema/invoice.schema.json").read_text("utf-8")) == INVOICE_SCHEMA
    manifest = materialize_cookbook_manifest("04", workflow_id="WF-x", workflow_name="n")
    assert manifest["nodes"]["extract_fields"]["code"] == shipped
    assert manifest["nodes"]["validate_schema"]["config"]["schema"] == INVOICE_SCHEMA


def test_the_dataset_has_the_mix_the_readme_describes() -> None:
    outcomes = [c["expectations"]["outcome"] for c in CASES]
    assert len(CASES) == 30
    assert outcomes.count("extracted") == 16
    assert outcomes.count("needs_review") == 13
    assert outcomes.count("duplicate") == 1
    assert len({c["inputs"]["key"] for c in CASES}) == 30
    for case in CASES:
        assert (ASSETS / "dataset/sources" / case["inputs"]["key"].split("/", 1)[1]).is_file()
    oversized = next(c for c in CASES if "oversized" in c["inputs"]["key"])
    assert oversized["source_bytes"] > MAX_BYTES


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_every_document_is_extracted_and_validated_as_the_dataset_says(
    case: dict[str, Any],
) -> None:
    expected = case["expectations"]
    record = _extract(case["inputs"]["key"])
    checked = apply_data_transform("json_schema", record, {"schema": INVOICE_SCHEMA})
    paths = sorted({e["path"] for e in checked["metadata"]["errors"]})

    if expected["outcome"] == "needs_review":
        assert not checked["valid"]
        assert paths == sorted(expected["errors"])
        return
    assert checked["valid"], checked["metadata"]["errors"]
    wanted = expected.get("record") or next(
        c["expectations"]["record"]
        for c in CASES
        if c["expectations"].get("record", {}).get("invoice_number") == expected["duplicate_of"]
    )
    assert {k: record[k] for k in wanted} == wanted


def test_oversized_and_non_utf8_documents_are_caught_by_the_input_rules() -> None:
    oversized = _extract(
        next(c for c in CASES if "oversized" in c["inputs"]["key"])["inputs"]["key"]
    )
    assert oversized["truncated"] is True and oversized["total"] is None
    latin1 = _extract(next(c for c in CASES if "latin1" in c["inputs"]["key"])["inputs"]["key"])
    assert latin1["encoding_clean"] is False and "�" in latin1["vendor"]


def test_extractor_refuses_a_key_that_does_not_match_exactly_one_object() -> None:
    request = rt.ToolSandboxRunRequest(
        source_code=rt._python_node_source(EXTRACT_FIELDS_CODE),
        callable_name=rt._PYTHON_NODE_CALLABLE,
        input={
            "input": None,
            "context": {},
            "inputs": {"files": []},
            "run_input": "incoming/x.txt",
        },
        timeout_seconds=20,
    )
    result = sandbox_from_optional_config(None, default_timeout_seconds=20).run_tool(request)
    assert result.status != "completed"
    assert "expected exactly one object under 'incoming/x.txt', found 0" in (result.error or "")


# ---------------------------------------------------------------------------
# The installed manifest, through the real runtime over a local bucket backend
# ---------------------------------------------------------------------------


@pytest.fixture
def buckets(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real ``_bucket_io`` over an in-process S3 (moto), the same backend the live lab uses."""
    boto3 = pytest.importorskip("boto3")
    moto = pytest.importorskip("moto")
    for name, value in {
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "CALIBER_WORKFLOW_STORAGE_BACKEND": "s3",
        "CALIBER_WORKFLOW_STORAGE_BUCKET": "caliber-workspaces",
        "CALIBER_WORKFLOW_STORAGE_REGION": "us-east-1",
        "CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE": "AWS_ACCESS_KEY_ID",
        "CALIBER_WORKFLOW_STORAGE_SECRET_KEY_SOURCE": "AWS_SECRET_ACCESS_KEY",
    }.items():
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in ("caliber-workspaces", INPUT_BUCKET, OUTPUT_BUCKET):
            s3.create_bucket(Bucket=bucket)
        for case in CASES:
            name = case["inputs"]["key"].split("/", 1)[1]
            s3.put_object(
                Bucket=INPUT_BUCKET,
                Key=case["inputs"]["key"],
                Body=(ASSETS / "dataset/sources" / name).read_bytes(),
            )
        yield s3


def _plan() -> RuntimePlan:
    manifest = materialize_cookbook_manifest("04", workflow_id="WF-x", workflow_name="n")
    manifest.pop("cookbook", None)
    ir = build_ir(parse_manifest(manifest), fake_resolver(), version="1")
    return RuntimePlan(ir=ir, resolver=fake_resolver())


def _stored(s3: Any) -> set[str]:
    listed = s3.list_objects_v2(Bucket=OUTPUT_BUCKET)
    return {o["Key"] for o in listed.get("Contents", [])}


def _read(s3: Any, key: str) -> dict[str, Any]:
    return json.loads(s3.get_object(Bucket=OUTPUT_BUCKET, Key=key)["Body"].read())


def test_a_valid_invoice_is_written_and_a_duplicate_is_refused(buckets: Any) -> None:
    key = "incoming/inv-2026-0101-acme.txt"
    first = execute(_plan(), key, executor=rt.FakeWorkflowExecutor())
    assert first.status == "completed", first.error
    assert _stored(buckets) == {"invoices/INV-2026-0101.json"}
    stored = _read(buckets, "invoices/INV-2026-0101.json")
    assert stored["total"] == 646.92 and stored["source_key"] == key

    again = execute(
        _plan(), "incoming/inv-2026-0101-acme-resend.txt", executor=rt.FakeWorkflowExecutor()
    )
    assert again.status == "error"
    assert "object already exists: 'invoices/INV-2026-0101.json'" in (again.error or "")
    assert _read(buckets, "invoices/INV-2026-0101.json") == stored


def test_an_invalid_invoice_goes_through_the_gate_and_lands_under_reviewed(buckets: Any) -> None:
    result = execute(
        _plan(), "incoming/inv-2026-0115-no-total.txt", executor=rt.FakeWorkflowExecutor()
    )
    assert result.status == "completed", result.error
    # Runtime approvals are off in this unit run, so the gate passes the document through; the live
    # pause-and-approve behaviour is covered by the lab's API and browser runs.
    assert _stored(buckets) == {"reviewed/inv-2026-0115-no-total.json"}
    reviewed = _read(buckets, "reviewed/inv-2026-0115-no-total.json")
    assert reviewed["review"]["decision"] == "approved"
    assert [e["path"] for e in reviewed["review"]["schema_errors"]] == ["total"]
    assert [s.node_id for s in result.steps if s.node_id == "emit_extracted"] == []


def test_a_key_that_matches_no_object_fails_with_a_clear_message(buckets: Any) -> None:
    result = execute(_plan(), "incoming/nope.txt", executor=rt.FakeWorkflowExecutor())
    assert result.status == "error"
    assert "expected exactly one object under 'incoming/nope.txt', found 0" in (result.error or "")


# ---------------------------------------------------------------------------
# The gallery's readiness check
# ---------------------------------------------------------------------------


def test_the_gallery_reports_the_object_store_check(client: Any) -> None:
    prefix = "/ajax-api/2.0/mlflow/caliber"
    recipes = {r["id"]: r for r in client.get(f"{prefix}/cookbooks").json()["data"]["recipes"]}
    check = recipes["04"]["readiness"]["checks"][0]
    assert check["label"].startswith("An S3-compatible object store")
    expected = (
        "ready"
        if evaluate_requirements(client.app.state.config)["object_store"]
        else "configuration_required"
    )
    assert check["status"] == expected
    assert recipes["04"]["readiness"]["checks"][1]["label"].startswith("Workflow queue")

    client.app.state.config = client.app.state.config.model_copy(
        update={
            "workflow_storage": client.app.state.config.workflow_storage.model_copy(
                update={"backend": "s3", "bucket": "caliber-workspaces"}
            )
        }
    )
    recipes = {r["id"]: r for r in client.get(f"{prefix}/cookbooks").json()["data"]["recipes"]}
    assert recipes["04"]["readiness"]["checks"][0]["status"] == "ready"
