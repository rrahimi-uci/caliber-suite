"""Load and apply the JSON Schemas of the control records.

Seven kinds, each with a schema per supported version in ``schemas/<kind>.<version>.json``. A record names its
version in ``schema_version``; the CloudEvents envelope (``event_envelope``) names it in ``specversion`` and its
``1.0`` is schema ``v1``. Adding an optional field to a schema keeps its version; removing or retyping a field
is a new version in a new file, and the old version stays supported until a record says otherwise (ADR 0003).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from importlib import resources
from typing import Any

import jsonschema

#: The control-record kinds, in the order ADR 0003 lists them.
KINDS = (
    "run_manifest",
    "proposal",
    "receipt",
    "policy_decision",
    "approval_request",
    "approval_decision",
    "event_envelope",
)
#: Versions a record of each kind may carry. ``event_envelope`` carries CloudEvents' ``specversion``.
SUPPORTED_VERSIONS: dict[str, tuple[str, ...]] = {
    "run_manifest": ("v1",),
    "proposal": ("v1",),
    "receipt": ("v1",),
    "policy_decision": ("v1",),
    "approval_request": ("v1",),
    "approval_decision": ("v1",),
    "event_envelope": ("1.0",),
}
#: Which field of a record names its version.
_VERSION_FIELD = {"event_envelope": "specversion"}
#: Schema file version for a record version whose spelling differs (``1.0`` is the first envelope schema).
_FILE_VERSION = {("event_envelope", "1.0"): "v1"}
_SCHEMA_PACKAGE = "caliber.contracts.schemas"


class UnknownContractKind(ValueError):  # noqa: N818 (names fixed by ADR 0003)
    """The kind is not one of :data:`KINDS`."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"unknown contract kind {kind!r}; known: {', '.join(KINDS)}")


class UnsupportedSchemaVersion(ValueError):  # noqa: N818 (names fixed by ADR 0003)
    """The record names a schema version this build does not support."""

    def __init__(self, kind: str, version: object, supported: tuple[str, ...]) -> None:
        self.kind = kind
        self.version = version
        self.supported = supported
        super().__init__(
            f"{kind} schema_version {version} is not supported; supported: {', '.join(supported)}"
        )


class ContractValidationError(ValueError):
    """The record does not satisfy its schema; ``errors`` lists every violation."""

    def __init__(self, kind: str, errors: list[str]) -> None:
        self.kind = kind
        self.errors = errors
        super().__init__(f"{kind} is invalid: " + "; ".join(errors))


def _check_kind(kind: str) -> None:
    if kind not in SUPPORTED_VERSIONS:
        raise UnknownContractKind(kind)


@functools.cache
def _load(kind: str, file_version: str) -> dict[str, Any]:
    text = (
        resources.files(_SCHEMA_PACKAGE).joinpath(f"{kind}.{file_version}.json").read_text("utf-8")
    )
    schema: dict[str, Any] = json.loads(text)
    return schema


def load_schema(kind: str, version: str) -> dict[str, Any]:
    """The JSON Schema of ``kind`` at ``version`` (a copy, so a caller cannot change the shared one)."""
    _check_kind(kind)
    supported = SUPPORTED_VERSIONS[kind]
    if version not in supported:
        raise UnsupportedSchemaVersion(kind, version, supported)
    file_version = _FILE_VERSION.get((kind, version), version)
    copy: dict[str, Any] = json.loads(json.dumps(_load(kind, file_version)))
    return copy


@functools.cache
def _validator(kind: str, version: str) -> jsonschema.protocols.Validator:
    return jsonschema.Draft202012Validator(load_schema(kind, version))


def validate_record(kind: str, payload: Mapping[str, Any]) -> None:
    """Return ``None`` when ``payload`` is a valid ``kind`` record; raise otherwise.

    Raises :class:`UnknownContractKind` for a kind that does not exist, :class:`UnsupportedSchemaVersion`
    when the record's version is not in :data:`SUPPORTED_VERSIONS`, and :class:`ContractValidationError`
    (with every violation, sorted by path) when the record is not an object, names no version, or breaks
    its schema.
    """
    _check_kind(kind)
    field = _VERSION_FIELD.get(kind, "schema_version")
    if not isinstance(payload, Mapping):
        raise ContractValidationError(kind, [f"expected an object, got {type(payload).__name__}"])
    if field not in payload:
        raise ContractValidationError(kind, [f"{field!r} is a required property"])
    version = payload[field]
    supported = SUPPORTED_VERSIONS[kind]
    if not isinstance(version, str) or version not in supported:
        raise UnsupportedSchemaVersion(kind, version, supported)
    errors = sorted(
        _validator(kind, version).iter_errors(dict(payload)), key=lambda e: list(e.path)
    )
    if errors:
        raise ContractValidationError(
            kind,
            [f"{'/'.join(str(p) for p in e.path) or '<record>'}: {e.message}" for e in errors],
        )
