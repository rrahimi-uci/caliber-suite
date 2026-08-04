"""Publisher — promotes an approved draft to the target registry."""

from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any, cast

logger = logging.getLogger(__name__)


class AssistantPublisher:
    """Publishes validated and approved drafts to the appropriate registry.

    Each artifact type has a dedicated ``_publish_<type>`` method.  The
    publisher operates on *validated* ``AssistantDraft`` ORM rows only —
    never on raw model output.
    """

    def publish(
        self,
        *,
        artifact_type: str,
        artifact: dict[str, Any],
        draft_id: str,
        session_factory: Any,
        user: str,
    ) -> dict[str, Any]:
        """Publish ``artifact`` and return a publish report dict."""
        handler = {
            "tool": self._publish_tool,
            "skill": self._publish_skill,
            "prompt": self._publish_prompt,
            "workflow": self._publish_workflow,
            "mcp_server": self._publish_mcp_server,
        }.get(artifact_type)

        if handler is None:
            return {"success": False, "error": f"Unknown artifact type: {artifact_type}"}

        return handler(
            artifact=artifact, draft_id=draft_id, session_factory=session_factory, user=user
        )

    # ------------------------------------------------------------------
    # Per-type publishers
    # ------------------------------------------------------------------

    def _publish_tool(
        self, *, artifact: dict[str, Any], draft_id: str, session_factory: Any, user: str
    ) -> dict[str, Any]:
        from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415
        from caliber.ids import new_tool_id  # noqa: PLC0415

        tool_id = new_tool_id()
        with session_factory() as db:
            tool = CaliberToolRegistry(
                tool_id=tool_id,
                name=artifact.get("name", ""),
                version="1.0.0",
                description=artifact.get("description", ""),
                module_path=artifact.get("module_path", f"assistant.drafts.{draft_id}"),
                callable_name=artifact.get("callable_name", artifact.get("name", "")),
                input_schema=artifact.get("input_schema", {}),
                output_schema=artifact.get("output_schema", {}),
                owner=user,
            )
            db.add(tool)
            db.commit()
        return {"success": True, "registry_id": tool_id, "type": "tool"}

    def _publish_skill(
        self, *, artifact: dict[str, Any], draft_id: str, session_factory: Any, user: str
    ) -> dict[str, Any]:
        del draft_id
        from caliber.db.models import CaliberSkill  # noqa: PLC0415
        from caliber.ids import new_skill_id  # noqa: PLC0415

        skill_id = new_skill_id()
        tags = artifact.get("tags", [])
        skill_metadata = artifact.get("skill_metadata", {})
        depends_on = artifact.get("depends_on", [])
        with session_factory() as db:
            skill = CaliberSkill(
                skill_id=skill_id,
                name=artifact.get("name", ""),
                description=artifact.get("description", ""),
                summary=artifact.get("summary", ""),
                content=artifact.get("prompt", artifact.get("content", "")),
                owner=user,
                category=artifact.get("category", "custom"),
                tags=list(tags) if isinstance(tags, list) else [],
                skill_metadata=dict(skill_metadata) if isinstance(skill_metadata, dict) else {},
                allowed_tools=artifact.get("allowed_tools"),
                depends_on=list(depends_on) if isinstance(depends_on, list) else [],
            )
            db.add(skill)
            db.commit()
        return {"success": True, "registry_id": skill_id, "type": "skill"}

    def _publish_prompt(
        self, *, artifact: dict[str, Any], draft_id: str, session_factory: Any, user: str
    ) -> dict[str, Any]:
        from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

        name = str(artifact.get("name") or "").strip()
        template = str(artifact.get("template") or "").strip()
        if not name:
            return {"success": False, "error": "Prompt name is required"}
        if not template:
            return {"success": False, "error": "Prompt template is required"}

        target_alias = str(artifact.get("target_alias") or "").strip()
        approval_id = str(
            artifact.get("approval_id") or artifact.get("policy_approval_id") or ""
        ).strip()
        tags = {
            "caliber.source": "caliber-assistant",
            "caliber.draft_id": draft_id,
            "caliber.actor": user,
        }
        if approval_id:
            tags["caliber.approval_id"] = approval_id

        try:
            result = prompt_routes.register_prompt_version(
                name=name,
                template=template,
                commit_message=str(
                    artifact.get("commit_message") or "published via CALIBER assistant"
                ),
                tags=tags,
                source="caliber-assistant",
                set_prod_alias=False,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        version_number = result.get("version")
        alias_changed = False
        alias_result: dict[str, Any] | None = None
        rollback_metadata: dict[str, Any] = {
            "available": False,
            "checkpoint_ids": [],
        }
        if target_alias:
            prior_info = None
            load_prompt_info = getattr(prompt_routes, "_load_prompt_release_info", None)
            if callable(load_prompt_info):
                prior_info = load_prompt_info(name, target_alias)
            try:
                from caliber.release_operations import (  # noqa: PLC0415
                    execute_prompt_alias_release,
                    prepare_prompt_alias_release,
                )

                with session_factory() as release_session:
                    operation = prepare_prompt_alias_release(
                        release_session,
                        name=name,
                        alias=target_alias,
                        version_before=(
                            int(prior_info["version"])
                            if isinstance(prior_info, dict)
                            and isinstance(prior_info.get("version"), int)
                            else None
                        ),
                        version_after=int(cast("int | str", version_number)),
                        actor=user,
                        effective_scopes=("operator",),
                        evidence={
                            "gate_state": "none",
                            "source": "assistant_publish",
                            "draft_id": draft_id,
                        },
                        approval_id=approval_id or None,
                    )
                    alias_result = execute_prompt_alias_release(
                        release_session,
                        operation,
                        mutate_alias=prompt_routes.set_prompt_alias_version,
                    )
            except Exception as exc:
                return {
                    "success": False,
                    "error": str(exc),
                    "registry_id": result.get("name", name),
                    "target_version": str(version_number or ""),
                    "version": version_number,
                    "uri": result.get("uri"),
                    "type": "prompt",
                    "alias_changed": False,
                    "target_alias": target_alias,
                    "result": result,
                }
            alias_changed = True
            alias_result["operation_id"] = operation.operation_id
            rollback_metadata = {
                "available": bool(approval_id),
                "approval_id": approval_id or None,
                "strategy": "prompt_alias",
                "checkpoint_ids": [],
                "artifact_ref_before": prior_info.get("artifact_ref")
                if isinstance(prior_info, dict)
                else None,
                "artifact_ref_after": f"prompts:/{name}@{target_alias}",
                "version_before": prior_info.get("version")
                if isinstance(prior_info, dict)
                else None,
                "version_after": version_number,
            }
            checkpoint_id = self._record_prompt_alias_checkpoint(
                session_factory=session_factory,
                approval_id=approval_id,
                name=name,
                target_alias=target_alias,
                prior_info=prior_info,
                version_after=version_number,
            )
            if checkpoint_id:
                rollback_metadata["checkpoint_ids"] = [checkpoint_id]

        return {
            "success": True,
            "registry_id": result.get("name", name),
            "target_version": str(result.get("version") or ""),
            "version": result.get("version"),
            "uri": result.get("uri"),
            "type": "prompt",
            "alias_changed": alias_changed,
            "target_alias": target_alias or None,
            "approval_id": approval_id or None,
            "result": result,
            "alias_result": alias_result,
            "dependency_checks": {
                "passed": True,
                "checks": ["prompt_name", "prompt_template", "prompt_registry"],
            },
            "impact_checks": {
                "passed": True,
                "checks": ["prompt_alias"] if target_alias else [],
            },
            "rollback_metadata": rollback_metadata,
        }

    def _record_prompt_alias_checkpoint(
        self,
        *,
        session_factory: Any,
        approval_id: str,
        name: str,
        target_alias: str,
        prior_info: dict[str, Any] | None,
        version_after: Any,
    ) -> str | None:
        if not approval_id:
            return None

        from caliber.db.models import (  # noqa: PLC0415
            CaliberApprovalRequest,
            CaliberRollbackCheckpoint,
        )
        from caliber.ids import new_checkpoint_id  # noqa: PLC0415

        try:
            clean_version_after = int(version_after)
        except Exception:
            clean_version_after = None

        version_before_raw = prior_info.get("version") if isinstance(prior_info, dict) else None
        try:
            version_before = int(version_before_raw) if version_before_raw is not None else None
        except Exception:
            version_before = None

        with session_factory() as db:
            approval = db.get(CaliberApprovalRequest, approval_id)
            if approval is None:
                return None
            checkpoint = CaliberRollbackCheckpoint(
                checkpoint_id=new_checkpoint_id(),
                approval_id=approval.approval_id,
                agent_id=approval.agent_id,
                artifact_type="prompt",
                artifact_name=name,
                artifact_ref_before=prior_info.get("artifact_ref")
                if isinstance(prior_info, dict)
                else None,
                artifact_ref_after=f"prompts:/{name}@{target_alias}",
                version_before=version_before,
                version_after=clean_version_after,
                snapshot_payload={
                    "source": "caliber-assistant",
                    "promotion_type": "prompt_alias",
                    "target_alias": target_alias,
                    "prompt_name": name,
                },
            )
            db.add(checkpoint)
            db.commit()
            return checkpoint.checkpoint_id

    def _publish_workflow(
        self, *, artifact: dict[str, Any], draft_id: str, session_factory: Any, user: str
    ) -> dict[str, Any]:
        del draft_id
        from caliber.db.models import CaliberWorkflow, CaliberWorkflowVersion  # noqa: PLC0415
        from caliber.ids import new_workflow_id, new_workflow_version_id  # noqa: PLC0415
        from caliber.workflows.compiler import COMPILER_VERSION  # noqa: PLC0415
        from caliber.workflows.manifest import compute_manifest_hash  # noqa: PLC0415

        manifest = copy.deepcopy(artifact.get("manifest", {}))
        if not isinstance(manifest, dict):
            return {"success": False, "error": "Workflow manifest must be an object"}
        wf_id = str(artifact.get("workflow_id") or manifest.get("workflow_id") or new_workflow_id())
        manifest["workflow_id"] = wf_id
        if artifact.get("name"):
            manifest.setdefault("name", artifact.get("name"))
        if artifact.get("description"):
            manifest.setdefault("description", artifact.get("description"))
        ver_id = new_workflow_version_id()
        with session_factory() as db:
            if db.get(CaliberWorkflow, wf_id) is not None:
                return {"success": False, "error": f"Workflow id {wf_id!r} already exists"}
            wf = CaliberWorkflow(
                workflow_id=wf_id,
                name=artifact.get("name", ""),
                description=artifact.get("description", ""),
                owner=user,
                default_experiment_id=artifact.get("default_experiment_id"),
            )
            db.add(wf)
            db.flush()
            ver = CaliberWorkflowVersion(
                version_id=ver_id,
                workflow_id=wf_id,
                version_number=1,
                manifest=manifest,
                manifest_hash=compute_manifest_hash(manifest),
                compiler_version=COMPILER_VERSION,
                validation_report=artifact.get("validation_report"),
                status="published",
                created_by=user,
                published_by=user,
                published_at=datetime.now(timezone.utc),
            )
            db.add(ver)
            db.commit()
        return {"success": True, "registry_id": wf_id, "version_id": ver_id, "type": "workflow"}

    def _publish_mcp_server(
        self, *, artifact: dict[str, Any], draft_id: str, session_factory: Any, user: str
    ) -> dict[str, Any]:
        del draft_id
        from sqlalchemy import select  # noqa: PLC0415

        from caliber.db.models import CaliberMcpServer  # noqa: PLC0415
        from caliber.ids import new_mcp_server_id  # noqa: PLC0415

        name = artifact.get("name", "")
        server_id = new_mcp_server_id()
        with session_factory() as db:
            existing = (
                db.execute(select(CaliberMcpServer).where(CaliberMcpServer.name == name))
                .scalars()
                .first()
            )
            if existing is not None:
                return {"success": False, "error": f"MCP server {name!r} already registered"}
            server = CaliberMcpServer(
                server_id=server_id,
                name=name,
                description=artifact.get("description", ""),
                transport=artifact.get("transport", "stdio"),
                uri=artifact.get("uri", ""),
                command=artifact.get("command", ""),
                args=artifact.get("args", []),
                env=artifact.get("env", {}),
                headers=artifact.get("headers", {}),
                auth_type=artifact.get("auth_type", "none"),
                auth_config=artifact.get("auth_config", {}),
                discovered_tools=list(artifact.get("discovered_tools", []))
                if isinstance(artifact.get("discovered_tools"), list)
                else [],
                icon=artifact.get("icon", ""),
                owner=user,
            )
            db.add(server)
            db.commit()
        return {"success": True, "registry_id": server_id, "type": "mcp_server"}
