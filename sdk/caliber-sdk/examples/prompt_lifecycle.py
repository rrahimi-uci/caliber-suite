"""Author a prompt, register a candidate, and promote it deliberately.

The shape of the refinement loop: registering a version is *not* a deployment.
The alias only moves when you move it, which is what lets a candidate be
measured before anyone depends on it.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def prompt_lifecycle(
    caliber: CaliberClient, *, agent_id: str = "intake-classifier"
) -> dict[str, Any]:
    """Register a new version, then promote it as a separate decision."""
    caliber.prompts.create(
        agent_id,
        template="Classify the request. Return ONLY JSON.",
        commit_message="initial",
    )

    # A candidate. Nothing is live yet: no alias has moved.
    candidate = caliber.prompts.register_version(
        agent_id,
        template="Classify the request. Return ONLY JSON with keys intent, priority.",
        commit_message="tighten the output contract",
    )
    version = candidate.get("version") if isinstance(candidate, dict) else None

    # Deployment is its own call, and its own audit event.
    if version is not None:
        caliber.prompts.promote(agent_id, int(version), alias="prod")

    return {"agent_id": agent_id, "promoted_version": version}
