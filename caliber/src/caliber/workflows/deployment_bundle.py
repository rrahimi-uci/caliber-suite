"""Deterministic, integrity-sealed workflow deployment bundles.

The bundle is the release envelope for a workflow version. It contains the
compiled program, the authoring manifest, an execution manifest with mutable
references resolved, and a lock for every external dependency. Prompt and skill
text is embedded; executable tools, MCP servers, object-store files, knowledge
artifacts, datasets, and child workflows remain external and are identified by
exact version/digest metadata. Secret *names* may appear, but secret values are
never read or written here.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
    CaliberMcpServer,
    CaliberSkill,
    CaliberWorkflowDeployment,
    CaliberWorkflowFile,
    CaliberWorkflowVersion,
)
from caliber.workflows.compiler import compile_workflow
from caliber.workflows.manifest import (
    AgentNode,
    FileInputNode,
    KnowledgeBuildNode,
    KnowledgeQueryNode,
    McpResourceNode,
    McpToolBinding,
    PromptRefInstructions,
    RegisteredFunctionToolBinding,
    SubworkflowNode,
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.tools import InMemoryToolResolver, ToolResolutionError

BUNDLE_KIND = "caliber.workflow_deployment_bundle"
BUNDLE_SCHEMA_VERSION = 1


class DeploymentBundleError(ValueError):
    """A deployment bundle is malformed or fails its integrity check."""


@dataclass(frozen=True)
class BundleVerification:
    valid: bool
    errors: list[str]
    digest: str | None
    dependency_count: int
    ready_to_deploy: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "digest": self.digest,
            "dependency_count": self.dependency_count,
            "ready_to_deploy": self.ready_to_deploy,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sealed_payload(bundle: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(bundle)
    payload.pop("integrity", None)
    return payload


def seal_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    sealed = deepcopy(bundle)
    sealed["integrity"] = {
        "algorithm": "sha256",
        "digest": content_digest(_sealed_payload(sealed)),
    }
    return sealed


def _validate_skill_snapshots(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["skill_snapshots must be an object"]
    if any(
        not isinstance(name, str)
        or not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("content"), str)
        for name, snapshot in value.items()
    ):
        return ["skill_snapshots entries must be objects with string content"]
    return []


def verify_bundle(bundle: Any) -> BundleVerification:
    errors: list[str] = []
    if not isinstance(bundle, dict):
        return BundleVerification(False, ["bundle must be a JSON object"], None, 0, False)
    if bundle.get("kind") != BUNDLE_KIND:
        errors.append(f"kind must be {BUNDLE_KIND!r}")
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        errors.append(f"schema_version must be {BUNDLE_SCHEMA_VERSION}")
    integrity = bundle.get("integrity")
    digest = integrity.get("digest") if isinstance(integrity, dict) else None
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        errors.append("integrity.algorithm must be 'sha256'")
    expected = content_digest(_sealed_payload(bundle))
    if not isinstance(digest, str) or digest != expected:
        errors.append("bundle digest does not match its contents")
    manifest = bundle.get("manifest")
    workflow = bundle.get("workflow")
    if not isinstance(manifest, dict):
        errors.append("manifest must be an object")
    elif not isinstance(workflow, dict) or workflow.get("manifest_hash") != compute_manifest_hash(
        manifest
    ):
        errors.append("workflow.manifest_hash does not match manifest")
    resolved_manifest = bundle.get("resolved_manifest")
    if not isinstance(resolved_manifest, dict):
        errors.append("resolved_manifest must be an object")
    raw_dependencies = bundle.get("dependencies")
    if not isinstance(raw_dependencies, list):
        errors.append("dependencies must be a list")
        raw_dependencies = []
    dependencies = [item for item in raw_dependencies if isinstance(item, dict)]
    if len(dependencies) != len(raw_dependencies):
        errors.append("dependencies entries must be objects")
    errors.extend(_validate_skill_snapshots(bundle.get("skill_snapshots")))
    unresolved = [item for item in dependencies if item.get("status") != "resolved"]
    declared_ready = bundle.get("ready_to_deploy")
    calculated_ready = not unresolved
    if declared_ready is not calculated_ready:
        errors.append("ready_to_deploy does not match dependency resolution status")
    return BundleVerification(
        valid=not errors,
        errors=errors,
        digest=digest if isinstance(digest, str) else None,
        dependency_count=len(raw_dependencies),
        ready_to_deploy=calculated_ready and not errors,
    )


def require_valid_bundle(bundle: Any) -> dict[str, Any]:
    verification = verify_bundle(bundle)
    if not verification.valid:
        raise DeploymentBundleError("; ".join(verification.errors))
    return cast(dict[str, Any], bundle)


def _dependency(
    *,
    kind: str,
    path: str,
    reference: str,
    status: str,
    version: str | None = None,
    portability: str = "external_required",
    snapshot: dict[str, Any] | None = None,
    detail: str = "",
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "kind": kind,
        "path": path,
        "reference": reference,
        "status": status,
        "version": version,
        "portability": portability,
        "detail": detail,
    }
    if snapshot is not None:
        item["snapshot"] = snapshot
        item["digest"] = content_digest(snapshot)
    return item


def _load_prompt_snapshot(  # noqa: PLR0911 - preserve precise provider failure detail
    name: str, alias: str
) -> tuple[dict[str, Any] | None, str]:
    try:
        import mlflow  # noqa: PLC0415
    except ImportError:
        return None, "MLflow is not installed, so the prompt alias could not be resolved."
    load_prompt = getattr(getattr(mlflow, "genai", None), "load_prompt", None) or getattr(
        mlflow, "load_prompt", None
    )
    if not callable(load_prompt):
        return None, "MLflow does not expose the prompt load API."
    ref = f"prompts:/{name}@{alias}"
    try:
        prompt = load_prompt(ref, allow_missing=True)
    except Exception as exc:  # provider boundary; surfaced in bundle status
        return None, f"Prompt alias resolution failed: {exc}"
    if prompt is None:
        return None, f"Prompt {ref!r} does not exist."
    raw_version = getattr(prompt, "version", None)
    template = getattr(prompt, "template", None) or getattr(prompt, "content", None)
    try:
        version = int(str(raw_version))
    except (TypeError, ValueError):
        return None, f"Prompt {ref!r} did not resolve to a concrete version."
    if not isinstance(template, str):
        return None, f"Prompt {ref!r} has no string template."
    return {"name": name, "alias": alias, "version": version, "template": template}, ""


def _dataset_snapshot(
    session: Session,
    dataset: CaliberEvalDataset,
    dataset_version: int,
) -> dict[str, Any]:
    rows = list(
        session.execute(
            select(CaliberEvalDatasetExample).where(
                CaliberEvalDatasetExample.dataset_id == dataset.dataset_id,
                CaliberEvalDatasetExample.dataset_version <= dataset_version,
            )
        ).scalars()
    )
    examples = [
        {
            "example_id": row.example_id,
            "input": row.input,
            "expected": row.expected,
            "weight": row.weight,
            "tags": row.tags or [],
        }
        for row in rows
        if row.superseded_version is None or row.superseded_version > dataset_version
    ]
    examples.sort(key=lambda item: str(item["example_id"]))
    return {
        "dataset_id": dataset.dataset_id,
        "name": dataset.name,
        "version": dataset_version,
        "examples_digest": content_digest(examples),
        "example_count": len(examples),
    }


def build_deployment_bundle(  # noqa: PLR0912, PLR0915 - dependency inventory
    session: Session,
    version: CaliberWorkflowVersion,
    manifest: WorkflowManifest,
    resolver: InMemoryToolResolver,
    imported_skill_snapshots: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve and seal one workflow version without copying secret values."""
    source_manifest = manifest.to_dict()
    resolved_manifest = deepcopy(source_manifest)
    dependencies: list[dict[str, Any]] = []
    skill_snapshots: dict[str, dict[str, Any]] = {}

    referenced_prompts = {
        node.instructions.ref
        for node in manifest.nodes.values()
        if isinstance(node, AgentNode) and isinstance(node.instructions, PromptRefInstructions)
    }
    for alias, prompt_artifact in sorted(manifest.artifacts.prompts.items()):
        if alias not in referenced_prompts:
            continue
        snapshot, detail = _load_prompt_snapshot(
            prompt_artifact.registry_name, prompt_artifact.alias
        )
        if snapshot is not None:
            for node in resolved_manifest["nodes"].values():
                instructions = node.get("instructions") if isinstance(node, dict) else None
                if (
                    isinstance(instructions, dict)
                    and instructions.get("type") == "mlflow_prompt"
                    and instructions.get("ref") == alias
                ):
                    node["instructions"] = {
                        "type": "inline",
                        "text": snapshot["template"],
                    }
            # The immutable template now lives on every consuming node and in
            # the dependency snapshot. Keeping the mutable registry artifact in
            # the execution manifest would make destination preflight ask for a
            # prompt alias that execution no longer needs.
            resolved_manifest["artifacts"]["prompts"].pop(alias, None)
        dependencies.append(
            _dependency(
                kind="prompt",
                path=f"artifacts.prompts.{alias}",
                reference=f"{prompt_artifact.registry_name}@{prompt_artifact.alias}",
                status="resolved" if snapshot is not None else "unresolved",
                version=str(snapshot["version"]) if snapshot is not None else None,
                portability="embedded",
                snapshot=snapshot,
                detail=detail or "Exact prompt template embedded in the bundle.",
            )
        )

    for alias, dataset_artifact in sorted(manifest.artifacts.eval_datasets.items()):
        dataset = (
            session.execute(
                select(CaliberEvalDataset).where(
                    CaliberEvalDataset.name == dataset_artifact.dataset_name
                )
            )
            .scalars()
            .first()
        )
        requested_version = dataset_artifact.version
        exact_version = (
            requested_version
            if dataset is not None
            and requested_version is not None
            and requested_version <= dataset.version
            else dataset.version
            if dataset is not None and requested_version is None
            else None
        )
        snapshot = (
            _dataset_snapshot(session, dataset, exact_version)
            if dataset is not None and exact_version is not None
            else None
        )
        if exact_version is not None:
            resolved_manifest["artifacts"]["eval_datasets"][alias]["version"] = exact_version
        dependencies.append(
            _dependency(
                kind="eval_dataset",
                path=f"artifacts.eval_datasets.{alias}",
                reference=dataset_artifact.dataset_name,
                status="resolved" if snapshot is not None else "unresolved",
                version=str(exact_version) if exact_version is not None else None,
                snapshot=snapshot,
                detail="Dataset identity and example digest pinned; examples remain in CALIBER storage."
                if snapshot is not None
                else (
                    f"Evaluation dataset version {requested_version} is newer than the available "
                    f"version {dataset.version}."
                    if dataset is not None and requested_version is not None
                    else "Evaluation dataset was not found."
                ),
            )
        )

    for local_name, binding in sorted(manifest.tools.items()):
        path = f"tools.{local_name}"
        if isinstance(binding, RegisteredFunctionToolBinding):
            try:
                entry = resolver.resolve(binding.registry_ref, binding.version_constraint).entry
            except ToolResolutionError as exc:
                dependencies.append(
                    _dependency(
                        kind="tool",
                        path=path,
                        reference=binding.registry_ref,
                        status="unresolved",
                        detail=str(exc),
                    )
                )
            else:
                resolved_manifest["tools"][local_name]["version_constraint"] = f"=={entry.version}"
                snapshot = {
                    "name": entry.name,
                    "version": entry.version,
                    "module_path": entry.module_path,
                    "callable_name": entry.callable_name,
                    "execution_backend": entry.execution_backend,
                    "input_schema": entry.input_schema,
                    "output_schema": entry.output_schema,
                    "side_effect_level": entry.side_effect_level,
                    "requires_approval": entry.requires_approval,
                    "secret_refs": sorted(entry.secret_refs),
                }
                dependencies.append(
                    _dependency(
                        kind="tool",
                        path=path,
                        reference=binding.registry_ref,
                        status="resolved",
                        version=entry.version,
                        snapshot=snapshot,
                        detail="Exact tool definition pinned; executable and named secrets remain external.",
                    )
                )
        elif isinstance(binding, McpToolBinding):
            server = session.get(CaliberMcpServer, binding.server_id)
            tool = next(
                (
                    item
                    for item in (server.discovered_tools if server is not None else [])
                    if isinstance(item, dict) and item.get("name") == binding.tool_name
                ),
                None,
            )
            snapshot = (
                {
                    "server_id": binding.server_id,
                    "tool_name": binding.tool_name,
                    "tool": tool,
                    "policy": (server.tool_policies or {}).get(binding.tool_name)
                    if server is not None
                    else None,
                }
                if tool is not None
                else None
            )
            dependencies.append(
                _dependency(
                    kind="mcp_tool",
                    path=path,
                    reference=f"{binding.server_id}/{binding.tool_name}",
                    status="resolved" if snapshot is not None else "unresolved",
                    version=binding.tool_schema_version or None,
                    snapshot=snapshot,
                    detail="MCP discovery schema pinned; server credentials remain external."
                    if snapshot is not None
                    else "MCP server/tool is unavailable or undiscovered.",
                )
            )

    skill_names = sorted(
        {
            skill
            for node in manifest.nodes.values()
            if isinstance(node, AgentNode)
            for skill in node.skills
        }
    )
    for name in skill_names:
        imported_snapshot = (imported_skill_snapshots or {}).get(name)
        snapshot = (
            deepcopy(imported_snapshot)
            if isinstance(imported_snapshot, dict)
            and isinstance(imported_snapshot.get("content"), str)
            else None
        )
        skill = None
        if snapshot is None:
            skill = (
                session.execute(select(CaliberSkill).where(CaliberSkill.name == name))
                .scalars()
                .first()
            )
        if snapshot is None and skill is not None and skill.status != "archived":
            snapshot = {
                "skill_id": skill.skill_id,
                "name": skill.name,
                "version": skill.version,
                "summary": skill.summary,
                "content": skill.content,
                "allowed_tools": skill.allowed_tools,
                "depends_on": sorted(skill.depends_on or []),
            }
        if snapshot is not None:
            skill_snapshots[name] = snapshot
        dependencies.append(
            _dependency(
                kind="skill",
                path="nodes.*.skills",
                reference=name,
                status="resolved" if snapshot is not None else "unresolved",
                version=str(snapshot.get("version")) if snapshot is not None else None,
                portability="embedded",
                snapshot=snapshot,
                detail="Exact skill content embedded in the bundle."
                if snapshot is not None
                else "Skill is missing or archived.",
            )
        )

    for node_id, node in sorted(manifest.nodes.items()):
        path = f"nodes.{node_id}"
        if isinstance(node, (KnowledgeQueryNode, KnowledgeBuildNode)) and node.knowledge_base_id:
            kb = session.get(CaliberKnowledgeBase, node.knowledge_base_id)
            version_ids = list(getattr(node, "version_ids", []) or [])
            if isinstance(node, KnowledgeQueryNode) and not version_ids and kb is not None:
                version_ids = [kb.active_version_id] if kb.active_version_id else []
                resolved_manifest["nodes"][node_id]["version_ids"] = version_ids
            kb_versions = [session.get(CaliberKnowledgeBaseVersion, item) for item in version_ids]
            valid_versions = [
                item
                for item in kb_versions
                if item is not None and item.knowledge_base_id == node.knowledge_base_id
            ]
            resolved = kb is not None and (
                isinstance(node, KnowledgeBuildNode)
                or (bool(version_ids) and len(valid_versions) == len(version_ids))
            )
            snapshot = None
            if kb is not None:
                snapshot = {
                    "knowledge_base_id": kb.knowledge_base_id,
                    "source_fingerprint": kb.source_fingerprint,
                    "version_ids": [item.knowledge_base_version_id for item in valid_versions],
                    "versions": [
                        {
                            "version_id": item.knowledge_base_version_id,
                            "version_number": item.version_number,
                            "source_fingerprint": item.source_fingerprint,
                            "manifest_uri": item.manifest_uri,
                        }
                        for item in valid_versions
                    ],
                }
            dependencies.append(
                _dependency(
                    kind="knowledge_base",
                    path=f"{path}.knowledge_base_id",
                    reference=node.knowledge_base_id,
                    status="resolved" if resolved else "unresolved",
                    version=",".join(version_ids) or None,
                    snapshot=snapshot,
                    detail="Knowledge build metadata pinned; indexed objects remain external."
                    if resolved
                    else "Knowledge base or required immutable version is unavailable.",
                )
            )
        elif isinstance(node, McpResourceNode):
            server = session.get(CaliberMcpServer, node.server_id)
            tool = next(
                (
                    item
                    for item in (server.discovered_tools if server is not None else [])
                    if isinstance(item, dict) and item.get("name") == node.tool_name
                ),
                None,
            )
            snapshot = (
                {"server_id": node.server_id, "tool_name": node.tool_name, "tool": tool}
                if tool is not None
                else None
            )
            dependencies.append(
                _dependency(
                    kind="mcp_tool",
                    path=path,
                    reference=f"{node.server_id}/{node.tool_name}",
                    status="resolved" if snapshot is not None else "unresolved",
                    snapshot=snapshot,
                    detail="MCP discovery schema pinned; server credentials remain external."
                    if snapshot is not None
                    else "MCP server/tool is unavailable or undiscovered.",
                )
            )
        elif isinstance(node, SubworkflowNode):
            target = (
                session.get(CaliberWorkflowVersion, node.version_id) if node.version_id else None
            )
            if target is None:
                if node.alias == "manual":
                    target = (
                        session.execute(
                            select(CaliberWorkflowVersion)
                            .where(CaliberWorkflowVersion.workflow_id == node.workflow_id)
                            .order_by(CaliberWorkflowVersion.version_number.desc())
                        )
                        .scalars()
                        .first()
                    )
                else:
                    deployment = (
                        session.execute(
                            select(CaliberWorkflowDeployment).where(
                                CaliberWorkflowDeployment.workflow_id == node.workflow_id,
                                CaliberWorkflowDeployment.alias == node.alias,
                                CaliberWorkflowDeployment.status == "active",
                            )
                        )
                        .scalars()
                        .first()
                    )
                    target = (
                        session.get(CaliberWorkflowVersion, deployment.version_id)
                        if deployment is not None
                        else None
                    )
            if target is not None and target.workflow_id == node.workflow_id:
                resolved_manifest["nodes"][node_id]["version_id"] = target.version_id
            else:
                target = None
            snapshot = (
                {
                    "workflow_id": target.workflow_id,
                    "version_id": target.version_id,
                    "version_number": target.version_number,
                    "manifest_hash": target.manifest_hash,
                }
                if target is not None
                else None
            )
            dependencies.append(
                _dependency(
                    kind="subworkflow",
                    path=path,
                    reference=f"{node.workflow_id}@{node.alias}",
                    status="resolved" if target is not None else "unresolved",
                    version=target.version_id if target is not None else None,
                    snapshot=snapshot,
                    detail="Child workflow pinned to an immutable version."
                    if target is not None
                    else "Child workflow alias could not be resolved.",
                )
            )
        elif isinstance(node, FileInputNode) and node.file_ref is not None:
            ref = node.file_ref
            row = session.get(CaliberWorkflowFile, ref.file_id)
            resolved = bool(
                row is not None
                and row.deleted_at is None
                and row.file_ref == ref.file_ref
                and row.sha256 == ref.sha256
                and row.size_bytes == ref.size_bytes
                and (
                    ref.object_version_id is None or row.object_version_id == ref.object_version_id
                )
            )
            snapshot = ref.model_dump(mode="json")
            dependencies.append(
                _dependency(
                    kind="managed_file",
                    path=f"{path}.file_ref",
                    reference=ref.file_ref,
                    status="resolved" if resolved else "unresolved",
                    version=ref.object_version_id,
                    snapshot=snapshot,
                    detail="File metadata and digest pinned; object bytes remain external."
                    if resolved
                    else "Managed file metadata no longer matches the pinned reference.",
                )
            )

    dependencies.sort(key=lambda item: (item["kind"], item["path"], item["reference"]))
    # Compile the resolved graph as the executable artifact. This is what makes
    # export and runtime use the same exact prompt/tool/child/KB identities that
    # the lock describes instead of following the authoring aliases again.
    resolved_compiled = compile_workflow(
        parse_manifest(resolved_manifest),
        resolver=resolver,
        version=str(version.version_number),
        skill_contents={
            name: content
            for name, item in skill_snapshots.items()
            if isinstance((content := item.get("content")), str)
        },
    )
    bundle = {
        "kind": BUNDLE_KIND,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "workflow": {
            "workflow_id": version.workflow_id,
            "version_id": version.version_id,
            "version_number": version.version_number,
            "manifest_hash": compute_manifest_hash(source_manifest),
            "compiler_version": resolved_compiled.report.get("compiler_version"),
        },
        "manifest": source_manifest,
        "resolved_manifest": resolved_manifest,
        "compiled": {
            "generated_python": resolved_compiled.generated_python,
            "compiler_report": resolved_compiled.report,
            "requirements": resolved_compiled.requirements,
        },
        "dependencies": dependencies,
        "skill_snapshots": skill_snapshots,
        "ready_to_deploy": all(item["status"] == "resolved" for item in dependencies),
        "portability": {
            "embedded": ["workflow", "compiled_program", "prompt_templates", "skill_content"],
            "external_required": [
                "tool_executables",
                "named_secrets",
                "mcp_servers",
                "dataset_rows",
                "knowledge_objects",
                "managed_file_bytes",
                "child_workflows",
            ],
        },
    }
    return seal_bundle(bundle)


__all__ = [
    "BUNDLE_KIND",
    "BUNDLE_SCHEMA_VERSION",
    "BundleVerification",
    "DeploymentBundleError",
    "build_deployment_bundle",
    "content_digest",
    "require_valid_bundle",
    "seal_bundle",
    "verify_bundle",
]
