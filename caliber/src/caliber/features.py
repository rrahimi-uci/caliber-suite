"""Feature flags for the areas the alpha does not stand behind.

``docs/start/alpha-scope.md`` splits the product into what the alpha supports and what
it does not: preview areas, a record-only area and an experimental one. Marking them in
the UI is honest, but it still ships them. A deployment that wants a clean alpha turns
one **off**: each area below is hidden by the SPA and refused by the API while its flag
is off.

Four flags, each covering the routes of one area:

* ``aria`` -- the Aria assistant and plans. Unlike the other three, this one ships **on**
  by default; set ``CALIBER_FEATURE_ARIA=false`` for a deployment that wants it off.
* ``integrations`` -- MCP servers and OpenAPI integrations. Off by default.
* ``cookbooks`` -- the guided cookbook labs. Off by default.
* ``release_candidates`` -- release-candidate signoff and workspace releases. Off by
  default. The live board, the timeline and release operations are core and stay on.

A flag gates by **path prefix** in one ASGI middleware rather than inside each route:
the gate cannot be forgotten on a new route in a gated area, a route added there is
refused until someone decides otherwise, and the routes stay registered, so the OpenAPI
document, the route inventories and the SDK coverage gate describe the same surface in
every deployment. A disabled call answers ``404`` with ``reason_code: feature_disabled``
and the variable to set, so a client can tell "this build does not have it on" from "no
such record".

Beta adds a second, separate answer: **per-adapter support** (:func:`adapter_capabilities`, the
``adapters`` object of ``GET /capabilities``). A flag says whether an area is *on*; it never says an
area is *supported*, and turning one on promotes nothing. Each adapter is exactly one of

* ``supported`` -- only the trusted reference connector and the release evaluation path, and only
  while the deployment is configured for the reference profile, its settings pass the profile, and
  the live code still matches what its scenario tests exercised (:data:`SUPPORT_EVIDENCE`);
* ``preview`` -- it can run, but nothing in the supported shape covers it (MCP servers and OpenAPI
  integrations are never above this while on, and every adapter is here outside the reference
  profile);
* ``unavailable`` -- it cannot run in this deployment, with the setting that says so.

Support is **derived, never asserted**: there is no setting that marks an adapter supported, and an
adapter with no :data:`SUPPORT_EVIDENCE` entry cannot be. The existing flag ``stability`` values
(``preview``, ``record_only``) are unchanged, so a reader that predates ``adapters`` keeps working.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from caliber.config import CaliberConfig
from caliber.effects.dispatcher import EffectRefused, admit_backend
from caliber.eval import evidence as eval_evidence
from caliber.orchestrator.workspace_release_evaluator import WorkspaceReleaseEvaluator
from caliber.profile import REFERENCE_MCP_ALLOWLIST, reference_profile_violations

PREFIX: Final[str] = "/ajax-api/2.0/mlflow/caliber"

#: ``reason_code`` of the 404 a disabled area returns.
REASON_CODE: Final[str] = "feature_disabled"


@dataclass(frozen=True)
class FeatureFlag:
    """One gated area."""

    id: str
    env_var: str
    config_field: str
    label: str
    #: ``preview`` or ``record_only``; the alpha scope note defines both.
    stability: str
    #: Path prefixes under :data:`PREFIX`. A path matches a prefix exactly or when it
    #: continues with ``/``, ``:`` or ``.`` (``/x/1``, ``/x/1:apply``), never as a
    #: longer word (``/cookbooks-extra`` is not ``/cookbooks``).
    prefixes: tuple[str, ...]
    #: Suffix patterns for per-project routes (``/projects/{id}/releases``), which a
    #: plain prefix cannot express. Each entry is the literal segment after the id.
    project_segments: tuple[str, ...] = ()


FEATURES: Final[tuple[FeatureFlag, ...]] = (
    FeatureFlag(
        id="aria",
        env_var="CALIBER_FEATURE_ARIA",
        config_field="feature_aria_enabled",
        label="Aria assistant and plans",
        stability="preview",
        prefixes=("/assistant", "/aria"),
    ),
    FeatureFlag(
        id="integrations",
        env_var="CALIBER_FEATURE_INTEGRATIONS",
        config_field="feature_integrations_enabled",
        label="MCP servers and OpenAPI integrations",
        stability="preview",
        prefixes=("/mcp-servers", "/openapi-integrations"),
    ),
    FeatureFlag(
        id="cookbooks",
        env_var="CALIBER_FEATURE_COOKBOOKS",
        config_field="feature_cookbooks_enabled",
        label="Cookbooks",
        stability="preview",
        prefixes=("/cookbooks",),
    ),
    FeatureFlag(
        id="release_candidates",
        env_var="CALIBER_FEATURE_RELEASE_CANDIDATES",
        config_field="feature_release_candidates_enabled",
        label="Release-candidate signoff and workspace releases",
        stability="record_only",
        prefixes=("/releases/candidates", "/releases/report-jobs"),
        project_segments=("releases",),
    ),
)

_BY_ID: Final[dict[str, FeatureFlag]] = {flag.id: flag for flag in FEATURES}

#: ``/projects/{id}/<segment>``: split gives ['', 'projects', id, segment, ...].
_PROJECT_SEGMENT_INDEX: Final[int] = 3


def feature_enabled(config: CaliberConfig, feature_id: str) -> bool:
    """Whether the area ``feature_id`` is on for this deployment."""
    return bool(getattr(config, _BY_ID[feature_id].config_field))


def _matches(path: str, prefix: str) -> bool:
    if not path.startswith(prefix):
        return False
    rest = path[len(prefix) :]
    return rest == "" or rest[0] in "/:."


def feature_for_path(path: str) -> FeatureFlag | None:
    """The gated area that owns ``path``, or ``None`` for an ungated path."""
    if not path.startswith(PREFIX + "/"):
        return None
    relative = path[len(PREFIX) :]
    for flag in FEATURES:
        if any(_matches(relative, prefix) for prefix in flag.prefixes):
            return flag
        if flag.project_segments and relative.startswith("/projects/"):
            parts = relative.split("/")  # ['', 'projects', '{id}', 'releases', ...]
            if (
                len(parts) > _PROJECT_SEGMENT_INDEX
                and parts[_PROJECT_SEGMENT_INDEX].split(":")[0] in flag.project_segments
            ):
                return flag
    return None


def disabled_message(flag: FeatureFlag) -> str:
    return f"{flag.label} is disabled in this deployment. Set {flag.env_var}=true to enable it."


# ---------------------------------------------------------------------------
# Per-adapter support (Beta, B15-Support.1, #755)
# ---------------------------------------------------------------------------

SUPPORTED: Final[str] = "supported"
PREVIEW: Final[str] = "preview"
UNAVAILABLE: Final[str] = "unavailable"
#: Every ``status`` an adapter can report.
SUPPORT_STATUSES: Final[tuple[str, ...]] = (UNAVAILABLE, PREVIEW, SUPPORTED)

#: ``reason_code`` values of a non-supported adapter. ``feature_disabled`` is :data:`REASON_CODE`, the code of
#: the 404 a disabled area answers, so a client sees one cause under one code.
REASON_NOT_REFERENCE_PROFILE: Final[str] = "not_reference_profile"
REASON_PROFILE_VIOLATIONS: Final[str] = "reference_profile_violations"
REASON_NO_SUPPORT_EVIDENCE: Final[str] = "no_support_evidence"
REASON_EVALUATOR_DISABLED: Final[str] = "evaluator_disabled"
REASON_SIMULATED_PROVIDER: Final[str] = "simulated_provider"
REASON_EVALUATOR_MISMATCH: Final[str] = "evaluator_evidence_mismatch"
REASON_MCP_NOT_IN_PROFILE: Final[str] = "mcp_not_in_reference_profile"
REASON_RECONCILER_DISABLED: Final[str] = "reconciler_disabled"
REASON_BACKGROUND_DISABLED: Final[str] = "background_tasks_disabled"
REASON_PROVIDER_UNRECOGNIZED: Final[str] = "eval_provider_unrecognized"

#: ``disabled_by`` when no setting changes the answer: the reference profile does not cover the adapter.
DISABLED_BY_PROFILE: Final[str] = "profile"
PROFILE_ENV_VAR: Final[str] = "CALIBER_PROFILE"
EVALUATOR_ENV_VAR: Final[str] = "CALIBER_WORKSPACE_RELEASE_EVALUATOR_ENABLED"
EVAL_PROVIDER_ENV_VAR: Final[str] = "CALIBER_EVAL_PROVIDER"
RECONCILER_ENV_VAR: Final[str] = "CALIBER_EFFECT_RECONCILER_ENABLED"
BACKGROUND_ENV_VAR: Final[str] = "CALIBER_BACKGROUND_TASKS_ENABLED"

#: Adapter ids, in the order ``/capabilities`` lists them.
DOCUMENT_REQUEST_CONNECTOR: Final[str] = "document_request_connector"
RELEASE_EVALUATION: Final[str] = "release_evaluation"
MCP_SERVERS: Final[str] = "mcp_servers"
OPENAPI_INTEGRATIONS: Final[str] = "openapi_integrations"


@dataclass(frozen=True)
class AdapterSpec:
    """One adapter a deployment reports on, and the area flag that gates its routes (if any)."""

    id: str
    #: ``connector``, ``release_evaluation``, ``mcp_server`` or ``openapi_integration``.
    kind: str
    label: str
    #: The :class:`FeatureFlag` id that turns the adapter's routes on, or ``None`` when no flag does.
    feature_id: str | None = None


ADAPTERS: Final[tuple[AdapterSpec, ...]] = (
    AdapterSpec(
        id=DOCUMENT_REQUEST_CONNECTOR,
        kind="connector",
        label="OpenAPI document-request connector",
    ),
    AdapterSpec(
        id=RELEASE_EVALUATION,
        kind="release_evaluation",
        label="Workspace release evaluation",
        feature_id="release_candidates",
    ),
    AdapterSpec(
        id=MCP_SERVERS,
        kind="mcp_server",
        label="MCP servers",
        feature_id="integrations",
    ),
    AdapterSpec(
        id=OPENAPI_INTEGRATIONS,
        kind="openapi_integration",
        label="OpenAPI integrations",
        feature_id="integrations",
    ),
)


@dataclass(frozen=True)
class SupportEvidence:
    """What lets an adapter be ``supported``: the scenario tests that demonstrate it, and the code they ran against.

    ``scenarios`` are pytest node ids relative to ``caliber/`` (``tests/...py::test_name``);
    ``tests/test_feature_flags.py`` fails when one does not resolve to a test function, so a claim cannot cite a
    test that does not exist. ``implementation`` is the ``module:qualname`` of the live code those scenarios
    exercised and ``evidence_schema_version`` (release evaluation only) the evidence shape they read; both are
    pinned here, not computed, so changing either withdraws the claim until a reviewer re-confirms the scenarios.
    """

    adapter_id: str
    scenarios: tuple[str, ...]
    implementation: str | None = None
    evidence_schema_version: int | None = None


#: The only adapters that may ever report ``supported``. An adapter not listed here (MCP servers, OpenAPI
#: integrations, every other transport) is capped at ``preview`` whatever the configuration says. The document-request
#: connector is additionally pinned by its admission record (``caliber.profile.REFERENCE_CONNECTORS``), checked live.
SUPPORT_EVIDENCE: Final[dict[str, SupportEvidence]] = {
    DOCUMENT_REQUEST_CONNECTOR: SupportEvidence(
        adapter_id=DOCUMENT_REQUEST_CONNECTOR,
        scenarios=(
            "tests/egi_scenarios/test_connector_capabilities.py::test_stable_key_mutates_once",
            "tests/egi_scenarios/test_connector_capabilities.py::test_stale_precondition_returns_412_and_no_mutation",
            "tests/egi_scenarios/test_connector_capabilities.py::test_query_by_key_after_lost_response",
            "tests/egi_scenarios/test_b04_dispatcher.py::test_dispatcher_uses_canonical_approval_and_registered_connector",
            "tests/egi_scenarios/test_v07_restart_exactly_once.py::test_kill_after_downstream_commit",
        ),
    ),
    RELEASE_EVALUATION: SupportEvidence(
        adapter_id=RELEASE_EVALUATION,
        scenarios=(
            "tests/egi_scenarios/test_v13_release_evaluation.py::test_evaluator_crash_recovers",
            "tests/egi_scenarios/test_v13_release_evaluation.py::test_worker_records_provider_run_before_completion_crash",
            "tests/egi_scenarios/test_v13_release_evaluation.py::test_recovery_before_provider_result_keeps_attempt_history",
        ),
        implementation="caliber.orchestrator.workspace_release_evaluator:WorkspaceReleaseEvaluator",
        evidence_schema_version=1,
    ),
}


def _verify_connector_admission() -> str | None:
    """``None`` when the live document-request transport is the one its pinned admission record admits, else the code."""
    try:
        admit_backend("document_request")
    except EffectRefused as exc:
        return exc.code
    return None


def _verify_evaluator_evidence(evidence: SupportEvidence) -> str | None:
    """``None`` when the live evaluator and its evidence shape match the pinned scenario evidence, else the code."""
    live = f"{WorkspaceReleaseEvaluator.__module__}:{WorkspaceReleaseEvaluator.__qualname__}"
    if (
        live != evidence.implementation
        or evidence.evidence_schema_version != eval_evidence.EVIDENCE_SCHEMA_VERSION
    ):
        return REASON_EVALUATOR_MISMATCH
    return None


def _entry(
    spec: AdapterSpec,
    status: str,
    message: str,
    *,
    reason_code: str | None = None,
    disabled_by: str | None = None,
) -> dict[str, Any]:
    """One adapter's block. ``evidence`` lists scenarios only for ``supported``: a claim, never a decoration."""
    evidence = SUPPORT_EVIDENCE[spec.id].scenarios if status == SUPPORTED else ()
    return {
        "status": status,
        "kind": spec.kind,
        "label": spec.label,
        "reason_code": reason_code,
        "disabled_by": disabled_by,
        "message": message,
        "evidence": list(evidence),
    }


