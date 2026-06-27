"""Background events: SSE streams, webhook notifications.

This package holds CALIBER's long-running side processes — the in-process
event bus, SSE streaming, and outbound webhook delivery.
"""

from __future__ import annotations

from caliber.events.bus import EventBus

__all__ = ["EventBus"]
