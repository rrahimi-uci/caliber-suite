"""Connector admission: which private transports may carry a protected effect (B12.3a, #738; ADR 0001 and 0003).

A connector is admitted only by what CALIBER itself can check about the code that is about to send:

* its **implementation identity**: the module and qualified name of the class (or function) registered as the transport,
  read from the registered object, never from a name a caller, a manifest or a model supplied;
* its **capability record** (:class:`~caliber.effects.connectors.ConnectorCapability`): the digest of the promise it makes
  about keys, preconditions and lookups, so a changed promise is a new digest that must be re-admitted;
* its **configuration digest**: the digest of the wire configuration the transport sends with (methods, paths, key header,
  content types), so a transport that starts sending somewhere else is not the one that was admitted.

Each is compared with the pinned record in :data:`caliber.profile.REFERENCE_CONNECTORS`. A name is never an identity: a tool
called ``document_request`` (or a proposal that claims the ``document-request`` connector) has no admission, so it is refused
at submit and at dispatch. Anything unmatched is refused with one short, stable, payload-free code, before a proposal is
recorded or an approval is consumed.

This is identity and declared shape, not integrity: in-process code that forges a class's ``__module__`` and ``__qualname__`` is
admitted, and the configuration digest covers declared constants, not behaviour. What it keeps out is data (a manifest, a tool
name, a client request, a model's output) posing as a connector.

This module is pure: it reads no database and sends nothing. The dispatcher supplies the registered transport.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from caliber.effects.connectors import ConnectorCapability
from caliber.effects.connectors.openapi_http import OpenApiOperation
from caliber.profile import TrustedConnector

#: No admission record for this transport, or the claimed connector is not the one the record admits.
NOT_ADMITTED: Final = "connector_not_admitted"
#: The registered transport is not the implementation the record pins.
IMPLEMENTATION_MISMATCH: Final = "connector_implementation_mismatch"
#: The connector has no capability record, or promises something the record does not allow.
CAPABILITY_UNPROVEN: Final = "connector_capability_unproven"
#: The connector's capability record is not the one the record pins.
CAPABILITY_MISMATCH: Final = "connector_capability_mismatch"
#: The transport's wire configuration is not the one the record pins.
CONFIGURATION_MISMATCH: Final = "connector_configuration_mismatch"
#: The settings handed to dispatch are not ones the admitted connector accepts.
CONFIG_NOT_ADMITTED: Final = "connector_config_not_admitted"

#: Every code this module raises, in the order a transport is checked.
REFUSAL_CODES: Final = (
    NOT_ADMITTED,
    IMPLEMENTATION_MISMATCH,
    CAPABILITY_UNPROVEN,
    CAPABILITY_MISMATCH,
    CONFIGURATION_MISMATCH,
    CONFIG_NOT_ADMITTED,
)

#: The risks a capability record can carry that admission must see accepted by name.
RISK_NOT_EXACTLY_ONCE: Final = "write_not_exactly_once"
RISK_RETENTION_UNCONFIRMED: Final = "key_retention_unconfirmed"

MAX_ATTEMPTS: Final = 5
MAX_TIMEOUT_SECONDS: Final = 300.0


class AdmissionRefused(Exception):  # noqa: N818 - stable name; the dispatcher turns it into ``EffectRefused``
    """A connector is not admitted. ``code`` is one of :data:`REFUSAL_CODES`; no message carries a payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AdmittedConnector:
    """What admission verified for one backend kind: the evidence a caller needs, never a secret or a payload."""

    backend_kind: str
    connector: str
    connector_version: str
    implementation: str
    capability_digest: str
    configuration_digest: str
    accepted_risks: tuple[str, ...]
    config_keys: tuple[str, ...]
    required_config_keys: tuple[str, ...]

    def evidence(self) -> dict[str, Any]:
        """The identity digests recorded with a dispatch: who was admitted, not what was sent."""
        return {
            "backend_kind": self.backend_kind,
            "connector": self.connector,
            "connector_version": self.connector_version,
            "implementation": self.implementation,
            "capability_digest": self.capability_digest,
            "configuration_digest": self.configuration_digest,
        }


def implementation_identity(backend: object) -> str:
    """``module:qualname`` of the registered transport: its class, or the function itself when it is one."""
    target = backend if inspect.isroutine(backend) else type(backend)
    return f"{getattr(target, '__module__', '?')}:{getattr(target, '__qualname__', '?')}"


