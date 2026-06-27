"""Derive a workflow service's JSON Schema invocation contract from its manifest.

A workflow published as a service exposes a typed input (the Start node's output
ports — what a caller feeds in) and a typed output (the Output node's input ports
— what the run produces). This module reads a stored manifest and projects those
PortMaps into JSON Schema objects the invoke endpoint validates requests against.
"""

from __future__ import annotations

from typing import Any

from caliber.workflows.manifest import (
    OutputNode,
    PortMap,
    StartNode,
    WorkflowManifestError,
    parse_manifest,
)

# Free-form fallback used when a manifest has no Start/Output node (or cannot be
# parsed): callers may send/receive any JSON object.
_FREEFORM_SCHEMA: dict[str, Any] = {"type": "object"}

# Map a PortSpec.type to its JSON Schema property. ``structured`` and any
# unrecognized type fall through to an open object.
_TYPE_TO_PROPERTY: dict[str, dict[str, Any]] = {
    "string": {"type": "string"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "structured": {"type": "object"},
}


def _property_for_type(port_type: str) -> dict[str, Any]:
    return dict(_TYPE_TO_PROPERTY.get(port_type, {"type": "object"}))


def _object_schema_from_ports(ports: PortMap) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name, spec in ports.items():
        properties[name] = _property_for_type(spec.type)
    return {"type": "object", "properties": properties}


def derive_service_schemas(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(input_schema, output_schema)`` for a workflow version manifest.

    ``input_schema`` is built from the Start node's ``outputs`` PortMap;
    ``output_schema`` from the (first) Output node's ``inputs`` PortMap. When no
    Start/Output node is present — or the manifest can't be parsed — the
    corresponding schema defaults to a free-form ``{"type": "object"}``.
    """
    try:
        parsed = parse_manifest(manifest)
    except (WorkflowManifestError, ValueError):
        return dict(_FREEFORM_SCHEMA), dict(_FREEFORM_SCHEMA)

    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    for node in parsed.nodes.values():
        if input_schema is None and isinstance(node, StartNode):
            input_schema = _object_schema_from_ports(node.outputs)
        elif output_schema is None and isinstance(node, OutputNode):
            output_schema = _object_schema_from_ports(node.inputs)

    if input_schema is None:
        input_schema = dict(_FREEFORM_SCHEMA)
    if output_schema is None:
        output_schema = dict(_FREEFORM_SCHEMA)
    return input_schema, output_schema
