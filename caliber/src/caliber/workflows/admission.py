"""Admission: pin, authorize and record a run in the transaction that creates it (B02.2a, #680).

A run that is *admitted* has, from the moment it is committed, exactly one immutable run manifest
(:mod:`caliber.run_manifest`), one grant (:mod:`caliber.policy.grants`) and one recorded policy decision
(:mod:`caliber.policy.decisions`); a run that is refused has none of the three and no row at all, because every
step happens in the caller's transaction and a refusal rolls it back. Nothing is filled in later: the manifest is built
and its pins resolved *before* the run row is written, and the run is written with ``manifest_legacy = false``.

The path, for one run:

1. :func:`prepare_admission` (reads, writes nothing): the workflow must be in a project; the subject is the typed
   user who is calling and the workload the workflow pinned to its version digest; every dependency is taken from the
   version's sealed deployment bundle (a version that was never compiled, or whose bundle is unresolved or altered,
   is refused); the configured rule bundle, when there is one, is loaded and verified (:func:`pin_rule_bundle`); and
   the manifest is built by :func:`caliber.run_manifest.build_run_manifest`.
2. The caller inserts the run (``manifest_legacy = false``) and flushes.
3. :func:`persist_admission`: issue the grant (never beyond the subject's current role), decide it under the policy,
   and, only on an allow, insert the manifest. A denial raises :class:`AdmissionDenied`; the caller rolls back.

Retries (:func:`check_retry_intent`): the caller has already authorized the request. An idempotency key that names an
existing run returns that run, with its original manifest, even if a deployment alias has since moved, when the
request is the same one (same workflow, release selector, input, files and project); a different request under the
same key is a ``409``.

Not admitted, deliberately and documented in ``docs/operate/reference-profile.md``: runs started by the cron
scheduler and by a service token (no typed subject exists for either yet) stay legacy runs.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.auth import CaliberIdentity
from caliber.db.models import (
    CaliberRunManifest,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.ids import new_workflow_run_id
from caliber.policy import grants
from caliber.policy.decisions import REFERENCE_POLICY_V1, DecisionInputError, Policy, decide
from caliber.policy.principal import AUTH_METHODS, PrincipalError, Subject, Workload
from caliber.run_manifest import (
    BuiltManifest,
    DeploymentScope,
    KnowledgeBasePin,
    ManifestError,
    PinnedRuleBundle,
    ResolvedPins,
    build_run_manifest,
    new_manifest_id,
    pin_rule_bundle,
)
from caliber.workflows.deployment_bundle import verify_bundle
from caliber.workflows.manifest import AgentNode, parse_manifest

logger = logging.getLogger("caliber.workflows.admission")

#: Dependency kinds a run actually uses. An unresolved evaluation dataset does not stop a run (it is for evaluation);
#: an unresolved one of these does, because the run could not be pinned to what it executes.
RUNTIME_DEPENDENCIES: Final = frozenset(
    {"prompt", "tool", "mcp_tool", "skill", "knowledge_base", "subworkflow", "managed_file"}
)

#: An agent whose model is ``inherit`` uses the deployment's default, which the provider route already names.
INHERITED_MODEL: Final = frozenset({"inherit", ""})

#: The action a run's grant authorizes (``resource_access.PROJECT_ACTIONS``) and the resource it names.
RUN_ACTION: Final = "resource.execute"

# Refusal codes of :class:`AdmissionDenied` (policy denials carry the decision's own reason codes).
PROJECT_REQUIRED: Final = "project_required"
SUBJECT_INVALID: Final = "subject_invalid"
WORKLOAD_UNAVAILABLE: Final = "workload_unavailable"
BUNDLE_UNAVAILABLE: Final = "bundle_unavailable"
MANIFEST_REFUSED: Final = "manifest_refused"
GRANT_REFUSED: Final = "grant_refused"
POLICY_DENIED: Final = "policy_denied"
OVERRIDE_NOT_ADMITTED: Final = "manifest_override_not_admitted"
AUTHORITY_UNAVAILABLE: Final = "authority_unavailable"
INTENT_CONFLICT: Final = "intent_conflict"


class AdmissionDenied(Exception):  # noqa: N818 - a refusal, named for what it says
    """Admission refused the run. ``status`` is the HTTP status to answer with; the caller rolls back its transaction."""

    def __init__(
        self, code: str, message: str, *, status: int = 409, reasons: tuple[str, ...] = ()
    ) -> None:
        self.code = code
        self.status = status
        self.reasons = reasons
        super().__init__(message)


@dataclass(frozen=True)
class AdmissionSettings:
    """What admission needs from the deployment's configuration."""

    enabled: bool
    policy: Policy = REFERENCE_POLICY_V1
    grant_ttl_seconds: int = 86400
    rule_bundle_path: str = ""
    rule_bundle_sha256: str = ""
    llm_provider: str = "fake"

    @classmethod
    def from_config(cls, config: Any) -> AdmissionSettings:
        """Read the settings. Raises ``ValueError`` for a rule bundle path without its digest (or the reverse)."""
        path = str(getattr(config, "rule_bundle_path", "") or "").strip()
        digest = str(getattr(config, "rule_bundle_sha256", "") or "").strip()
        if bool(path) != bool(digest):
            raise ValueError(
                "CALIBER_RULE_BUNDLE_PATH and CALIBER_RULE_BUNDLE_SHA256 must be set together"
            )
        enabled = getattr(config, "profile", "") == "reference" or bool(
            getattr(config, "admission_enabled", False)
        )
        return cls(
            enabled=enabled,
            grant_ttl_seconds=int(getattr(config, "admission_grant_ttl_seconds", 86400)),
            rule_bundle_path=path,
            rule_bundle_sha256=digest,
            llm_provider=str(getattr(config, "llm_provider", "fake") or "fake"),
        )


