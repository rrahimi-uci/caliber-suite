"""GitHub webhook-delivery reconciliation (`P4-E` slice 3).

CALIBER's webhook ingress (``routes/github_webhooks.py``) is receive-only
and stateless: a delivery GitHub could not reach -- CALIBER down, a network
partition, GitHub's own retry window exhausted -- is simply lost today,
with no catch-up path. A GitHub App independently retains a rolling log of
its own recent webhook deliveries, regardless of whether the configured
receiver ever saw them, and can be asked to *redeliver* any one of them --
GitHub re-POSTs the exact original payload, HMAC signature and all, to the
App's currently configured webhook URL.

This module is that catch-up path: given a connection, list its App's
recent deliveries, compare their delivery ids against the already-durable,
already-idempotent source-event inbox
(:mod:`caliber.workspace_source_events`) to find ones CALIBER has no
record of, and ask GitHub to redeliver each. Redelivery re-enters through
the exact same authenticated, signature-verified route a live webhook
would (``routes/github_webhooks.py``) -- this module never inserts
anything into the inbox itself; it only asks GitHub to try again. That is
a deliberate design choice: reconciliation gets HMAC verification,
idempotency, and normalization for free by reusing the real ingress path
rather than duplicating any of it.

Distinct auth shape: GitHub's ``/app/hooks/deliveries`` endpoints are
App-scoped, not installation-scoped -- they take the App's own JWT
directly as the bearer credential (:func:`caliber.github_app_auth.mint_app_jwt`),
never an installation access token. Only the connection's private key is
touched here, never its webhook secret (see
:func:`caliber.workspace_source_connections.resolve_private_key`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberWorkspaceSourceConnection, CaliberWorkspaceSourceEvent
from caliber.github_app_auth import GitHubAppAuthError, mint_app_jwt
from caliber.github_source_control import GitHubTransport
from caliber.secret_store import SecretStore
from caliber.workspace_source_connections import (
    WorkspaceSourceConnectionError,
    resolve_private_key,
)

#: GitHub's App-scoped (not installation-scoped) webhook-delivery admin API.
DELIVERIES_PATH = "/app/hooks/deliveries"
#: Hard ceiling on pages fetched per reconciliation call, so a request can
#: never turn into an unbounded scan of the App's entire delivery history.
MAX_PAGES = 20
PAGE_SIZE = 30
_CURSOR_RE = re.compile(r"[?&]cursor=([^&>]+)")


class WebhookReconciliationError(RuntimeError):
    """Reconciliation could not complete against GitHub's delivery API."""

    code = "webhook_reconciliation_error"


@dataclass(frozen=True)
class MissedDelivery:
    """One normalized entry from GitHub's webhook-delivery log.

    Untrusted data about a delivery, not a verified event -- ``guid`` is
    compared against the durable inbox purely to decide whether redelivery
    is worth requesting; it is never inserted into the inbox directly.
    """

    delivery_id: int
    guid: str
    event_type: str
    action: str | None
    delivered_at: str | None
    status: str | None
    installation_id: str | None


@dataclass(frozen=True)
class ReconciliationResult:
    """Summary of one reconciliation pass over a connection."""

    checked: int
    missed: tuple[MissedDelivery, ...]
    redelivery_requested: tuple[str, ...]  # guids GitHub accepted a redelivery request for


