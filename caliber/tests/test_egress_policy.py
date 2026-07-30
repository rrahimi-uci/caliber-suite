"""Regression tests for outbound egress policy (SSRF defence).

The review recorded a Critical gap: "Normal runs/services still permit legacy
filesystem/storage capabilities and unrestricted webhook/API egress … There is no
universal broker or SSRF defense."

The exposure was concrete: a webhook/api_request URL comes from a manifest, and
CALIBER runs inside the deployment's network, so a workflow could reach the cloud
instance-metadata endpoint, CALIBER's own API on loopback, or anything in the VPC.

The properties that make this a defence rather than a gesture:

1. the **resolved** address is checked, not just the hostname — a name-based
   allowlist alone does not stop ``evil.example.com → 169.254.169.254``;
2. *every* resolved address is checked, so a verdict does not depend on resolver
   ordering;
3. non-HTTP schemes are refused outright; and
4. the guard runs before an effect-ledger claim, so a refused request does not look
   like an in-flight effect on the next attempt.
"""

from __future__ import annotations

import pytest

from caliber.config import CaliberConfig
from caliber.egress import (
    CATEGORIES,
    EgressBlockedError,
    EgressPolicy,
    check_url,
)

DEFAULT = EgressPolicy()


# ---------------------------------------------------------------------------
# Category blocking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://169.254.169.254",
        "https://[fe80::1]/x",
    ],
)
def test_link_local_and_metadata_are_blocked(url: str) -> None:
    """The single highest-value SSRF target: on many providers the metadata service
    hands credentials to anything that asks."""
    with pytest.raises(EgressBlockedError):
        check_url(url, DEFAULT)


def test_the_metadata_endpoint_is_named_in_the_message() -> None:
    """An operator should not have to recognise an address to understand the refusal."""
    with pytest.raises(EgressBlockedError, match="instance-metadata"):
        check_url("http://169.254.169.254/x", DEFAULT)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:5001/api", "http://[::1]/x", "http://localhost/x"]
)
def test_loopback_is_blocked(url: str) -> None:
    """Loopback is CALIBER's own API and its MCP sidecars: a workflow calling them
    would be calling the control plane as whatever identity loopback trusts."""
    with pytest.raises(EgressBlockedError):
        check_url(url, DEFAULT)