@dataclass(frozen=True)
class PreparedAdmission:
    """Everything resolved before the run row exists."""

    run_id: str
    grant_id: str
    built: BuiltManifest
    subject: Subject
    workload: Workload
    purpose: str
    resource: str
    now: datetime


@dataclass(frozen=True)
class AdmissionRecord:
    """What admission stored, for the response and the tests."""

    run_id: str
    grant_id: str
    decision_id: str
    manifest_id: str
    manifest_digest: str


def new_run_id() -> str:
    """A fresh run id, allocated before the run exists so the manifest can name it."""
    return new_workflow_run_id()


def purpose_for(source: str) -> str:
    """Why the run exists, derived from how it was started (the manifest and the decision both carry it)."""
    return f"workflow_run:{source}"


def _subject(identity: CaliberIdentity, project_id: str) -> Subject:
    method = identity.credential_kind or "trusted_header"
    if method not in AUTH_METHODS:
        raise AdmissionDenied(
            SUBJECT_INVALID, f"the credential kind {method!r} cannot start an admitted run"
        )
    try:
        return Subject(user_id=identity.user_id, auth_method=method, project_id=project_id)
    except PrincipalError as exc:
        raise AdmissionDenied(
            SUBJECT_INVALID, f"the caller cannot be a run's subject: {exc}"
        ) from exc


def _dependencies(version: CaliberWorkflowVersion) -> tuple[str, list[dict[str, Any]]]:
    """The sealed bundle's digest and dependency lock; anything unsealed, altered or unresolved is refused."""
    compiled = version.compiled_bundle if isinstance(version.compiled_bundle, dict) else {}
    bundle = compiled.get("deployment_bundle")
    if not isinstance(bundle, dict):
        raise AdmissionDenied(
            BUNDLE_UNAVAILABLE,
            "this workflow version has no sealed deployment bundle: compile and publish it before running it",
        )
    verification = verify_bundle(bundle)
    if not verification.valid or not verification.digest:
        raise AdmissionDenied(
            BUNDLE_UNAVAILABLE, "the version's deployment bundle failed its integrity check"
        )
    dependencies = [item for item in bundle.get("dependencies", []) if isinstance(item, dict)]
    unresolved = sorted(
        f"{item.get('kind')}:{item.get('reference')}"
        for item in dependencies
        if item.get("kind") in RUNTIME_DEPENDENCIES and item.get("status") != "resolved"
    )
    if unresolved:
        raise AdmissionDenied(
            BUNDLE_UNAVAILABLE,
            f"the version has unresolved dependencies and cannot be pinned: {unresolved[:5]}",
        )
    return verification.digest, dependencies


