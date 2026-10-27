"""The context manifest: what one agent step was given, ordered, labelled, authorized and digest-bound (B07.3a, #704).

A :class:`ContextManifest <caliber.contracts>` (kind ``context_manifest``, schema ``v1``, ADR 0003) lists, in the order the
provider sees them, every segment of an agent step's context with:

* a **trust tier**: ``instruction`` (what the platform and the workflow's author say) or ``data`` (everything else:
  the user's input, retrieved text, tool output, history, memory). Only a system or workflow instruction is an
  instruction, and every one is; **retrieved text is data**, so text a source contains can never be promoted to an
  instruction by being retrieved;
* its **source** (prompt, input, knowledge-base version, tool, memory, history), the **retrieval** and **transform**
  that produced it, and the digest of its content (the content itself is not stored here);
* for payload loaded from a knowledge base or memory, the id of the recorded ``context.read`` **authorization
  decision** that allowed it (:mod:`caliber.context.authorize`): a segment that names a source but no decision is
  refused, and :func:`store_context_manifest` checks each id against the recorded decision (an allow, for this run,
  of that exact source);
* its **position**, the **omitted** and **redacted** items and the **token allocation**.

The record is stored once as canonical JSON with its SHA-256 (it has no digest field: the digest excludes itself), unique
per ``(run, step request, revision)``: re-assembling a step is a new revision, never an edit. Reading it back
(:func:`read_context_manifest`) checks the digest, the schema (an unknown version is refused, never read as the nearest)
and every rule above again, so a reordered or relabelled record is caught twice (by the digest and by the positions).
Nothing here talks to a provider: B08 consumes the record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.context.sources import source_resource
from caliber.contracts import ContractValidationError, UnsupportedSchemaVersion, validate_record
from caliber.db.models import CaliberContextManifest, CaliberPolicyDecision
from caliber.db.transactions import begin_sqlite_transaction
from caliber.run_manifest import (
    FIXED_REGION,
    FIXED_RESIDENCY,
    FIXED_TENANT_ID,
    DeploymentScope,
    canonical_bytes,
    digest_of,
)

KIND: Final = "context_manifest"
SCHEMA_VERSION: Final = "v1"
INSTRUCTION: Final = "instruction"
DATA: Final = "data"
INSTRUCTION_KINDS: Final = frozenset({"system_instruction", "workflow_instruction"})
#: Sources whose payload is loaded from a store and so needs an authorization decision.
AUTHORIZED_SOURCES: Final = frozenset({"knowledge_base", "memory"})
CONTEXT_READ: Final = "context.read"

INVALID: Final = "invalid_manifest"
UNKNOWN_VERSION: Final = "unknown_version"
TIER_VIOLATION: Final = "tier_violation"
UNAUTHORIZED_PAYLOAD: Final = "unauthorized_payload"
ORDERING: Final = "ordering"
TOKENS: Final = "tokens"
AUTHORIZATION_REJECTED: Final = "authorization_rejected"
DUPLICATE: Final = "duplicate_revision"
REVISION_GAP: Final = "revision_gap"
_MAX_BYTES: Final = 1 << 20


class ContextManifestError(ValueError):
    """The manifest cannot be built, read or stored; ``code`` is one of this module's constants."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ContextSegment:
    """One segment as the assembler hands it over: ``content`` is hashed and not kept."""

    tier: str
    kind: str
    source: Mapping[str, Any]
    content: str
    tokens: int
    retrieval: Mapping[str, Any] | None = None
    transform: Mapping[str, Any] | None = None
    authorization_decision_id: str | None = None

    def to_record(self, position: int) -> dict[str, Any]:
        record: dict[str, Any] = {
            "position": position,
            "tier": self.tier,
            "kind": self.kind,
            "source": dict(self.source),
            "content_digest": hashlib.sha256(self.content.encode("utf-8")).hexdigest(),
            "tokens": self.tokens,
        }
        if self.retrieval is not None:
            record["retrieval"] = dict(self.retrieval)
        if self.transform is not None:
            record["transform"] = dict(self.transform)
        if self.authorization_decision_id is not None:
            record["authorization_decision_id"] = self.authorization_decision_id
        return record


