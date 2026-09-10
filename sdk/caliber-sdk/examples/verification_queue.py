"""Flag a concern manually and confirm it — Stage ① Verify.

Verifying an item here does **not** create a refinement job. Today's four
job-creation paths (prompt optimization, skill calibration, workflow
calibration, an Aria-proposed promotion) still create and self-verify their
own item in one step; this route is for a concern raised separately from an
already-running job. See ``docs/workspace-plan.md`` section 2.2 for the full
account of what's built and what's deliberately deferred.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def flag_and_verify(caliber: CaliberClient, *, agent_id: str = "support-agent") -> dict[str, Any]:
    """Flag a concern, list pending items, then confirm it's real."""
    item = caliber.verification_queue.create(
        agent_id,
        category="hallucination",
        free_text="Cited a policy section that doesn't exist.",
        severity="critical",
    )

    # A different reviewer (or the same one, later) lists what's pending and
    # decides — a separate step from the one that flagged it.
    pending = caliber.verification_queue.list(status="pending", agent_id=agent_id)
    assert any(row.item_id == item.item_id for row in pending)

    verified = caliber.verification_queue.verify(
        item.item_id, verification_notes="Confirmed against the source doc."
    )
    return {"item_id": verified.item_id, "status": verified.status}
