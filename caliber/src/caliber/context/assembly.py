"""Record an agent step's context before the provider sees it (B07.3b, #710).

The runtime hands :func:`context_recorder_for`'s callable the :class:`~caliber.workflows.runtime.AgentContextAssembly` of
every agent step (and every handoff target) *before* any of it reaches the provider. The recorder turns it into a
:mod:`caliber.context.manifest` ``ContextManifest``, validates it, stores it once (its own short transaction,
committed) and returns; if it cannot, it raises :class:`~caliber.context.authorize.ContextDenied` and the step does not
run, so a provider is never given context that has no valid, authorized record.

How the assembly is labelled:

* the agent's instructions (and its skills) are ``workflow_instruction`` segments: the only ``instruction`` tier;
* the history is ``history`` data;
* each input is data. Text delivered by a knowledge query is ``retrieved_text`` (one segment per version it read, citing
  the ``context.read`` decision :class:`~caliber.context.authorize.SourceGuard` recorded when it authorized that version;
  a query whose versions or authorization are not known is refused); the run's own input is ``user_input``; anything
  else is ``tool_output`` of the node that produced it.

So text a source contains, however it is worded, is recorded as data and can never become an instruction. Token counts
here are an accounting estimate (four characters a token); enforcing a budget on a provider call is B08.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy.orm import Session

from caliber.context.authorize import ContextDenied, SourceGuard
from caliber.context.manifest import (
    ContextManifestError,
    ContextSegment,
    build_context_manifest,
    next_revision,
    store_context_manifest,
)
from caliber.db.models import CaliberKnowledgeBaseVersion
from caliber.run_manifest import DeploymentScope
from caliber.workflows.runtime import AgentContextAssembly, AgentContextInput

DEFAULT_TOKEN_BUDGET: Final = 1_000_000


def _tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _instruction_segments(assembly: AgentContextAssembly) -> list[ContextSegment]:
    segments: list[ContextSegment] = []
    prompt = assembly.instructions
    if prompt is not None:
        text = prompt.inline_text or prompt.mlflow_uri or ""
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
    return segments


def _input_segments(
    session: Session, item: AgentContextInput, guard: SourceGuard
) -> list[ContextSegment]:
    if item.origin_node_type == "knowledge_query":
        result = item.origin_result or {}
        versions = [str(v) for v in result.get("resolved_version_ids") or []]
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
                    authorization_decision_id=guard.decisions.get(version_id),
                )
            )
        return segments
    if item.origin_node_type in (None, "start"):
        return [
            ContextSegment("data", "user_input", {"kind": "input"}, item.text, _tokens(item.text))
        ]
    return [
        ContextSegment(
            "data",
            "tool_output",
            {"kind": "tool", "reference": item.origin_node_id or "unknown"},
            item.text,
            _tokens(item.text),
        )
    ]


def context_recorder_for(
    session_factory: Callable[[], Session],
    run_manifest: dict[str, Any],
    guard: SourceGuard,
    *,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> Callable[[AgentContextAssembly], None]:
    """The recorder the worker hands ``build_plan`` for one admitted run (see the module docstring)."""
    run_id = str(run_manifest["workflow_run_id"])
    project_id = run_manifest.get("project_id")

    def record(assembly: AgentContextAssembly) -> None:
        with session_factory() as session:
            segments = _instruction_segments(assembly)
            segments += [
                ContextSegment(
                    "data",
                    "history",
                    {"kind": "history"},
                    f"{m.get('role')}: {m.get('content')}",
                    _tokens(str(m.get("content"))),
                )
                for m in assembly.history
            ]
            for item in assembly.inputs:
                segments += _input_segments(session, item, guard)
            step_request_id = f"{assembly.node_id}-{uuid.uuid4().hex[:24]}"[:128]
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
                )
                store_context_manifest(session, built)
                session.commit()
            except ContextManifestError as exc:
                session.rollback()
                raise ContextDenied(exc.code) from None

    return record


__all__ = ["DEFAULT_TOKEN_BUDGET", "context_recorder_for"]
