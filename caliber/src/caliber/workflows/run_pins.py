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
* the model route the deployment would use now must be one the run was pinned to (``model_route_changed``): the
  provider, and the default model when an agent says ``inherit``;
* what the bundle names *outside* itself must still be what it pinned (``pins_changed``): each registered tool's
  definition in the registry (module, callable, backend, side-effect level, approval flag, named secrets), each MCP
  tool's discovered schema and policy, and each child workflow's sealed bundle (checked down the whole tree, and the
  child must be a published version).

Still **live at run time**, said plainly: the deployment's other model settings (reasoning effort, base URL, API
choice, parallel tool calls, prompt cache), MCP server addresses and credentials, the values behind named secrets,
and the contents of files and datasets that the bundle identifies by digest or version rather than embeds.

Legacy runs have no manifest and keep today's path untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberMcpServer,
    CaliberRunManifest,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import get_visible, synthetic_identity
from caliber.run_manifest import ManifestError, read_manifest
from caliber.workflows.deployment_bundle import sealed_digest, tool_snapshot, verify_bundle
from caliber.workflows.tools import ToolResolutionError

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
    default_model: str = "",
) -> RunPins | None:
    """Check that ``run`` still matches its manifest; returns the pins (``None`` for a legacy run).

    ``snapshot`` is the manifest the worker is about to execute, ``llm_provider`` the provider this deployment would use
    now and ``default_model`` the model an ``inherit`` agent would get. Raises :class:`PinsError`; the worker marks the run failed with its ``code``.
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
    if _inherits_default_model(snapshot) and f"{llm_provider}/{default_model}" not in pins.get(
        "model_routes", []
    ):
        raise PinsError(
            MODEL_ROUTE_CHANGED,
            f"the default model is now {llm_provider}/{default_model!r}, which the run was not pinned to",
        )
    _verify_external(
        session,
        version.workflow_id if version is not None else run.workflow_id,
        bundle,
        depth=0,
        seen=set(),
    )
    return RunPins(
        manifest_id=loaded.manifest_id,
        digest=loaded.digest,
        record=loaded.record,
        pins=loaded.pins,
        bundle=bundle,
    )


_MAX_CHILD_DEPTH = 8


def _inherits_default_model(snapshot: dict[str, Any] | None) -> bool:
    nodes = (snapshot or {}).get("nodes")
    return isinstance(nodes, dict) and any(
        isinstance(node, dict)
        and node.get("type") == "agent"
        and (node.get("model") or "inherit") == "inherit"
        for node in nodes.values()
    )


def _verify_external(
    session: Session, workflow_id: str, bundle: dict[str, Any], *, depth: int, seen: set[str]
) -> None:
    """The live things a sealed bundle only *names*: registered tools, MCP tools and child workflows."""
    from caliber.workflows.promoter import (  # noqa: PLC0415 - promoter imports the worker's dependencies
        build_workflow_identity,
        resolver_from_session,
    )

    resolver = resolver_from_session(session, build_workflow_identity(session, workflow_id))
    identity = synthetic_identity(*_owner_of(session, workflow_id))
    for item in bundle.get("dependencies", []):
        if not isinstance(item, dict) or not isinstance(item.get("snapshot"), dict):
            continue
        kind, snapshot = item.get("kind"), item["snapshot"]
        if kind == "tool":
            try:
                live = tool_snapshot(
                    resolver.resolve(str(item.get("reference")), f"=={snapshot['version']}").entry
                )
            except ToolResolutionError as exc:
                raise PinsError(
                    PINS_CHANGED, f"the pinned tool {item.get('reference')!r} is gone: {exc}"
                ) from exc
            if live != snapshot:
                raise PinsError(
                    PINS_CHANGED,
                    f"the registered tool {item.get('reference')!r} was changed after admission "
                    "(its definition is not the pinned one)",
                )
        elif kind == "mcp_tool":
            _verify_mcp(session, identity, item)
        elif kind == "subworkflow":
            _verify_child(session, item, depth=depth, seen=seen)


def _owner_of(session: Session, workflow_id: str) -> tuple[str, str | None]:
    from caliber.db.models import CaliberWorkflow  # noqa: PLC0415

    workflow = session.get(CaliberWorkflow, workflow_id)
    return (workflow.owner or "", workflow.project_id) if workflow is not None else ("", None)


def _verify_mcp(session: Session, identity: Any, item: dict[str, Any]) -> None:
    snapshot = item["snapshot"]
    server = get_visible(
        session, CaliberMcpServer, CaliberMcpServer.server_id, snapshot.get("server_id"), identity
    )
    tool = next(
        (
            entry
            for entry in (server.discovered_tools if server is not None else [])
            if isinstance(entry, dict) and entry.get("name") == snapshot.get("tool_name")
        ),
        None,
    )
    live: dict[str, Any] = {
        "server_id": snapshot.get("server_id"),
        "tool_name": snapshot.get("tool_name"),
        "tool": tool,
    }
    if "policy" in snapshot:
        live["policy"] = (
            (server.tool_policies or {}).get(snapshot.get("tool_name"))
            if server is not None
            else None
        )
    if tool is None or live != snapshot:
        raise PinsError(
            PINS_CHANGED,
            f"the MCP tool {item.get('reference')!r} is not the one pinned (its discovered schema or policy changed)",
        )


def _verify_child(session: Session, item: dict[str, Any], *, depth: int, seen: set[str]) -> None:
    snapshot = item["snapshot"]
    version_id = str(snapshot.get("version_id") or "")
    if depth >= _MAX_CHILD_DEPTH:
        raise PinsError(PINS_CHANGED, "child workflows are nested too deeply to verify")
    if version_id in seen:
        return  # a cycle is verified once
    seen.add(version_id)
    child = session.get(CaliberWorkflowVersion, version_id)
    sealed = _sealed_bundle(child)
    if (
        child is None
        or sealed is None
        or child.status != "published"
        or not snapshot.get("bundle_digest")
        or sealed_digest(sealed) != snapshot["bundle_digest"]
    ):
        raise PinsError(
            PINS_CHANGED,
            f"the child workflow version {version_id!r} is not the published, sealed one this run was pinned to",
        )
    _verify_external(session, child.workflow_id, sealed, depth=depth + 1, seen=seen)


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
