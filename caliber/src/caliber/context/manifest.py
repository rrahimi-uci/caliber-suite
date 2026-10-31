"""The context manifest: what one agent step was given, ordered, labelled, authorized and digest-bound (B07.3a, #704).

A :class:`ContextManifest <caliber.contracts>` (kind ``context_manifest``, schema ``v1``, ADR 0003) lists, in the order the
provider sees them, every segment of an agent step's context with:

* a **trust tier**: ``instruction`` (what the platform and the workflow's author say) or ``data`` (everything else:
  the user's input, retrieved text, tool output, history, memory). Only a system/workflow instruction or a
  provider-required structured-output suffix is an
  instruction, and every one is; **retrieved text is data**, so text a source contains can never be promoted to an
  instruction by being retrieved;
* its **source** (prompt, input, knowledge-base version, tool, memory, history), the **retrieval** and **transform**
  that produced it, and the digest of its content (the content itself is not stored here);
* for payload loaded from a knowledge base or memory, the id of the recorded ``context.read`` **authorization
  decision** that allowed it (:mod:`caliber.context.authorize`): a segment that names a source but no decision is
  refused, and :func:`store_context_manifest` checks each id against the recorded decision (an allow, for this run,
  of that exact source);
* its **position**, the **omitted** and **redacted** items and the **token allocation**;
* when the run was admitted with a rule bundle, a **rule_bundle** reference (B07.5b, #736): the bundle's id, version and
  SHA-256 and, per selected rule, its id, provenance (``source_ref``) and text digest (:mod:`caliber.context.rules`).
  :func:`store_context_manifest` checks the cited digest against the one the run manifest pinned at admission, and
  never writes the run manifest.

The record is stored once as canonical JSON with its SHA-256 (it has no digest field: the digest excludes itself), unique
per ``(run, step request, revision)``: re-assembling a step is a new revision, never an edit. Reading it back
(:func:`read_context_manifest`) checks the digest, the schema (an unknown version is refused, never read as the nearest)
and every rule above again, so a reordered or relabelled record is caught twice (by the digest and by the positions).
Nothing here talks to a provider: B08 consumes the record.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.context.sources import memory_resource, source_resource
from caliber.contracts import ContractValidationError, UnsupportedSchemaVersion, validate_record
from caliber.db.models import (
    CaliberContextManifest,
    CaliberPolicyDecision,
    CaliberRunManifest,
    CaliberWorkflowRun,
)
from caliber.db.transactions import begin_sqlite_transaction
from caliber.redaction import Redactor
from caliber.run_manifest import (
    FIXED_REGION,
    FIXED_RESIDENCY,
    FIXED_TENANT_ID,
    DeploymentScope,
    ManifestError,
    canonical_bytes,
    digest_of,
    read_manifest,
)

KIND: Final = "context_manifest"
SCHEMA_VERSION: Final = "v1"
INSTRUCTION: Final = "instruction"
DATA: Final = "data"
INSTRUCTION_KINDS: Final = frozenset(
    {"system_instruction", "workflow_instruction", "structured_output_suffix"}
)
#: Sources whose payload is loaded from a store and so needs an authorization decision.
AUTHORIZED_SOURCES: Final = frozenset({"knowledge_base", "memory"})
#: Segment kinds that always carry an authorization decision, whatever source they name.
AUTHORIZED_KINDS: Final = frozenset({"retrieved_text", "memory"})
CONTEXT_READ: Final = "context.read"
#: The capped sections of a step's context and the segment kind each one is made of.
SECTION_KINDS: Final = {"retrieved": "retrieved_text", "history": "history"}

INVALID: Final = "invalid_manifest"
UNKNOWN_VERSION: Final = "unknown_version"
TIER_VIOLATION: Final = "tier_violation"
UNAUTHORIZED_PAYLOAD: Final = "unauthorized_payload"
ORDERING: Final = "ordering"
TOKENS: Final = "tokens"
AUTHORIZATION_REJECTED: Final = "authorization_rejected"
DUPLICATE: Final = "duplicate_revision"
REVISION_GAP: Final = "revision_gap"
#: The pinned rule bundle's bytes are no longer the admitted bytes (B07.5b, #736).
RULE_BUNDLE_CHANGED: Final = "rule_bundle_changed"
#: The pinned rule bundle cannot be read, or the run manifest's pin cannot be used.
RULE_BUNDLE_UNAVAILABLE: Final = "rule_bundle_unavailable"
#: A context manifest cites a rule bundle other than the one the run was admitted with, or omits it.
RULE_BUNDLE_MISMATCH: Final = "rule_bundle_mismatch"
#: A context manifest cites a rule bundle for a run that was admitted without one.
RULE_BUNDLE_UNPINNED: Final = "rule_bundle_unpinned"
_MAX_BYTES: Final = 1 << 20
_MAX_NODE_STEP_ID_LENGTH: Final = 128


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
    authorization_decision_ids: tuple[str, ...] = ()

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
        if self.authorization_decision_ids:
            record["authorization_decision_ids"] = list(self.authorization_decision_ids)
        return record


def _check_rules(record: Mapping[str, Any]) -> None:
    """The rules the schema cannot state: contiguous positions, redactions, refused sources, token arithmetic.
    (The tier, kind-to-source and authorization rules are the schema's own.)"""
    segments: Sequence[Mapping[str, Any]] = record["segments"]
    for index, segment in enumerate(segments):
        if segment["position"] != index:
            raise ContextManifestError(
                ORDERING,
                f"segment {index} has position {segment['position']}: the order was changed",
            )
    positions = len(segments)
    redacted_at: set[int] = set()
    for item in record.get("redacted", []):
        if item["position"] >= positions:
            raise ContextManifestError(INVALID, "a redaction names a segment that does not exist")
        if item["position"] in redacted_at:
            raise ContextManifestError(
                INVALID, "a segment is redacted twice: list its fields together"
            )
        redacted_at.add(item["position"])
    for item in record.get("omitted", []):
        if item["reason"] == "unauthorized" and set(item["source"]) - {"kind"}:
            raise ContextManifestError(
                INVALID,
                "a source the run was refused is recorded by kind only, never by identifier",
            )
    rule_ids = [rule["rule_id"] for rule in record.get("rule_bundle", {}).get("rules", [])]
    if len(set(rule_ids)) != len(rule_ids):
        raise ContextManifestError(INVALID, "a rule is selected twice")
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
    _check_sections(allocation, segments)


def _check_sections(allocation: Mapping[str, Any], segments: Sequence[Mapping[str, Any]]) -> None:
    """Each recorded section is listed once, adds up to its segments and stays within its cap."""
    seen: set[str] = set()
    for item in allocation.get("sections", []):
        section = item["section"]
        used = sum(int(s["tokens"]) for s in segments if s["kind"] == SECTION_KINDS[section])
        if section in seen:
            raise ContextManifestError(TOKENS, f"the {section} section is allocated twice")
        seen.add(section)
        if item["used_tokens"] != used:
            raise ContextManifestError(
                TOKENS, f"the {section} section's tokens do not add up to its segments"
            )
        if item["used_tokens"] > item["cap_tokens"]:
            raise ContextManifestError(TOKENS, f"the {section} section uses more than its cap")


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
                f"segment {index}: only a system/workflow instruction or structured-output suffix is an instruction",
            )
        needs = (
            segment["source"].get("kind") in AUTHORIZED_SOURCES
            or segment.get("kind") in AUTHORIZED_KINDS
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
        record = read_context_manifest(self.manifest_bytes, self.manifest_digest)
        _strict_integers(record)
        if _canonical(record) != self.manifest_bytes:
            raise ContextManifestError(
                INVALID,
                "a manifest is stored as canonical JSON: one record, one byte form, one digest",
            )

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

    @property
    def step(self) -> dict[str, Any] | None:
        value = self.record.get("step")
        return dict(value) if isinstance(value, dict) else None

    @property
    def node_step_id(self) -> str | None:
        step = self.step
        if step is None:
            return None
        return _node_step_id(self.workflow_run_id, step["node_id"], step["occurrence_id"])

    def row_values(self) -> dict[str, Any]:
        created = datetime.fromisoformat(str(self.record["created_at"]).replace("Z", "+00:00"))
        return {
            "context_manifest_id": self.context_manifest_id,
            "workflow_run_id": self.workflow_run_id,
            "step_request_id": self.step_request_id,
            "node_step_id": self.node_step_id,
            "occurrence_id": (self.step or {}).get("occurrence_id"),
            "attempt": (self.step or {}).get("attempt"),
            "revision": self.revision,
            "schema_version": SCHEMA_VERSION,
            "manifest_bytes": self.manifest_bytes,
            "manifest_digest": self.manifest_digest,
            "created_at": created.astimezone(timezone.utc).replace(tzinfo=None),
        }


def _check_types(
    revision: object,
    token_budget: object,
    segments: Sequence[ContextSegment],
    omitted: Sequence[Mapping[str, Any]],
    redacted: Sequence[Mapping[str, Any]],
) -> None:
    """Counts are integers, not 1.0 (the schema accepts it, but its canonical bytes would differ) and sources are objects."""
    for name, value in (("revision", revision), ("token_budget", token_budget)):
        if type(value) is not int:
            raise ContextManifestError(INVALID, f"{name} must be an integer")
    for segment in segments:
        if type(segment.tokens) is not int or not isinstance(segment.source, Mapping):
            raise ContextManifestError(
                INVALID, "a segment's tokens must be an integer and its source an object"
            )
    for item in (*omitted, *redacted):
        for key in ("tokens", "position"):
            if key in item and type(item[key]) is not int:
                raise ContextManifestError(INVALID, f"{key} must be an integer")


def _strict_integers(record: Mapping[str, Any]) -> None:
    """A stored record's counts are integers (``1.0`` passes the schema but is a second form of ``1``)."""
    allocation = record["token_allocation"]
    counts: list[object] = [
        record["revision"],
        *(value for key, value in allocation.items() if key != "sections"),
    ]
    step = record.get("step")
    if isinstance(step, Mapping):
        counts.append(step["attempt"])
    for section in allocation.get("sections", []):
        counts += [section["cap_tokens"], section["used_tokens"]]
    for segment in record["segments"]:
        counts += [segment["position"], segment["tokens"]]
    for item in (*record.get("omitted", []), *record.get("redacted", [])):
        counts += [item[key] for key in ("tokens", "position") if key in item]
    if any(type(value) is not int for value in counts):
        raise ContextManifestError(INVALID, "a stored manifest's counts are integers")


def _timestamp(moment: datetime) -> str:
    try:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    except (OverflowError, ValueError) as exc:
        raise ContextManifestError(INVALID, "created_at is out of range") from exc


def _node_step_id(workflow_run_id: str, node_id: str, occurrence_id: str) -> str:
    """Return a bounded, deterministic identity for one logical node occurrence."""
    raw = f"{workflow_run_id}:{node_id}:{occurrence_id}"
    if len(raw) <= _MAX_NODE_STEP_ID_LENGTH:
        return raw
    return "ns-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def node_step_id_for(workflow_run_id: str, node_id: str, occurrence_id: str) -> str:
    """The ``node_step_id`` a context manifest of this node occurrence is stored under.

    The one join key between a step's context manifest and the effect proposal of the same ``(run, node, occurrence)``
    (B11.3b): readers compute it with this function and never re-derive the format.
    """
    return _node_step_id(workflow_run_id, node_id, occurrence_id)


@dataclass(frozen=True)
class PackedContext:
    """What :func:`pack_context` decided: the segments to give the provider, and what the manifest records of it."""

    segments: tuple[ContextSegment, ...]
    omitted: tuple[dict[str, Any], ...]
    redacted: tuple[dict[str, Any], ...]
    sections: tuple[dict[str, Any], ...]


def pack_context(
    segments: Sequence[ContextSegment],
    *,
    caps: Mapping[str, int],
    redactor: Redactor,
    count_tokens: Callable[[str], int] | None = None,
) -> PackedContext:
    """Redact and cap the context of one agent step before it is hashed, recorded or given to a provider.

    **Redaction.** Every ``data``-tier segment's text goes through ``redactor`` first (an ``instruction`` is the
    operator's own text and is never altered), and the digest is taken of the redacted text. A segment that changed is
    recorded once in ``redacted`` (``fields: ["content"]``, ``reason: "redactor:pattern-1,pattern-3"``: which rules
    fired, never what they matched). A segment with nothing left but the replacement marker is dropped and listed in
    ``omitted`` as ``redacted``. If ``count_tokens`` is given the redacted segment's tokens are recounted; otherwise the
    caller's count stands.

    **Caps.** ``caps`` maps a section (``retrieved``, ``history``; see ``SECTION_KINDS``) to the most tokens it may use.
    Within a section the segments are kept in order while they fit; the first one that does not fit, and every later one
    in that section (they rank below it), is dropped and listed in ``omitted`` with reason ``token_budget`` and its
    tokens. A section with no cap is unlimited. ``sections`` records each capped section's cap and what it used.

    Give it the whole final list of segments: the ``redacted`` positions are indexes into the packed result, so
    segments added or removed afterwards make them stale.

    Pure: nothing here talks to a store or a provider. The agent-step adapters (B08) call it to decide what the provider
    is given, and :func:`build_context_manifest` records the result; the assembly recorder of B07.3b records the
    context as the runtime passes it and does not pack it.
    """
    for section, cap in caps.items():
        if section not in SECTION_KINDS or type(cap) is not int or cap < 0:
            raise ContextManifestError(
                INVALID,
                f"caps name the sections {sorted(SECTION_KINDS)} with non-negative integers",
            )
    kind_section = {kind: section for section, kind in SECTION_KINDS.items()}
    used = dict.fromkeys(caps, 0)
    full: set[str] = set()
    kept: list[ContextSegment] = []
    omitted: list[dict[str, Any]] = []
    redacted: list[dict[str, Any]] = []
    for segment in segments:
        text, rules = (
            redactor.redact_string_with_rules(segment.content)
            if segment.tier == DATA
            else (segment.content, ())
        )
        if rules and not text.replace(redactor.replacement, "").strip():
            omitted.append(_omission(segment, "redacted"))
            continue
        tokens = count_tokens(text) if rules and count_tokens is not None else segment.tokens
        capped = kind_section.get(segment.kind)
        if capped in caps and capped is not None:
            if capped in full or used[capped] + tokens > caps[capped]:
                full.add(capped)
                omitted.append(_omission(segment, "token_budget", tokens))
                continue
            used[capped] += tokens
        if rules:
            redacted.append(
                {
                    "position": len(kept),
                    "fields": ["content"],
                    "reason": "redactor:" + ",".join(rules),
                }
            )
            kept.append(dataclasses.replace(segment, content=text, tokens=tokens))
        else:
            kept.append(segment)
    sections = tuple(
        {"section": section, "cap_tokens": caps[section], "used_tokens": used[section]}
        for section in SECTION_KINDS
        if section in caps
    )
    return PackedContext(tuple(kept), tuple(omitted), tuple(redacted), sections)


def _omission(segment: ContextSegment, reason: str, tokens: int | None = None) -> dict[str, Any]:
    return {
        "source": dict(segment.source),
        "reason": reason,
        "tokens": segment.tokens if tokens is None else tokens,
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
    sections: Sequence[Mapping[str, Any]] = (),
    rule_bundle: Mapping[str, Any] | None = None,
    step: Mapping[str, Any] | None = None,
) -> BuiltContextManifest:
    """Build and validate the manifest of one agent step. Pure: raises :class:`ContextManifestError`.

    The segments are numbered in the order given (so the order is the assembler's, recorded once). ``scope`` is the
    deployment's fixed one (no organization switching). ``sections`` is :attr:`PackedContext.sections`: the per-section
    caps and what each section used. ``rule_bundle`` is :func:`caliber.context.rules.rule_bundle_reference`'s entry
    for a run admitted with a rule bundle (``None`` otherwise).
    """
    if not isinstance(scope, DeploymentScope) or scope != DeploymentScope.fixed(scope.project_id):
        raise ContextManifestError(
            INVALID,
            f"this deployment is fixed to organization {FIXED_TENANT_ID!r}, region {FIXED_REGION!r} "
            f"and residency {FIXED_RESIDENCY!r}",
        )
    if created_at.tzinfo is None:
        raise ContextManifestError(INVALID, "created_at must be timezone-aware")
    _check_types(revision, token_budget, segments, omitted, redacted)
    for item in sections:
        for key in ("cap_tokens", "used_tokens"):
            if type(item.get(key)) is not int:
                raise ContextManifestError(INVALID, f"a section's {key} must be an integer")
    try:
        records = [segment.to_record(position) for position, segment in enumerate(segments)]
    except (UnicodeEncodeError, ValueError) as exc:
        raise ContextManifestError(INVALID, "a segment's content is not valid text") from exc
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
            **({"sections": [dict(item) for item in sections]} if sections else {}),
        },
        "created_at": _timestamp(created_at),
    }
    if step is not None:
        record["step"] = dict(step)
    if scope.project_id is not None:
        record["project_id"] = scope.project_id
    if omitted:
        record["omitted"] = [dict(item) for item in omitted]
    if redacted:
        record["redacted"] = [dict(item) for item in redacted]
    if rule_bundle is not None:
        record["rule_bundle"] = copy.deepcopy(dict(rule_bundle))
    _validate(record)
    data = _canonical(record)
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
    _canonical(record)  # a lone surrogate parses but can be neither stored nor digested
    return record


