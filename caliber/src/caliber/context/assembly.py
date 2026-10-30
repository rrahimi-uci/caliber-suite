"""Record an agent step's context before the provider sees it (B07.3b, #710).

The runtime hands :func:`context_recorder_for`'s callable the :class:`~caliber.workflows.runtime.AgentContextAssembly` of
every agent step (and every handoff target) *before* any of it reaches the provider. The recorder turns it into a
:mod:`caliber.context.manifest` ``ContextManifest``, validates it, stores it once (its own short transaction,
committed) and returns; if it cannot, it raises :class:`~caliber.context.authorize.ContextDenied` and the step does not
run, so a provider is never given context that has no valid, authorized record.

How the assembly is labelled:

* the agent's instructions (as resolved when the step is assembled: a registry prompt is loaded, not just named) and its
  skills are ``workflow_instruction`` segments: the only ``instruction`` tier;
* the history is one ``history`` data segment;
* the input is the one the provider is sent, as data. Text a knowledge query's edge delivered straight to the agent is
  ``retrieved_text`` (one segment per version it read, citing the ``context.read`` decision
  :class:`~caliber.context.authorize.SourceGuard` recorded when it authorized that version; a query whose versions the
  run never authorized is refused); the run's own input is ``user_input``; any other text, including text a knowledge
  query's output reached only through another node (a transform, a ``for_each`` item, ...), is ``tool_output`` of the
  node that delivered it or of unknown origin, which is still data.

So text a source contains, however it is worded, is recorded as data and can never become an instruction. Provider
metadata is included as digest-bound segments: tool specifications and the structured-output suffix are recorded before
the provider call, tool results are recorded as a new immutable revision after the call, and knowledge-query answer
synthesis uses the same recorder before its provider call. Token counts here are an
accounting estimate (four characters a token, and the recorded budget is the larger of the configured one and the
total); enforcing a budget on a provider call is B08.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.context.authorize import ContextDenied, SourceGuard
from caliber.context.manifest import (
    ContextManifestError,
    ContextSegment,
    build_context_manifest,
    next_revision,
    store_context_manifest,
)
from caliber.context.rules import rule_bundle_reference
from caliber.db.models import CaliberKnowledgeBaseVersion
from caliber.policy.grants import AUTHORITY_UNAVAILABLE
from caliber.run_manifest import DeploymentScope
from caliber.workflows.runtime import AgentContextAssembly, AgentContextInput

DEFAULT_TOKEN_BUDGET: Final = 1_000_000


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _step_request_id(run_id: str, assembly: AgentContextAssembly) -> str:
    occurrence = assembly.occurrence_id or assembly.node_id
    raw = f"{run_id}:{assembly.node_id}:{occurrence}:{assembly.attempt}"
    return "step-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _instruction_segments(assembly: AgentContextAssembly) -> list[ContextSegment]:
    segments: list[ContextSegment] = []
    prompt = assembly.instructions
    if prompt is not None:
        text = assembly.instruction_text
        reference = prompt.mlflow_uri or f"inline:{assembly.node_id}"
        segments.append(
            ContextSegment(
                "instruction",
                "workflow_instruction",
                {"kind": "prompt", "reference": reference},
                text,
                _tokens(text),
            )
        )
    for index, skill in enumerate(assembly.skill_instructions):
        segments.append(
            ContextSegment(
                "instruction",
                "workflow_instruction",
                {"kind": "prompt", "reference": f"skill:{assembly.node_id}:{index}"},
                skill,
                _tokens(skill),
            )
        )
    if assembly.structured_output_suffix:
        segments.append(
            ContextSegment(
                "instruction",
                "structured_output_suffix",
                {"kind": "prompt", "reference": f"structured-output:{assembly.node_id}"},
                assembly.structured_output_suffix,
                _tokens(assembly.structured_output_suffix),
            )
        )
    return segments


def _provider_segments(assembly: AgentContextAssembly) -> list[ContextSegment]:
    segments: list[ContextSegment] = []
    for index, spec in enumerate(assembly.tool_specs):
        if not isinstance(spec, dict):
            raise ContextDenied("manifest_invalid")
        function = spec.get("function")
        name = spec.get("name")
        if isinstance(function, dict):
            name = function.get("name", name)
        reference = str(name or f"tool-{index}")
        text = _json_text(spec)
        segments.append(
            ContextSegment(
                "data",
                "tool_spec",
                {"kind": "tool", "reference": reference},
                text,
                _tokens(text),
            )
        )
    for result in assembly.tool_results:
        if not isinstance(result, dict):
            raise ContextDenied("manifest_invalid")
        reference = str(result.get("tool") or result.get("name") or "tool-result")
        text = _json_text(result)
        segments.append(
            ContextSegment(
                "data",
                "tool_output",
                {"kind": "tool", "reference": reference},
                text,
                _tokens(text),
            )
        )
    return segments


def _memory_segments(assembly: AgentContextAssembly) -> list[ContextSegment]:
    if not assembly.memory_history:
        return []
    if not assembly.memory_reference or not assembly.memory_authorization_decision_id:
        raise ContextDenied("authorization_rejected")
    text = _json_text(list(assembly.memory_history))
    return [
        ContextSegment(
            "data",
            "memory",
            {"kind": "memory", "reference": assembly.memory_reference},
            text,
            _tokens(text),
            authorization_decision_id=assembly.memory_authorization_decision_id,
        )
    ]


def _input_segments(
    session: Session, item: AgentContextInput, guard: SourceGuard | None, node_id: str
) -> list[ContextSegment]:
    # Without a guard the run authorized no source, so a query's text is not labelled as retrieved from one: it is the
    # output of the node that delivered it, which is still data.
    if item.origin_node_type == "knowledge_query" and guard is not None:
        result = item.origin_result or {}
        embedded_provenance = result.get("context_provenance")
        decisions_by_version: dict[str, str] = {}
        if isinstance(embedded_provenance, list):
            for reference in embedded_provenance:
                if not isinstance(reference, dict):
                    continue
                source = reference.get("source")
                decision_id = reference.get("authorization_decision_id")
                if (
                    isinstance(source, dict)
                    and source.get("kind") == "knowledge_base"
                    and isinstance(source.get("version_id"), str)
                    and isinstance(decision_id, str)
                ):
                    decisions_by_version[source["version_id"]] = decision_id
        # A query that does not publish its ``result`` still read the versions the guard authorized for this run.
        versions = list(decisions_by_version) or [
            str(v) for v in result.get("resolved_version_ids") or []
        ]
        if not versions:
            versions = list(guard.decisions)
        if not versions:
            raise ContextDenied(
                "manifest_invalid"
            )  # retrieved text with no known source is refused
        first = (result.get("versions") or [{}])[0]
        mode = str((first if isinstance(first, dict) else {}).get("retrieval_mode") or "query")
        segments = []
        for position, version_id in enumerate(versions):
            row = session.get(CaliberKnowledgeBaseVersion, version_id)
            if row is None:
                raise ContextDenied("manifest_invalid")
            decision_id = decisions_by_version.get(version_id) or guard.decisions.get(version_id)
            if not decision_id:
                raise ContextDenied("authorization_rejected")
            segments.append(
                ContextSegment(
                    "data",
                    "retrieved_text",
                    {
                        "kind": "knowledge_base",
                        "knowledge_base_id": row.knowledge_base_id,
                        "version_id": version_id,
                    },
                    item.text,
                    _tokens(item.text) if position == 0 else 0,  # one text, counted once
                    retrieval={"mode": mode},
                    authorization_decision_id=decision_id,
                )
            )
        return segments
    if item.origin_node_type == "start":
        return [
            ContextSegment("data", "user_input", {"kind": "input"}, item.text, _tokens(item.text))
        ]
    return [
        ContextSegment(
            "data",
            "tool_output",
            {"kind": "tool", "reference": item.origin_node_id or f"inline:{node_id}"},
            item.text,
            _tokens(item.text),
            transform=item.transform,
            authorization_decision_ids=item.authorization_decision_ids,
        )
    ]


def context_recorder_for(
    session_factory: Callable[[], Session],
    run_manifest: dict[str, Any],
    guard: SourceGuard | None,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> Callable[[AgentContextAssembly], str]:
    """The recorder the worker hands ``build_plan`` for one admitted run (see the module docstring).

    ``guard`` is ``None`` for a run admitted without context sources whose manifest pins a rule bundle: its steps are
    still recorded (with the verified bundle cited) but no knowledge-base text is labelled as retrieved.
    """
    run_id = str(run_manifest["workflow_run_id"])
    project_id = run_manifest.get("project_id")

    def record(assembly: AgentContextAssembly) -> str:
        if assembly.step_request_id:
            step_request_id = assembly.step_request_id
        elif assembly.occurrence_id is None:
            # Direct callers from the pre-linkage API did not provide a logical
            # occurrence. Preserve their independent-record behavior; the live
            # runtime always supplies one and therefore gets deterministic IDs.
            step_request_id = f"{assembly.node_id[:90]}-{uuid.uuid4().hex[:24]}"
        else:
            step_request_id = _step_request_id(run_id, assembly)
        try:
            # The pinned rule bundle's bytes are verified against the admitted digest now, at use, for every step.
            verified = rule_bundle_reference(run_manifest)
            rule_bundle = verified.record if verified is not None else None
        except ContextManifestError as exc:
            raise ContextDenied(exc.code, message=str(exc)) from None
        try:
            with session_factory() as session:
                segments = _instruction_segments(assembly)
                segments += _memory_segments(assembly)
                if (
                    assembly.history
                ):  # one segment: a history of any length stays within the manifest's bounds
                    transcript = "\n".join(
                        f"{m.get('role')}: {m.get('content')}" for m in assembly.history
                    )
                    segments.append(
                        ContextSegment(
                            "data", "history", {"kind": "history"}, transcript, _tokens(transcript)
                        )
                    )
                segments += _provider_segments(assembly)
                for item in assembly.inputs:
                    segments += _input_segments(session, item, guard, assembly.node_id)
                try:
                    built = build_context_manifest(
                        context_manifest_id=f"cm-{uuid.uuid4().hex}",
                        workflow_run_id=run_id,
                        step_request_id=step_request_id,
                        revision=next_revision(session, run_id, step_request_id),
                        scope=DeploymentScope.fixed(
                            project_id if isinstance(project_id, str) else None
                        ),
                        segments=segments,
                        token_budget=max(token_budget, sum(s.tokens for s in segments)),
                        created_at=datetime.now(timezone.utc),
                        rule_bundle=rule_bundle,
                        step={
                            "node_id": assembly.node_id,
                            "occurrence_id": assembly.occurrence_id or assembly.node_id,
                            "attempt": assembly.attempt,
                        },
                    )
                    store_context_manifest(session, built)
                    session.commit()
                    return step_request_id
                except ContextManifestError as exc:
                    session.rollback()
                    raise ContextDenied(exc.code) from None
        except SQLAlchemyError:
            # raw SQL and parameters never reach the run's error: the context simply cannot be recorded now
            raise ContextDenied(AUTHORITY_UNAVAILABLE) from None

    return record


__all__ = ["DEFAULT_TOKEN_BUDGET", "context_recorder_for"]
