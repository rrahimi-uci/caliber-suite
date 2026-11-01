"""The control-record contracts: ten kinds, versioned schemas, typed rejection.

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
    registry,
    validate_record,
)

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures" / "contracts"
SCHEMAS = Path(contracts.__file__).parent / "schemas"
ADR = TESTS.parent.parent / "docs" / "architecture" / "adr" / "0003-control-record-schemas.md"
VERSION_FIELD = {"event_envelope": "schemaversion"}


def fixture_files() -> list[Path]:
    return sorted(FIXTURES.glob("*/*.json"))


def load_fixture(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def valid_record(kind: str) -> dict[str, Any]:
    return load_fixture(FIXTURES / kind / "valid_1.json")


# ---------------------------------------------------------------- the schemas


def test_every_kind_has_a_schema_file_and_every_schema_file_a_kind() -> None:
    assert len(set(KINDS)) == len(KINDS)
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
    # The envelope's version attribute is optional (absent means v1); every other kind requires it.
    assert (field in schema["required"]) == (kind != "event_envelope")


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_loads_as_v1(kind: str) -> None:
    assert SUPPORTED_VERSIONS[kind] == ("v1",)
    assert load_schema(kind, "v1")["$id"] == f"https://caliber.dev/schemas/{kind}.v1.json"


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
    assert len(fixture_files()) >= 30


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


def test_the_envelope_names_its_schema_version_in_schemaversion_and_defaults_to_v1() -> None:
    envelope = valid_record("event_envelope")
    assert "schemaversion" not in envelope and validate_record("event_envelope", envelope) is None
    assert validate_record("event_envelope", {**envelope, "schemaversion": "v1"}) is None
    with pytest.raises(UnsupportedSchemaVersion, match="supported: v1"):
        validate_record("event_envelope", {**envelope, "schemaversion": "v2"})
    with pytest.raises(ContractValidationError, match="specversion"):
        validate_record("event_envelope", {**envelope, "specversion": "2.0"})


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


IMPOSSIBLE_TIMESTAMPS = [
    "2026-13-45T00:00:00Z",
    "2026-02-30T00:00:00Z",
    "2026-02-29T00:00:00Z",  # 2026 is not a leap year
    "0000-00-00T00:00:00Z",
    "2026-10-26T25:61:61Z",
    "2026-10-26T24:00:00Z",
    "2026-10-26T09:05:00+25:99",
    "2026-10-26T09:05:00Z\n",
    "2026-10-26 09:05:00Z",
    "2026-10-26T09:05:00",
]
REAL_TIMESTAMPS = [
    "2026-10-26T09:05:00Z",
    "2028-02-29T23:59:59.999+05:30",
    "2026-12-31T23:59:60Z",  # a leap second
    "2026-10-26T09:05:00-08:00",
]


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
        ("event_envelope", {"specversion": "0.3"}),
        (
            "event_envelope",
            {"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01\n"},
        ),
        ("policy_decision", {"reason_codes": [1]}),
        ("policy_decision", {"result": "deny", "reason_codes": []}),
        ("receipt", {"verified_at": None}),  # verified_by is "query"
        ("receipt", {"verified_by": "none"}),  # but verified_at is set
        ("proposal", {"args": {"nan": float("nan")}}),
        ("proposal", {"args": {"bytes": b"x"}}),
        ("proposal", {"args": {"set": {1, 2}}}),
        *[("approval_request", {"expires_at": bad}) for bad in IMPOSSIBLE_TIMESTAMPS],
        *[("run_manifest", {"created_at": bad}) for bad in IMPOSSIBLE_TIMESTAMPS],
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
        (
            "event_envelope",
            {"causationid": None, "traceparent": None, "dataref": None, "datahash": None},
        ),
        ("policy_decision", {"result": "allow", "reason_codes": []}),
        *[("approval_request", {"expires_at": ok}) for ok in REAL_TIMESTAMPS],
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


# ---------------------------------------------------------------- nested structure and plain JSON


def object_paths(schema: dict[str, Any], path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Paths (through properties and array items) to every object schema, starting at the root."""
    found: list[tuple[str, ...]] = []
    if schema.get("type") == "object" and "properties" in schema:
        found.append(path)
        for name, child in schema["properties"].items():
            found += object_paths(child, (*path, name))
    if isinstance(schema.get("items"), dict):
        found += object_paths(schema["items"], (*path, "[]"))
    return found


def at(record: Any, path: tuple[str, ...]) -> list[dict[str, Any]]:
    """The objects of ``record`` found at ``path`` (``[]`` fans out over an array)."""
    nodes = [record]
    for step in path:
        nodes = [
            x
            for n in nodes
            for x in (n if step == "[]" else [n[step]] if isinstance(n, dict) and step in n else [])
        ]
    return [n for n in nodes if isinstance(n, dict)]