def _gate_off(spec: AdapterSpec, config: CaliberConfig) -> dict[str, Any] | None:
    """``unavailable`` while the flag that gates ``spec``'s routes is off: the same cause, code and text as the 404."""
    if spec.feature_id is None or feature_enabled(config, spec.feature_id):
        return None
    flag = _BY_ID[spec.feature_id]
    return _entry(
        spec,
        UNAVAILABLE,
        disabled_message(flag),
        reason_code=REASON_CODE,
        disabled_by=flag.env_var,
    )


def _outside_reference(spec: AdapterSpec, config: CaliberConfig) -> dict[str, Any] | None:
    """The block for a deployment that is not a valid reference deployment, or ``None`` when it is one."""
    if config.profile != "reference":
        return _entry(
            spec,
            PREVIEW,
            f"{spec.label} is preview in this deployment: it is supported only under "
            f"{PROFILE_ENV_VAR}=reference. Set {PROFILE_ENV_VAR}=reference and start from a configuration "
            "that passes the profile to use it as a supported path.",
            reason_code=REASON_NOT_REFERENCE_PROFILE,
            disabled_by=PROFILE_ENV_VAR,
        )
    violations = reference_profile_violations(config)
    if violations:
        codes = ", ".join(v.code for v in violations)
        return _entry(
            spec,
            UNAVAILABLE,
            f"{spec.label} is unavailable: this configuration breaks the reference profile ({codes}). "
            "Change the settings named in the startup refusal.",
            reason_code=REASON_PROFILE_VIOLATIONS,
            disabled_by=DISABLED_BY_PROFILE,
        )
    return None


