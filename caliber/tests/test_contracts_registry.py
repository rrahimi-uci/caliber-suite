"""The control-record contracts: seven kinds, versioned schemas, typed rejection.

Fixtures under ``tests/fixtures/contracts/<kind>/`` are discovered by file-name prefix, so a package that adds
a record field or a kind adds fixtures and does not edit this test:

* ``valid_*.json`` must validate;
* ``invalid_unknown_version_*.json`` must raise :class:`UnsupportedSchemaVersion`;
* any other ``invalid_*.json`` must raise :class:`ContractValidationError`.

ADR 0003 is the human-readable form of the same contracts, and ``test_adr_0003_matches_the_schemas`` fails when
the two disagree on a field or on whether it is required.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from caliber import contracts
from caliber.contracts import (
    KINDS,
    SUPPORTED_VERSIONS,
    ContractValidationError,
    UnknownContractKind,
    UnsupportedSchemaVersion,
    load_schema,
    validate_record,
)

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures" / "contracts"
SCHEMAS = Path(contracts.__file__).parent / "schemas"
ADR = TESTS.parent.parent / "docs" / "architecture" / "adr" / "0003-control-record-schemas.md"
VERSION_FIELD = {"event_envelope": "specversion"}


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*/*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def valid_record(kind: str) -> dict[str, Any]:
    return load_fixture(FIXTURES / kind / "valid_1.json")


# ---------------------------------------------------------------- the schemas


def test_there_are_seven_kinds_and_a_schema_for_each() -> None:
    assert len(KINDS) == 7
    assert set(KINDS) == set(SUPPORTED_VERSIONS)
    on_disk = {p.name for p in SCHEMAS.glob("*.json")}
    assert on_disk == {f"{kind}.v1.json" for kind in KINDS}


@pytest.mark.parametrize("kind", KINDS)
def test_every_schema_is_valid_draft_2020_12(kind: str) -> None:
    version = SUPPORTED_VERSIONS[kind][0]
    schema = load_schema(kind, version)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == f"https://caliber.dev/schemas/{kind}.v1.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("kind", KINDS)
def test_the_version_field_is_a_constant(kind: str) -> None:
    field = VERSION_FIELD.get(kind, "schema_version")
    schema = load_schema(kind, SUPPORTED_VERSIONS[kind][0])
    assert schema["properties"][field] == {"const": SUPPORTED_VERSIONS[kind][0]}
    assert field in schema["required"]


@pytest.mark.parametrize("kind", ["run_manifest", "proposal", "event_envelope"])
def test_tenant_and_region_are_required_where_the_plan_says(kind: str) -> None:
    required = set(load_schema(kind, SUPPORTED_VERSIONS[kind][0])["required"])
    assert {"tenant_id" if kind != "event_envelope" else "tenantid"} <= required
    if kind != "event_envelope":
        assert "region" in required


def test_load_schema_returns_a_copy() -> None:
    schema = load_schema("proposal", "v1")
    schema["required"].clear()
    assert load_schema("proposal", "v1")["required"], "the shared schema was changed through a copy"


def test_load_schema_rejects_an_unknown_kind_or_version() -> None:
    with pytest.raises(UnknownContractKind, match="unknown contract kind 'nope'"):
        load_schema("nope", "v1")
    with pytest.raises(UnsupportedSchemaVersion) as unsupported:
        load_schema("proposal", "v2")
    assert unsupported.value.supported == ("v1",)


# ---------------------------------------------------------------- the fixtures


def test_every_kind_has_the_three_required_fixtures() -> None:
    for kind in KINDS:
        names = {p.name for p in (FIXTURES / kind).glob("*.json")}
        assert {
            "valid_1.json",
            "invalid_missing_field.json",
            "invalid_unknown_version.json",
        } <= names, kind
    assert len(fixture_files()) >= 21


@pytest.mark.parametrize("path", fixture_files(), ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_fixture_behaves_as_its_name_says(path: Path) -> None:
    kind, payload = path.parent.name, load_fixture(path)
    assert kind in KINDS, f"a fixture directory for an unknown kind: {kind}"
    if path.name.startswith("valid"):
        assert validate_record(kind, payload) is None
    elif path.name.startswith("invalid_unknown_version"):
        with pytest.raises(UnsupportedSchemaVersion):
            validate_record(kind, payload)
    else:
        assert path.name.startswith("invalid"), f"{path.name}: a fixture is valid_* or invalid_*"
        with pytest.raises(ContractValidationError) as rejected:
            validate_record(kind, payload)
        assert rejected.value.errors


# ---------------------------------------------------------------- the required behavior


def test_unknown_version_is_rejected() -> None:
    record = {**valid_record("proposal"), "schema_version": "v9"}
    with pytest.raises(UnsupportedSchemaVersion) as rejected:
        validate_record("proposal", record)
    assert str(rejected.value) == "proposal schema_version v9 is not supported; supported: v1"
    assert (rejected.value.kind, rejected.value.version, rejected.value.supported) == (
        "proposal",
        "v9",
        ("v1",),
    )
    assert isinstance(rejected.value, ValueError)


def test_the_envelope_names_its_version_in_specversion() -> None:
    record = {**valid_record("event_envelope"), "specversion": "2.0"}
    with pytest.raises(UnsupportedSchemaVersion, match="supported: 1.0"):
        validate_record("event_envelope", record)
    assert validate_record("event_envelope", valid_record("event_envelope")) is None


def test_missing_field_is_rejected() -> None:
    record = valid_record("proposal")
    del record["intent_hash"]
    with pytest.raises(ContractValidationError) as rejected:
        validate_record("proposal", record)
    assert rejected.value.kind == "proposal"
    assert any("intent_hash" in error for error in rejected.value.errors)
    assert str(rejected.value).startswith("proposal is invalid: ")
    assert isinstance(rejected.value, ValueError)


def test_every_violation_is_reported_not_only_the_first() -> None:
    record = valid_record("proposal")
    del record["intent_hash"]
    record["effect_class"] = "destroy"
    record["state"] = "lost"
    with pytest.raises(ContractValidationError) as rejected:
        validate_record("proposal", record)
    assert len(rejected.value.errors) == 3
    assert [e.split(":")[0] for e in rejected.value.errors] == ["<record>", "effect_class", "state"]


def test_a_record_without_a_version_is_a_validation_error_not_a_crash() -> None:
    record = valid_record("receipt")
    del record["schema_version"]
    with pytest.raises(ContractValidationError, match="'schema_version' is a required property"):
        validate_record("receipt", record)
    envelope = valid_record("event_envelope")
    del envelope["specversion"]
    with pytest.raises(ContractValidationError, match="'specversion' is a required property"):
        validate_record("event_envelope", envelope)


@pytest.mark.parametrize("version", [1, None, ["v1"], "V1", ""])
def test_a_version_that_is_not_a_supported_string_is_unsupported(version: object) -> None:
    with pytest.raises(UnsupportedSchemaVersion):
        validate_record("receipt", {**valid_record("receipt"), "schema_version": version})


@pytest.mark.parametrize("payload", [None, [], "proposal", 7])
def test_a_record_that_is_not_an_object_is_rejected(payload: object) -> None:
    with pytest.raises(ContractValidationError, match="expected an object"):
        validate_record("proposal", payload)  # type: ignore[arg-type]


def test_an_unknown_kind_is_a_typed_error() -> None:
    with pytest.raises(UnknownContractKind) as unknown:
        validate_record("invoice", {})
    assert unknown.value.kind == "invoice"
    assert isinstance(unknown.value, ValueError)


# ---------------------------------------------------------------- what each schema refuses


def with_change(kind: str, **changes: Any) -> dict[str, Any]:
    record = copy.deepcopy(valid_record(kind))
    record.update(changes)
    return record


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("proposal", {"surprise": 1}),  # additionalProperties: false
        ("proposal", {"effect_class": "write"}),
        ("proposal", {"state": "done"}),
        ("proposal", {"target": {"connector": "x"}}),
        ("proposal", {"target": {"connector": "x", "resource": "y", "extra": 1}}),
        ("proposal", {"args": "not an object"}),
        ("proposal", {"tenant_id": ""}),
        ("proposal", {"expected_resource_version": 3}),
        ("run_manifest", {"created_at": "yesterday"}),
        ("run_manifest", {"legacy": "no"}),
        ("run_manifest", {"pins": {"workflow_version": "x"}}),
        ("run_manifest", {"subject": {"user_id": "u"}}),
        ("receipt", {"verified_by": "trust"}),
        ("receipt", {"verified_at": "soon"}),
        ("policy_decision", {"result": "maybe"}),
        ("policy_decision", {"reason_codes": "because"}),
        ("approval_request", {"allowed_responses": []}),
        ("approval_request", {"allowed_responses": ["approve", "approve"]}),
        ("approval_request", {"allowed_responses": ["comment"]}),
        ("approval_request", {"expires_at": "2026-10-26"}),
        ("approval_decision", {"decision": "abstain"}),
        ("approval_decision", {"reason": None}),
        ("event_envelope", {"data": {"inline": "payload"}}),
        ("event_envelope", {"traceparent": "not-a-traceparent"}),
        ("event_envelope", {"time": "now"}),
    ],
)
def test_a_malformed_record_is_rejected(kind: str, changes: dict[str, Any]) -> None:
    with pytest.raises(ContractValidationError):
        validate_record(kind, with_change(kind, **changes))


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("proposal", {"expected_resource_version": None}),
        ("receipt", {"verified_by": "none", "verified_at": None}),
        (
            "run_manifest",
            {"legacy": True, "rule_bundle_path": "bundles/a", "rule_bundle_sha256": "ab"},
        ),
        ("approval_request", {"allowed_responses": ["approve"], "expected_resource_version": None}),
        ("approval_decision", {"reason": ""}),
        ("policy_decision", {"result": "deny", "reason_codes": ["no_grant"]}),
        ("event_envelope", {"causationid": None}),
    ],
)
def test_the_documented_optional_and_nullable_shapes_are_accepted(
    kind: str, changes: dict[str, Any]
) -> None:
    assert validate_record(kind, with_change(kind, **changes)) is None


def test_optional_fields_may_be_left_out() -> None:
    manifest = valid_record("run_manifest")
    for field in ("residency", "legacy", "rule_bundle_path", "rule_bundle_sha256"):
        del manifest[field]
    assert validate_record("run_manifest", manifest) is None
    envelope = valid_record("event_envelope")
    for field in ("causationid", "traceparent", "dataref", "datahash"):
        del envelope[field]
    assert validate_record("event_envelope", envelope) is None


def test_the_approval_binds_to_the_intent_of_its_proposal() -> None:
    """The fixtures describe one journey: the request's intent_hash is the proposal's."""
    assert (
        valid_record("approval_request")["intent_hash"] == valid_record("proposal")["intent_hash"]
    )
    assert (
        valid_record("approval_request")["proposal_id"] == valid_record("proposal")["proposal_id"]
    )
    assert valid_record("receipt")["proposal_id"] == valid_record("proposal")["proposal_id"]


# ---------------------------------------------------------------- ADR 0003 agrees with the schemas


def adr_tables() -> dict[str, dict[str, bool]]:
    """``{kind: {field: required}}`` from the ``### <kind>`` tables of ADR 0003's Decision section."""
    text = ADR.read_text(encoding="utf-8")
    decision = text.split("## Decision", 1)[1].split("\n## Consequences", 1)[0]
    tables: dict[str, dict[str, bool]] = {}
    for kind, body in re.findall(r"^### (\w+)\n(.*?)(?=^### |\Z)", decision, re.M | re.S):
        fields: dict[str, bool] = {}
        for line in body.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("| `") and len(cells) == 4:
                fields[cells[0].strip("`")] = cells[2] == "yes"
        tables[kind] = fields
    return tables


