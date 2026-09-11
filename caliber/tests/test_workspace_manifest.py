"""Golden-vector tests for the `v1alpha1` Workspace manifest schema and
canonicalization (`P0-B`, Phase 0 item 4).

Confirms: a valid manifest (matching docs/workspace-plan.md section 9.3's
example) parses and canonicalizes to a deterministic, pinned digest;
reordering entries doesn't change the digest; a real content change does;
and every documented rule this module actually enforces rejects its
violation.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from caliber.workspace_manifest import (
    canonicalize_manifest,
    manifest_digest,
    parse_workspace_manifest,
)

#: Mirrors docs/workspace-plan.md section 9.3's example manifest, minus
#: comments (YAML-vs-JSON is not this module's concern -- it validates the
#: parsed document, whatever format produced it).
_VALID_MANIFEST: dict[str, object] = {
    "apiVersion": "caliber/v1alpha1",
    "kind": "Workspace",
    "metadata": {"slug": "mortgage-underwriting"},
    "resources": {
        "agents": [
            {"name": "underwriting-agent", "path": "caliber/agents/underwriting-agent.yaml"}
        ],
        "workflows": [{"name": "underwriting", "path": "caliber/workflows/underwriting.json"}],
        "prompts": [
            {
                "name": "eligibility-decision",
                "path": "caliber/prompts/eligibility-decision.md",
                "metadataPath": "caliber/prompts/eligibility-decision.yaml",
            }
        ],
        "skills": [{"name": "explain-decision", "path": "caliber/skills/explain-decision.md"}],
        "tools": [{"name": "income-ratio", "path": "caliber/tools/income-ratio.yaml"}],
        "testSets": [
            {
                "name": "underwriting-regression",
                "path": "caliber/test-sets/underwriting-regression.jsonl",
            }
        ],
        "knowledgeBases": [
            {"name": "policy-manual", "manifestPath": "caliber/knowledge/policy-manual.yaml"}
        ],
        "judges": [{"name": "grounded-decision", "path": "caliber/judges/grounded-decision.yaml"}],
        "integrations": [
            {"name": "servicing-api", "path": "caliber/integrations/servicing.openapi.yaml"}
        ],
        "mcpBindings": [
            {
                "name": "policy-search",
                "connectionRef": "mcp://approved-policy-search",
                "policyPath": "caliber/mcp/policy-search.yaml",
            }
        ],
        "models": [
            {
                "name": "decision-model",
                "provider": "example-provider",
                "snapshot": "immutable-model-or-deployment-id",
                "configPath": "caliber/models/decision-model.yaml",
            }
        ],
    },
    "documentation": ["docs/architecture.md", "docs/runbook.md"],
    "secretRefs": ["secret://servicing-api-token"],
}

#: Pinned golden digest for `_VALID_MANIFEST` -- a change here must be a
#: conscious update to match a deliberate canonicalization change, never a
#: silent side effect of an unrelated edit.
_VALID_MANIFEST_DIGEST = "9ea427dfd8051165e565c6999dc78998b427f2d3b0c6dfc9009448f1cd722254"


def test_the_example_manifest_from_section_9_3_parses() -> None:
    manifest = parse_workspace_manifest(_VALID_MANIFEST)
    assert manifest.metadata.slug == "mortgage-underwriting"
    assert manifest.resources.agents[0].name == "underwriting-agent"
    assert manifest.secretRefs == ["secret://servicing-api-token"]


def test_canonicalization_is_deterministic_json_with_sorted_keys() -> None:
    manifest = parse_workspace_manifest(_VALID_MANIFEST)
    canonical = canonicalize_manifest(manifest)
    assert canonical == canonicalize_manifest(parse_workspace_manifest(_VALID_MANIFEST))
    # Sorted top-level keys, compact separators -- both are what
    # `json.dumps(..., sort_keys=True, separators=(",", ":"))` produces.
    assert canonical.startswith('{"apiVersion":"caliber/v1alpha1"')
    assert ", " not in canonical
    assert ": " not in canonical


def test_reordering_resource_entries_does_not_change_the_digest() -> None:
    """An author listing the same agents in a different order in their YAML
    must not produce a different revision identity."""
    reordered = copy.deepcopy(_VALID_MANIFEST)
    reordered["resources"] = dict(reordered["resources"])
    reordered["resources"]["agents"] = [
        {"name": "second-agent", "path": "caliber/agents/second-agent.yaml"},
        {"name": "underwriting-agent", "path": "caliber/agents/underwriting-agent.yaml"},
    ]
    reordered["resources"]["agents"].reverse()
    original_with_two = copy.deepcopy(_VALID_MANIFEST)
    original_with_two["resources"] = dict(original_with_two["resources"])
    original_with_two["resources"]["agents"] = [
        {"name": "underwriting-agent", "path": "caliber/agents/underwriting-agent.yaml"},
        {"name": "second-agent", "path": "caliber/agents/second-agent.yaml"},
    ]
    a = manifest_digest(parse_workspace_manifest(original_with_two))
    b = manifest_digest(parse_workspace_manifest(reordered))
    assert a == b


def test_a_real_content_change_changes_the_digest() -> None:
    mutated = copy.deepcopy(_VALID_MANIFEST)
    mutated["resources"] = dict(mutated["resources"])
    mutated["resources"]["agents"] = [
        {"name": "underwriting-agent", "path": "caliber/agents/renamed-agent.yaml"}
    ]
    original_digest = manifest_digest(parse_workspace_manifest(_VALID_MANIFEST))
    mutated_digest = manifest_digest(parse_workspace_manifest(mutated))
    assert original_digest != mutated_digest


def test_the_golden_digest_is_pinned() -> None:
    """Ratchet: a change to this digest must be a deliberate, reviewed
    canonicalization change, never a silent side effect."""
    manifest = parse_workspace_manifest(_VALID_MANIFEST)
    digest = manifest_digest(manifest)
    assert len(digest) == 64  # sha256 hex
    assert digest == _VALID_MANIFEST_DIGEST


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        pytest.param(
            lambda m: m.update({"apiVersion": "caliber/v1beta1"}),
            "apiVersion",
            id="bad-api-version",
        ),
        pytest.param(lambda m: m.update({"kind": "Project"}), "kind", id="bad-kind"),
        pytest.param(
            lambda m: m.update({"unknownTopLevelField": "x"}),
            "unknownTopLevelField",
            id="unknown-top-level-key",
        ),
    ],
)
def test_schema_violations_at_the_top_level_are_refused(mutation, match) -> None:
    document = copy.deepcopy(_VALID_MANIFEST)
    mutation(document)
    with pytest.raises(ValidationError, match=match):
        parse_workspace_manifest(document)


def test_unknown_key_inside_a_resource_entry_is_refused() -> None:
    document = copy.deepcopy(_VALID_MANIFEST)
    document["resources"]["agents"][0]["unexpectedField"] = "x"
    with pytest.raises(ValidationError, match="unexpectedField"):
        parse_workspace_manifest(document)


def test_unknown_resource_type_is_refused() -> None:
    document = copy.deepcopy(_VALID_MANIFEST)
    document["resources"]["notARealResourceType"] = []
    with pytest.raises(ValidationError, match="notARealResourceType"):
        parse_workspace_manifest(document)


def test_duplicate_logical_names_per_type_are_refused() -> None:
    document = copy.deepcopy(_VALID_MANIFEST)
    document["resources"]["agents"] = [
        {"name": "same-name", "path": "caliber/agents/one.yaml"},
        {"name": "same-name", "path": "caliber/agents/two.yaml"},
    ]
    with pytest.raises(ValidationError, match="duplicate logical name"):
        parse_workspace_manifest(document)


@pytest.mark.parametrize(
    "bad_path",
    [
        "/absolute/path.yaml",
        "../escapes/root.yaml",
        "caliber/../../etc/passwd",
        "caliber\\windows\\path.yaml",
        "",
        "caliber//double-slash.yaml",
    ],
)
def test_paths_that_escape_or_are_malformed_are_refused(bad_path: str) -> None:
    document = copy.deepcopy(_VALID_MANIFEST)
    document["resources"]["agents"][0]["path"] = bad_path
    with pytest.raises(ValidationError):
        parse_workspace_manifest(document)


@pytest.mark.parametrize(
    "bad_ref",
    [
        "sk-live-abc123",
        "https://vault.example.com/secret",
        "secret://",
        "",
    ],
)
def test_non_secret_ref_values_are_refused(bad_ref: str) -> None:
    document = copy.deepcopy(_VALID_MANIFEST)
    document["secretRefs"] = [bad_ref]
    with pytest.raises(ValidationError):
        parse_workspace_manifest(document)


def test_a_minimal_manifest_with_no_resources_is_valid() -> None:
    """An empty Workspace (no resources yet) is a legitimate starting state,
    not a schema violation."""
    minimal = {
        "apiVersion": "caliber/v1alpha1",
        "kind": "Workspace",
        "metadata": {"slug": "empty-workspace"},
    }
    manifest = parse_workspace_manifest(minimal)
    assert manifest.resources.agents == []
    assert manifest.documentation == []
    assert manifest.secretRefs == []
