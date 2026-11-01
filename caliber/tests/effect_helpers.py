"""Test helpers for the effect ledger: the execution-owner lease an owner captures before it dispatches or settles (B05.5a)."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from caliber.ownership import Lease, acquire, current_lease, database_now, release

if TYPE_CHECKING:
    import pytest

    from caliber.effects.admission import AdmittedConnector

TTL_SECONDS = 600.0

#: The module and callable of one of CALIBER's own shipped tools (a seeded write-class built-in).
#:
#: The reference profile runs no administrator-registered Python module (B04.7, #774; EP-015, EP-016, EP-019), so a
#: scenario that drives the governed dispatch of a registered tool in that profile registers the tool under *this* pair, the
#: only Python a registry row may point at there. What a test's fake does when it is called is injected as before; the row's
#: name stays the test's own (a name is not an identity, so the row is still an administrator-registered tool to the
#: classifier).
SHIPPED_TOOL_MODULE = "caliber.workflows.demo_tools"
SHIPPED_TOOL_CALLABLE = "write_workdir_file"


def leased(session: Session, owner_id: str) -> Lease:
    """The lease ``owner_id`` holds now: the one already held, or a fresh acquisition (a new token) after taking it over.

    Another owner's unexpired lease is released first, as if it had lapsed, so asking for a different owner models a
    takeover and leaves every lease captured earlier stale.
    """
    stored = current_lease(session)
    if (
        stored is not None
        and stored.owner_id == owner_id
        and stored.expires_at > database_now(session)
    ):
        return stored
    if stored is not None and stored.expires_at > database_now(session):
        release(session, stored)
    lease = acquire(session, owner_id, ttl_seconds=TTL_SECONDS)
    assert lease is not None
    return lease


def admit_fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    backend: object,
    *,
    connector: str = "document-request",
    version: str = "1",
    config_keys: tuple[str, ...] = (),
    required_config_keys: tuple[str, ...] = (),
) -> AdmittedConnector:
    """Register ``backend`` as ``kind`` and pin it in the trusted table, as a reviewer would for a real connector.

    The fake gets the document-request capability record (under its own identity) and a one-key configuration, and
    the trusted table is extended for this test only, so the dispatcher admits it through the same code a real
    transport uses. Returns the admission.
    """
    import caliber.effects.dispatcher as dispatcher_module
    from caliber import profile
    from caliber.effects.admission import (
        capability_risks,
        configuration_digest,
        implementation_identity,
    )
    from caliber.effects.connectors.document_request import CAPABILITY

    capability = replace(CAPABILITY, connector=connector, version=version)
    configuration = {"fake": kind}
    record = profile.TrustedConnector(
        backend_kind=kind,
        connector=connector,
        connector_version=version,
        implementation=implementation_identity(backend),
        capability_digest=capability.digest(),
        configuration_digest=configuration_digest(configuration),
        accepted_risks=capability_risks(capability),
        config_keys=config_keys,
        required_config_keys=required_config_keys,
    )
    monkeypatch.setattr(profile, "REFERENCE_CONNECTORS", (*profile.REFERENCE_CONNECTORS, record))
    monkeypatch.setitem(
        dispatcher_module.BACKENDS,
        kind,
        dispatcher_module.BackendRegistration(
            backend, connector, version, capability=capability, configuration=configuration
        ),
    )
    return dispatcher_module.admit_backend(kind)
