"""The reference deployment profile: which settings a reference deployment must not have.

``CALIBER_PROFILE=reference`` makes the server refuse to start while any of these settings is in an
unsafe state, and say so for every one of them at once. The default ``development`` profile never
looks at this module, so nothing changes for a local install.

Each violation names the setting, the environment variable that changes it and the safe value. Later
work extends the list (origin and egress rules, secret handling, the one-owner rule); a new rule is a
new entry in :func:`reference_profile_violations` with a code the tests pin.
"""

from __future__ import annotations

from dataclasses import dataclass

from caliber.config import CaliberConfig
from caliber.testing.fault_barriers import ENV_VAR as FAULT_BARRIERS_ENV
from caliber.testing.fault_barriers import armed_barriers


@dataclass(frozen=True)
class ProfileViolation:
    """One setting that the reference profile does not allow."""

    code: str
    setting: str
    env_var: str
    message: str


def _violation(code: str, setting: str, value: str, env_var: str, safe: str) -> ProfileViolation:
    return ProfileViolation(
        code=code,
        setting=setting,
        env_var=env_var,
        message=f"{setting} is {value}; set {env_var}={safe} for the reference profile",
    )


def reference_profile_violations(config: CaliberConfig) -> list[ProfileViolation]:
    """Every way ``config`` breaks the reference profile, in a fixed order; empty when it is safe.

    The database URL is described, never quoted: it can carry a password.
    """
    found: list[ProfileViolation] = []
    if config.database_url.strip().lower().startswith("sqlite"):
        found.append(
            _violation(
                "sqlite_database",
                "database_url",
                "a SQLite file",
                "CALIBER_DATABASE_URL",
                "<a PostgreSQL URL>",
            )
        )
    if not config.csrf_enabled:
        found.append(
            _violation("csrf_disabled", "csrf_enabled", "false", "CALIBER_CSRF_ENABLED", "true")
        )
    if config.auth_dev_fallback_enabled:
        found.append(
            _violation(
                "dev_fallback_enabled",
                "auth_dev_fallback_enabled",
                "true",
                "CALIBER_AUTH_DEV_FALLBACK_ENABLED",
                "false",
            )
        )
    if config.auth_bootstrap_allow_insecure_default:
        found.append(
            _violation(
                "insecure_bootstrap",
                "auth_bootstrap_allow_insecure_default",
                "true",
                "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT",
                "false",
            )
        )
    if config.auth_mode == "trusted_header" and not config.auth_trusted_proxy_secret_env.strip():
        found.append(
            ProfileViolation(
                code="trusted_header_without_proxy_secret",
                setting="auth_mode",
                env_var="CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV",
                message=(
                    "auth_mode is trusted_header with no proxy secret, so any client can claim any "
                    "identity; set CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV=<the name of the variable "
                    "that holds the proxy secret> (or CALIBER_AUTH_MODE=session) for the reference "
                    "profile"
                ),
            )
        )
    if not config.egress_policy_enabled:
        found.append(
            _violation(
                "egress_policy_disabled",
                "egress_policy_enabled",
                "false",
                "CALIBER_EGRESS_POLICY_ENABLED",
                "true",
            )
        )
    if config.mcp_allow_insecure_http:
        found.append(
            _violation(
                "mcp_insecure_http",
                "mcp_allow_insecure_http",
                "true",
                "CALIBER_MCP_ALLOW_INSECURE_HTTP",
                "false",
            )
        )
    if config.approval_allow_self_approval:
        found.append(
            _violation(
                "self_approval_allowed",
                "approval_allow_self_approval",
                "true",
                "CALIBER_APPROVAL_ALLOW_SELF_APPROVAL",
                "false",
            )
        )
    armed = sorted(armed_barriers())
    if armed:
        found.append(
            ProfileViolation(
                code="fault_barriers_enabled",
                setting="fault_barriers",
                env_var=FAULT_BARRIERS_ENV,
                message=(
                    f"fault barriers are armed ({', '.join(armed)}), so this process exits on purpose "
                    f"when it reaches one; unset {FAULT_BARRIERS_ENV} for the reference profile"
                ),
            )
        )
    return found