def resolve_pins(version: CaliberWorkflowVersion, settings: AdmissionSettings) -> ResolvedPins:
    """The run's pins, from the version's sealed bundle; never from a mutable alias at run time."""
    digest, dependencies = _dependencies(version)
    prompts: list[str] = []
    tools: list[str] = []
    skills: list[str] = []
    knowledge: set[tuple[str, str]] = set()
    for item in dependencies:
        kind, reference = item.get("kind"), str(item.get("reference") or "")
        raw_version = item.get("version")
        if kind == "prompt":
            prompts.append(f"{reference.rsplit('@', 1)[0]}@{raw_version}")
        elif kind == "tool":
            tools.append(f"{reference}@{raw_version}")
        elif kind == "mcp_tool":
            tools.append(f"{reference}@{raw_version or item.get('digest') or ''}")
        elif kind == "skill":
            skills.append(f"{reference}@{raw_version}")
        elif kind == "knowledge_base":
            raw_snapshot = item.get("snapshot")
            snapshot: dict[str, Any] = raw_snapshot if isinstance(raw_snapshot, dict) else {}
            for entry in snapshot.get("versions", []) or []:
                if isinstance(entry, dict) and entry.get("version_id"):
                    knowledge.add(
                        (str(snapshot.get("knowledge_base_id")), str(entry["version_id"]))
                    )
        # eval datasets, child workflows and managed files are covered by the bundle digest.
    try:
        return ResolvedPins(
            workflow_version=version.version_id,
            bundle_digest=digest,
            prompt_versions=tuple(prompts),
            tool_versions=tuple(tools),
            skill_versions=tuple(skills),
            knowledge_base_versions=tuple(
                KnowledgeBasePin(kb, ver) for kb, ver in sorted(knowledge)
            ),
            model_routes=_model_routes(version, settings),
            policy_version=settings.policy.version,
        )
    except ManifestError as exc:
        raise AdmissionDenied(MANIFEST_REFUSED, f"{exc.code}: {exc}") from exc


def _model_routes(version: CaliberWorkflowVersion, settings: AdmissionSettings) -> tuple[str, ...]:
    """The provider and each agent's model on it: ``openai``, ``openai/gpt-4o``."""
    routes = {settings.llm_provider}
    try:
        manifest = parse_manifest(version.manifest)
    except Exception as exc:  # a stored version whose manifest no longer parses cannot be pinned
        raise AdmissionDenied(BUNDLE_UNAVAILABLE, "the version's manifest cannot be read") from exc
    routes |= {
        f"{settings.llm_provider}/{node.model}"
        for node in manifest.nodes.values()
        if isinstance(node, AgentNode) and node.model not in INHERITED_MODEL
    }
    return tuple(sorted(routes))


def prepare_admission(
    *,
    settings: AdmissionSettings,
    workflow: CaliberWorkflow,
    version: CaliberWorkflowVersion,
    identity: CaliberIdentity,
    source: str,
    now: datetime,
    run_tenant_id: str,
) -> PreparedAdmission:
    """Resolve and validate everything, building the manifest; writes nothing. Raises :class:`AdmissionDenied`."""
    if not workflow.project_id:
        raise AdmissionDenied(
            PROJECT_REQUIRED,
            "an admitted run needs a project-scoped workflow; this workflow has no project",
        )
    subject = _subject(identity, workflow.project_id)
    try:
        workload = Workload.for_workflow(workflow.name, version.manifest_hash)
    except PrincipalError as exc:
        raise AdmissionDenied(
            WORKLOAD_UNAVAILABLE,
            f"this workflow version has no usable digest to pin the workload to: {exc}",
        ) from exc
    pins = resolve_pins(version, settings)
    pinned: PinnedRuleBundle | None = None
    if settings.rule_bundle_path:
        try:
            pinned = pin_rule_bundle(settings.rule_bundle_path, settings.rule_bundle_sha256)
        except ManifestError as exc:
            raise AdmissionDenied(MANIFEST_REFUSED, f"{exc.code}: {exc}") from exc
    run_id = new_run_id()
    try:
        built = build_run_manifest(
            manifest_id=new_manifest_id(),
            workflow_run_id=run_id,
            pins=pins,
            subject=subject,
            workload=workload,
            purpose=purpose_for(source),
            scope=DeploymentScope.fixed(workflow.project_id),
            created_at=now,
            requested_tenant_id=run_tenant_id,
            rule_bundle=pinned,
            rule_bundles_enabled=bool(settings.rule_bundle_path),
        )
    except ManifestError as exc:
        raise AdmissionDenied(MANIFEST_REFUSED, f"{exc.code}: {exc}") from exc
    return PreparedAdmission(
        run_id=run_id,
        grant_id=f"gr-{uuid.uuid4().hex}",
        built=built,
        subject=subject,
        workload=workload,
        purpose=purpose_for(source),
        resource=f"workflow:{workflow.workflow_id}",
        now=now,
    )


