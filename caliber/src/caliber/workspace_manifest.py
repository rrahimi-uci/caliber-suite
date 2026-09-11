"""The `v1alpha1` Workspace manifest: schema and canonicalization (`P0-B`,
Phase 0 item 4).

``docs/workspace-plan.md`` section 9.3 ("The Git workspace manifest") already
specifies this manifest's shape (an example YAML document) and its rules in
prose. Nothing validates or canonicalizes it today -- this module is that
missing piece, scoped deliberately narrow:

* It validates the manifest *document itself*: schema shape (unknown
  keys/types refused), path normalization (POSIX-relative, cannot escape the
  configured root), duplicate logical names per resource type, and
  `secretRefs` entries being `secret://` references rather than raw secret
  values.
* It canonicalizes the manifest *document* into a deterministic digest:
  normalized JSON, all object keys sorted, and every resource-type's entry
  list sorted by logical name (so two authors listing the same resources in
  a different order produce the same digest).

It deliberately does **not** implement the rest of section 9.3's rules --
source bundle size/file count/individual file size/decompression ratio/
symlink/media-type bounds, mutable-URL/unversioned-pin rejection, mutable-row
snapshotting before a revision is ready, repository/commit content-conflict
detection, or materialization-failure semantics. Every one of those needs
the actual archive/bundle materialization pipeline, which does not exist yet
(Phase 4, item 3: "Implement the manifest parser, JSON Schema validation,
path/archive limits, and canonical digest golden tests"). This module is
the "manifest parser... and canonical digest" half of that Phase 4 item,
built now because it is a pure, bounded function over the manifest document
alone -- it needs no bundle, no database, no route. `revision_sha256`
(section 9.3) is a larger digest that also folds in canonical source-tree
entries and the complete source-bundle SHA-256; this module's
:func:`manifest_digest` covers only the manifest-document ingredient of that
larger formula, not the whole thing.

This is unrelated to :mod:`caliber.workflows.manifest`, which validates a
single *workflow's* node-graph manifest (a different schema, a different
concept that happens to share the word "manifest"). This module follows
that one's established pattern -- strict Pydantic schema, deterministic
canonical hash -- as precedent, not by importing from it.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Field names below (`metadataPath`, `apiVersion`, `secretRefs`, etc.) are
# deliberately camelCase: they must match section 9.3's manifest wire format
# exactly, and Pydantic's `extra="forbid"` means an aliasing layer would just
# duplicate every name rather than avoid this.
# ruff: noqa: N815

CURRENT_API_VERSION = "caliber/v1alpha1"
CURRENT_KIND = "Workspace"


def _validate_repo_relative_path(value: str) -> str:
    """POSIX-relative, cannot escape the configured root.

    Section 9.3: "paths are normalized POSIX repository-relative paths and
    cannot escape the configured root."
    """
    if not value or not value.strip():
        raise ValueError("path must not be empty")
    if "\\" in value:
        raise ValueError(f"path must use POSIX separators, not backslashes: {value!r}")
    if value.startswith("/"):
        raise ValueError(f"path must be repository-relative, not absolute: {value!r}")
    segments = value.split("/")
    if any(segment == ".." for segment in segments):
        raise ValueError(f"path escapes the repository root: {value!r}")
    if any(segment == "" for segment in segments):
        raise ValueError(f"path must not contain empty segments (e.g. '//'): {value!r}")
    return value


_RepoPath = Annotated[str, Field(min_length=1)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceManifestMetadata(_StrictModel):
    slug: str


class SimplePathResource(_StrictModel):
    """Every resource type whose only pointer is a single `path`: agents,
    workflows, skills, tools, testSets, judges, integrations."""

    name: str
    path: _RepoPath

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


class PromptResource(_StrictModel):
    name: str
    path: _RepoPath
    metadataPath: _RepoPath | None = None

    @field_validator("path", "metadataPath")
    @classmethod
    def _check_path(cls, value: str | None) -> str | None:
        return _validate_repo_relative_path(value) if value is not None else None


class KnowledgeBaseResource(_StrictModel):
    name: str
    manifestPath: _RepoPath

    @field_validator("manifestPath")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


class McpBindingResource(_StrictModel):
    name: str
    connectionRef: str
    policyPath: _RepoPath

    @field_validator("policyPath")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


class ModelResource(_StrictModel):
    name: str
    provider: str
    snapshot: str
    configPath: _RepoPath

    @field_validator("configPath")
    @classmethod
    def _check_path(cls, value: str) -> str:
        return _validate_repo_relative_path(value)


def _check_unique_names(entries: list[BaseModel], type_name: str) -> None:
    """Section 9.3: "duplicate logical names per type are refused."""
    names = [entry.name for entry in entries]  # type: ignore[attr-defined]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise ValueError(f"duplicate logical name(s) in {type_name!r}: {sorted(duplicates)}")


