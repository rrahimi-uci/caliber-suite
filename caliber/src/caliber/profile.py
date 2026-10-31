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
from typing import Final

from caliber.config import CaliberConfig
from caliber.testing.fault_barriers import ENV_VAR as FAULT_BARRIERS_ENV
from caliber.testing.fault_barriers import armed_barriers

#: The parts of a trace that can hold personal data: a span's ``inputs`` and ``outputs`` and the tool arguments,
#: retrieved documents and model output recorded in its attributes (B11.4b, #677). In the reference profile a caller
#: who may see that a trace exists but may not read its payloads (``trace.payload.read``) gets each of these as
#: ``[redacted:<field>]``; the keys stay, so the shape of the trace is not a secret, the contents are.
GOVERNED_TRACE_FIELDS: Final[tuple[str, ...]] = (
    "inputs",
    "outputs",
    "attributes.tool_arguments",
    "attributes.retrieved_documents",
    "attributes.model_output",
)


#: The span attributes known to carry no payload. Real traces put the same inputs and outputs in other attributes
#: too (MLflow's own ``mlflow.spanInputs``/``mlflow.spanOutputs``/``mlflow.chat.*``, CALIBER's ``caliber.tool.input``
#: and ``caliber.tool.output``), so redaction is **fail-closed**: every span attribute that is not on this list is
#: replaced by ``[redacted:attributes.<key>]`` for a caller without ``trace.payload.read``.
SAFE_SPAN_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "caliber.run_id",
        "caliber.trace_id",
        "caliber.node_id",
        "caliber.job_id",
        "caliber.approval_id",
        "caliber.proposal_id",
        "caliber.decision_id",
        "caliber.effect_id",
        "caliber.refusal_code",
        "caliber.source",
        "caliber.status",
        "caliber.stage.status",
        "caliber.model",
        "caliber.tokens",
        "caliber.prompt_tokens",
        "caliber.completion_tokens",
        "caliber.cost_usd",
        "caliber.tool",
        "caliber.tool.latency_ms",
        "caliber.mcp.transport",
        "caliber.mcp.server_id",
        "caliber.assistant.session_id",
        "caliber.assistant.intent_name",
        "mlflow.spanType",
        "mlflow.spanFunctionName",
        "mlflow.spanLogLevel",
        "mlflow.traceRequestId",
    }
)


def payload_governance_active(config: object | None) -> bool:
    """Whether trace payloads are governed (redacted unless the caller may read them): the reference profile.

    A missing config is governed too: with nothing to say the profile is not the reference one, the answer is
    the cautious one.
    """
    return config is None or getattr(config, "profile", "") == "reference"


@dataclass(frozen=True)
class ProfileViolation:
    """One setting that the reference profile does not allow."""

    code: str
    setting: str
    env_var: str
    message: str


@dataclass(frozen=True)
class Requirement:
    """One thing the reference profile requires, as the operator page lists it."""

    code: str
    setting: str
    env_var: str
    required: str
    why: str


#: Every rule of :func:`reference_profile_violations`, in the order it is checked. The operator page
#: ``docs/operate/reference-profile.md`` has one row per entry, and ``tests/test_profile.py`` fails when the
#: page, this table and the rules disagree, so a new rule cannot be added without documenting it.
REFERENCE_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "sqlite_database",
        "database_url",
        "CALIBER_DATABASE_URL",
        "a PostgreSQL URL",
        "SQLite allows one writer and the reference profile's recovery and ownership guarantees need row locks and real transactions.",
    ),
    Requirement(
        "csrf_disabled",
        "csrf_enabled",
        "CALIBER_CSRF_ENABLED",
        "true (with CALIBER_CSRF_SIGNING_SECRET set)",
        "Without CSRF protection a page the approver visits can submit an approval for them.",
    ),
    Requirement(
        "dev_fallback_enabled",
        "auth_dev_fallback_enabled",
        "CALIBER_AUTH_DEV_FALLBACK_ENABLED",
        "false",
        "The no-header fallback makes an unauthenticated request an admin.",
    ),
    Requirement(
        "insecure_bootstrap",
        "auth_bootstrap_allow_insecure_default",
        "CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT",
        "false",
        "It allows an `admin`/`admin` bootstrap account when no password is set and the session cookie is not marked secure.",
    ),
    Requirement(
        "trusted_header_without_proxy_secret",
        "auth_mode",
        "CALIBER_AUTH_TRUSTED_PROXY_SECRET_ENV",
        "`session`, or `trusted_header` with the name of the variable that holds the proxy secret",
        "Trusting an identity header without a proxy secret lets any client claim any identity.",
    ),
    Requirement(
        "egress_policy_disabled",
        "egress_policy_enabled",
        "CALIBER_EGRESS_POLICY_ENABLED",
        "true",
        "The egress policy is what stops a tool or webhook from reaching internal addresses.",
    ),
    Requirement(
        "mcp_insecure_http",
        "mcp_allow_insecure_http",
        "CALIBER_MCP_ALLOW_INSECURE_HTTP",
        "false",
        "Plain-HTTP MCP servers send tool calls and results in the clear.",
    ),
    Requirement(
        "self_approval_allowed",
        "approval_allow_self_approval",
        "CALIBER_APPROVAL_ALLOW_SELF_APPROVAL",
        "false",
        "The account that started a run could approve its own protected action.",
    ),
    Requirement(
        "memory_inference_enabled",
        "memory_infer",
        "CALIBER_MEMORY_INFER",
        "false while CALIBER_MEMORY_ENABLED is true",
        "LLM-inferred memory writes create durable, free-form facts nobody owns; the owned memory lifecycle does not exist yet.",
    ),
    Requirement(
        "fault_barriers_enabled",
        "fault_barriers",
        "CALIBER_FAULT_BARRIERS",
        "unset",
        "An armed fault barrier makes the process exit on purpose; it exists only for scenario tests.",
    ),
)


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
    if config.memory_enabled and config.memory_infer:
        found.append(
            _violation(
                "memory_inference_enabled",
                "memory_infer",
                "true while memory is enabled",
                "CALIBER_MEMORY_INFER",
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