def _preview_only(spec: AdapterSpec) -> dict[str, Any]:
    """``preview`` for an adapter with no :data:`SUPPORT_EVIDENCE` entry: nothing can promote it."""
    return {
        "status": PREVIEW,
        "kind": spec.kind,
        "label": spec.label,
        "reason_code": REASON_NO_SUPPORT_EVIDENCE,
        "disabled_by": DISABLED_BY_PROFILE,
        "message": (
            f"{spec.label} are preview: no scenario evidence supports them in the reference profile, "
            "and no setting changes that."
        ),
        "evidence": [],
    }


def _needs_loops(spec: AdapterSpec, config: CaliberConfig, what: str) -> dict[str, Any] | None:
    """``unavailable`` while the background loops are off: the scenario evidence relies on ``what`` running."""
    if config.background_tasks_enabled:
        return None
    return _entry(
        spec,
        UNAVAILABLE,
        f"{spec.label} is unavailable: {what} runs in a background loop and "
        f"{BACKGROUND_ENV_VAR} is false. Set {BACKGROUND_ENV_VAR}=true to enable it.",
        reason_code=REASON_BACKGROUND_DISABLED,
        disabled_by=BACKGROUND_ENV_VAR,
    )


def _document_request_connector(spec: AdapterSpec, config: CaliberConfig) -> dict[str, Any]:
    refused = _outside_reference(spec, config)
    if refused is not None:
        return refused
    # The cited recovery scenarios (a killed or lost dispatch is found by key and never re-sent) pass because the
    # reconciler loop runs, so a deployment that turns it, or the loops, off is not the one the evidence covers.
    stopped = _needs_loops(spec, config, "the effect reconciler")
    if stopped is not None:
        return stopped
    if not config.effect_reconciler_enabled:
        return _entry(
            spec,
            UNAVAILABLE,
            f"{spec.label} is unavailable: the effect reconciler is off, so an effect whose outcome was never "
            f"recorded stays blocked. Set {RECONCILER_ENV_VAR}=true to enable it.",
            reason_code=REASON_RECONCILER_DISABLED,
            disabled_by=RECONCILER_ENV_VAR,
        )
    code = _verify_connector_admission()
    if code is not None:
        return _entry(
            spec,
            UNAVAILABLE,
            f"{spec.label} is unavailable: the registered transport is not the one the reference profile admits "
            f"({code}).",
            reason_code=code,
            disabled_by=DISABLED_BY_PROFILE,
        )
    return _entry(
        spec,
        SUPPORTED,
        f"{spec.label} is supported: the reference profile admits this transport by its pinned implementation, "
        "capability record and configuration.",
    )