def _canonical(record: Mapping[str, Any]) -> bytes:
    try:
        return canonical_bytes(record)
    except (
        UnicodeEncodeError,
        ValueError,
    ) as exc:  # a lone surrogate can be neither stored nor digested
        raise ContextManifestError(
            INVALID, "the manifest holds text that cannot be encoded"
        ) from exc


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
    """Every authorization id must be a recorded ``context.read`` allow, for this run, of that exact source, decided
    no later than the manifest. (A decision may be cited by any step of its run: it authorizes the read, not a step.)"""
    created = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
    for index, segment in enumerate(record["segments"]):
        decision_ids: list[str] = []
        if segment.get("authorization_decision_id") is not None:
            decision_ids.append(str(segment["authorization_decision_id"]))
        decision_ids.extend(str(item) for item in segment.get("authorization_decision_ids", []))
        if not decision_ids:
            continue
        source = segment["source"]
        expected = (
            source_resource(source["knowledge_base_id"], source["version_id"])
            if source["kind"] == "knowledge_base"
            else memory_resource(source["reference"])
            if source["kind"] == "memory"
            else None
        )
        # A singular decision id is reserved for a segment whose source is the
        # exact authorized store object.  Derived values use the plural field;
        # otherwise an unrelated tool payload could smuggle a valid decision
        # from another source into the manifest.
        if expected is None and segment.get("authorization_decision_id") is not None:
            raise ContextManifestError(
                AUTHORIZATION_REJECTED,
                f"segment {index}: its authorization decision is not a recorded allow of this read by this run",
            )
        for decision_id in decision_ids:
            row = session.get(CaliberPolicyDecision, decision_id)
            # A transformed/for_each value is labelled with the delivery node,
            # not the original store source.  Its provenance decision ids still
            # have to be exact, run-scoped context reads, but the segment's
            # ``tool`` source is not itself an authority resource to compare.
            decision_matches_source = row is not None and (
                row.resource == expected
                if expected is not None
                else isinstance(row.resource, str)
                and row.resource.startswith(("knowledge_base:", "memory:"))
            )
            if (
                row is None
                or row.result != "allow"
                or row.workflow_run_id != record["workflow_run_id"]
                or row.action != CONTEXT_READ
                or not decision_matches_source
                or row.evaluated_at.replace(tzinfo=timezone.utc) > created
            ):
                raise ContextManifestError(
                    AUTHORIZATION_REJECTED,
                    f"segment {index}: its authorization decision is not a recorded allow of this read by this run",
                )


