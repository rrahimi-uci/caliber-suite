"""Load and apply the JSON Schemas of the control records.

Seven kinds, each with a schema per supported version in ``schemas/<kind>.<version>.json``. A record names its
version in ``schema_version``. The CloudEvents envelope (``event_envelope``) cannot: its ``specversion`` is
CloudEvents' own and stays ``1.0``, so it names its schema version in the extension attribute ``schemaversion``,
which defaults to ``v1`` when absent. Adding an optional field to a schema keeps its version; removing or
retyping a field is a new version in a new file, and the old version stays supported until a decision record
retires it (ADR 0003).
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping
from datetime import datetime
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
    "event_envelope": ("v1",),
}
#: Which field of a record names its version, and the version assumed when a kind allows the field to be absent.
_VERSION_FIELD: dict[str, tuple[str, str | None]] = {"event_envelope": ("schemaversion", "v1")}
_DEFAULT_VERSION_FIELD: tuple[str, str | None] = ("schema_version", None)
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


def _as_plain(value: object) -> object:
    """``json.dumps`` hook: a mapping (``UserDict``, ``MappingProxyType``) becomes a dict; nothing else is JSON."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _check_kind(kind: object) -> None:
    if not isinstance(kind, str) or kind not in SUPPORTED_VERSIONS:
        raise UnknownContractKind(str(kind))


_RFC3339 = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-](\d{2}):(\d{2}))\Z"
)
_FORMATS = jsonschema.FormatChecker(formats=())
_MAX_OFFSET_HOUR, _MAX_MINUTE = 23, 59


@_FORMATS.checks("date-time")  # type: ignore[untyped-decorator]
def _is_rfc3339(value: object) -> bool:
    """An RFC 3339 ``date-time``: a real calendar date and time with an offset (``Z`` or ``+hh:mm``)."""
    if not isinstance(value, str):
        return (
            True  # a format constrains strings only; the schema's ``type`` decides about the rest
        )
    if not (match := _RFC3339.match(value)):
        return False
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if match.group(9) is not None and (
        int(match.group(9)) > _MAX_OFFSET_HOUR or int(match.group(10)) > _MAX_MINUTE
    ):
        return False
    try:
        datetime(year, month, day, hour, minute, min(second, _MAX_MINUTE))  # a leap second is 60
    except ValueError:
        return False
    return True


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
    copy: dict[str, Any] = json.loads(json.dumps(_load(kind, version)))
    return copy


@functools.cache
def _validator(kind: str, version: str) -> jsonschema.protocols.Validator:
    return jsonschema.Draft202012Validator(load_schema(kind, version), format_checker=_FORMATS)


def validate_record(kind: str, payload: Mapping[str, Any]) -> None:
    """Return ``None`` when ``payload`` is a valid ``kind`` record; raise otherwise.

    Raises :class:`UnknownContractKind` for a kind that does not exist, :class:`UnsupportedSchemaVersion`
    when the record's version is not in :data:`SUPPORTED_VERSIONS`, and :class:`ContractValidationError`
    (with every violation, sorted by path) when the record is not an object, is not plain JSON (a set, bytes,
    ``NaN``), names no version, or breaks its schema. The payload is not changed.
    """
    _check_kind(kind)
    if not isinstance(payload, Mapping):
        raise ContractValidationError(kind, [f"expected an object, got {type(payload).__name__}"])
    try:
        # A JSON round trip makes nested mappings and tuples plain, and refuses what no JSON store can hold.
        record: dict[str, Any] = json.loads(json.dumps(payload, allow_nan=False, default=_as_plain))
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(kind, [f"not plain JSON: {exc}"]) from exc
    field, default = _VERSION_FIELD.get(kind, _DEFAULT_VERSION_FIELD)
    if field not in record and default is None:
        raise ContractValidationError(kind, [f"{field!r} is a required property"])
    version = record.get(field, default)
    supported = SUPPORTED_VERSIONS[kind]
    if not isinstance(version, str) or version not in supported:
        raise UnsupportedSchemaVersion(kind, version, supported)
    errors = sorted(_validator(kind, version).iter_errors(record), key=lambda e: list(e.path))
    if errors:
        raise ContractValidationError(
            kind,
            [f"{'/'.join(str(p) for p in e.path) or '<record>'}: {e.message}" for e in errors],
        )