def _check_rules(record: Mapping[str, Any]) -> None:
    """The rules the schema cannot state: contiguous positions, authorization of every loaded source, token arithmetic.
    (The tier rule is the schema's own: an instruction is only a system or workflow instruction.)"""
    segments: Sequence[Mapping[str, Any]] = record["segments"]
    for index, segment in enumerate(segments):
        if segment["position"] != index:
            raise ContextManifestError(
                ORDERING,
                f"segment {index} has position {segment['position']}: the order was changed",
            )
        if segment["source"]["kind"] in AUTHORIZED_SOURCES and not segment.get(
            "authorization_decision_id"
        ):
            raise ContextManifestError(
                UNAUTHORIZED_PAYLOAD,
                f"segment {index} was loaded from a {segment['source']['kind']} with no authorization decision",
            )
    positions = len(segments)
    for item in record.get("redacted", []):
        if item["position"] >= positions:
            raise ContextManifestError(INVALID, "a redaction names a segment that does not exist")
    allocation = record["token_allocation"]
    total = sum(int(segment["tokens"]) for segment in segments)
    by_tier = {
        tier: sum(int(s["tokens"]) for s in segments if s["tier"] == tier)
        for tier in (INSTRUCTION, DATA)
    }
    if (
        allocation["used"] != total
        or allocation["instruction"] != by_tier[INSTRUCTION]
        or allocation["data"] != by_tier[DATA]
    ):
        raise ContextManifestError(TOKENS, "the token allocation does not add up to the segments")
    if allocation["used"] > allocation["budget"]:
        raise ContextManifestError(TOKENS, "the segments use more tokens than the budget allows")


def _classify(record: Mapping[str, Any]) -> ContextManifestError | None:
    """Name the first rule a record that fails the schema also breaks, where it can be read well enough to say."""
    segments = record.get("segments")
    if not isinstance(segments, list):
        return None
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not isinstance(segment.get("source"), dict):
            continue
        if segment.get("position") != index:
            return ContextManifestError(ORDERING, f"segment {index} is out of order")
        if (segment.get("tier") == INSTRUCTION) != (segment.get("kind") in INSTRUCTION_KINDS):
            return ContextManifestError(
                TIER_VIOLATION,
                f"segment {index}: only a system or workflow instruction is an instruction",
            )
        needs = (
            segment["source"].get("kind") in AUTHORIZED_SOURCES
            or segment.get("kind") == "retrieved_text"
        )
        if needs and not segment.get("authorization_decision_id"):
            return ContextManifestError(
                UNAUTHORIZED_PAYLOAD, f"segment {index} was loaded with no authorization decision"
            )
    return None


def _validate(record: Mapping[str, Any]) -> None:
    try:
        validate_record(KIND, record)
    except UnsupportedSchemaVersion as exc:
        raise ContextManifestError(UNKNOWN_VERSION, str(exc)) from exc
    except ContractValidationError as exc:
        named = _classify(record)
        if named is not None:
            raise named from exc
        raise ContextManifestError(INVALID, str(exc)) from exc
    _check_rules(record)


