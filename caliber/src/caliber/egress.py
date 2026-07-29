"""Outbound egress policy — SSRF defence for workflow-initiated HTTP.

The review recorded this as a Critical gap: "Normal runs/services still permit
legacy filesystem/storage capabilities and unrestricted webhook/API egress … There
is no universal broker or SSRF defense."

The concrete exposure: a ``webhook`` or ``api_request`` node's URL comes from a
manifest an operator (or an imported workflow, or Aria) authored, and CALIBER runs
inside the deployment's network. Nothing stopped a workflow from reaching:

* ``169.254.169.254`` — the cloud instance-metadata service, which on many providers
  hands out credentials to anything that asks;
* ``127.0.0.1`` / ``::1`` — CALIBER's own API and the MCP sidecars, letting a
  workflow call the control plane as whatever identity the loopback trusts;
* RFC1918 addresses — everything else inside the VPC.

## The policy

Deny-by-category with an explicit operator allowlist, applied to the **resolved IP**,
not just the hostname. Resolving first is the point: ``evil.example.com`` with an A
record of ``169.254.169.254`` passes any name-based check, which is why hostname
allowlists alone do not stop SSRF.

Blocked categories (each independently toggleable, all on by default):

``link_local``
    Includes the metadata endpoint. The single highest-value SSRF target.
``loopback``
    CALIBER's own API and sidecars.
``private``
    RFC1918 / unique-local — the rest of the internal network.
``other_reserved``
    Multicast, unspecified, reserved ranges — no legitimate webhook target.

An operator who genuinely needs an internal target adds it to
``egress_allowed_hosts``, which is checked **after** resolution and matches on
hostname or literal address. That keeps "call our internal ticketing service"
possible without reopening the metadata endpoint.

## Redirects

A permitted URL that 302s to the metadata endpoint is the classic bypass, so a
single pre-flight check is only sufficient if redirects are not followed. The shipped
sender sets ``follow_redirects=False`` explicitly for exactly this reason, and says so
at the call site. A custom ``RuntimePlan.webhook_sender`` that *does* follow redirects
must call :func:`check_url` on each hop; this module cannot enforce that for it.

## What this is not

Not a proxy and not a general effect broker. It constrains where a workflow's HTTP
nodes may connect; it does not mediate filesystem or bucket capabilities, which the
report tracks separately.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("caliber.egress")

#: Category names, in the order a verdict reports them.
LINK_LOCAL = "link_local"
LOOPBACK = "loopback"
PRIVATE = "private"
OTHER_RESERVED = "other_reserved"

CATEGORIES: tuple[str, ...] = (LINK_LOCAL, LOOPBACK, PRIVATE, OTHER_RESERVED)

#: Schemes a workflow HTTP node may use. ``file://``, ``gopher://``, and friends are
#: refused outright — they are never a webhook target and are classic SSRF vectors.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Named for what it is, so a blocked-request message can say so rather than printing
#: an address the operator has to recognise.
_METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})


class EgressBlockedError(RuntimeError):
    """An outbound request was refused by egress policy."""


@dataclass(frozen=True)
class EgressPolicy:
    """Resolved egress policy. Built from config once per plan."""

    enabled: bool = True
    block_link_local: bool = True
    block_loopback: bool = True
    block_private: bool = True
    block_other_reserved: bool = True
    #: Hostnames or literal addresses always permitted, even if they resolve into a
    #: blocked category. This is how an internal service stays reachable.
    allowed_hosts: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: Any) -> EgressPolicy:
        if config is None:
            # No config threaded: apply the safe default rather than no policy. A
            # preview/eval path that reaches here should still be constrained.
            return cls()
        raw_hosts = str(getattr(config, "egress_allowed_hosts", "") or "")
        hosts = frozenset(item.strip().lower() for item in raw_hosts.split(",") if item.strip())
        blocked = str(getattr(config, "egress_blocked_categories", "") or "")
        categories = {item.strip().lower() for item in blocked.split(",") if item.strip()}
        if not categories:
            categories = set(CATEGORIES)
        return cls(
            enabled=bool(getattr(config, "egress_policy_enabled", True)),
            block_link_local=LINK_LOCAL in categories,
            block_loopback=LOOPBACK in categories,
            block_private=PRIVATE in categories,
            block_other_reserved=OTHER_RESERVED in categories,
            allowed_hosts=hosts,
        )

    def blocked_category(self, address: str) -> str | None:
        """Which blocked category ``address`` falls into, or ``None`` if permitted."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        if ip.is_link_local and self.block_link_local:
            return LINK_LOCAL
        if ip.is_loopback and self.block_loopback:
            return LOOPBACK
        if ip.is_private and not ip.is_loopback and not ip.is_link_local and self.block_private:
            return PRIVATE
        if (ip.is_multicast or ip.is_unspecified or ip.is_reserved) and self.block_other_reserved:
            return OTHER_RESERVED
        return None


