"""CALIBER Workflow Studio backend.

This package makes workflows a first-class CALIBER object: a versioned,
visually-authored manifest that compiles to the OpenAI Agents SDK and runs
inside the existing CALIBER/MLflow process with node-level trace metadata.

Module map (plan §25.1):

* :mod:`~caliber.workflows.manifest` — Pydantic manifest models + canonical hash.
* :mod:`~caliber.workflows.manifest_migrate` — schema-version migration registry.
* :mod:`~caliber.workflows.ir` — typed intermediate representation.
* :mod:`~caliber.workflows.validation` — graph validation (cycles, types, reachability).
* :mod:`~caliber.workflows.tools` — registered-tool binding wrappers.
* :mod:`~caliber.workflows.guardrails` — CALIBER guardrail adapters.
* :mod:`~caliber.workflows.sandbox` — preview-run tool sandboxing.
* :mod:`~caliber.workflows.compiler` — manifest → IR → SDK objects + generated code.
* :mod:`~caliber.workflows.runtime` — runtime plan execution with tracing context.
* :mod:`~caliber.workflows.diff` — semantic graph diff for the approval UI.
* :mod:`~caliber.workflows.patch` — semantic patch application.
* :mod:`~caliber.workflows.promoter` — workflow publish / alias / rollback.
"""

from __future__ import annotations

from caliber.workflows.manifest import (
    CURRENT_SCHEMA_VERSION,
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "WorkflowManifest",
    "compute_manifest_hash",
    "parse_manifest",
]