def _headers(app_jwt: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {app_jwt}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _next_cursor(headers: Mapping[str, str]) -> str | None:
    """Parse an RFC 5988 ``Link`` header for ``rel="next"``'s ``cursor`` value."""
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        if 'rel="next"' not in part:
            continue
        match = _CURSOR_RE.search(part)
        if match:
            return match.group(1)
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def list_recent_deliveries(
    transport: GitHubTransport,
    app_jwt: str,
    *,
    installation_id: str | None = None,
    max_pages: int = MAX_PAGES,
) -> list[MissedDelivery]:
    """Page through the App's recent webhook deliveries, newest first.

    ``installation_id``, when given, filters to only that installation's
    deliveries -- ``/app/hooks/deliveries`` otherwise lists every
    installation of the App. This architecture binds one connection to one
    App/installation (see `P4-E`'s connection-storage slice), so filtering
    keeps a reconciliation call scoped to the connection it was asked
    about even though the underlying endpoint is App-wide.

    Bounded by ``max_pages`` (each page up to ``PAGE_SIZE`` entries).
    """
    deliveries: list[MissedDelivery] = []
    cursor: str | None = None
    for _ in range(max_pages):
        params = {"per_page": str(PAGE_SIZE)}
        if cursor:
            params["cursor"] = cursor
        response = transport.request(
            "GET", DELIVERIES_PATH, headers=_headers(app_jwt), params=params
        )
        if not (200 <= response.status_code < 300):  # noqa: PLR2004 - HTTP success range
            raise WebhookReconciliationError(
                f"{WebhookReconciliationError.code}: deliveries request failed with status "
                f"{response.status_code}"
            )
        payload = response.payload
        if not isinstance(payload, list):
            raise WebhookReconciliationError(
                f"{WebhookReconciliationError.code}: deliveries response is not a list"
            )
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            delivery_id = entry.get("id")
            guid = entry.get("guid")
            event_type = entry.get("event")
            if (
                not isinstance(delivery_id, int)
                or not isinstance(guid, str)
                or not guid
                or not isinstance(event_type, str)
                or not event_type
            ):
                continue
            entry_installation_id = entry.get("installation_id")
            normalized_installation_id = (
                str(entry_installation_id) if entry_installation_id is not None else None
            )
            if installation_id is not None and normalized_installation_id != installation_id:
                continue
            deliveries.append(
                MissedDelivery(
                    delivery_id=delivery_id,
                    guid=guid,
                    event_type=event_type,
                    action=_as_str(entry.get("action")),
                    delivered_at=_as_str(entry.get("delivered_at")),
                    status=_as_str(entry.get("status")),
                    installation_id=normalized_installation_id,
                )
            )
        cursor = _next_cursor(response.headers)
        if not cursor or not payload:
            break
    return deliveries


def redeliver(transport: GitHubTransport, app_jwt: str, delivery_id: int) -> None:
    """Ask GitHub to redeliver one delivery.

    Fire-and-forget from here: GitHub responds ``202`` and asynchronously
    re-POSTs the original payload to the App's currently configured
    webhook URL. This function inserts nothing anywhere -- the redelivered
    POST re-enters through ``routes/github_webhooks.py`` exactly like a
    live delivery would, including its own HMAC verification and the
    durable inbox's own idempotency.
    """
    response = transport.request(
        "POST", f"{DELIVERIES_PATH}/{delivery_id}/attempts", headers=_headers(app_jwt)
    )
    if not (200 <= response.status_code < 300):  # noqa: PLR2004 - HTTP success range
        raise WebhookReconciliationError(
            f"{WebhookReconciliationError.code}: redelivery request for delivery "
            f"{delivery_id} failed with status {response.status_code}"
        )


def find_missed_deliveries(
    session: Session, source_id: str, candidates: list[MissedDelivery]
) -> list[MissedDelivery]:
    """Filter ``candidates`` down to deliveries this source's inbox has no row for.

    Comparison is by ``guid`` -- the same value stored as
    ``CaliberWorkspaceSourceEvent.provider_delivery_id`` by
    ``workspace_source_events.py::record_source_event`` (GitHub's
    ``X-GitHub-Delivery`` header on the original delivery).
    """
    recorded = set(
        session.execute(
            select(CaliberWorkspaceSourceEvent.provider_delivery_id).where(
                CaliberWorkspaceSourceEvent.source_id == source_id
            )
        )
        .scalars()
        .all()
    )
    return [delivery for delivery in candidates if delivery.guid not in recorded]


def reconcile_connection(
    session: Session,
    secret_store: SecretStore,
    connection: CaliberWorkspaceSourceConnection,
    transport: GitHubTransport,
    *,
    max_pages: int = MAX_PAGES,
) -> ReconciliationResult:
    """Find every delivery missing from this connection's source inbox and
    request GitHub redeliver each one.

    Resolves only the connection's private key (never its webhook secret --
    see :func:`caliber.workspace_source_connections.resolve_private_key`)
    to mint the App-level JWT the deliveries API requires. Raises
    :class:`WebhookReconciliationError` if a redelivery request itself
    fails; a delivery GitHub already rejected the redelivery request for is
    reported via how many fewer guids appear in
    ``redelivery_requested`` than in ``missed`` -- this function does not
    retry a failed redelivery request internally, leaving that to the
    caller's own retry/backoff policy (e.g. a subsequent reconciliation
    call, which is naturally idempotent: an already-recorded delivery
    stops appearing in ``missed`` once it succeeds).
    """
    try:
        private_key = resolve_private_key(session, secret_store, connection)
    except WorkspaceSourceConnectionError as exc:
        raise WebhookReconciliationError(str(exc)) from exc
    try:
        app_jwt = mint_app_jwt(
            app_id_provider=lambda: connection.app_id,
            private_key_provider=lambda: private_key,
        )
    except GitHubAppAuthError as exc:
        raise WebhookReconciliationError(str(exc)) from exc

    recent = list_recent_deliveries(
        transport, app_jwt, installation_id=connection.installation_id, max_pages=max_pages
    )
    missed = find_missed_deliveries(session, connection.source_id, recent)
    redelivered: list[str] = []
    for delivery in missed:
        redeliver(transport, app_jwt, delivery.delivery_id)
        redelivered.append(delivery.guid)

    return ReconciliationResult(
        checked=len(recent),
        missed=tuple(missed),
        redelivery_requested=tuple(redelivered),
    )


__all__ = [
    "DELIVERIES_PATH",
    "MAX_PAGES",
    "PAGE_SIZE",
    "MissedDelivery",
    "ReconciliationResult",
    "WebhookReconciliationError",
    "find_missed_deliveries",
    "list_recent_deliveries",
    "reconcile_connection",
    "redeliver",
]