def _resolve_addresses(host: str) -> list[str]:
    """Every address ``host`` resolves to.

    **All** of them are checked, not just the first: a name with both a public and a
    link-local record would otherwise pass or fail depending on resolver ordering,
    which is not a security property.

    A resolution failure is deliberately **not** a policy violation. The property
    being enforced is "do not reach internal addresses", and a name that does not
    resolve reaches nothing — the request fails at connect time on its own. Blocking
    here would instead break the legitimate case where DNS resolves in an outbound
    proxy's network but not in this process. A deployment that routes egress through
    such a proxy should enforce policy at the proxy too; that residual is stated
    rather than papered over.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        logger.debug("egress policy could not resolve %r; leaving it to the request", host)
        return []
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str) and sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    return addresses


def check_url(url: str, policy: EgressPolicy | None) -> None:
    """Raise :class:`EgressBlockedError` if ``url`` may not be requested.

    Order matters: scheme, then host presence, then the allowlist, then resolution
    and category checks. The allowlist is consulted *before* resolution so an
    explicitly permitted internal host does not need to resolve into a permitted
    category — which is the whole reason an operator would add it.
    """
    resolved_policy = policy if policy is not None else EgressPolicy()
    if not resolved_policy.enabled:
        return

    parts = urlsplit((url or "").strip())
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise EgressBlockedError(
            f"outbound scheme {scheme or '(none)'!r} is not permitted; "
            f"use one of {sorted(ALLOWED_SCHEMES)}"
        )
    host = (parts.hostname or "").strip()
    if not host:
        raise EgressBlockedError(f"outbound URL {url!r} has no host")

    lowered = host.lower()
    if lowered in resolved_policy.allowed_hosts:
        return

    # A literal address in the URL is checked directly — no resolution needed, and
    # resolving it would be a needless DNS round trip.
    direct = resolved_policy.blocked_category(lowered.strip("[]"))
    if direct is not None:
        raise EgressBlockedError(_describe(lowered, lowered, direct))

    for address in _resolve_addresses(host):
        if address.lower() in resolved_policy.allowed_hosts:
            continue
        category = resolved_policy.blocked_category(address)
        if category is not None:
            raise EgressBlockedError(_describe(host, address, category))


def _describe(host: str, address: str, category: str) -> str:
    if address.strip("[]") in _METADATA_ADDRESSES:
        target = "the cloud instance-metadata endpoint"
    elif category == LOOPBACK:
        target = "a loopback address (CALIBER's own API and sidecars)"
    elif category == PRIVATE:
        target = "a private network address"
    elif category == LINK_LOCAL:
        target = "a link-local address"
    else:
        target = "a reserved address"
    via = f" (resolved to {address})" if address.lower() != host.lower() else ""
    return (
        f"outbound request to {host!r}{via} is blocked: it points at {target}. "
        "Add the host to CALIBER_EGRESS_ALLOWED_HOSTS if this is intentional."
    )


__all__ = [
    "ALLOWED_SCHEMES",
    "CATEGORIES",
    "LINK_LOCAL",
    "LOOPBACK",
    "OTHER_RESERVED",
    "PRIVATE",
    "EgressBlockedError",
    "EgressPolicy",
    "check_url",
]