@dataclass(frozen=True)
class BuiltContextManifest:
    """The canonical bytes of a validated record and their SHA-256 (the record is derived from the bytes)."""

    manifest_bytes: bytes
    manifest_digest: str

    def __post_init__(self) -> None:
        read_context_manifest(self.manifest_bytes, self.manifest_digest)

    @property
    def record(self) -> dict[str, Any]:
        record: dict[str, Any] = json.loads(self.manifest_bytes)
        return record

    @property
    def context_manifest_id(self) -> str:
        return str(self.record["context_manifest_id"])

    @property
    def workflow_run_id(self) -> str:
        return str(self.record["workflow_run_id"])

    @property
    def step_request_id(self) -> str:
        return str(self.record["step_request_id"])

    @property
    def revision(self) -> int:
        return int(self.record["revision"])

    def row_values(self) -> dict[str, Any]:
        created = datetime.fromisoformat(str(self.record["created_at"]).replace("Z", "+00:00"))
        return {
            "context_manifest_id": self.context_manifest_id,
            "workflow_run_id": self.workflow_run_id,
            "step_request_id": self.step_request_id,
            "revision": self.revision,
            "schema_version": SCHEMA_VERSION,
            "manifest_bytes": self.manifest_bytes,
            "manifest_digest": self.manifest_digest,
            "created_at": created.astimezone(timezone.utc).replace(tzinfo=None),
        }


def build_context_manifest(
    *,
    context_manifest_id: str,
    workflow_run_id: str,
    step_request_id: str,
    revision: int,
    scope: DeploymentScope,
    segments: Sequence[ContextSegment],
    token_budget: int,
    created_at: datetime,
    omitted: Sequence[Mapping[str, Any]] = (),
    redacted: Sequence[Mapping[str, Any]] = (),
) -> BuiltContextManifest:
    """Build and validate the manifest of one agent step. Pure: raises :class:`ContextManifestError`.

    The segments are numbered in the order given (so the order is the assembler's, recorded once). ``scope`` is the
    deployment's fixed one (no organization switching).
    """
    if not isinstance(scope, DeploymentScope) or scope != DeploymentScope.fixed(scope.project_id):
        raise ContextManifestError(
            INVALID,
            f"this deployment is fixed to organization {FIXED_TENANT_ID!r}, region {FIXED_REGION!r} "
            f"and residency {FIXED_RESIDENCY!r}",
        )
    if created_at.tzinfo is None:
        raise ContextManifestError(INVALID, "created_at must be timezone-aware")
    records = [segment.to_record(position) for position, segment in enumerate(segments)]
    by_tier = {
        tier: sum(r["tokens"] for r in records if r["tier"] == tier) for tier in (INSTRUCTION, DATA)
    }
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "context_manifest_id": context_manifest_id,
        "workflow_run_id": workflow_run_id,
        "step_request_id": step_request_id,
        "revision": revision,
        "tenant_id": scope.tenant_id,
        "region": scope.region,
        "residency": scope.residency,
        "segments": records,
        "token_allocation": {
            "budget": token_budget,
            "used": sum(by_tier.values()),
            "instruction": by_tier[INSTRUCTION],
            "data": by_tier[DATA],
        },
        "created_at": created_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    if scope.project_id is not None:
        record["project_id"] = scope.project_id
    if omitted:
        record["omitted"] = [dict(item) for item in omitted]
    if redacted:
        record["redacted"] = [dict(item) for item in redacted]
    _validate(record)
    data = canonical_bytes(record)
    return BuiltContextManifest(manifest_bytes=data, manifest_digest=digest_of(data))