def _release_evaluation(spec: AdapterSpec, config: CaliberConfig) -> dict[str, Any]:
    if not config.workspace_release_evaluator_enabled:
        return _entry(
            spec,
            UNAVAILABLE,
            f"{spec.label} is unavailable in this deployment. Set {EVALUATOR_ENV_VAR}=true to enable it.",
            reason_code=REASON_EVALUATOR_DISABLED,
            disabled_by=EVALUATOR_ENV_VAR,
        )
    refused = _outside_reference(spec, config)
    if refused is not None:
        return refused
    stopped = _needs_loops(spec, config, "the release evaluator")
    if stopped is not None:
        return stopped
    # An allowlist, spelled the way ``caliber.eval.provider.build_provider`` reads it (``lower()``, no stripping):
    # anything else is either simulated (``fake``) or not a provider this build can construct, never a real one.
    provider = str(config.eval_provider).lower()
    if provider != "mlflow":
        simulated = provider == "fake"
        return _entry(
            spec,
            PREVIEW,
            (
                f"{spec.label} is preview while its scores are simulated. "
                if simulated
                else f"{spec.label} is preview: {EVAL_PROVIDER_ENV_VAR} is not a provider this build supports. "
            )
            + f"Set {EVAL_PROVIDER_ENV_VAR}=mlflow so the evaluation is a real one.",
            reason_code=REASON_SIMULATED_PROVIDER if simulated else REASON_PROVIDER_UNRECOGNIZED,
            disabled_by=EVAL_PROVIDER_ENV_VAR,
        )
    code = _verify_evaluator_evidence(SUPPORT_EVIDENCE[spec.id])
    if code is not None:
        return _entry(
            spec,
            UNAVAILABLE,
            f"{spec.label} is unavailable: the evaluator or its evidence shape is not the one its scenario "
            f"evidence covers ({code}).",
            reason_code=code,
            disabled_by=DISABLED_BY_PROFILE,
        )
    return _entry(
        spec,
        SUPPORTED,
        f"{spec.label} is supported: the evaluator and its evidence shape match the scenario evidence.",
    )