def _verify_run(session: Session, record: Mapping[str, Any]) -> None:
    """The run exists, and the record's scope and project are the deployment's and the run's."""
    run = session.get(CaliberWorkflowRun, record["workflow_run_id"])
    if run is None:
        raise ContextManifestError(INVALID, "the manifest names a run that does not exist")
    fixed = DeploymentScope.fixed(run.project_id)
    if (
        record["tenant_id"] != fixed.tenant_id
        or record["region"] != fixed.region
        or record.get("residency", fixed.residency) != fixed.residency
        or record.get("project_id") != run.project_id
    ):
        raise ContextManifestError(INVALID, "the manifest's scope is not the run's")


def _verify_rule_bundle(session: Session, record: Mapping[str, Any]) -> None:
    """The rule bundle the manifest cites is the one the run was admitted with, by digest.

    The admitted digest is read from the run's stored manifest (immutable, and never written here). **This is the whole
    check**: the store compares the cited digest with the run's pin and nothing else in the entry (the bundle id,
    version and per-rule fields), because it cannot re-read the file; the recorder
    (:func:`caliber.context.rules.rule_bundle_reference`), which verified the bytes the entry was built from, is the
    trust boundary for the rest of it. A run admitted with a
    bundle must cite exactly that digest in every step's manifest; a run admitted without one must cite none, so a
    bundle enabled after admission is never applied to it.
    """
    row = session.execute(
        select(CaliberRunManifest).where(
            CaliberRunManifest.workflow_run_id == record["workflow_run_id"]
        )
    ).scalar_one_or_none()
    admitted: object = None
    if row is not None:
        try:
            admitted = read_manifest(bytes(row.manifest_bytes), row.manifest_digest).get(
                "rule_bundle_sha256"
            )
        except ManifestError:
            raise ContextManifestError(INVALID, "the run's manifest does not read back") from None
    cited = record.get("rule_bundle")
    if admitted is None:
        if cited is not None:
            raise ContextManifestError(
                RULE_BUNDLE_UNPINNED, "the run was admitted without a rule bundle"
            )
        return
    if cited is None or cited["sha256"] != admitted:
        raise ContextManifestError(
            RULE_BUNDLE_MISMATCH,
            "the manifest does not cite the rule bundle the run was admitted with",
        )


