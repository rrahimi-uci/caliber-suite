"""What a run was pinned to, read back and checked before the worker executes it (B02.2b, #683).

Admission (:mod:`caliber.workflows.admission`) stores an immutable run manifest whose ``pins`` name every version the
run depends on, and executes the version's *sealed, resolved* manifest. This module is the worker's half:
:func:`load_run_manifest` reads the manifest back (digest and schema checked) and :func:`verify_run_pins` refuses to run
anything that no longer matches it, so a change after admission (a recompiled version, another provider, a replaced
snapshot) fails the run closed with a named reason instead of silently running something else:

* an admitted run (``manifest_legacy = false``) must have its manifest (``manifest_missing``: no partial admitted run is
  dispatchable) and it must read back intact and name this run (``manifest_corrupt``);
* the pinned workflow version must be the run's (``pins_mismatch``) and must still exist with the sealed bundle that was
  pinned (``pins_changed``: the bundle's digest is the pin; a version recompiled after admission fails);
* the snapshot the worker executes must be exactly that bundle's resolved manifest (``pins_mismatch``): prompts are
  inline, tools carry exact versions, knowledge bases and child workflows their version ids, skills their embedded
  snapshots, so no alias is looked up again at run time;
* the model route the deployment would use now must be one the run was pinned to (``model_route_changed``).

Legacy runs have no manifest and keep today's path untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberRunManifest, CaliberWorkflowRun, CaliberWorkflowVersion
from caliber.run_manifest import ManifestError, read_manifest
from caliber.workflows.deployment_bundle import verify_bundle

logger = logging.getLogger("caliber.workflows.run_pins")

MANIFEST_MISSING: Final = "manifest_missing"
MANIFEST_CORRUPT: Final = "manifest_corrupt"
PINS_MISMATCH: Final = "pins_mismatch"
PINS_CHANGED: Final = "pins_changed"
MODEL_ROUTE_CHANGED: Final = "model_route_changed"
CODES: Final = frozenset(
    {MANIFEST_MISSING, MANIFEST_CORRUPT, PINS_MISMATCH, PINS_CHANGED, MODEL_ROUTE_CHANGED}
)


class PinsError(Exception):
    """The run no longer matches what it was admitted with. ``code`` is one of :data:`CODES`."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RunPins:
    """A run's manifest as read back: its id and digest, the whole record and its ``pins``."""

    manifest_id: str
    digest: str
    record: dict[str, Any]
    pins: dict[str, Any]
    #: The sealed deployment bundle the pins describe, set by :func:`verify_run_pins` (``None`` from a bare load).
    bundle: dict[str, Any] | None = None


def load_run_manifest(session: Session, run_id: str) -> RunPins | None:
    """The run's manifest, or ``None`` when it has none (a legacy run). Raises :class:`PinsError` when the stored
    bytes do not read back (digest, JSON or schema) or name another run."""
    row = session.execute(
        select(CaliberRunManifest).where(CaliberRunManifest.workflow_run_id == run_id)
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        record = read_manifest(bytes(row.manifest_bytes), row.manifest_digest)
    except ManifestError as exc:
        raise PinsError(MANIFEST_CORRUPT, f"the run manifest does not read back: {exc}") from exc
    if record.get("workflow_run_id") != run_id or record.get("manifest_id") != row.manifest_id:
        raise PinsError(MANIFEST_CORRUPT, "the run manifest names another run")
    return RunPins(
        manifest_id=row.manifest_id,
        digest=row.manifest_digest,
        record=record,
        pins=dict(record["pins"]),
    )


def verify_run_pins(
    session: Session,
    run: CaliberWorkflowRun,
    version: CaliberWorkflowVersion | None,
    snapshot: dict[str, Any] | None,
    *,
    llm_provider: str,
) -> RunPins | None:
    """Check that ``run`` still matches its manifest; returns the pins (``None`` for a legacy run).

    ``snapshot`` is the manifest the worker is about to execute and ``llm_provider`` the provider this deployment
    would use now. Raises :class:`PinsError`; the worker marks the run failed with its ``code``.
    """
    if run.manifest_legacy:
        return None
    loaded = load_run_manifest(session, run.workflow_run_id)
    if loaded is None:
        raise PinsError(
            MANIFEST_MISSING,
            "an admitted run has no manifest, so it is not dispatched (admission stores both or neither)",
        )
    pins = loaded.pins
    if pins.get("workflow_version") != run.workflow_version_id:
        raise PinsError(PINS_MISMATCH, "the run's workflow version is not the one it was pinned to")
    bundle = _sealed_bundle(version)
    if bundle is None:
        raise PinsError(
            PINS_CHANGED, "the pinned workflow version or its sealed bundle no longer exists"
        )
    verification = verify_bundle(bundle)
    if not verification.valid or verification.digest != pins.get("bundle_digest"):
        raise PinsError(
            PINS_CHANGED,
            "the workflow version's sealed bundle is not the one the run was pinned to "
            "(the version was recompiled after admission)",
        )
    if snapshot != bundle.get("resolved_manifest"):
        raise PinsError(
            PINS_MISMATCH, "the manifest to execute is not the pinned bundle's resolved manifest"
        )
    if llm_provider not in pins.get("model_routes", []):
        raise PinsError(
            MODEL_ROUTE_CHANGED,
            f"the model provider is now {llm_provider!r}, which the run was not pinned to "
            f"{sorted(pins.get('model_routes', []))}",
        )
    return RunPins(
        manifest_id=loaded.manifest_id,
        digest=loaded.digest,
        record=loaded.record,
        pins=loaded.pins,
        bundle=bundle,
    )


def _sealed_bundle(version: CaliberWorkflowVersion | None) -> dict[str, Any] | None:
    if version is None or not isinstance(version.compiled_bundle, dict):
        return None
    bundle = version.compiled_bundle.get("deployment_bundle")
    return bundle if isinstance(bundle, dict) else None


__all__ = [
    "CODES",
    "MANIFEST_CORRUPT",
    "MANIFEST_MISSING",
    "MODEL_ROUTE_CHANGED",
    "PINS_CHANGED",
    "PINS_MISMATCH",
    "PinsError",
    "RunPins",
    "load_run_manifest",
    "verify_run_pins",
]
