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


@dataclass(frozen=True)
class TrustedConnector:
    """One private transport the reference profile admits, pinned by what CALIBER can verify about it (B12.3a, #738).

    ``implementation`` is ``module:qualname`` of the registered transport class; ``capability_digest`` is
    :meth:`caliber.effects.connectors.ConnectorCapability.digest` of its capability record and ``configuration_digest`` the
    digest of its wire configuration (:func:`caliber.effects.admission.configuration_digest`). ``accepted_risks`` names every
    risk the capability record carries that the profile accepts, so a transport cannot be admitted with a weakness nobody
    named. ``config_keys`` are the only settings dispatch may hand the transport, ``required_config_keys`` the ones it
    must. The values are pinned here, not computed from the registered objects, so changing a transport's promise or
    wire shape fails admission until a reviewer changes the pin.
    """

    backend_kind: str
    connector: str
    connector_version: str
    implementation: str
    capability_digest: str
    configuration_digest: str
    accepted_risks: tuple[str, ...]
    config_keys: tuple[str, ...]
    required_config_keys: tuple[str, ...]


#: The only connectors the reference profile admits. Admission binds a transport's implementation, capability record and
#: configuration, never a tool or connector *name* a caller, a manifest or a model supplies: a custom tool called
#: ``document_request`` is not this connector. Host-file exports and PostgreSQL MCP writes (EP-114) are not listed, so
#: they are refused until their dispatcher contracts exist (ADR 0001, #534). The ``mcp`` entry admits the MCP transport only
#: for a call whose server record and administrator tool policy classify it as protected (B04.5, #759); it does not admit a
#: server, which stays subject to the stdio and remote-host allowlists.
REFERENCE_CONNECTORS: Final[tuple[TrustedConnector, ...]] = (
    TrustedConnector(
        backend_kind="document_request",
        connector="document-request",
        connector_version="1",
        implementation="caliber.effects.dispatcher:_Backend",
        capability_digest="sha256:6a174c4bcd3c3ba0d66d972536840b0b31c224d195299b150a4ae89a02421bf1",
        configuration_digest="sha256:9c9fed2385a51d4a7741455a33c46de061a8bc55f81f486d6734e15c3a4913e6",
        accepted_risks=("key_retention_unconfirmed",),
        config_keys=("base_url", "max_attempts", "timeout_seconds"),
        required_config_keys=("base_url",),
    ),
    TrustedConnector(
        backend_kind="http",
        connector="http",
        connector_version="1",
        implementation="caliber.effects.dispatcher:_HttpBackend",
        capability_digest="sha256:9ff6abe2c9c957f2c3083ab553a80ad9cbe87c624635f4ec78dc00ef18ab5e29",
        configuration_digest="sha256:d9242a5e4448cc62152084fc1b32f4c1bbfe312cb9bf10c72f3078c3d10811bf",
        accepted_risks=("write_not_exactly_once", "key_retention_unconfirmed"),
        config_keys=("sender", "receipt_verifier"),
        required_config_keys=("sender", "receipt_verifier"),
    ),
    TrustedConnector(
        backend_kind="registered_tool",
        connector="registered_tool",
        connector_version="1",
        implementation="caliber.effects.dispatcher:_RegisteredToolBackend",
        capability_digest="sha256:e422472ab94943bac42cc608499b04bbe2b3ba57f7dbd45d5e406963f44423d2",
        configuration_digest="sha256:9f5b6f6febab4139dd724202caa24745ffbdef5c46323bdf0c2c384639298756",
        accepted_risks=("write_not_exactly_once", "key_retention_unconfirmed"),
        config_keys=("invoker", "receipt_verifier"),
        required_config_keys=("invoker", "receipt_verifier"),
    ),
    TrustedConnector(
        backend_kind="openapi_http",
        connector="openapi-http",
        connector_version="1",
        implementation="caliber.effects.dispatcher:_OpenApiBackend",
        capability_digest="sha256:bc8e7cf48ad504d8380faa63784a39b0d56aa240ee3df67a0c607dfbfebd5172",
        configuration_digest="sha256:d728a05ff403ee3168b1ada877f1d7ea5e6977cfae762ebf08bfba6826a631f3",
        accepted_risks=("write_not_exactly_once", "key_retention_unconfirmed"),
        config_keys=("operation", "receipt_verifier"),
        required_config_keys=("operation", "receipt_verifier"),
    ),
    TrustedConnector(
        backend_kind="mcp",
        connector="mcp-tool",
        connector_version="1",
        implementation="caliber.effects.dispatcher:_McpToolBackend",
        capability_digest="sha256:7065fbd3a0086fb61b23eb33fb2a987c8134b95a8bc3aed4bce5a57143491af2",
        configuration_digest="sha256:4ae543f596dc155a567231b83ad61bf6ffd5c96f37844e19663ba0f476c0da83",
        accepted_risks=("write_not_exactly_once", "key_retention_unconfirmed"),
        config_keys=("call", "receipt_verifier"),
        required_config_keys=("call", "receipt_verifier"),
    ),
)


def reserved_connector_names() -> frozenset[str]:
    """Every name a trusted connector goes by (its backend kind and its connector id).

    Tool registration reserves them, so a user tool cannot be called ``document_request`` (B12.3a, #738).
    """
    return frozenset(name for t in REFERENCE_CONNECTORS for name in (t.backend_kind, t.connector))


def payload_governance_active(config: object | None) -> bool:
    """Whether trace payloads are governed (redacted unless the caller may read them): the reference profile.

    A missing config is governed too: with nothing to say the profile is not the reference one, the answer is
    the cautious one.
    """
    return config is None or getattr(config, "profile", "") == "reference"


def apply_distinct_approver_required(config: object | None) -> bool:
    """Whether Apply must be done by someone other than the job's requester (the two-person rule).

    True when ``CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER`` is on, **and always in the reference profile**: the
    startup check already refuses a reference process with the flag off, and this keeps the rule closed if a
    process ever reaches a request in that state anyway. A missing config is treated as the reference profile,
    the same cautious answer :func:`payload_governance_active` gives.
    """
    return (
        config is None
        or getattr(config, "profile", "") == "reference"
        or getattr(config, "apply_require_distinct_approver", False) is True
    )


def openapi_draft_preview_read_only(config: object | None) -> bool:
    """Whether the operator's OpenAPI draft preview (inventory EP-033) may fire only a read: the reference profile (#1072).

    Preview fires one real upstream call for an unpublished draft, gated by ``allow_in_preview``, which the same operator
    can set, so in the reference profile a non-read method would be a write with no second person, proposal, lease or
    receipt. There it sends through the public entry, which refuses ``POST``, ``PUT``, ``PATCH`` and ``DELETE`` with
    ``unsupported_effect_path``; the development profile keeps the exempt, any-method behaviour. A missing config is treated
    as the reference profile, the same cautious answer :func:`payload_governance_active` gives.
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
        "apply_distinct_approver_disabled",
        "apply_require_distinct_approver",
        "CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER",
        "true",
        "The account that requested a refinement job could apply its own candidate to production, and a job with no recorded requester could be applied unreviewed.",
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
    if not config.apply_require_distinct_approver:
        found.append(
            _violation(
                "apply_distinct_approver_disabled",
                "apply_require_distinct_approver",
                "false",
                "CALIBER_APPLY_REQUIRE_DISTINCT_APPROVER",
                "true",
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
