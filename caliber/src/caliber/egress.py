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

## DNS rebinding: the checked address must be the connected address (closes N4)

A pre-flight check alone is a **time-of-check/time-of-use** bug, and the report tracked
it as N4. :func:`check_url` resolves ``evil.example``, sees a public address, and
permits it; ``httpx`` then resolves the name *again* when it opens the connection, and a
DNS server under the attacker's control can answer ``169.254.169.254`` the second time.
Every category check passes and the request still reaches the metadata endpoint. A short
TTL is enough; no race needs to be won, because the two resolutions are independent
lookups.

The fix is to make the connection use the address that was actually vetted.
:func:`resolve_pinned` returns the validated address alongside the original hostname,
and :func:`build_client` returns an ``httpx.Client`` whose transport rewrites the request
URL to that literal address immediately before dispatch, while preserving:

* ``Host:`` — so name-based virtual hosting still routes correctly; and
* the TLS ``server_hostname`` (via httpcore's ``sni_hostname`` extension) — so SNI **and
  certificate hostname verification** still happen against the real name. Pinning to an
  IP without this would silently break certificate validation, trading an SSRF hole for a
  TLS one.

The rewrite happens inside the transport rather than at the call site, so the vetted
address cannot be invalidated between the check and the connect: there is no second
resolution to disagree with the first.

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
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

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


@dataclass(frozen=True)
class PinnedTarget:
    """A vetted destination, expressed so the connection cannot re-resolve the name.

    ``url`` addresses the literal IP that policy actually approved. ``host_header`` and
    ``sni_hostname`` carry the original name forward so virtual hosting and TLS
    certificate verification keep working — see the module docstring on why omitting the
    latter would trade an SSRF hole for a TLS one.
    """

    url: str
    host_header: str
    sni_hostname: str


def resolve_pinned(url: str, policy: EgressPolicy | None) -> PinnedTarget | None:
    """Validate ``url`` and return the address to connect to, or ``None``.

    Raises :class:`EgressBlockedError` exactly as :func:`check_url` does — this is the
    same policy, returning the vetted address instead of discarding it.

    ``None`` means "permitted, and there is nothing to pin": policy disabled, an
    operator-allowlisted host, a URL that already names a literal address, or a name that
    does not resolve here. Each of those either has no second resolution to worry about or
    is already trusted by explicit operator choice.
    """
    resolved_policy = policy if policy is not None else EgressPolicy()
    if not resolved_policy.enabled:
        return None

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
        return None

    # A literal address in the URL is checked directly — no resolution needed, and
    # resolving it would be a needless DNS round trip. Nothing to pin either: the
    # connection uses the same literal, so there is no check/connect divergence.
    stripped = lowered.strip("[]")
    direct = resolved_policy.blocked_category(stripped)
    if direct is not None:
        raise EgressBlockedError(_describe(lowered, lowered, direct))
    if _is_literal_address(stripped):
        return None

    # Every address is validated, and the *first validated* one is what gets connected
    # to. Validating all of them matters: a name with both a public and a link-local
    # record must be refused rather than permitted on resolver ordering.
    addresses = _resolve_addresses(host)
    for address in addresses:
        if address.lower() in resolved_policy.allowed_hosts:
            continue
        category = resolved_policy.blocked_category(address)
        if category is not None:
            raise EgressBlockedError(_describe(host, address, category))
    if not addresses:
        return None
    return PinnedTarget(
        url=_replace_host(parts, addresses[0]),
        host_header=parts.netloc.rsplit("@", 1)[-1],
        sni_hostname=host,
    )


def check_url(url: str, policy: EgressPolicy | None) -> None:
    """Raise :class:`EgressBlockedError` if ``url`` may not be requested.

    Order matters: scheme, then host presence, then the allowlist, then resolution
    and category checks. The allowlist is consulted *before* resolution so an
    explicitly permitted internal host does not need to resolve into a permitted
    category — which is the whole reason an operator would add it.

    This is the check-only entry point, kept because callers that do not own the
    connection still need it. It cannot close the rebinding gap on its own; a caller that
    *does* own the connection should use :func:`build_client` instead.
    """
    resolve_pinned(url, policy)


def _is_literal_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _replace_host(parts: SplitResult, address: str) -> str:
    """Rebuild a URL with ``address`` in place of its hostname, preserving everything
    else — port, userinfo, path, query, fragment."""
    literal = f"[{address}]" if ":" in address else address
    netloc = literal
    port = parts.port
    if port is not None:
        netloc = f"{netloc}:{port}"
    userinfo = parts.netloc.rsplit("@", 1)[0] if "@" in parts.netloc else ""
    if userinfo:
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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


class EgressGuardTransport(httpx.BaseTransport):
    """Applies egress policy at dispatch time and connects to the vetted address.

    Placing this in the transport rather than at the call site is the point. A check
    performed earlier can be invalidated by the resolution ``httpx`` does when it opens
    the connection; a check performed here supplies the address that connection uses, so
    there is no second lookup that could disagree.

    Redirects are still not followed by the shipped client, but this transport makes that
    a defence-in-depth choice rather than the only defence: a caller that enables them
    gets every hop re-checked, because each hop is a separate ``handle_request``.
    """

    def __init__(self, policy: EgressPolicy | None, inner: httpx.BaseTransport | None = None):
        self._policy = policy
        self._inner = inner if inner is not None else httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Raises EgressBlockedError for a refused destination. Deliberately allowed to
        # propagate: the caller translates it into a node-level failure with the reason.
        pinned = resolve_pinned(str(request.url), self._policy)
        if pinned is not None:
            request.url = httpx.URL(pinned.url)
            request.headers["Host"] = pinned.host_header
            # Mutated rather than reassigned: httpx may hold its own reference to this
            # dict, and replacing it would drop extensions a caller set deliberately.
            request.extensions["sni_hostname"] = pinned.sni_hostname
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def build_client(
    *,
    policy: EgressPolicy | None,
    timeout: float | None = None,
    **kwargs: Any,
) -> httpx.Client:
    """An ``httpx.Client`` that enforces egress policy on the connection it opens.

    ``follow_redirects`` defaults to ``False`` and callers should leave it that way; see
    the module docstring. It is no longer load-bearing on its own, because the transport
    re-checks every hop, but an unfollowed redirect is still one fewer thing to reason
    about.
    """
    kwargs.setdefault("follow_redirects", False)
    return httpx.Client(
        timeout=timeout,
        transport=EgressGuardTransport(policy),
        **kwargs,
    )


__all__ = [
    "ALLOWED_SCHEMES",
    "CATEGORIES",
    "LINK_LOCAL",
    "LOOPBACK",
    "OTHER_RESERVED",
    "PRIVATE",
    "EgressBlockedError",
    "EgressGuardTransport",
    "EgressPolicy",
    "PinnedTarget",
    "build_client",
    "check_url",
    "resolve_pinned",
]
