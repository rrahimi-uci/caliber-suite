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
request is the same one; a different request under the same key is a ``409``. "The same request" is the canonical
*intent* (:func:`intent_of`: the workflow, the release and version selectors as named, the input, the purpose and the
server-derived scope), and its SHA-256 (:func:`request_digest_of`) is stored in the immutable manifest, so the
comparison does not depend on anything that can change after admission.

The cron scheduler and service-token route use a server-created :class:`~caliber.policy.principal.ServicePrincipal`
when admission is enabled. The principal is bound to the local tenant, project, workload, purpose and allowed
action/resource set; it is never supplied by the caller and does not use human project membership. When admission is
disabled, those paths remain explicitly labelled legacy for development compatibility.
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.auth import CaliberIdentity
from caliber.context.sources import SourceNotApproved, require_approved, source_resource
from caliber.db.models import (
    CaliberKnowledgeBaseVersion,
    CaliberRunManifest,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.ids import new_workflow_run_id
from caliber.policy import grants
from caliber.policy.decisions import (
    REFERENCE_POLICY_V1,
    DecisionInputError,
    Policy,
    PolicyDecision,
    audit_decision,
    decide,
)
from caliber.policy.principal import (
    AUTH_METHODS,
    SERVICE_PRINCIPAL_AUTH_METHOD,
    PrincipalError,
    ServicePrincipal,
    ServicePrincipalError,
    Subject,
    Workload,
)
from caliber.run_manifest import (
    BuiltManifest,
    DeploymentScope,
    KnowledgeBasePin,
    ManifestError,
    PinnedRuleBundle,
    ResolvedPins,
    build_run_manifest,
    canonical_bytes,
    digest_of,
    new_manifest_id,
    pin_rule_bundle,
    read_manifest,
)
from caliber.workflows.deployment_bundle import verify_bundle
from caliber.workflows.manifest import AgentNode, compute_manifest_hash, parse_manifest

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
#: The action a run's grant carries for each context source it may read (B07.2).
CONTEXT_READ_ACTION: Final = "context.read"

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
SOURCE_NOT_APPROVED: Final = "source_not_approved"
SERVICE_PRINCIPAL_REQUIRED: Final = "service_principal_required"


class AdmissionDenied(Exception):  # noqa: N818 - a refusal, named for what it says
    """Admission refused the run. ``status`` is the HTTP status to answer with; the caller rolls back its transaction."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        reasons: tuple[str, ...] = (),
        decision: PolicyDecision | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.reasons = reasons
        #: The policy decision that refused the run, when one did: its row is rolled back with the run, so
        #: :func:`record_denial` writes its audit row afterwards.
        self.decision = decision
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
    #: The model an agent that says ``inherit`` gets (``llm_diagnosis_model``); pinned as ``provider/model``.
    default_model: str = ""
    #: A run must be started against a workspace release and environment (runtime lineage): the reference profile.
    lineage_required: bool = False
    #: Every pinned knowledge-base version must be an approved context source of the project: the reference profile.
    approved_sources_required: bool = False

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
            default_model=str(getattr(config, "llm_diagnosis_model", "") or ""),
            lineage_required=getattr(config, "profile", "") == "reference",
            approved_sources_required=getattr(config, "profile", "") == "reference",
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
    #: The sealed, resolved manifest the run executes: what the pins describe (stored as the run's snapshot).
    execution_manifest: dict[str, Any]
    #: The context sources (``(knowledge_base_id, version_id)``) the run may read, each granted ``context.read`` on its
    #: own resource: set only when approved sources are required (the reference profile), empty otherwise.
    sources: frozenset[tuple[str, str]] = frozenset()
    #: The server-created binding, when this run entered through cron or a service token.
    service_principal: ServicePrincipal | None = None


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


def run_resource(workflow_id: str) -> str:
    """The resource a run's grant and its decisions name: the workflow it executes."""
    return f"workflow:{workflow_id}"


def purpose_for(source: str) -> str:
    """Why the run exists, derived from how it was started (the manifest and the decision both carry it)."""
    return f"workflow_run:{source}"


def _subject(identity: CaliberIdentity, project_id: str) -> Subject:
    method = identity.credential_kind or "trusted_header"
    if method not in AUTH_METHODS | {SERVICE_PRINCIPAL_AUTH_METHOD}:
        raise AdmissionDenied(
            SUBJECT_INVALID, f"the credential kind {method!r} cannot start an admitted run"
        )
    try:
        return Subject(user_id=identity.user_id, auth_method=method, project_id=project_id)
    except PrincipalError as exc:
        raise AdmissionDenied(
            SUBJECT_INVALID, f"the caller cannot be a run's subject: {exc}"
        ) from exc


def _dependencies(
    version: CaliberWorkflowVersion,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """The sealed bundle's digest, dependency lock and resolved manifest; anything unsealed, altered or unresolved is
    refused."""
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
    unsealed_children = sorted(
        str(item.get("reference"))
        for item in dependencies
        if item.get("kind") == "subworkflow"
        and not (isinstance(item.get("snapshot"), dict) and item["snapshot"].get("bundle_digest"))
    )
    if unsealed_children:
        raise AdmissionDenied(
            BUNDLE_UNAVAILABLE,
            "a child workflow has no sealed bundle to pin (publish it, then compile this version again): "
            f"{unsealed_children[:5]}",
        )
    resolved = bundle["resolved_manifest"]  # verify_bundle guarantees an object
    return verification.digest, dependencies, deepcopy(resolved)


def resolve_pins(version: CaliberWorkflowVersion, settings: AdmissionSettings) -> ResolvedPins:
    """The run's pins, from the version's sealed bundle; never from a mutable alias at run time."""
    return _resolve(version, settings)[0]


def _resolve(
    version: CaliberWorkflowVersion, settings: AdmissionSettings
) -> tuple[ResolvedPins, dict[str, Any]]:
    """The pins and the sealed resolved manifest the run will execute (what the pins describe)."""
    digest, dependencies, execution_manifest = _dependencies(version)
    prompts: set[str] = set()
    tools: set[str] = set()
    skills: set[str] = set()
    knowledge: set[tuple[str, str]] = set()
    for item in dependencies:
        kind, reference = item.get("kind"), str(item.get("reference") or "")
        raw_version = item.get("version")
        if kind == "prompt":
            prompts.add(f"{reference.rsplit('@', 1)[0]}@{raw_version}")
        elif kind == "tool":
            tools.add(f"{reference}@{raw_version}")
        elif kind == "mcp_tool":
            tools.add(f"{reference}@{raw_version or item.get('digest') or ''}")
        elif kind == "skill":
            skills.add(f"{reference}@{raw_version}")
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
        pins = ResolvedPins(
            workflow_version=version.version_id,
            bundle_digest=digest,
            prompt_versions=tuple(sorted(prompts)),
            tool_versions=tuple(sorted(tools)),
            skill_versions=tuple(sorted(skills)),
            knowledge_base_versions=tuple(
                KnowledgeBasePin(kb, ver) for kb, ver in sorted(knowledge)
            ),
            model_routes=_model_routes(version, settings),
            policy_version=settings.policy.version,
        )
        return pins, execution_manifest
    except ManifestError as exc:
        raise AdmissionDenied(MANIFEST_REFUSED, f"{exc.code}: {exc}") from exc


_MAX_CHILD_DEPTH: Final = 8


def _refuse_source(message: str) -> AdmissionDenied:
    return AdmissionDenied(SOURCE_NOT_APPROVED, message, status=403, reasons=(SOURCE_NOT_APPROVED,))


def _child_sources(
    session: Session, snapshot: dict[str, Any], depth: int, seen: set[str]
) -> set[tuple[str, str]]:
    """The knowledge-base versions a pinned child workflow reads (its own sealed bundle, checked against the pin)."""
    if depth >= _MAX_CHILD_DEPTH:
        raise _refuse_source("child workflows are nested too deeply to check their context sources")
    child = session.get(CaliberWorkflowVersion, str(snapshot.get("version_id") or ""))
    if child is None:
        raise _refuse_source(
            "a child workflow cannot be read, so its context sources cannot be checked"
        )
    if _dependencies(child)[0] != snapshot.get("bundle_digest"):
        raise _refuse_source(
            "a child workflow is not the one this version was compiled against, so its context sources "
            "cannot be checked"
        )
    return _knowledge_sources(session, child, depth=depth + 1, seen=seen)


def _query_sources(session: Session, manifest: dict[str, Any]) -> set[tuple[str, str]]:
    """The versions named on the resolved manifest's knowledge-query nodes; a node that takes them from an input
    port cannot be pinned, so it is refused."""
    raw_nodes = manifest.get("nodes")
    nodes: dict[str, Any] = raw_nodes if isinstance(raw_nodes, dict) else {}
    queries = {
        str(node_id): node
        for node_id, node in nodes.items()
        if isinstance(node, dict) and node.get("type") == "knowledge_query"
    }
    for edge in manifest.get("edges", []) or []:
        if not isinstance(edge, dict) or str(edge.get("to")) not in queries:
            continue
        raw_map = edge.get("map")
        mapped: dict[str, Any] = raw_map if isinstance(raw_map, dict) else {}
        if "version_ids" in mapped.values():
            raise _refuse_source(
                f"knowledge query {edge.get('to')!r} takes its versions from an input, so they cannot be "
                "pinned or approved: name the versions on the node"
            )
    found: set[tuple[str, str]] = set()
    for node in queries.values():
        for version_id in node.get("version_ids") or []:
            row = session.get(CaliberKnowledgeBaseVersion, str(version_id))
            named = str(node.get("knowledge_base_id") or "unknown")
            found.add((row.knowledge_base_id if row is not None else named, str(version_id)))
    return found


def _knowledge_sources(
    session: Session,
    version: CaliberWorkflowVersion,
    *,
    depth: int = 0,
    seen: set[str] | None = None,
) -> set[tuple[str, str]]:
    """Every ``(knowledge_base_id, version_id)`` this version, *and the child workflows it runs inline*, can read.

    The sealed bundle's dependency lock lists the knowledge bases the nodes name; a query node that names only
    ``version_ids`` has no such entry, so the resolved manifest's nodes are read too (:func:`_query_sources`). A child
    workflow is read from its own sealed bundle (it must be the one the parent pinned), recursively and
    depth-bounded.
    """
    seen = seen if seen is not None else set()
    if version.version_id in seen:
        return set()
    seen.add(version.version_id)
    _digest, dependencies, manifest = _dependencies(version)
    found = _query_sources(session, manifest)
    for item in dependencies:
        raw_snapshot = item.get("snapshot")
        snapshot: dict[str, Any] = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        if item.get("kind") == "knowledge_base":
            for entry in snapshot.get("versions", []) or []:
                if isinstance(entry, dict) and entry.get("version_id"):
                    found.add((str(snapshot.get("knowledge_base_id")), str(entry["version_id"])))
        elif item.get("kind") == "subworkflow":
            found |= _child_sources(session, snapshot, depth, seen)
    return found


def _require_approved_sources(
    session: Session,
    workflow: CaliberWorkflow,
    identity: CaliberIdentity,
    version: CaliberWorkflowVersion,
    pins: ResolvedPins,
) -> frozenset[tuple[str, str]]:
    """Reference profile: every knowledge-base version the run can read must be approved for the project; returns
    them (each becomes a ``context.read`` grant).

    Only a caller who may run the workflow at all learns what the project has approved (the role check comes
    first), and the refusal names every offending ``knowledge_base_id@version_id``.
    """
    if identity.credential_kind != SERVICE_PRINCIPAL_AUTH_METHOD:
        from caliber.resource_access import require_project_access_if_scoped  # noqa: PLC0415 - avoids an import cycle

        try:
            require_project_access_if_scoped(
                session, identity, workflow.project_id, "resource.execute"
            )
        except HTTPException as exc:
            raise AdmissionDenied(
                GRANT_REFUSED, "the caller may not run this workflow", status=403
            ) from exc
    sources = _knowledge_sources(session, version) | {
        (pin.knowledge_base_id, pin.version_id) for pin in pins.knowledge_base_versions
    }
    try:
        require_approved(session, str(workflow.project_id), sources)
    except SourceNotApproved as exc:
        raise _refuse_source(str(exc)) from exc
    return frozenset(sources)


def _model_routes(version: CaliberWorkflowVersion, settings: AdmissionSettings) -> tuple[str, ...]:
    """The provider and each agent's model on it: ``openai``, ``openai/gpt-4o``."""
    routes = {settings.llm_provider}
    try:
        manifest = parse_manifest(version.manifest)
    except Exception as exc:  # a stored version whose manifest no longer parses cannot be pinned
        raise AdmissionDenied(BUNDLE_UNAVAILABLE, "the version's manifest cannot be read") from exc
    agents = [node for node in manifest.nodes.values() if isinstance(node, AgentNode)]
    routes |= {
        f"{settings.llm_provider}/{node.model}"
        for node in agents
        if node.model not in INHERITED_MODEL
    }
    if settings.default_model and any(node.model in INHERITED_MODEL for node in agents):
        # An agent that says ``inherit`` runs on the deployment's default model: pin it, so changing it later is
        # a change the worker refuses (``model_route_changed``) rather than one the run silently follows.
        routes.add(f"{settings.llm_provider}/{settings.default_model}")
    return tuple(sorted(routes))


def prepare_admission(
    *,
    session: Session,
    settings: AdmissionSettings,
    workflow: CaliberWorkflow,
    version: CaliberWorkflowVersion,
    identity: CaliberIdentity,
    source: str,
    now: datetime,
    run_tenant_id: str,
    intent: dict[str, Any],
    service_principal: ServicePrincipal | None = None,
) -> PreparedAdmission:
    """Resolve and validate everything, building the manifest; writes nothing. Raises :class:`AdmissionDenied`.

    ``intent`` is the request's canonical intent (:func:`intent_of`); its digest is recorded in the manifest.
    """
    if not workflow.project_id:
        raise AdmissionDenied(
            PROJECT_REQUIRED,
            "an admitted run needs a project-scoped workflow; this workflow has no project",
        )
    if (
        identity.credential_project_id is not None
        and identity.credential_project_id != workflow.project_id
    ):
        raise AdmissionDenied(
            GRANT_REFUSED,
            "the credential is bound to a different project than this workflow's",
            status=403,
        )
    if identity.active_project_id is not None and identity.active_project_id != workflow.project_id:
        raise AdmissionDenied(
            GRANT_REFUSED,
            "the caller is acting in a different project than this workflow's",
            status=403,
        )
    subject = _subject(identity, workflow.project_id)
    if service_principal is not None and (
        identity.user_id != service_principal.principal_id
        or identity.credential_kind != SERVICE_PRINCIPAL_AUTH_METHOD
        or identity.credential_id != service_principal.principal_id
    ):
        raise AdmissionDenied(
            SERVICE_PRINCIPAL_REQUIRED,
            "the authenticated service principal does not match the server binding",
            status=403,
        )
    try:
        workload = Workload.for_workflow(workflow.name, version.manifest_hash)
    except PrincipalError as exc:
        raise AdmissionDenied(
            WORKLOAD_UNAVAILABLE,
            f"this workflow version has no usable digest to pin the workload to: {exc}",
        ) from exc
    pins, execution_manifest = _resolve(version, settings)
    sources: frozenset[tuple[str, str]] = frozenset()
    if settings.approved_sources_required:
        sources = _require_approved_sources(session, workflow, identity, version, pins)
    pinned: PinnedRuleBundle | None = None
    if settings.rule_bundle_path:
        try:
            pinned = pin_rule_bundle(settings.rule_bundle_path, settings.rule_bundle_sha256)
        except ManifestError as exc:
            raise AdmissionDenied(MANIFEST_REFUSED, f"{exc.code}: {exc}") from exc
    run_id = new_run_id()
    grant_id = (
        f"gr-{uuid.uuid4().hex}"  # named in the manifest, so a worker finds exactly this grant
    )
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
            request_digest=request_digest_of(intent),
            grant_id=grant_id,
        )
    except ManifestError as exc:
        raise AdmissionDenied(MANIFEST_REFUSED, f"{exc.code}: {exc}") from exc
    bound_service_principal = service_principal
    if service_principal is not None:
        try:
            service_principal.validate_binding(
                tenant_id=run_tenant_id,
                project_id=workflow.project_id,
                workflow_id=workflow.workflow_id,
                workload=workload,
                purpose=purpose_for(source),
                action=RUN_ACTION,
                resource=run_resource(workflow.workflow_id),
                now=now,
            )
            bound_service_principal = service_principal.with_admission_resources(
                actions=frozenset({CONTEXT_READ_ACTION}) if sources else frozenset(),
                resources=frozenset(source_resource(kb, version_id) for kb, version_id in sources),
            )
        except ServicePrincipalError as exc:
            raise AdmissionDenied(exc.code, str(exc), status=403) from exc
    return PreparedAdmission(
        run_id=run_id,
        grant_id=grant_id,
        built=built,
        subject=subject,
        workload=workload,
        purpose=purpose_for(source),
        resource=run_resource(workflow.workflow_id),
        now=now,
        execution_manifest=execution_manifest,
        sources=sources,
        service_principal=bound_service_principal,
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
            allowed_actions=[RUN_ACTION, *([CONTEXT_READ_ACTION] if prepared.sources else [])],
            allowed_resources=[
                prepared.resource,
                *(source_resource(kb, version) for kb, version in sorted(prepared.sources)),
            ],
            policy_version=settings.policy.version,
            expires_at=prepared.now + timedelta(seconds=settings.grant_ttl_seconds),
            now=prepared.now,
            scopes=scopes,
            service_principal=prepared.service_principal,
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
            decision=decision,
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


def record_denial(session: Session, exc: AdmissionDenied) -> None:
    """Audit a refusal the policy made, *after* the caller rolled back (the run, grant and decision rows went with it).

    Written in its own short transaction from the decision's identifiers and reasons; an unrecorded decision (the store
    was down) has its own log line instead, and a failure here is logged, never raised: the caller is already refusing.
    """
    decision = exc.decision
    if decision is None or not decision.persisted:
        return
    try:
        audit_decision(session, decision)
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        logger.error(
            "admission denial could not be audited",
            extra={"decision_id": decision.decision_id, "run_id": decision.run_id},
        )


def intent_of(
    *,
    workflow_id: str,
    alias: str,
    actor: str,
    input_text: str,
    project_id: str | None,
    input_files: list[dict[str, Any]] | None = None,
    version_selector: str | None = None,
    workspace_release_id: str | None = None,
    environment_id: str | None = None,
    model_id: str | None = None,
    config_sha256: str | None = None,
    priority: int = 0,
    session_id: str | None = None,
    source: str = "api",
    input_json: str | None = None,
) -> dict[str, Any]:
    """The canonical intent of a request: what was *asked for* (selectors, input, who, why, where), never what the
    selectors resolve to now. ``source`` gives the purpose and the project the server-derived scope. Stored on the
    run when it is admitted, its digest in the manifest, and compared on every retry of its key. ``input_json`` is the
    canonical JSON of an input that was not text (so ``{"b": 2, "a": 1}`` and ``{"a": 1, "b": 2}`` are one request and
    the string ``"{'a': 1}"`` is another)."""
    scope = DeploymentScope.fixed(project_id or None)
    return {
        "workflow_id": workflow_id,
        "release_selector": alias or "",
        "version_selector": version_selector or None,
        "actor": actor,
        "input": ""
        if input_json
        else input_text or "",  # an object's text is its repr, which is order-dependent
        "input_json": input_json or None,
        "input_files": [dict(item) for item in input_files or []],
        "project_id": project_id or None,
        "workspace_release_id": workspace_release_id or None,
        "environment_id": environment_id or None,
        "model_id": model_id or None,
        "config_sha256": config_sha256 or None,
        "priority": int(priority or 0),
        "session_id": session_id or None,
        "purpose": purpose_for(source),
        "scope": {
            "tenant_id": scope.tenant_id,
            "project_id": scope.project_id,
            "region": scope.region,
            "residency": scope.residency,
        },
    }


def request_digest_of(intent: dict[str, Any]) -> str:
    """The SHA-256 of the canonical bytes of an intent: the manifest's ``request_digest``."""
    return digest_of(canonical_bytes(intent))


#: What an intent recorded before ``purpose``, ``scope`` and ``input_json`` existed carried; a stored intent that lacks
#: any of these is not an intent this code wrote, and is refused rather than matched on whatever it has.
_FIRST_INTENT_KEYS: Final = frozenset(
    {
        "workflow_id",
        "release_selector",
        "version_selector",
        "actor",
        "input",
        "input_files",
        "project_id",
        "workspace_release_id",
        "environment_id",
        "model_id",
        "config_sha256",
        "priority",
        "session_id",
    }
)


def _manifest_request_digest(session: Session, run: CaliberWorkflowRun) -> str | None:
    """The ``request_digest`` the run's manifest recorded, or ``None`` for a legacy run or a manifest written before the
    field existed. An admitted run whose manifest is missing or unreadable is refused (never guessed around): admission
    writes the two together."""
    if run.manifest_legacy is not False:
        return None
    row = session.execute(
        select(CaliberRunManifest).where(CaliberRunManifest.workflow_run_id == run.workflow_run_id)
    ).scalar_one_or_none()
    if row is None:
        raise AdmissionDenied(
            MANIFEST_REFUSED,
            "an admitted run has no stored manifest, so the request cannot be compared with it",
            status=500,
        )
    try:
        record = read_manifest(bytes(row.manifest_bytes), row.manifest_digest)
    except ManifestError as exc:
        raise AdmissionDenied(
            MANIFEST_REFUSED,
            f"the run's stored manifest cannot be read ({exc.code}), so the request cannot be compared with it",
            status=500,
        ) from exc
    digest = record.get("request_digest")
    return digest if isinstance(digest, str) else None


def check_retry_intent(
    session: Session, existing: CaliberWorkflowRun, intent: dict[str, Any]
) -> None:
    """A request that reuses an idempotency key must be the request that created the run, from the same caller
    (409 otherwise, with one message for every kind of difference, so it is no oracle).

    The caller has authorized the request already. The comparison is on the *selectors* the original request named,
    never on what an alias points at now, so a moved alias does not make a retry a different request and the
    original run and manifest come back. The authority is the ``request_digest`` in the run's immutable manifest;
    a run admitted before that field existed is compared on the intent stored in its summary (the fields it
    recorded), and a legacy run on the fields the run row itself keeps.
    """
    recorded = _manifest_request_digest(session, existing)
    stored = (existing.summary or {}).get("admission_intent")
    if recorded is not None:
        same = recorded == request_digest_of(intent)
    elif isinstance(stored, dict) and stored.keys() >= _FIRST_INTENT_KEYS:
        same = all(intent.get(key) == value for key, value in stored.items())
    elif existing.manifest_legacy is False:
        same = False  # admitted, but neither a digest nor a complete intent to compare with
    else:
        same = (
            existing.workflow_id == intent["workflow_id"]
            and (existing.deployment_alias or "") == intent["release_selector"]
            and (existing.input_payload or "") == intent["input"]
            and (existing.project_id or None) == intent["project_id"]
            and list((existing.summary or {}).get("input_files") or []) == intent["input_files"]
            and (existing.created_by or "") == intent["actor"]
        )
    if not same:
        raise AdmissionDenied(
            INTENT_CONFLICT,
            "this idempotency key was already used for a different request; use a new key for a new request",
        )


def admitted_manifest_metadata(
    version: CaliberWorkflowVersion, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """The run summary's manifest fields for an admitted run: it executes the sealed resolved manifest, so that is the
    snapshot (``manifest_mode: snapshot``) and its hash; the authoring version's hash stays alongside."""
    return {
        "manifest_mode": "snapshot",
        "manifest_hash": compute_manifest_hash(snapshot),
        "workflow_version_number": version.version_number,
        "authoring_manifest_hash": version.manifest_hash,
    }


__all__ = [
    "AUTHORITY_UNAVAILABLE",
    "BUNDLE_UNAVAILABLE",
    "CONTEXT_READ_ACTION",
    "GRANT_REFUSED",
    "INTENT_CONFLICT",
    "MANIFEST_REFUSED",
    "OVERRIDE_NOT_ADMITTED",
    "POLICY_DENIED",
    "PROJECT_REQUIRED",
    "RUN_ACTION",
    "SOURCE_NOT_APPROVED",
    "SUBJECT_INVALID",
    "WORKLOAD_UNAVAILABLE",
    "AdmissionDenied",
    "AdmissionRecord",
    "AdmissionSettings",
    "PreparedAdmission",
    "admitted_manifest_metadata",
    "check_retry_intent",
    "intent_of",
    "new_run_id",
    "persist_admission",
    "prepare_admission",
    "purpose_for",
    "record_denial",
    "request_digest_of",
    "resolve_pins",
    "run_resource",
]