class WorkspaceManifestResources(_StrictModel):
    agents: list[SimplePathResource] = Field(default_factory=list)
    workflows: list[SimplePathResource] = Field(default_factory=list)
    prompts: list[PromptResource] = Field(default_factory=list)
    skills: list[SimplePathResource] = Field(default_factory=list)
    tools: list[SimplePathResource] = Field(default_factory=list)
    testSets: list[SimplePathResource] = Field(default_factory=list)
    knowledgeBases: list[KnowledgeBaseResource] = Field(default_factory=list)
    judges: list[SimplePathResource] = Field(default_factory=list)
    integrations: list[SimplePathResource] = Field(default_factory=list)
    mcpBindings: list[McpBindingResource] = Field(default_factory=list)
    models: list[ModelResource] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_all_types_unique(self) -> WorkspaceManifestResources:
        for type_name in self.__class__.model_fields:
            _check_unique_names(getattr(self, type_name), type_name)
        return self


class WorkspaceManifest(_StrictModel):
    """The `v1alpha1` Workspace manifest, per `docs/workspace-plan.md`
    section 9.3."""

    apiVersion: Literal["caliber/v1alpha1"]
    kind: Literal["Workspace"]
    metadata: WorkspaceManifestMetadata
    resources: WorkspaceManifestResources = Field(default_factory=WorkspaceManifestResources)
    documentation: list[str] = Field(default_factory=list)
    secretRefs: list[str] = Field(default_factory=list)

    @field_validator("documentation")
    @classmethod
    def _check_documentation_paths(cls, value: list[str]) -> list[str]:
        return [_validate_repo_relative_path(path) for path in value]

    @field_validator("secretRefs")
    @classmethod
    def _check_secret_refs(cls, value: list[str]) -> list[str]:
        """Section 9.3: "secret-looking values are refused; only
        `secret://` references are accepted." This module can only check the
        one field the manifest schema itself designates for secrets --
        detecting a secret-looking value smuggled into an unrelated string
        field is a materialization-time content scan, out of scope here."""
        for ref in value:
            if not ref.startswith("secret://"):
                raise ValueError(f"secretRefs entries must be secret:// references, got: {ref!r}")
            if ref == "secret://":
                raise ValueError("secretRefs entry has an empty secret name")
        return value


def parse_workspace_manifest(document: dict[str, object]) -> WorkspaceManifest:
    """Validate a raw manifest document (e.g. parsed from YAML) against the
    `v1alpha1` schema. Raises :class:`pydantic.ValidationError` -- with
    every violation this module checks, not just the first -- on any schema
    violation: unknown key, wrong type, duplicate name, escaping path, or a
    non-`secret://` secret reference."""
    return WorkspaceManifest.model_validate(document)


def _sort_recursively(value: object) -> object:
    """Recursively sort dict keys and, within `resources`, each resource
    type's entry list by `name` -- so two manifests differing only in
    author-chosen ordering canonicalize identically."""
    if isinstance(value, dict):
        return {key: _sort_recursively(val) for key, val in sorted(value.items())}
    if isinstance(value, list):
        sorted_list = [_sort_recursively(item) for item in value]
        if sorted_list and all(isinstance(item, dict) and "name" in item for item in sorted_list):
            dict_entries = cast("list[dict[str, str]]", sorted_list)
            dict_entries.sort(key=lambda item: item["name"])
        return sorted_list
    return value


def canonicalize_manifest(manifest: WorkspaceManifest) -> str:
    """Deterministic canonical JSON for a validated manifest.

    Section 9.3: the canonical digest "uses normalized JSON, sorted resource
    keys." This function covers the manifest-document half of that formula
    -- see the module docstring for what it deliberately excludes (source
    tree/bundle content hashes, which need real materialization).
    """
    raw = manifest.model_dump(mode="json")
    canonical = _sort_recursively(raw)
    return json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def manifest_digest(manifest: WorkspaceManifest) -> str:
    """SHA-256 of :func:`canonicalize_manifest`'s output, hex-encoded."""
    return hashlib.sha256(canonicalize_manifest(manifest).encode("utf-8")).hexdigest()


__all__ = [
    "CURRENT_API_VERSION",
    "CURRENT_KIND",
    "KnowledgeBaseResource",
    "McpBindingResource",
    "ModelResource",
    "PromptResource",
    "SimplePathResource",
    "WorkspaceManifest",
    "WorkspaceManifestMetadata",
    "WorkspaceManifestResources",
    "canonicalize_manifest",
    "manifest_digest",
    "parse_workspace_manifest",
]
