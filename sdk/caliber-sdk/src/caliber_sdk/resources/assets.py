"""Prompts, skills, and tools — the governed asset families.

These are the surfaces the Cookbooks build on, so the methods here follow the
recipes rather than the routes: create, test, version, calibrate.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..models._decode import decode, decode_list
from ..models.assets import (
    Agent,
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

    def rollback(self, name: str, *, alias: str = "prod") -> Any:
        """Rotate the alias back to the version that was live before the
        current one, read from the promotion audit trail.

        Raises :class:`~caliber_sdk.CaliberConflictError` (409) when there is
        no recorded prior live version to restore.
        """
        return self._post(f"/prompts/{name}/rollback", json={"alias": alias})

    def set_baseline(self, name: str, *, test_run_id: str) -> Any:
        """Pin a prompt test run as the comparison baseline for future runs."""
        return self._post(f"/prompts/{name}/baseline", json={"test_run_id": test_run_id})

    def bind(self, name: str, *, kind: str, **params: Any) -> Any:
        """Record where a prompt is wired in.

        ``kind`` is ``"agent"`` (requires ``agent_id=``), ``"workflow_node"``
        (requires ``workflow_id=`` and ``node_id=``), or ``"standalone"``.
        """
        return self._post(f"/prompts/{name}/bind", json={"kind": kind, **params})

    def delete(self, name: str) -> Any:
        """Delete a prompt registry entry and its CALIBER-side records."""
        return self._delete(f"/prompts/{name}")

    def version(self, name: str, version: int) -> Any:
        """Load the full template for one specific registry version."""
        return self._get(f"/prompts/{name}/versions/{version}")

    def workspace(self, name: str) -> Any:
        """Runtime facts + computed lifecycle status (Bound > Calibrated >
        Tested > Has test set > Draft) for the Prompts-tab workspace view."""
        return self._get(f"/prompts/{name}/workspace")

    def test_render(self, agent_id: str, *, variables: dict[str, Any] | None = None) -> Any:
        """Render a deployed prompt template with caller-supplied variables."""
        return self._post(f"/prompts/{agent_id}/test-render", json={"variables": variables or {}})

    def template_library(self) -> Any:
        """The prompt-builder catalog (base templates + modifiers) used by
        the Create Prompt flow."""
        return self._get("/prompts/template-library")

    def preview_template(self, *, base_template_id: str, **params: Any) -> Any:
        """Compile a prompt-builder recipe into a single prompt + validation
        report, without creating anything. ``params`` may carry
        ``modifier_ids``, ``builder_values``, ``preview_variables``,
        ``runtime_variables``, ``template_override``, ``section_overrides``.
        """
        return self._post(
            "/prompts/template-library/preview",
            json={"base_template_id": base_template_id, **params},
        )

    def calibration_options(self) -> Any:
        """Optimizer/scorer capabilities for a manual calibration run."""
        return self._get("/prompts/calibration/options")

    def optimization_options(self) -> Any:
        """Alias of :meth:`calibration_options` -- the server backs both
        URLs with the same handler; both are modelled so a caller reaching
        for either name finds it."""
        return self._get("/prompts/optimization/options")

    def create_calibration_run(self, **payload: Any) -> Any:
        """Queue a manual prompt calibration run. ``payload`` requires
        ``agent_id``, ``eval_dataset_id``, ``optimizer_type``, and
        ``scorers``; see :meth:`calibration_options` for what's available."""
        return self._post("/prompts/calibration/runs", json=payload)

    def create_optimization_run(self, **payload: Any) -> Any:
        """Alias of :meth:`create_calibration_run` -- same handler, the
        other URL."""
        return self._post("/prompts/optimization/runs", json=payload)

    def create_test_run(
        self, *, agent_id: str, results: Sequence[dict[str, Any]], **params: Any
    ) -> Any:
        """Persist a completed prompt-test run. ``results`` is the per-case
        list; the server recomputes pass/fail/partial counts and the overall
        score from it rather than trusting client-supplied aggregates."""
        return self._post(
            "/prompts/test-runs",
            json={"agent_id": agent_id, "results": list(results), **params},
        )

    def test_runs(self, **params: Any) -> Any:
        """Run history summaries, newest first."""
        return self._get("/prompts/test-runs", params=params or None)

    def test_run(self, test_run_id: str) -> Any:
        """One run's full per-case results."""
        return self._get(f"/prompts/test-runs/{test_run_id}")


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

    def rollback(self, skill_id: str) -> Any:
        """Restore the immediately-prior content snapshot as a **new**
        version (skills are forward-only; this never rewrites history).
        Raises :class:`~caliber_sdk.CaliberConflictError` (409) when there is
        no earlier version to restore.
        """
        return self._post(f"/skills/{skill_id}/rollback")

    def set_baseline(self, skill_id: str, *, test_run_id: str) -> Any:
        """Pin a skill test run as the comparison baseline for future runs."""
        return self._post(f"/skills/{skill_id}/baseline", json={"test_run_id": test_run_id})

    def bind(self, skill_id: str, *, kind: str, **params: Any) -> Any:
        """Record where a skill is wired in.

        ``kind`` is ``"agent"`` (requires ``agent_id=``, adds the skill's name
        to that agent's referenced skills), ``"workflow_node"``, or
        ``"standalone"``.
        """
        return self._post(f"/skills/{skill_id}/bind", json={"kind": kind, **params})

    def calibrate(self, skill_id: str, **options: Any) -> Any:
        """Agent-free calibration front door: queues a refinement job against
        a hidden, auto-provisioned target rather than requiring an operator
        to pick an agent first. ``options`` may carry ``optimizer_type`` and
        ``notes``. Distinct from :meth:`ToolsAPI.calibrate` -- a skill
        calibration produces a verification item and refinement job, not a
        standalone :class:`~caliber_sdk.models.CalibrationJob`.
        """
        return self._post(f"/skills/{skill_id}/calibrate", json=options)

    def workspace(self, skill_id: str) -> Any:
        """Runtime facts for the Skills-tab workspace view."""
        return self._get(f"/skills/{skill_id}/workspace")

    def package(self, skill_id: str) -> Any:
        """Preview the OpenAI-compatible package (``SKILL.md`` +
        ``agents/openai.yaml`` + bundled resources) generated for a skill.
        Read-only; malformed resource metadata surfaces as ``warnings``
        rather than failing, so the preview still shows what *can* be
        generated."""
        return self._get(f"/skills/{skill_id}/package")

    def package_zip(self, skill_id: str) -> bytes:
        """Download the generated package as a ZIP archive."""
        return self._transport.download(f"/skills/{skill_id}/package.zip")

    def import_package(
        self,
        files: Sequence[dict[str, Any]],
        *,
        owner: str,
        **options: Any,
    ) -> Any:
        """Create a skill from an OpenAI-style package.

        ``files`` is a list of ``{"path": ..., "content": ...}`` objects (one
        ``SKILL.md`` with kebab-case ``name`` frontmatter, resources only
        under ``scripts/``/``references/``/``assets/``). ``options`` may
        carry ``category``, ``tags``, ``skill_metadata``, ``allowed_tools``,
        ``depends_on`` -- caller ``skill_metadata`` is merged *over* the
        parsed package metadata, never replacing it.
        """
        return self._post(
            "/skills/import-package",
            json={"files": list(files), "owner": owner, **options},
        )

    def import_package_zip(
        self,
        filename: str,
        content: bytes,
        *,
        conflict_strategy: str = "reject",
        rename_to: str | None = None,
    ) -> Any:
        """Import a ZIP package directly. Multipart, so it does not go
        through the JSON path.

        ``conflict_strategy`` is ``"reject"`` (default), ``"rename"``
        (requires ``rename_to``, a kebab-case name), or ``"merge"``
        (admin-only, forward-versioned update of an existing skill).
        """
        files = {"file": (filename, content, "application/zip")}
        data: dict[str, str] = {"conflict_strategy": conflict_strategy}
        if rename_to is not None:
            data["rename_to"] = rename_to
        response = self._transport.request(
            "POST", "/skills/import-package.zip", files=files, data=data
        )
        return response.data

    def create_test_run(
        self, *, skill_id: str, results: Sequence[dict[str, Any]], **params: Any
    ) -> Any:
        """Persist a completed skill-test run. ``results`` is the per-case
        list; the server recomputes pass/fail/partial counts and the overall
        score from it rather than trusting client-supplied aggregates."""
        return self._post(
            "/skills/test-runs",
            json={"skill_id": skill_id, "results": list(results), **params},
        )

    def test_runs(self, **params: Any) -> Any:
        """Run history summaries, newest first."""
        return self._get("/skills/test-runs", params=params or None)

    def test_run(self, test_run_id: str) -> Any:
        """One run's full per-case results."""
        return self._get(f"/skills/test-runs/{test_run_id}")


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
        """Score every saved test case now and return the aggregate.

        **Synchronous** -- this call blocks until all cases finish (the
        server's own docstring for this route calls it "score saved test
        cases", not a queue). The response has no ``job_id``/``status``, so
        those fields decode to their defaults; ``pass_rate`` is real, and
        ``total``/``passed``/``cases``/``ran_at`` land in ``.extra``. For
        two hundred cases this holds a connection open for minutes -- use
        :meth:`submit_calibration_job` + :meth:`wait_for_calibration` for the
        durable, poll-instead-of-block form.
        """
        return decode(CalibrationJob, self._post(f"/tools/{tool_id}/calibrate", json=options))

    def submit_calibration_job(self, tool_id: str) -> CalibrationJob:
        """Queue a calibration run against the tool's saved test cases.

        Returns immediately (202) with a real ``job_id`` to poll via
        :meth:`calibration_job` or :meth:`wait_for_calibration` -- the
        durable counterpart to :meth:`calibrate`, for a case count large
        enough that holding a connection open is the wrong trade.
        """
        return decode(CalibrationJob, self._post(f"/tools/{tool_id}/calibration-jobs"))

    def resolve_calibration_job(
        self, tool_id: str, job_id: str, *, action: str, reason: str
    ) -> Any:
        """Abandon or explicitly retry an ambiguously ``running`` job.

        ``action`` is ``"abandon"`` or ``"retry"``; ``reason`` is required
        (a non-empty resolution reason). Automatic requeue is unsafe because
        an authored tool may have side effects -- this is the operator
        decision that a stuck job needs.
        """
        return self._post(
            f"/tools/{tool_id}/calibration-jobs/{job_id}/resolve",
            json={"action": action, "reason": reason},
        )

    def calibration_job(self, tool_id: str, job_id: str) -> CalibrationJob:
        return decode(CalibrationJob, self._get(f"/tools/{tool_id}/calibration-jobs/{job_id}"))

    def calibration_jobs(self, tool_id: str) -> _List[CalibrationJob]:
        payload = self._get(f"/tools/{tool_id}/calibration-jobs")
        items = payload.get("jobs") if isinstance(payload, dict) else None
        return decode_list(CalibrationJob, items)

    def wait_for_calibration(
        self, tool_id: str, job_id: str, *, timeout: float = 600.0, **options: Any
    ) -> CalibrationJob:
        """Poll a calibration job (from :meth:`submit_calibration_job`) until
        it stops.

        Returns the terminal job rather than raising on failure: a failed
        calibration is a result to inspect, not an error in the call.
        """
        return wait_for(
            lambda: self.calibration_job(tool_id, job_id),
            is_done=lambda job: job.is_terminal,
            timeout=timeout,
            **options,
        )

    def archive(self, tool_id: str) -> Tool:
        """Retire a tool. Refuses (409) while an active workflow deployment
        still references it -- undeploy first."""
        return decode(Tool, self._post(f"/tools/{tool_id}/archive"))

    def set_baseline(self, tool_id: str, *, test_run_id: str) -> Any:
        """Pin a persisted tool-test run as the comparison baseline. The run
        must belong to this tool."""
        return self._post(f"/tools/{tool_id}/baseline", json={"test_run_id": test_run_id})

    def source(self, tool_id: str) -> Any:
        """The tool callable's real source, signature, and docstring.
        ``available=False`` (with an ``error``) for a non-Python-callable
        execution backend -- there is no source to show."""
        return self._get(f"/tools/{tool_id}/source")

    def usage(self, tool_id: str) -> Any:
        """Workflow versions that reference this tool, scoped to the caller's
        visible workflows. Meant to warn before deprecate/archive."""
        return self._get(f"/tools/{tool_id}/usage")

    def versions(self, tool_id: str) -> _List[Tool]:
        """Every version in this tool's family (same ``name``), newest
        first. Tools have no live alias to promote/roll back -- this is a
        read-only history, not a release surface."""
        return decode_list(Tool, self._get(f"/tools/{tool_id}/versions"))

    def workspace(self, tool_id: str) -> Any:
        """Runtime facts + computed lifecycle status (Published > Hardened >
        Tested > Has fixtures > Draft) for the Tools-tab workspace view."""
        return self._get(f"/tools/{tool_id}/workspace")

    def save_test_cases(self, tool_id: str, test_cases: Sequence[dict[str, Any]]) -> Any:
        """Persist the saved fixture set a calibration run scores against.
        Replaces the whole set (not a merge)."""
        return self._put(f"/tools/{tool_id}/test-cases", json={"test_cases": list(test_cases)})

    def test_invoke(self, tool_id: str, *, input: dict[str, Any] | None = None) -> Any:
        """Invoke the tool once under preview effect policy: ``write``/
        ``external_action`` tools are always mocked; ``read`` tools run live
        only when ``allow_in_preview`` is set. Not durable -- for a recorded
        run, see :meth:`create_test_run`.
        """
        return self._post(f"/tools/{tool_id}/test-run", json={"input": input or {}})

    def create_test_run(
        self, *, tool_id: str, results: Sequence[dict[str, Any]], **params: Any
    ) -> Any:
        """Persist a completed tool-test run. ``results`` is the per-case
        list; the server recomputes pass/fail/partial counts and the overall
        score from it rather than trusting client-supplied aggregates."""
        return self._post(
            "/tools/test-runs",
            json={"tool_id": tool_id, "results": list(results), **params},
        )

    def test_runs(self, **params: Any) -> Any:
        """Run history summaries, newest first."""
        return self._get("/tools/test-runs", params=params or None)

    def test_run(self, test_run_id: str) -> Any:
        """One run's full per-case results."""
        return self._get(f"/tools/test-runs/{test_run_id}")


