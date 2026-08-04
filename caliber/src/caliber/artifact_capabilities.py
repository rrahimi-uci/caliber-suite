"""Machine-readable per-family guarantee surface.

The shared UI substrate does not imply shared lifecycle semantics. This registry
is the executable counterpart to the paper's family table: routes and clients can
disclose what a family supports without inferring guarantees from which component
happens to render it.
"""

from __future__ import annotations

from typing import Final

ARTIFACT_FAMILY_CAPABILITIES: Final[dict[str, dict[str, object]]] = {
    "prompt": {
        "history": "immutable_registry_versions",
        "live_target": "alias",
        "promotable": True,
        "rollbackable": True,
        "evidence_bearing": True,
        "gate_mode": "enforced_refinement_advisory_direct",
        "calibration": "provider_optimizer_and_eval",
    },
    "workflow": {
        "history": "immutable_published_versions",
        "live_target": "deployment_alias",
        "promotable": True,
        "rollbackable": True,
        "evidence_bearing": True,
        "gate_mode": "enforced_deployment_gate",
        "calibration": "manifest_replay",
    },
    "knowledge_base": {
        "history": "immutable_build_versions",
        "live_target": "active_version_id",
        "promotable": True,
        "rollbackable": True,
        "evidence_bearing": True,
        "gate_mode": "none",
        "calibration": "retrieval_quality",
    },
    "skill": {
        "history": "forward_only_snapshots",
        "live_target": "current_record",
        "promotable": False,
        "rollbackable": True,
        "evidence_bearing": True,
        "gate_mode": "enforced_refinement_only",
        "calibration": "agent_free_optimizer",
    },
    "tool": {
        "history": "named_version_rows",
        "live_target": "none",
        "promotable": False,
        "rollbackable": False,
        "evidence_bearing": True,
        "gate_mode": "none",
        "calibration": "revision_fenced_suites",
    },
    "test_set": {
        "history": "versioned_examples",
        "live_target": "none",
        "promotable": False,
        "rollbackable": False,
        "evidence_bearing": True,
        "gate_mode": "evidence_asset",
        "calibration": "not_applicable",
    },
    "mcp_server": {
        "history": "audited_edits",
        "live_target": "managed_definition",
        "promotable": False,
        "rollbackable": False,
        "evidence_bearing": True,
        "gate_mode": "workflow_preflight",
        "calibration": "connection_and_policy_tests",
    },
    "judge": {
        "history": "reusable_named_scorer",
        "live_target": "none",
        "promotable": False,
        "rollbackable": False,
        "evidence_bearing": True,
        "gate_mode": "scoring_asset",
        "calibration": "human_alignment",
    },
    "agent": {
        "history": "anchor_record",
        "live_target": "enabled_flag",
        "promotable": False,
        "rollbackable": False,
        "evidence_bearing": False,
        "gate_mode": "not_applicable",
        "calibration": "not_applicable",
    },
}