def read_context_manifest(
    manifest_bytes: bytes, manifest_digest: str | None = None
) -> dict[str, Any]:
    """Read a stored manifest back: digest, JSON, schema (unknown versions refused) and every rule re-checked."""
    if type(manifest_bytes) is not bytes or not manifest_bytes:
        raise ContextManifestError(INVALID, "there are no manifest bytes to read")
    if len(manifest_bytes) > _MAX_BYTES:
        raise ContextManifestError(INVALID, "the manifest is larger than 1 MiB")
    if manifest_digest is not None and digest_of(manifest_bytes) != manifest_digest:
        raise ContextManifestError(INVALID, "the manifest bytes do not match their digest")
    try:
        record = json.loads(
            manifest_bytes.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constants,
        )
    except (ValueError, RecursionError) as exc:
        raise ContextManifestError(INVALID, f"the manifest is not valid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise ContextManifestError(INVALID, "the manifest must be an object")
    _validate(record)
    canonical_bytes(record)  # a lone surrogate parses but can be neither stored nor digested
    return record


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a key appears twice")
    return dict(pairs)


def _no_constants(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


# ------------------------------------------------------------------------------------------ storage


def next_revision(session: Session, workflow_run_id: str, step_request_id: str) -> int:
    """The revision a new manifest of this step takes: one more than the latest stored, or 1."""
    latest = session.execute(
        select(func.max(CaliberContextManifest.revision)).where(
            CaliberContextManifest.workflow_run_id == workflow_run_id,
            CaliberContextManifest.step_request_id == step_request_id,
        )
    ).scalar_one()
    return int(latest or 0) + 1


def _verify_authorizations(session: Session, record: Mapping[str, Any]) -> None:
    """Every authorization id must be a recorded ``context.read`` allow, for this run, of that exact source."""
    for index, segment in enumerate(record["segments"]):
        decision_id = segment.get("authorization_decision_id")
        if decision_id is None:
            continue
        row = session.get(CaliberPolicyDecision, decision_id)
        source = segment["source"]
        if (
            row is None
            or row.result != "allow"
            or row.workflow_run_id != record["workflow_run_id"]
            or row.action != CONTEXT_READ
            or (
                source["kind"] == "knowledge_base"
                and row.resource
                != source_resource(source["knowledge_base_id"], source["version_id"])
            )
        ):
            raise ContextManifestError(
                AUTHORIZATION_REJECTED,
                f"segment {index}: its authorization decision is not a recorded allow of this read by this run",
            )


def store_context_manifest(session: Session, built: BuiltContextManifest) -> CaliberContextManifest:
    """Insert the manifest once. Flushes, never commits.

    Checks every segment's authorization decision against the recorded decisions, and that the revision follows the
    step's latest (a gap or a repeat is refused). A concurrent insert of the same revision is :data:`DUPLICATE`.
    """
    _verify_authorizations(session, built.record)
    expected = next_revision(session, built.workflow_run_id, built.step_request_id)
    if built.revision != expected:
        code = DUPLICATE if built.revision < expected else REVISION_GAP
        raise ContextManifestError(
            code, f"this step's next revision is {expected}, not {built.revision}"
        )
    row = CaliberContextManifest(**built.row_values())
    begin_sqlite_transaction(session)
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError as exc:
        raise ContextManifestError(DUPLICATE, "this step revision already has a manifest") from exc
    return row


def load_context_manifest(
    session: Session, workflow_run_id: str, step_request_id: str, revision: int | None = None
) -> BuiltContextManifest | None:
    """A stored manifest, re-verified on the way out (the latest revision when none is named), or ``None``."""
    statement = select(CaliberContextManifest).where(
        CaliberContextManifest.workflow_run_id == workflow_run_id,
        CaliberContextManifest.step_request_id == step_request_id,
    )
    statement = (
        statement.where(CaliberContextManifest.revision == revision)
        if revision is not None
        else statement.order_by(CaliberContextManifest.revision.desc()).limit(1)
    )
    row = session.execute(statement.execution_options(populate_existing=True)).scalars().first()
    if row is None:
        return None
    return BuiltContextManifest(bytes(row.manifest_bytes), row.manifest_digest)


__all__ = [
    "AUTHORIZATION_REJECTED",
    "AUTHORIZED_SOURCES",
    "DATA",
    "DUPLICATE",
    "INSTRUCTION",
    "INSTRUCTION_KINDS",
    "INVALID",
    "ORDERING",
    "REVISION_GAP",
    "TIER_VIOLATION",
    "TOKENS",
    "UNAUTHORIZED_PAYLOAD",
    "UNKNOWN_VERSION",
    "BuiltContextManifest",
    "ContextManifestError",
    "ContextSegment",
    "build_context_manifest",
    "load_context_manifest",
    "next_revision",
    "read_context_manifest",
    "store_context_manifest",
]