def _mcp_servers(spec: AdapterSpec, config: CaliberConfig) -> dict[str, Any]:
    if config.profile == "reference" and not REFERENCE_MCP_ALLOWLIST:
        return {
            **_preview_only(spec),
            "status": UNAVAILABLE,
            "reason_code": REASON_MCP_NOT_IN_PROFILE,
            "message": (
                f"{spec.label} are unavailable under the reference profile: it lists no MCP command or "
                "host, so no server is reachable."
            ),
        }
    return _preview_only(spec)


def _openapi_integrations(spec: AdapterSpec, _config: CaliberConfig) -> dict[str, Any]:
    return _preview_only(spec)


_RESOLVERS: Final[dict[str, Callable[[AdapterSpec, CaliberConfig], dict[str, Any]]]] = {
    DOCUMENT_REQUEST_CONNECTOR: _document_request_connector,
    RELEASE_EVALUATION: _release_evaluation,
    MCP_SERVERS: _mcp_servers,
    OPENAPI_INTEGRATIONS: _openapi_integrations,
}


def adapter_capabilities(config: CaliberConfig) -> dict[str, dict[str, Any]]:
    """The ``adapters`` object of ``GET /capabilities``: every adapter, each ``supported``, ``preview`` or ``unavailable``.

    Derived from the live configuration and the pinned :data:`SUPPORT_EVIDENCE`, never from a setting that says
    "supported". A closed area flag wins over everything else (the adapter is unavailable, with the 404's own code and
    text); an adapter with no evidence entry is never above ``preview``.
    """
    return {
        spec.id: _gate_off(spec, config) or _RESOLVERS[spec.id](spec, config) for spec in ADAPTERS
    }