def configuration_digest(configuration: Mapping[str, Any]) -> str:
    """``sha256:`` of the canonical JSON of a transport's wire configuration: what a record pins."""
    data = json.dumps(configuration, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def capability_risks(capability: ConnectorCapability) -> tuple[str, ...]:
    """The risks a capability record carries, which a trusted record must accept by name to admit it."""
    risks: list[str] = []
    if not capability.carries_a_write_exactly_once:
        risks.append(RISK_NOT_EXACTLY_ONCE)
    if not capability.retention_confirmed:
        risks.append(RISK_RETENTION_UNCONFIRMED)
    return tuple(risks)


def admit(
    trusted: Sequence[TrustedConnector],
    backend_kind: str,
    *,
    backend: object,
    connector: str,
    connector_version: str,
    capability: ConnectorCapability | None,
    configuration: Mapping[str, Any] | None,
) -> AdmittedConnector:
    """Admit ``backend`` only when it is the implementation, capability and configuration ``trusted`` pins for its kind.

    Raises :class:`AdmissionRefused` with the first mismatch, checked in the order of :data:`REFUSAL_CODES`.
    """
    record = next((t for t in trusted if t.backend_kind == backend_kind), None)
    if record is None or (connector, connector_version) != (
        record.connector,
        record.connector_version,
    ):
        raise AdmissionRefused(NOT_ADMITTED)
    if implementation_identity(backend) != record.implementation:
        raise AdmissionRefused(IMPLEMENTATION_MISMATCH)
    if not isinstance(capability, ConnectorCapability):
        raise AdmissionRefused(CAPABILITY_UNPROVEN)
    if (
        capability.connector,
        capability.version,
    ) != (record.connector, record.connector_version) or (
        capability.digest() != record.capability_digest
    ):
        raise AdmissionRefused(CAPABILITY_MISMATCH)
    if not set(capability_risks(capability)) <= set(record.accepted_risks):
        raise AdmissionRefused(CAPABILITY_UNPROVEN)
    if (
        not isinstance(configuration, Mapping)
        or configuration_digest(configuration) != record.configuration_digest
    ):
        raise AdmissionRefused(CONFIGURATION_MISMATCH)
    return AdmittedConnector(
        backend_kind=record.backend_kind,
        connector=record.connector,
        connector_version=record.connector_version,
        implementation=record.implementation,
        capability_digest=record.capability_digest,
        configuration_digest=record.configuration_digest,
        accepted_risks=record.accepted_risks,
        config_keys=record.config_keys,
        required_config_keys=record.required_config_keys,
    )


def _valid_base_url(value: object) -> bool:
    """An ``http(s)`` URL with a host, a valid port if any, and no userinfo, whitespace, control characters, query or fragment.

    This is a shape check only: the egress policy (``caliber.egress``) still decides at send time whether the host may be
    reached; it needs a policy and name resolution, which this pure module does not have.
    """
    if not isinstance(value, str) or not value:
        return False
    if any(ch.isspace() or not ch.isprintable() for ch in value):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port  # raises ValueError for a non-numeric or out-of-range port
        return (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and "@" not in parts.netloc
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        return False


def _valid_max_attempts(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_ATTEMPTS


def _valid_timeout(value: object) -> bool:
    # Compared without converting an ``int`` to ``float``: ``float(10**400)`` raises ``OverflowError``.
    if type(value) is int:
        return 0 < value <= MAX_TIMEOUT_SECONDS
    return type(value) is float and math.isfinite(value) and 0 < value <= MAX_TIMEOUT_SECONDS


#: What each setting a trusted record may list must satisfy. A key with no checker here is never admitted, so a record
#: cannot list a key whose value nobody validates.
_CONFIG_CHECKS: Final[Mapping[str, Callable[[object], bool]]] = {
    "base_url": _valid_base_url,
    "max_attempts": _valid_max_attempts,
    "timeout_seconds": _valid_timeout,
    "sender": callable,
    "receipt_verifier": callable,
    "invoker": callable,
    "operation": lambda value: isinstance(value, OpenApiOperation),
}


def check_config(admitted: AdmittedConnector, config: Mapping[str, Any] | None) -> None:
    """Refuse (:data:`CONFIG_NOT_ADMITTED`) settings the admitted connector does not accept.

    Every required key must be present, no key outside the record's list may be, and each value must pass its check
    (a http(s) base URL with a host and no credentials; attempts 1 to :data:`MAX_ATTEMPTS`; a finite timeout of at most
    :data:`MAX_TIMEOUT_SECONDS`; a callable). The refusal never says which key failed or what it held.
    """
    settings: Mapping[str, Any] = {} if config is None else config
    if not isinstance(settings, Mapping):
        raise AdmissionRefused(CONFIG_NOT_ADMITTED)
    keys = set(settings)
    if not keys <= set(admitted.config_keys) or not set(admitted.required_config_keys) <= keys:
        raise AdmissionRefused(CONFIG_NOT_ADMITTED)
    for key in keys:
        check = _CONFIG_CHECKS.get(key)
        try:
            admitted_value = check is not None and check(settings[key])
        except Exception:  # a check that cannot decide refuses: the contract is a typed code, never an arbitrary error
            admitted_value = False
        if not admitted_value:
            raise AdmissionRefused(CONFIG_NOT_ADMITTED)


__all__ = [
    "CAPABILITY_MISMATCH",
    "CAPABILITY_UNPROVEN",
    "CONFIGURATION_MISMATCH",
    "CONFIG_NOT_ADMITTED",
    "IMPLEMENTATION_MISMATCH",
    "MAX_ATTEMPTS",
    "MAX_TIMEOUT_SECONDS",
    "NOT_ADMITTED",
    "REFUSAL_CODES",
    "RISK_NOT_EXACTLY_ONCE",
    "RISK_RETENTION_UNCONFIRMED",
    "AdmissionRefused",
    "AdmittedConnector",
    "admit",
    "capability_risks",
    "check_config",
    "configuration_digest",
    "implementation_identity",
]
