"""Versioned contracts for CALIBER's control records (ADR 0003).

``validate_record(kind, payload)`` checks a record against the JSON Schema of its kind and schema version, and
rejects an unknown version or a malformed record with a typed error. The schemas ship in the wheel under
``contracts/schemas/``.
"""

from caliber.contracts.registry import (
    KINDS,
    SUPPORTED_VERSIONS,
    ContractValidationError,
    UnknownContractKind,
    UnsupportedSchemaVersion,
    load_schema,
    validate_record,
)

__all__ = [
    "KINDS",
    "SUPPORTED_VERSIONS",
    "ContractValidationError",
    "UnknownContractKind",
    "UnsupportedSchemaVersion",
    "load_schema",
    "validate_record",
]