class AgentsAPI(Resource):
    """The agent record — the anchor a verification item, refinement job,
    approval, and rollback checkpoint all hang off of."""

    def list(self) -> list[Agent]:
        return decode_list(Agent, self._get("/agents"))

    def get(self, agent_id: str) -> Agent:
        return decode(Agent, self._get(f"/agents/{agent_id}"))

    def create(
        self,
        agent_id: str,
        *,
        experiment_id: str,
        name: str,
        **options: Any,
    ) -> Agent:
        """Register a new agent. ``agent_id`` and ``experiment_id`` are both
        one-shot: identity is fixed at registration (re-keying means delete +
        re-create, so the audit trail stays clean), and a re-used
        ``experiment_id`` is a 409 (unique per agent).
        """
        body: dict[str, Any] = {
            "agent_id": agent_id,
            "experiment_id": experiment_id,
            "name": name,
            **options,
        }
        return decode(Agent, self._post("/agents", json=body))

    def update(self, agent_id: str, **changes: Any) -> Agent:
        """Partial update. ``enabled=False`` is the pause lever the refinement
        worker reads before claiming a queued job for this agent."""
        return decode(Agent, self._patch(f"/agents/{agent_id}", json=changes))

    def delete(self, agent_id: str) -> bool:
        """Remove an agent and cascade its dependent verification/refinement/
        approval/checkpoint/regression rows in one transaction."""
        payload = self._delete(f"/agents/{agent_id}")
        return isinstance(payload, dict) and bool(payload.get("deleted"))

    def skills(self, agent_id: str) -> Any:
        """Skills this agent references in its ``optimizer_config``, plus any
        cited name that didn't resolve (``missing``) -- e.g. an archived or
        renamed skill an agent still points at. Left untyped: it nests
        ``Skill``-shaped records inside a response envelope this SDK doesn't
        otherwise model, and the two-key shape is simple enough to read off
        the dict directly.
        """
        return self._get(f"/agents/{agent_id}/skills")

    def experiment(self, agent_id: str) -> Any:
        """Whether this agent's configured MLflow experiment is actually
        reachable. Left untyped: the server itself keeps this shape open
        (``ExperimentBindingSchema`` allows extra fields) because it reflects
        whatever MLflow reports, not a fixed CALIBER contract.
        """
        return self._get(f"/agents/{agent_id}/experiment")

    def checkpoints(self, agent_id: str) -> Any:
        """Rollback checkpoints for this agent, newest first.

        Left untyped: the checkpoint shape (``RollbackCheckpointSchema``) is
        the promoter's internal record, not a governed contract, and every
        artifact type serializes its own version fields into it.
        """
        return self._get(f"/agents/{agent_id}/checkpoints")

    def rollback(self, agent_id: str, *, checkpoint_id: str | None = None) -> Any:
        """Roll this agent's live artifact back to a prior version.

        Without ``checkpoint_id``, rolls back to the most recent unused
        checkpoint — the "undo the last promotion" affordance. Raises
        :class:`~caliber_sdk.CaliberConflictError` (409) if the checkpoint
        was already rolled back or claimed by a concurrent call.
        """
        body: dict[str, Any] = {}
        if checkpoint_id is not None:
            body["checkpoint_id"] = checkpoint_id
        return self._post(f"/agents/{agent_id}/rollback", json=body)


__all__ = ["AgentsAPI", "PromptsAPI", "SkillsAPI", "ToolsAPI"]