def schema_at(schema: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    for step in path:
        schema = schema["items"] if step == "[]" else schema["properties"][step]
    return schema


def nested_cases() -> list[tuple[str, tuple[str, ...], str, str]]:
    cases: list[tuple[str, tuple[str, ...], str, str]] = []
    for kind in KINDS:
        schema = load_schema(kind, "v1")
        for path in object_paths(schema):
            node = schema_at(schema, path)
            cases += [(kind, path, name, "remove") for name in node["required"]]
            cases.append((kind, path, "unexpected_field", "add"))
    return cases


@pytest.mark.parametrize(
    ("kind", "path", "name", "action"),
    nested_cases(),
    ids=lambda v: "/".join(v) if isinstance(v, tuple) else str(v),
)
def test_every_required_field_and_every_object_boundary_is_enforced_at_every_level(
    kind: str, path: tuple[str, ...], name: str, action: str
) -> None:
    record = copy.deepcopy(valid_record(kind))
    targets = at(record, path)
    if not targets:
        pytest.skip(f"the valid {kind} fixture has nothing at {'/'.join(path)}")
    for target in targets:
        if action == "remove":
            target.pop(name, None)
        else:
            target[name] = 1
    with pytest.raises(ContractValidationError):
        validate_record(kind, record)


def test_a_mapping_subclass_is_accepted_at_every_level() -> None:
    import collections
    import types

    record = valid_record("proposal")
    record["target"] = collections.OrderedDict(record["target"])
    assert validate_record("proposal", collections.UserDict(record)) is None
    assert validate_record("proposal", types.MappingProxyType(record)) is None


def test_a_payload_is_not_changed_by_validation() -> None:
    record = valid_record("run_manifest")
    before = copy.deepcopy(record)
    validate_record("run_manifest", record)
    assert record == before


def test_a_record_that_is_not_plain_json_names_why() -> None:
    record = valid_record("proposal")
    record["args"] = {"nan": float("inf")}
    with pytest.raises(ContractValidationError, match="not plain JSON"):
        validate_record("proposal", record)


def test_a_record_nested_too_deep_is_a_validation_error_for_an_existing_kind() -> None:
    record = valid_record("proposal")
    nested: dict[str, Any] = {}
    for _ in range(100_000):
        nested = {"k": nested}
    record["args"] = nested
    with pytest.raises(ContractValidationError, match="not plain JSON") as rejected:
        validate_record("proposal", record)
    assert rejected.value.kind == "proposal"


def test_recursion_inside_the_validator_is_a_validation_error_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Overflowing:
        def iter_errors(self, _record: object) -> Any:
            raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(registry, "_validator", lambda _kind, _version: Overflowing())
    for kind in ("proposal", "agent_step_result"):
        with pytest.raises(ContractValidationError, match="nested too deeply") as rejected:
            validate_record(kind, valid_record(kind))
        assert rejected.value.kind == kind


def test_only_the_agent_step_kinds_are_refused_quietly() -> None:
    assert {"agent_step_request", "agent_step_result"} == registry.QUIET_KINDS
    assert set(KINDS) >= registry.QUIET_KINDS


def test_an_unhashable_kind_is_an_unknown_kind_not_a_type_error() -> None:
    with pytest.raises(UnknownContractKind):
        validate_record([], {})  # type: ignore[arg-type]
    with pytest.raises(UnknownContractKind):
        load_schema({}, "v1")  # type: ignore[arg-type]


def test_the_missing_field_fixtures_name_the_field_they_lack() -> None:
    for kind in KINDS:
        valid = valid_record(kind)
        missing = load_fixture(FIXTURES / kind / "invalid_missing_field.json")
        gone = set(valid) - set(missing)
        assert len(gone) == 1, (
            f"{kind}: invalid_missing_field must lack exactly one field, lacks {gone}"
        )
        with pytest.raises(ContractValidationError) as rejected:
            validate_record(kind, missing)
        assert [f"'{next(iter(gone))}' is a required property"] == [
            e.split(": ", 1)[1] for e in rejected.value.errors
        ]


#: The nested objects and the fields each requires, stated independently of the schema files (the nested cases
#: above are derived from the schemas, so a field dropped from a schema's ``required`` would otherwise vanish
#: from the tests with it). ADR 0003 describes these in prose, not in its tables.
NESTED_REQUIRED: dict[tuple[str, tuple[str, ...]], set[str]] = {
    ("run_manifest", ("pins",)): {
        "workflow_version",
        "bundle_digest",
        "prompt_versions",
        "tool_versions",
        "skill_versions",
        "knowledge_base_versions",
        "model_routes",
        "policy_version",
    },
    ("run_manifest", ("pins", "knowledge_base_versions", "[]")): {
        "knowledge_base_id",
        "version_id",
    },
    ("run_manifest", ("subject",)): {"user_id", "auth_method"},
    ("run_manifest", ("workload",)): {"workload_id", "name", "workflow_version_digest"},
    ("proposal", ("target",)): {"connector", "resource"},
    ("agent_step_request", ("limits",)): {"max_turns", "max_tool_calls", "max_tokens", "deadline"},
    ("agent_step_result", ("proposed_actions", "[]")): {
        "effect_class",
        "target",
        "connector_version",
        "args",
        "expected_resource_version",
    },
    ("agent_step_result", ("proposed_actions", "[]", "target")): {"connector", "resource"},
}


#: The optional fields a nested object has besides the required ones, stated here for the same reason. A proposed action may name the
#: function the model called (``tool``, B04.6 #768): a selector that is not part of the intent, so it is never required.
NESTED_OPTIONAL: dict[tuple[str, tuple[str, ...]], set[str]] = {
    ("agent_step_result", ("proposed_actions", "[]")): {"tool"},
}


@pytest.mark.parametrize(("kind", "path"), list(NESTED_REQUIRED))
def test_nested_objects_require_exactly_the_documented_fields(
    kind: str, path: tuple[str, ...]
) -> None:
    node = schema_at(load_schema(kind, "v1"), path)
    optional = NESTED_OPTIONAL.get((kind, path), set())
    assert set(node["required"]) == NESTED_REQUIRED[(kind, path)]
    assert set(node["properties"]) == NESTED_REQUIRED[(kind, path)] | optional
    assert node["additionalProperties"] is False
