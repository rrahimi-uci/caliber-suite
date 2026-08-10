"""Prompts, skills, and tools — the governed asset families.

These are the surfaces the Cookbooks build on, so the methods here follow the
recipes rather than the routes: create, test, version, calibrate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models._decode import decode, decode_list
from ..models.assets import (
    CalibrationJob,
    Prompt,
    Skill,
    SkillRender,
    SkillSelection,
    SkillVersion,
    Tool,
)
from ..waiters import wait_for
from ._base import Resource

#: Every resource class defines a ``list()`` method, and a class body resolves
#: annotations in order -- so any annotation written *after* that method reads
#: ``list[X]`` as the method rather than the builtin. Aliasing the builtin here
#: keeps the natural API name without the collision.
_List = list


class PromptsAPI(Resource):
    """Prompt registry surfaces.

    Prompts are MLflow registry objects that CALIBER governs. Versions are
    immutable and an alias points at one of them, so "update a prompt" is
    always "register a new version", never an edit in place.
    """

    def list(self) -> list[Prompt]:
        return decode_list(Prompt, self._get("/prompts"))

    def get(self, agent_id: str) -> Prompt:
        return decode(Prompt, self._get(f"/prompts/{agent_id}"))

    def create(self, name: str, template: str, *, commit_message: str | None = None) -> Any:
        """Register a prompt and its first version."""
        body: dict[str, Any] = {"name": name, "template": template}
        if commit_message is not None:
            body["commit_message"] = commit_message
        return self._post("/prompts", json=body)

    def versions(self, agent_id: str) -> Any:
        """Every registered version, newest first."""
        return self._get(f"/prompts/{agent_id}/versions")

    def register_version(
        self, agent_id: str, template: str, *, commit_message: str | None = None
    ) -> Any:
        """Add a version without touching the live alias.

        The alias is rotated separately by :meth:`promote`, so authoring is
        never a deployment — the property the whole refinement loop depends on.
        """
        body: dict[str, Any] = {"template": template}
        if commit_message is not None:
            body["commit_message"] = commit_message
        return self._post(f"/prompts/{agent_id}/versions", json=body)

    def promote(self, agent_id: str, version: int, *, alias: str = "prod") -> Any:
        """Point an alias at a version. This is the deployment step."""
        return self._post(f"/prompts/{agent_id}/aliases/{alias}", json={"version": version})


class SkillsAPI(Resource):
    """Skill registry, rendering, selection testing, and versions."""

    def list(self, *, status: str | None = None, tag: str | None = None) -> list[Skill]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if tag:
            params["tag"] = tag
        return decode_list(Skill, self._get("/skills", params=params or None))

    def get(self, skill_id: str) -> Skill:
        return decode(Skill, self._get(f"/skills/{skill_id}"))

    def create(
        self,
        name: str,
        *,
        content: str,
        owner: str,
        summary: str | None = None,
        description: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> Skill:
        """Create a skill.

        ``owner`` is required by the server and is therefore keyword-required
        here rather than defaulted to the caller's identity: a skill's owner is
        a governance field, and quietly inferring it would make authorship an
        accident of which credential happened to run the script.
        """
        body: dict[str, Any] = {"name": name, "content": content, "owner": owner}
        for key, value in (("summary", summary), ("description", description)):
            if value is not None:
                body[key] = value
        if tags is not None:
            body["tags"] = list(tags)
        return decode(Skill, self._post("/skills", json=body))

    def update(self, skill_id: str, **changes: Any) -> Skill:
        return decode(Skill, self._patch(f"/skills/{skill_id}", json=changes))

    def render(self, skill_id: str, *, variables: dict[str, Any] | None = None) -> SkillRender:
        """Substitute ``{{variables}}`` and report what was left unresolved."""
        return decode(
            SkillRender,
            self._post(f"/skills/{skill_id}/test-render", json={"variables": variables or {}}),
        )

    def test_selection(self, skill_id: str, query: str) -> SkillSelection:
        """Would this skill be auto-selected for this query?"""
        return decode(
            SkillSelection,
            self._post(f"/skills/{skill_id}/test-selection", json={"query": query}),
        )

    def versions(self, skill_id: str) -> _List[SkillVersion]:
        return decode_list(SkillVersion, self._get(f"/skills/{skill_id}/versions"))


class ToolsAPI(Resource):
    """Tool registry, fixtures, and calibration."""

    def list(self, *, status: str | None = None) -> list[Tool]:
        params = {"status": status} if status else None
        return decode_list(Tool, self._get("/tools", params=params))

    def get(self, tool_id: str) -> Tool:
        return decode(Tool, self._get(f"/tools/{tool_id}"))

    def register(
        self,
        name: str,
        *,
        version: str,
        module_path: str,
        callable_name: str,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        **options: Any,
    ) -> Tool:
        body: dict[str, Any] = {
            "name": name,
            # Required by the server: a tool without a version cannot be
            # referenced by a workflow that pins one.
            "version": version,
            "module_path": module_path,
            "callable_name": callable_name,
            "input_schema": input_schema or {},
            "output_schema": output_schema or {},
            **options,
        }
        return decode(Tool, self._post("/tools", json=body))

    def update(self, tool_id: str, **changes: Any) -> Tool:
        return decode(Tool, self._patch(f"/tools/{tool_id}", json=changes))

    def calibrate(self, tool_id: str, **options: Any) -> CalibrationJob:
        """Queue a calibration run. Returns immediately with a job to poll."""
        return decode(CalibrationJob, self._post(f"/tools/{tool_id}/calibrate", json=options))

    def calibration_job(self, tool_id: str, job_id: str) -> CalibrationJob:
        return decode(CalibrationJob, self._get(f"/tools/{tool_id}/calibration-jobs/{job_id}"))

    def calibration_jobs(self, tool_id: str) -> _List[CalibrationJob]:
        payload = self._get(f"/tools/{tool_id}/calibration-jobs")
        items = payload.get("jobs") if isinstance(payload, dict) else None
        return decode_list(CalibrationJob, items)

    def wait_for_calibration(
        self, tool_id: str, job_id: str, *, timeout: float = 600.0, **options: Any
    ) -> CalibrationJob:
        """Poll a calibration job until it stops.

        Returns the terminal job rather than raising on failure: a failed
        calibration is a result to inspect, not an error in the call.
        """
        return wait_for(
            lambda: self.calibration_job(tool_id, job_id),
            is_done=lambda job: job.is_terminal,
            timeout=timeout,
            **options,
        )


__all__ = ["PromptsAPI", "SkillsAPI", "ToolsAPI"]