def persist_admission(
    session: Session,
    prepared: PreparedAdmission,
    run: CaliberWorkflowRun,
    *,
    identity: CaliberIdentity,
    settings: AdmissionSettings,
) -> AdmissionRecord:
    """Issue the grant, decide it and store the manifest, in the caller's transaction (flushes, never commits).

    ``run`` is the already flushed row, created with ``manifest_legacy = false`` and ``prepared.run_id``. A refusal
    raises :class:`AdmissionDenied` and the caller must roll back: the run, the grant and the decision are one unit.
    """
    if run.workflow_run_id != prepared.run_id or run.manifest_legacy:
        raise AdmissionDenied(
            MANIFEST_REFUSED,
            "the run does not match the admission that was prepared for it",
            status=500,
        )
    scopes = frozenset(identity.scopes)
    try:
        grants.create_grant(
            session,
            grant_id=prepared.grant_id,
            run_id=prepared.run_id,
            subject=prepared.subject,
            workload=prepared.workload,
            project_id=str(run.project_id),
            allowed_actions=[RUN_ACTION],
            allowed_resources=[prepared.resource],
            policy_version=settings.policy.version,
            expires_at=prepared.now + timedelta(seconds=settings.grant_ttl_seconds),
            now=prepared.now,
            scopes=scopes,
        )
    except grants.GrantError as exc:
        raise AdmissionDenied(GRANT_REFUSED, f"the grant was refused: {exc}", status=403) from exc
    except SQLAlchemyError as exc:
        logger.warning("admission could not issue the grant", exc_info=True)
        raise AdmissionDenied(
            AUTHORITY_UNAVAILABLE, "the grant store is unavailable", status=503
        ) from exc
    try:
        decision = decide(
            session,
            run_id=prepared.run_id,
            grant_id=prepared.grant_id,
            subject=prepared.subject,
            workload=prepared.workload,
            action=RUN_ACTION,
            resource=prepared.resource,
            purpose=prepared.purpose,
            policy=settings.policy,
            scopes=scopes,
            now=prepared.now,
        )
    except DecisionInputError as exc:
        raise AdmissionDenied(POLICY_DENIED, f"the decision could not be asked: {exc}") from exc
    if not decision.allowed:
        reasons = decision.reason_codes or (AUTHORITY_UNAVAILABLE,)
        status = 503 if AUTHORITY_UNAVAILABLE in reasons else 403
        raise AdmissionDenied(
            POLICY_DENIED,
            f"the policy denied this run: {', '.join(reasons)}",
            status=status,
            reasons=reasons,
        )
    try:
        session.add(CaliberRunManifest(**prepared.built.row_values()))
        session.flush()
    except IntegrityError as exc:
        raise AdmissionDenied(MANIFEST_REFUSED, "the run already has a manifest") from exc
    return AdmissionRecord(
        run_id=prepared.run_id,
        grant_id=prepared.grant_id,
        decision_id=decision.decision_id,
        manifest_id=prepared.built.manifest_id,
        manifest_digest=prepared.built.manifest_digest,
    )


def check_retry_intent(
    existing: CaliberWorkflowRun,
    *,
    workflow_id: str,
    alias: str,
    input_text: str,
    input_files: list[dict[str, Any]] | None,
    project_id: str | None,
) -> None:
    """A request that reuses an idempotency key must be the request that created the run (409 otherwise).

    The caller has authorized the request already. The comparison is on the *selector* the original request named
    (``deployment_alias``), never on what the alias points at now, so a moved alias does not make a retry a
    different request and the original run and manifest come back.
    """
    original_files = (existing.summary or {}).get("input_files") or []
    same = (
        existing.workflow_id == workflow_id
        and (existing.deployment_alias or "") == (alias or "")
        and (existing.input_payload or "") == (input_text or "")
        and (existing.project_id or None) == (project_id or None)
        and list(original_files) == list(input_files or [])
    )
    if not same:
        raise AdmissionDenied(
            INTENT_CONFLICT,
            "this idempotency key was already used for a different request; use a new key for a new request",
        )


__all__ = [
    "AUTHORITY_UNAVAILABLE",
    "BUNDLE_UNAVAILABLE",
    "GRANT_REFUSED",
    "INTENT_CONFLICT",
    "MANIFEST_REFUSED",
    "OVERRIDE_NOT_ADMITTED",
    "POLICY_DENIED",
    "PROJECT_REQUIRED",
    "RUN_ACTION",
    "SUBJECT_INVALID",
    "WORKLOAD_UNAVAILABLE",
    "AdmissionDenied",
    "AdmissionRecord",
    "AdmissionSettings",
    "PreparedAdmission",
    "check_retry_intent",
    "new_run_id",
    "persist_admission",
    "prepare_admission",
    "purpose_for",
    "resolve_pins",
]
