"""N4 — the checked address must be the connected address.

``check_url`` resolving a name and approving it is a time-of-check/time-of-use bug on its
own: ``httpx`` resolves the name *again* when it opens the connection, and a DNS server
under the attacker's control can answer ``169.254.169.254`` the second time. Every
category check passes and the request still reaches the metadata endpoint.

The test that matters here is therefore the **adversarial** one: a resolver that returns a
public address on the first lookup and the metadata address on the second. Under the old
pre-flight-only design that reaches the metadata service. It must not now.

These tests never open a socket. The resolver is stubbed, and the transport under the
guard is a recording fake, so what is asserted is the address the connection *would* use —
which is exactly the property in question.
"""

from __future__ import annotations

import httpx
import pytest

from caliber import egress
from caliber.egress import (
    EgressBlockedError,
    EgressGuardTransport,
    EgressPolicy,
    resolve_pinned,
)

METADATA = "169.254.169.254"
PUBLIC = "93.184.216.34"


class RecordingTransport(httpx.BaseTransport):
    """Captures what the guard actually handed down to the connection layer."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def sequence_resolver(monkeypatch: pytest.MonkeyPatch):
    """Install a resolver whose answers are scripted per call.

    This is the attacker's DNS server: it can say anything it likes on each lookup, and
    nothing in the process can stop it from changing its mind.
    """

    def install(*answers: list[str]):
        calls = {"n": 0}
        remaining = list(answers)

        def fake(host: str) -> list[str]:
            index = min(calls["n"], len(remaining) - 1)
            calls["n"] += 1
            return remaining[index]

        monkeypatch.setattr(egress, "_resolve_addresses", fake)
        return calls

    return install


def test_a_name_that_rebinds_to_the_metadata_endpoint_is_not_connected_to(
    sequence_resolver,
) -> None:
    """The core N4 reproduction.

    First lookup: a public address, so policy permits the request. Second lookup — the one
    the connection would have used — the metadata endpoint. The connection must go to the
    address that was vetted, not to whatever DNS says the second time.
    """
    calls = sequence_resolver([PUBLIC], [METADATA])
    inner = RecordingTransport()
    guard = EgressGuardTransport(EgressPolicy(), inner=inner)
    client = httpx.Client(transport=guard)

    client.get("https://rebind.example/path?q=1")

    assert len(inner.requests) == 1
    sent = inner.requests[0]
    # The vetted public address, never the metadata address the resolver switched to.
    assert sent.url.host == PUBLIC
    assert sent.url.host != METADATA
    # And only one resolution happened, so there is no second answer to be tricked by.
    assert calls["n"] == 1


def test_pinning_preserves_the_host_header_and_tls_hostname(sequence_resolver) -> None:
    """Pinning to an IP without carrying the name forward would trade an SSRF hole for a
    TLS one: the certificate would be checked against the address, not the host."""
    sequence_resolver([PUBLIC])
    inner = RecordingTransport()
    client = httpx.Client(transport=EgressGuardTransport(EgressPolicy(), inner=inner))

    client.get("https://api.example/v1/thing")

    sent = inner.requests[0]
    assert sent.url.host == PUBLIC
    assert sent.headers["Host"] == "api.example"
    # httpcore uses this as ``server_hostname``, which drives SNI *and* certificate
    # hostname verification.
    assert sent.extensions["sni_hostname"] == "api.example"


def test_the_path_query_and_port_survive_pinning(sequence_resolver) -> None:
    sequence_resolver([PUBLIC])
    inner = RecordingTransport()
    client = httpx.Client(transport=EgressGuardTransport(EgressPolicy(), inner=inner))

    client.get("https://api.example:8443/a/b?x=1&y=2")

    sent = inner.requests[0]
    assert sent.url.host == PUBLIC
    assert sent.url.port == 8443
    assert sent.url.path == "/a/b"
    assert sent.url.query == b"x=1&y=2"
    assert sent.headers["Host"] == "api.example:8443"


def test_a_blocked_destination_never_reaches_the_transport(sequence_resolver) -> None:
    """Enforcement, not annotation: the inner transport must not be called at all."""
    sequence_resolver([METADATA])
    inner = RecordingTransport()
    client = httpx.Client(transport=EgressGuardTransport(EgressPolicy(), inner=inner))

    with pytest.raises(EgressBlockedError, match="instance-metadata"):
        client.get("https://evil.example/")

    assert inner.requests == []


def test_every_resolved_address_is_checked_not_just_the_first(sequence_resolver) -> None:
    """A name with both a public and a link-local record must be refused rather than
    permitted on resolver ordering, which is not a security property."""
    sequence_resolver([PUBLIC, METADATA])
    inner = RecordingTransport()
    client = httpx.Client(transport=EgressGuardTransport(EgressPolicy(), inner=inner))

    with pytest.raises(EgressBlockedError):
        client.get("https://mixed.example/")

    assert inner.requests == []


def test_a_literal_address_is_passed_through_unpinned(sequence_resolver) -> None:
    """A URL that already names an address has no second resolution to diverge from, so
    there is nothing to pin and no DNS lookup to make."""
    calls = sequence_resolver([PUBLIC])
    inner = RecordingTransport()
    client = httpx.Client(transport=EgressGuardTransport(EgressPolicy(), inner=inner))

    client.get(f"https://{PUBLIC}/x")

    assert inner.requests[0].url.host == PUBLIC
    assert calls["n"] == 0, "a literal address must not trigger a DNS lookup"


def test_an_allowlisted_host_is_permitted_without_pinning(sequence_resolver) -> None:
    """The allowlist is checked before resolution deliberately, so an internal name that
    resolves into a blocked category stays reachable when an operator said so."""
    sequence_resolver([METADATA])
    policy = EgressPolicy(allowed_hosts=frozenset({"internal.example"}))
    inner = RecordingTransport()
    client = httpx.Client(transport=EgressGuardTransport(policy, inner=inner))

    client.get("https://internal.example/ticket")

    assert inner.requests[0].url.host == "internal.example"


def test_a_disabled_policy_does_not_pin_or_block(sequence_resolver) -> None:
    sequence_resolver([METADATA])
    inner = RecordingTransport()
    guard = EgressGuardTransport(EgressPolicy(enabled=False), inner=inner)
    client = httpx.Client(transport=guard)

    client.get("https://anything.example/")

    assert inner.requests[0].url.host == "anything.example"


def test_a_redirect_hop_is_re_checked_because_each_hop_is_its_own_request(
    sequence_resolver,
) -> None:
    """``follow_redirects=False`` was previously the *only* thing stopping a 302 to the
    metadata endpoint. Now a caller who enables redirects is still protected, because the
    guard sits in the transport and every hop passes through it."""
    # Two lookups happen, one per hop: the first permits the start URL, the second is the
    # redirect target. A third scripted answer would never be read.
    sequence_resolver([PUBLIC], [METADATA])

    class Redirecting(httpx.BaseTransport):
        def __init__(self) -> None:
            self.hops: list[str] = []

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.hops.append(str(request.url))
            if len(self.hops) == 1:
                return httpx.Response(302, headers={"Location": "https://evil.example/"})
            return httpx.Response(200)

    inner = Redirecting()
    client = httpx.Client(
        transport=EgressGuardTransport(EgressPolicy(), inner=inner),
        follow_redirects=True,
    )

    with pytest.raises(EgressBlockedError):
        client.get("https://start.example/")

    # The first hop was made; the redirect target was refused before connecting.
    assert len(inner.hops) == 1


def test_resolve_pinned_returns_none_when_a_name_does_not_resolve(
    sequence_resolver,
) -> None:
    """A resolution failure is not a policy violation — a name that does not resolve
    reaches nothing, and blocking here would break the outbound-proxy deployment."""
    sequence_resolver([])
    assert resolve_pinned("https://nowhere.example/", EgressPolicy()) is None


def test_a_non_http_scheme_is_still_refused() -> None:
    with pytest.raises(EgressBlockedError, match="scheme"):
        resolve_pinned("file:///etc/passwd", EgressPolicy())
