"""Connect, identify yourself, and see what the deployment supports."""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def quickstart(caliber: CaliberClient) -> dict[str, Any]:
    """Report who you are and which API surfaces are GA on this deployment."""
    identity = caliber.me.get()
    if identity.is_anonymous:
        # /me answers "who am I" rather than requiring a credential, so an
        # invalid token shows up here as anonymous instead of an exception.
        raise SystemExit("no usable credential — check CALIBER_TOKEN")

    capabilities = caliber.capabilities_api.get()
    return {
        "user_id": identity.user_id,
        "scopes": identity.scopes,
        "ga_surfaces": sorted(capabilities.sdk_stability.get("ga", [])),
        "queue_enabled": capabilities.workflow_runs.queue_enabled,
    }
