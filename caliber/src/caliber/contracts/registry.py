"""Load and apply the JSON Schemas of the control records.

Ten kinds, each with a schema per supported version in ``schemas/<kind>.<version>.json``. A record names its
version in ``schema_version``. The CloudEvents envelope (``event_envelope``) cannot: its ``specversion`` is
CloudEvents' own and stays ``1.0``, so it names its schema version in the extension attribute ``schemaversion``,
which defaults to ``v1`` when absent. Adding an optional field to a schema keeps its version; removing or
retyping a field is a new version in a new file, and the old version stays supported until a decision record
retires it (ADR 0003).

Two kinds, the agent-step records (:data:`QUIET_KINDS`), take model-influenced input and are refused *quietly*: a refusal
names the field and the rule it broke, never the offending value, an unexpected property name or an unbounded amount of
text, a record over :data:`MAX_QUIET_RECORD_CHARS` is refused before it is validated, and an integer field accepts only an
integer (``1.0`` is not one). Every other kind keeps the messages and the lenient integers it always had.
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
    "context_manifest",
    "proposal",
    "receipt",
    "policy_decision",
    "approval_request",
    "approval_decision",
    "event_envelope",
    "agent_step_request",
    "agent_step_result",
)
#: Versions a record of each kind may carry. ``event_envelope`` carries CloudEvents' ``specversion``.
SUPPORTED_VERSIONS: dict[str, tuple[str, ...]] = {
    "run_manifest": ("v1",),
    "context_manifest": ("v1",),
    "proposal": ("v1",),
    "receipt": ("v1",),
    "policy_decision": ("v1",),
    "approval_request": ("v1",),
    "approval_decision": ("v1",),
    "event_envelope": ("v1",),
    "agent_step_request": ("v1",),
    "agent_step_result": ("v1",),
}
#: Which field of a record names its version, and the version assumed when a kind allows the field to be absent.
_VERSION_FIELD: dict[str, tuple[str, str | None]] = {"event_envelope": ("schemaversion", "v1")}
_DEFAULT_VERSION_FIELD: tuple[str, str | None] = ("schema_version", None)
_SCHEMA_PACKAGE = "caliber.contracts.schemas"
#: Kinds whose refusals carry no value from the record (see the module docstring).
QUIET_KINDS: frozenset[str] = frozenset({"agent_step_request", "agent_step_result"})
#: The most JSON text a quiet-kind record may have; checked before validation so a huge value is never walked.
MAX_QUIET_RECORD_CHARS = 1 << 20
_MAX_MESSAGE_CHARS = 160
_MAX_QUIET_ERRORS = 20


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
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?"
    r"(Z|[+-]([0-9]{2}):([0-9]{2}))\Z"
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


_STRICT_INTEGERS = jsonschema.Draft202012Validator.TYPE_CHECKER.redefine(
    "integer", lambda _checker, value: type(value) is int
)
#: ``Draft202012Validator`` whose ``integer`` is a Python ``int`` and nothing else (not ``True``, not ``1.0``).
_StrictValidator = jsonschema.validators.extend(
    jsonschema.Draft202012Validator, type_checker=_STRICT_INTEGERS
)


@functools.cache
def _validator(kind: str, version: str) -> jsonschema.protocols.Validator:
    cls = _StrictValidator if kind in QUIET_KINDS else jsonschema.Draft202012Validator
    return cls(load_schema(kind, version), format_checker=_FORMATS)


def _bounded(text: str, limit: int = _MAX_MESSAGE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _version_label(version: object) -> str:
    """What a quiet kind says about a version it does not support: a short plain token, else only its type."""
    if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,16}", version):
        return version
    return f"<{type(version).__name__}>"


_QUIET_BOUNDS = frozenset({"minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum"})


def _quiet_message(error: jsonschema.ValidationError) -> str:  # noqa: PLR0911 - one line per keyword
    """A refusal reason built from the rule that failed (which the schema defines), never from the instance."""
    keyword, rule = str(error.validator), error.validator_value
    if keyword == "required":
        return str(error.message)  # names a property the schema requires, not one the record chose
    if keyword == "additionalProperties":
        return "additional properties are not allowed"
    if keyword == "pattern":
        return "does not match the required pattern"
    if keyword == "enum":
        return _bounded(f"is not one of {list(rule)}")
    if keyword == "const":
        return _bounded(f"must equal {rule!r}")
    if keyword == "type":
        return f"must be of type {rule if isinstance(rule, str) else '/'.join(rule)}"
    if keyword == "format":
        return f"is not a valid {rule}"
    return f"violates {keyword} {rule}" if keyword in _QUIET_BOUNDS else f"violates {keyword}"


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
    quiet = kind in QUIET_KINDS
    try:
        # A JSON round trip makes nested mappings and tuples plain, and refuses what no JSON store can hold
        # (a set, bytes, NaN, or a value nested so deep that Python cannot serialise it).
        text = json.dumps(payload, allow_nan=False, default=_as_plain, ensure_ascii=not quiet)
        if not quiet or len(text) <= MAX_QUIET_RECORD_CHARS:
            record: dict[str, Any] = json.loads(text)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractValidationError(
            kind, [f"not plain JSON: {_bounded(str(exc)) if quiet else exc}"]
        ) from exc
    if quiet and len(text) > MAX_QUIET_RECORD_CHARS:
        raise ContractValidationError(
            kind, [f"the record is larger than {MAX_QUIET_RECORD_CHARS} characters of JSON"]
        )
    field, default = _VERSION_FIELD.get(kind, _DEFAULT_VERSION_FIELD)
    if field not in record and default is None:
        raise ContractValidationError(kind, [f"{field!r} is a required property"])
    version = record.get(field, default)
    supported = SUPPORTED_VERSIONS[kind]
    if not isinstance(version, str) or version not in supported:
        raise UnsupportedSchemaVersion(
            kind, _version_label(version) if quiet else version, supported
        )
    try:
        errors = sorted(_validator(kind, version).iter_errors(record), key=lambda e: list(e.path))
    except RecursionError as exc:  # a schema that recurses into a record nested too deep
        raise ContractValidationError(
            kind, ["the record is nested too deeply to validate"]
        ) from exc
    if errors:
        if not quiet:
            raise ContractValidationError(
                kind,
                [f"{'/'.join(str(p) for p in e.path) or '<record>'}: {e.message}" for e in errors],
            )
        messages = [
            f"{'/'.join(str(p) for p in e.path) or '<record>'}: {_quiet_message(e)}"
            for e in errors[:_MAX_QUIET_ERRORS]
        ]
        if len(errors) > _MAX_QUIET_ERRORS:
            messages.append(f"and {len(errors) - _MAX_QUIET_ERRORS} more violations")
        raise ContractValidationError(kind, messages)