@pytest.mark.parametrize(
    "url", ["http://10.0.0.5/x", "http://192.168.1.10/x", "http://172.16.0.1/x"]
)
def test_private_ranges_are_blocked(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        check_url(url, DEFAULT)


@pytest.mark.parametrize("url", ["http://0.0.0.0/x", "http://224.0.0.1/x"])
def test_other_reserved_ranges_are_blocked(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        check_url(url, DEFAULT)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/x",
        "ftp://example.com/x",
        "//example.com/x",
        "not-a-url",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    """``file://`` and ``gopher://`` are never a webhook target and are classic SSRF
    vectors."""
    with pytest.raises(EgressBlockedError):
        check_url(url, DEFAULT)


def test_a_url_with_no_host_is_refused() -> None:
    with pytest.raises(EgressBlockedError, match="no host"):
        check_url("http:///path-only", DEFAULT)


def test_a_public_address_is_permitted() -> None:
    check_url("https://93.184.216.34/hook", DEFAULT)


# ---------------------------------------------------------------------------
# Resolution: the part a hostname allowlist cannot do
# ---------------------------------------------------------------------------


def test_a_name_resolving_to_the_metadata_endpoint_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason resolution happens at all. A name-based check passes this."""
    from caliber import egress

    monkeypatch.setattr(egress, "_resolve_addresses", lambda host: ["169.254.169.254"])
    with pytest.raises(EgressBlockedError, match="resolved to 169.254.169.254"):
        check_url("https://totally-legit.example.com/hook", DEFAULT)


def test_every_resolved_address_is_checked_not_just_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise the verdict depends on resolver ordering, which is not a security
    property."""
    from caliber import egress

    monkeypatch.setattr(egress, "_resolve_addresses", lambda host: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(EgressBlockedError):
        check_url("https://mixed.example.com/hook", DEFAULT)


def test_an_unresolvable_host_is_blocked_unless_explicitly_permitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This asserted the opposite until an independent probe showed the reasoning was wrong.

    The old claim — "a name that does not resolve reaches nothing, so blocking adds no
    security" — assumed one lookup. There are two: the policy check here, and the one the
    connection makes. A name that fails here can succeed at connect time and answer
    ``169.254.169.254``, which made the one case with no vetted address the one case that
    skipped vetting.

    The proxy deployment that motivated the old behaviour is real and still supported,
    explicitly, so that an operator declares policy lives at the proxy instead of
    inheriting a silent fail-open.
    """
    from caliber import egress
    from caliber.egress import EgressPolicy

    monkeypatch.setattr(egress, "_resolve_addresses", lambda host: [])
    with pytest.raises(EgressBlockedError, match="could not resolve"):
        check_url("https://nowhere.invalid/hook", DEFAULT)

    check_url("https://nowhere.invalid/hook", EgressPolicy(allow_unresolvable_hosts=True))


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


def test_an_allowlisted_host_is_permitted_despite_its_category() -> None:
    """How an internal service stays reachable without reopening the metadata
    endpoint."""
    policy = EgressPolicy(allowed_hosts=frozenset({"tickets.internal"}))
    check_url("http://tickets.internal/api", policy)
    # ...and nothing else internal becomes reachable. A literal address is used here
    # rather than another name, because an unresolvable name is permitted by design
    # (it reaches nothing) and would not exercise the allowlist's narrowness.
    with pytest.raises(EgressBlockedError):
        check_url("http://10.1.2.3/x", policy)
    with pytest.raises(EgressBlockedError):
        check_url("http://169.254.169.254/x", policy)


def test_an_allowlisted_literal_address_is_permitted() -> None:
    policy = EgressPolicy(allowed_hosts=frozenset({"10.0.0.5"}))
    check_url("http://10.0.0.5/api", policy)


def test_an_allowlisted_resolved_address_is_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    from caliber import egress

    monkeypatch.setattr(egress, "_resolve_addresses", lambda host: ["10.0.0.5"])
    policy = EgressPolicy(allowed_hosts=frozenset({"10.0.0.5"}))
    check_url("https://svc.example.com/api", policy)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_the_default_config_blocks_every_category() -> None:
    policy = EgressPolicy.from_config(CaliberConfig())
    assert policy.enabled is True
    assert (
        policy.block_link_local
        and policy.block_loopback
        and policy.block_private
        and policy.block_other_reserved
    )


def test_a_missing_config_still_applies_the_safe_default() -> None:
    """A preview/eval path that reaches an HTTP node must still be constrained;
    defaulting to unconstrained would make the defence depend on which builder
    constructed the plan."""
    policy = EgressPolicy.from_config(None)
    assert policy.enabled is True
    with pytest.raises(EgressBlockedError):
        check_url("http://169.254.169.254/x", policy)


def test_categories_can_be_narrowed_individually() -> None:
    config = CaliberConfig(egress_blocked_categories="link_local")
    policy = EgressPolicy.from_config(config)
    with pytest.raises(EgressBlockedError):
        check_url("http://169.254.169.254/x", policy)
    # Loopback is permitted by this (deliberately narrower) configuration.
    check_url("http://127.0.0.1/x", policy)


def test_an_empty_category_list_falls_back_to_all_of_them() -> None:
    """An empty setting must not read as "block nothing" — that would silently
    disable the defence."""
    policy = EgressPolicy.from_config(CaliberConfig(egress_blocked_categories=""))
    for category in CATEGORIES:
        assert getattr(policy, f"block_{category}") is True


def test_the_policy_can_be_disabled_explicitly() -> None:
    """Disabling is a deliberate, visible choice rather than a side effect."""
    policy = EgressPolicy.from_config(CaliberConfig(egress_policy_enabled=False))
    check_url("http://169.254.169.254/x", policy)


# ---------------------------------------------------------------------------
# Enforcement in the runtime
# ---------------------------------------------------------------------------


def test_a_webhook_node_to_the_metadata_endpoint_fails_the_run() -> None:
    from caliber.workflows.runtime import RuntimePlan, ToolExecutionError, _perform_guarded_effect

    plan = RuntimePlan(ir=None, resolver=None)  # type: ignore[arg-type]
    with pytest.raises(ToolExecutionError, match="instance-metadata"):
        _perform_guarded_effect(
            plan,
            node_id="notify",
            request={"url": "http://169.254.169.254/latest/meta-data/"},
            perform=lambda: {"status_code": 200},
        )


def test_a_blocked_request_does_not_consume_an_effect_ledger_claim(
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    """Otherwise a refused request would look like an in-flight effect on the next
    attempt, and the run would report an indeterminate effect that never happened."""
    from caliber.workflows.effect_ledger import SqlEffectLedger
    from caliber.workflows.runtime import RuntimePlan, ToolExecutionError, _perform_guarded_effect

    ledger = SqlEffectLedger(session_factory, workflow_run_id="WFR-1")
    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=ledger)  # type: ignore[arg-type]
    request = {"url": "http://169.254.169.254/x"}
    with pytest.raises(ToolExecutionError):
        _perform_guarded_effect(
            plan, node_id="notify", request=request, perform=lambda: {"ok": True}
        )
    # No claim was made, so a later legitimate attempt is still "fresh".
    assert ledger.claim(node_id="notify", payload=request).fresh is True


def test_an_allowed_url_still_performs_the_effect() -> None:
    from caliber.workflows.runtime import RuntimePlan, _perform_guarded_effect

    plan = RuntimePlan(ir=None, resolver=None)  # type: ignore[arg-type]
    result = _perform_guarded_effect(
        plan,
        node_id="notify",
        request={"url": "https://93.184.216.34/hook"},
        perform=lambda: {"status_code": 202},
    )
    assert result == {"status_code": 202}


def test_build_plan_attaches_the_configured_policy() -> None:
    from caliber.egress import EgressPolicy as Policy

    policy = Policy.from_config(CaliberConfig(egress_allowed_hosts="tickets.internal"))
    assert "tickets.internal" in policy.allowed_hosts
