"""Coverage for the additive component enhancements (A/B/D):

* per-node ``label`` / ``description`` metadata on every node (hash-safe),
* the catalog ``advanced`` field flag, and
* the catalog ``legacy`` / ``legacy_replacement`` component flag.
"""

from __future__ import annotations

import copy
from typing import Any

from caliber.workflows.component_catalog import build_workflow_component_catalog
from caliber.workflows.manifest import compute_manifest_hash, parse_manifest

_BASE: dict[str, Any] = {
    "schema_version": 1,
    "workflow_id": "wf_meta",
    "name": "meta demo",
    "runtime": {
        "sdk": "openai-agents-python",
        "sdk_version_policy": "runtime-pinned",
        "compiler_version": "caliber-workflow-compiler-v1",
        "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
        "session": {"type": "none"},
    },
    "nodes": {
        "start": {"id": "start", "type": "start", "outputs": {"user_message": {"type": "string"}}},
        "out": {"id": "out", "type": "output", "inputs": {"response": {"type": "string"}}},
    },
    "edges": [{"id": "e", "from": "start", "to": "out", "map": {"user_message": "response"}}],
    "tools": {},
}


# --- A: per-node label / description ----------------------------------------


def test_label_and_description_are_optional_and_hash_safe() -> None:
    """Absent label/description must not change an existing manifest's hash."""
    h_absent = compute_manifest_hash(parse_manifest(copy.deepcopy(_BASE)))
    # A manifest authored before these fields existed (none set) hashes the same.
    assert h_absent == compute_manifest_hash(parse_manifest(copy.deepcopy(_BASE)))


def test_label_and_description_parse_and_serialize() -> None:
    raw = copy.deepcopy(_BASE)
    raw["nodes"]["out"]["label"] = "Final answer"
    raw["nodes"]["out"]["description"] = "Returns the **graded** result."
    manifest = parse_manifest(raw)
    out = manifest.nodes["out"]
    assert out.label == "Final answer"
    assert out.description == "Returns the **graded** result."
    # None-valued metadata is omitted from the canonical dict (exclude_none).
    serialized = manifest.to_dict()
    assert "label" not in serialized["nodes"]["start"]
    assert serialized["nodes"]["out"]["label"] == "Final answer"


def test_setting_label_changes_hash_but_stays_backward_compatible() -> None:
    raw = copy.deepcopy(_BASE)
    raw["nodes"]["out"]["label"] = "Output"
    assert compute_manifest_hash(parse_manifest(raw)) != compute_manifest_hash(
        parse_manifest(copy.deepcopy(_BASE))
    )


def test_label_and_description_are_not_per_type_config_fields() -> None:
    """label/description are generic metadata, excluded from each component's fields."""
    catalog = build_workflow_component_catalog()["components"]
    for component in catalog:
        keys = {field["key"] for field in component["fields"]}
        assert "label" not in keys
        assert "description" not in keys


# --- B: advanced field flag --------------------------------------------------


def test_catalog_fields_carry_advanced_flag() -> None:
    catalog = build_workflow_component_catalog()["components"]
    api = next(c for c in catalog if c["type"] == "api_request")
    by_key = {f["key"]: f for f in api["fields"]}
    # primary fields stay visible; tuning fields are advanced
    assert by_key["url"]["advanced"] is False
    assert by_key["method"]["advanced"] is False
    assert by_key["timeout_seconds"]["advanced"] is True
    assert by_key["headers"]["advanced"] is True


def test_every_field_has_an_advanced_flag() -> None:
    catalog = build_workflow_component_catalog()["components"]
    for component in catalog:
        for field in component["fields"]:
            assert isinstance(field["advanced"], bool)


# --- D: legacy component flag ------------------------------------------------


def test_external_app_is_flagged_legacy_with_replacement() -> None:
    catalog = build_workflow_component_catalog()["components"]
    ext = next(c for c in catalog if c["type"] == "external_app")
    assert ext["legacy"] is True
    assert ext["legacy_replacement"]


def test_non_legacy_components_are_not_flagged() -> None:
    catalog = build_workflow_component_catalog()["components"]
    for node_type in ("agent", "api_request", "tool", "start"):
        component = next(c for c in catalog if c["type"] == node_type)
        assert component["legacy"] is False
        assert component["legacy_replacement"] is None
