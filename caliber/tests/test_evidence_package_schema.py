"""B14.1 evidence-package contract and failure fixtures."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from caliber.contracts import ContractValidationError, UnsupportedSchemaVersion, validate_record

FIXTURES = Path(__file__).parent / "fixtures" / "contracts" / "evidence_package"
CONTRACT_FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _contract_fixture(kind: str, name: str = "valid_1.json") -> dict[str, Any]:
    return json.loads((CONTRACT_FIXTURES / kind / name).read_text(encoding="utf-8"))


def test_sample_validates() -> None:
    assert validate_record("evidence_package", _fixture("valid_1.json")) is None


def test_unknown_version_rejected() -> None:
    with pytest.raises(UnsupportedSchemaVersion) as rejected:
        validate_record("evidence_package", _fixture("invalid_unknown_version.json"))
    assert str(rejected.value) == (
        "evidence_package schema_version v9 is not supported; supported: v1"
    )


def test_member_metadata_hash_is_canonical_and_parent_link_is_in_package() -> None:
    record = _fixture("valid_1.json")
    record["members"][1]["metadata"]["state"] = "failed"
    with pytest.raises(ContractValidationError, match="metadata_sha256"):
        validate_record("evidence_package", record)


def test_package_digest_covers_the_selected_view_and_normalizes_members() -> None:
    record = _fixture("valid_1.json")
    reordered = copy.deepcopy(record)
    reordered["members"].reverse()
    reordered["members"][0]["parent_ids"] = list(reversed(reordered["members"][0]["parent_ids"]))
    assert validate_record("evidence_package", reordered) is None

    changed_view = copy.deepcopy(record)
    changed_view["view"]["redaction"] = "none"
    with pytest.raises(ContractValidationError, match="package_sha256"):
        validate_record("evidence_package", changed_view)


def test_package_digest_mismatch_is_refused() -> None:
    record = _fixture("valid_1.json")
    record["package_sha256"] = "0" * 64
    with pytest.raises(ContractValidationError, match="package_sha256"):
        validate_record("evidence_package", record)


def test_duplicate_members_and_unregistered_kinds_are_refused() -> None:
    record = _fixture("valid_1.json")
    duplicate = copy.deepcopy(record)
    duplicate["members"].append(copy.deepcopy(duplicate["members"][0]))
    with pytest.raises(ContractValidationError, match="member_id values must be unique"):
        validate_record("evidence_package", duplicate)

    unregistered = copy.deepcopy(record)
    unregistered["members"][0]["kind"] = "unregistered_kind"
    with pytest.raises(ContractValidationError, match="registered contract kind"):
        validate_record("evidence_package", unregistered)


def test_quiet_contract_refusals_keep_version_format_and_size_bounded() -> None:
    unsupported = _contract_fixture("agent_step_request")
    unsupported["schema_version"] = 1
    with pytest.raises(UnsupportedSchemaVersion, match="<int>"):
        validate_record("agent_step_request", unsupported)

    wrong_const = _contract_fixture("agent_step_result")
    wrong_const["stop_reason"] = "completed"
    with pytest.raises(ContractValidationError, match="must equal"):
        validate_record("agent_step_result", wrong_const)

    bad_format = _contract_fixture("agent_step_request")
    bad_format["limits"]["deadline"] = "2026-02-30T09:10:00Z"
    with pytest.raises(ContractValidationError, match="valid date-time"):
        validate_record("agent_step_request", bad_format)

    oversized = _contract_fixture("agent_step_request")
    oversized["workflow_run_id"] = "x" * (1 << 20)
    with pytest.raises(ContractValidationError, match="larger than"):
        validate_record("agent_step_request", oversized)

    many_errors = _contract_fixture("agent_step_result")
    for field in ("step_request_id", "workflow_run_id", "node_id", "occurrence_id"):
        many_errors[field] = ""
    many_errors["attempt"] = 0
    many_errors["stop_reason"] = "completed"
    action = many_errors["proposed_actions"][0]
    action["effect_class"] = "destroy"
    action["target"]["connector"] = ""
    action["target"]["resource"] = ""
    action["connector_version"] = ""
    action["expected_resource_version"] = ""
    for field in ("prompt_tokens", "completion_tokens", "total_tokens", "turns", "tool_calls"):
        many_errors["usage"][field] = -1
    with pytest.raises(ContractValidationError, match="and .* more violations"):
        validate_record("agent_step_result", many_errors)

    record = _fixture("valid_1.json")
    record["members"][1]["parent_ids"] = ["foreign-member"]
    with pytest.raises(ContractValidationError, match="parent_ids/0"):
        validate_record("evidence_package", record)


def test_missing_link_status_and_evaluator_provenance_are_explicit() -> None:
    record = _fixture("valid_1.json")
    assert record["missing_links"][0] == {
        "link": "acceptance",
        "status": "unrecorded",
        "code": "acceptance_record_not_available",
        "expected_member_id": None,
    }
    assert record["evaluator_provenance"][0]["provider"] == "caliber.acceptance"

    bad_status = copy.deepcopy(record)
    bad_status["missing_links"][0]["status"] = "guessed"
    with pytest.raises(ContractValidationError, match="status"):
        validate_record("evidence_package", bad_status)


def test_signatures_are_optional_and_outside_manifest_digest_contract() -> None:
    record = _fixture("valid_1.json")
    del record["signatures"]
    assert validate_record("evidence_package", record) is None

    with pytest.raises(ContractValidationError, match="unexpected"):
        validate_record("evidence_package", {**record, "unregistered": True})