@pytest.mark.skipif(not ADR.exists(), reason="repository docs not present")
def test_adr_0003_matches_the_schemas() -> None:
    tables = adr_tables()
    assert list(tables) == list(KINDS)
    for kind in KINDS:
        schema = load_schema(kind, SUPPORTED_VERSIONS[kind][0])
        required = set(schema["required"])
        assert set(tables[kind]) == set(schema["properties"]), kind
        assert {f for f, req in tables[kind].items() if req} == required, kind


def enum_values(fragment: dict[str, Any]) -> set[str]:
    """Every enum value a property allows, including those of an array's items."""
    found = set(fragment.get("enum", []))
    if isinstance(fragment.get("items"), dict):
        found |= enum_values(fragment["items"])
    return found


@pytest.mark.skipif(not ADR.exists(), reason="repository docs not present")
def test_adr_0003_lists_every_enum_value_of_every_field() -> None:
    decision = (
        ADR.read_text(encoding="utf-8").split("## Decision", 1)[1].split("\n## Consequences", 1)[0]
    )
    for kind in KINDS:
        schema = load_schema(kind, SUPPORTED_VERSIONS[kind][0])
        section = re.split(r"^### ", decision.split(f"### {kind}\n", 1)[1], maxsplit=1, flags=re.M)[
            0
        ]
        for field, fragment in schema["properties"].items():
            row = next(line for line in section.splitlines() if line.startswith(f"| `{field}` |"))
            for value in enum_values(fragment):
                assert f"`{value}`" in row, f"{kind}.{field}: ADR 0003 does not list `{value}`"