def capabilities_block(config: CaliberConfig) -> dict[str, dict[str, Any]]:
    """The ``features`` object of ``GET /capabilities``: every flag, on or off."""
    return {
        flag.id: {
            "enabled": feature_enabled(config, flag.id),
            "env_var": flag.env_var,
            "label": flag.label,
            "stability": flag.stability,
        }
        for flag in FEATURES
    }


class FeatureGateMiddleware:
    """Refuse requests to a disabled area before they reach auth, CSRF or a handler.

    Raw ASGI, like the other middlewares, so it adds no per-request cost beyond a prefix
    check on API paths. It reads the flag from ``app.state.config`` on every request, so a
    test (or an embedding application) that swaps the config sees the change.
    """

    def __init__(self, app: Any, *, config: CaliberConfig) -> None:
        self._app = app
        self._config = config

    def _live_config(self, scope: dict[str, Any]) -> CaliberConfig:
        """``app.state.config`` when present, else the config this was built with."""
        state = getattr(scope.get("app"), "state", None)
        live = getattr(state, "config", None)
        return live if live is not None else self._config

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            flag = feature_for_path(str(scope.get("path", "")))
            if flag is not None and not feature_enabled(self._live_config(scope), flag.id):
                await _refuse(flag, send)
                return
        await self._app(scope, receive, send)


async def _refuse(flag: FeatureFlag, send: Any) -> None:
    body = json.dumps(
        {
            "detail": disabled_message(flag),
            "status_code": 404,
            "reason_code": REASON_CODE,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "ADAPTERS",
    "FEATURES",
    "PREFIX",
    "PREVIEW",
    "REASON_CODE",
    "SUPPORTED",
    "SUPPORT_EVIDENCE",
    "SUPPORT_STATUSES",
    "UNAVAILABLE",
    "AdapterSpec",
    "FeatureFlag",
    "FeatureGateMiddleware",
    "SupportEvidence",
    "adapter_capabilities",
    "capabilities_block",
    "disabled_message",
    "feature_enabled",
    "feature_for_path",
]