def store_context_manifest(session: Session, built: BuiltContextManifest) -> CaliberContextManifest:
    """Insert the manifest once. Flushes, never commits.

    Checks the run, the scope and project, the cited rule bundle against the run's admitted pin, and every segment's
    authorization decision against the recorded decisions, and that the revision follows the step's latest (a gap or a
    repeat is refused). A concurrent insert of the same revision is :data:`DUPLICATE`.
    """
    record = built.record
    _verify_run(session, record)
    _verify_rule_bundle(session, record)
    _verify_authorizations(session, record)
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
        taken = session.execute(
            select(CaliberContextManifest.context_manifest_id).where(
                CaliberContextManifest.workflow_run_id == built.workflow_run_id,
                CaliberContextManifest.step_request_id == built.step_request_id,
                CaliberContextManifest.revision == built.revision,
            )
        ).first()
        if taken is not None:
            raise ContextManifestError(
                DUPLICATE, "this step revision already has a manifest"
            ) from exc
        raise ContextManifestError(INVALID, "the manifest could not be stored") from exc
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
    "AUTHORIZED_KINDS",
    "AUTHORIZED_SOURCES",
    "DATA",
    "DUPLICATE",
    "INSTRUCTION",
    "INSTRUCTION_KINDS",
    "INVALID",
    "ORDERING",
    "REVISION_GAP",
    "RULE_BUNDLE_CHANGED",
    "RULE_BUNDLE_MISMATCH",
    "RULE_BUNDLE_UNAVAILABLE",
    "RULE_BUNDLE_UNPINNED",
    "SECTION_KINDS",
    "TIER_VIOLATION",
    "TOKENS",
    "UNAUTHORIZED_PAYLOAD",
    "UNKNOWN_VERSION",
    "BuiltContextManifest",
    "ContextManifestError",
    "ContextSegment",
    "PackedContext",
    "build_context_manifest",
    "load_context_manifest",
    "next_revision",
    "node_step_id_for",
    "pack_context",
    "read_context_manifest",
    "store_context_manifest",
]
