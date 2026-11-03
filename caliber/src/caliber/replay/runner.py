"""Bounded, read-only historical replay (B09.4, #805).

Historical replay is deliberately smaller than a workflow execution. It reads one already-admitted run manifest, verifies
the immutable observation index and object bytes, projects recorded case reads through the acceptance package's existing
normalization, and calls the pure acceptance judge. It never calls the live case reader, a provider, a dispatcher, or
``record_decision``. A shape this contract cannot interpret is a refusal, not an invitation to infer a replacement input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.acceptance.completion import (
    CRITERIA_BY_CONNECTOR,
    MAX_LISTED_REQUESTS,
    case_record_from_observation,
    observations_for_proposal,
)
from caliber.acceptance.decision import (
    AcceptanceDecision,
    AcceptanceError,
    judge,
    read_decision,
)
from caliber.acceptance.predicates import PredicateError
from caliber.db.models import CaliberEffectProposal, CaliberWorkflowRun
from caliber.replay.observations import (
    ObservationError,
    ObservationIntegrityError,
    ObservationRecord,
    ObservationScopeError,
    list_observation_rows,
    read_observation,
)
from caliber.run_manifest import ManifestError
from caliber.storage.base import StorageBackend
from caliber.workflows.run_pins import PinsError, load_run_manifest

REPLAY_SCHEMA_VERSION: Final = "v1"
MAX_OBSERVATIONS: Final = 256
MAX_OBSERVATION_BYTES: Final = 1 << 20
OBSERVATION_ID = re.compile(r"OBS-[0-9a-f]{8}")
_MODEL_KEYS: Final = frozenset(
    {"final_output", "structured_output", "tool_calls", "model", "prompt_version"}
)
_SUMMARY_DECISION_KEYS: Final = frozenset(
    {"decision_id", "candidate_id", "outcome", "observation_digest", "failed_criteria"}
)

MANIFEST_MISSING: Final = "manifest_missing"
MANIFEST_CORRUPT: Final = "manifest_corrupt"
MANIFEST_UNSUPPORTED: Final = "manifest_unsupported"
PINS_MISMATCH: Final = "pins_mismatch"
OBSERVATIONS_MISSING: Final = "observations_missing"
OBSERVATION_SCOPE: Final = "observation_scope"
OBSERVATION_INTEGRITY: Final = "observation_integrity"
OBSERVATION_MALFORMED: Final = "observation_malformed"
OBSERVATION_UNSUPPORTED: Final = "observation_unsupported"
OBSERVATION_LIMIT: Final = "observation_limit"
DECISIONS_MISSING: Final = "decisions_missing"
DECISIONS_CORRUPT: Final = "decisions_corrupt"
CRITERIA_UNSUPPORTED: Final = "criteria_unsupported"
REPLAY_ERROR: Final = "replay_error"

CODES: Final = frozenset(
    {
        MANIFEST_MISSING,
        MANIFEST_CORRUPT,
        MANIFEST_UNSUPPORTED,
        PINS_MISMATCH,
        OBSERVATIONS_MISSING,
        OBSERVATION_SCOPE,
        OBSERVATION_INTEGRITY,
        OBSERVATION_MALFORMED,
        OBSERVATION_UNSUPPORTED,
        OBSERVATION_LIMIT,
        DECISIONS_MISSING,
        DECISIONS_CORRUPT,
        CRITERIA_UNSUPPORTED,
        REPLAY_ERROR,
    }
)


class ReplayError(ValueError):
    """A historical replay refusal; the code is stable and the message carries no stored payload."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        if code not in CODES:
            raise ValueError(f"unknown replay refusal code {code!r}")
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ReplayDecision:
    """Stable decision comparison; database ids and timestamps are intentionally absent."""

    candidate_id: str
    criteria_version: str
    outcome: str
    observation_digest: str
    failed_criteria: tuple[str, ...]
    reason_code: str | None
    original_decision_id: str
    semantic_match: bool
    observation_match: bool


@dataclass(frozen=True)
class ReplayReport:
    """The deterministic public result of one bounded historical replay."""

    schema_version: str
    workflow_run_id: str
    manifest_id: str
    manifest_digest: str
    observation_count: int
    observation_ids: tuple[str, ...]
    criteria_version: str
    decisions: tuple[ReplayDecision, ...]
    matches_original: bool


def _refuse(code: str, message: str, *, status_code: int = 409) -> ReplayError:
    return ReplayError(code, message, status_code=status_code)


def _json_payload(record: ObservationRecord) -> object:
    if record.content is None:
        raise _refuse(OBSERVATION_MALFORMED, "an observation has no content")
    try:
        return json.loads(
            record.content.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _refuse(OBSERVATION_MALFORMED, "an observation is not finite UTF-8 JSON") from exc


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def _reject_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant {value}")


def _validate_ids(observation_ids: Sequence[str] | None) -> tuple[str, ...] | None:
    if observation_ids is None:
        return None
    if isinstance(observation_ids, (str, bytes, bytearray)):
        raise _refuse(
            OBSERVATION_MALFORMED, "observation_ids must be a JSON array", status_code=400
        )
    ids = tuple(observation_ids)
    if not ids:
        raise _refuse(OBSERVATION_MALFORMED, "observation_ids must not be empty", status_code=400)
    if len(ids) > MAX_OBSERVATIONS:
        raise _refuse(OBSERVATION_LIMIT, f"at most {MAX_OBSERVATIONS} observations may be replayed")
    if any(type(item) is not str for item in ids):
        raise _refuse(
            OBSERVATION_MALFORMED, "observation_ids must contain strings", status_code=400
        )
    if len(set(ids)) != len(ids):
        raise _refuse(
            OBSERVATION_MALFORMED, "observation_ids must not repeat an id", status_code=400
        )
    if any(OBSERVATION_ID.fullmatch(item) is None for item in ids):
        raise _refuse(
            OBSERVATION_MALFORMED,
            "observation_ids contain an invalid observation id",
            status_code=400,
        )
    return ids


def _verified_rows(
    session: Session,
    *,
    run: CaliberWorkflowRun,
    project_id: str | None,
    principal: str,
    observation_ids: tuple[str, ...] | None,
) -> list[ObservationRecord]:
    if observation_ids is None:
        rows = list_observation_rows(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=project_id,
            principal=principal,
            limit=MAX_OBSERVATIONS + 1,
        )
        if len(rows) > MAX_OBSERVATIONS:
            raise _refuse(
                OBSERVATION_LIMIT, f"at most {MAX_OBSERVATIONS} observations may be replayed"
            )
    else:
        rows = list_observation_rows(
            session,
            workflow_run_id=run.workflow_run_id,
            project_id=project_id,
            principal=principal,
            observation_ids=observation_ids,
        )
        if len(rows) != len(observation_ids):
            # Deliberately do not distinguish a missing id from a foreign id. The scoped index query has already run,
            # but no unscoped existence probe is permitted to become an observation oracle.
            raise _refuse(
                OBSERVATION_SCOPE, "one or more observations are outside the replay scope"
            )
    if not rows:
        raise _refuse(OBSERVATIONS_MISSING, "the run has no visible observations")
    if any(
        row.workflow_run_id != run.workflow_run_id
        or row.project_id != project_id
        or row.principal != principal
        for row in rows
    ):
        raise _refuse(OBSERVATION_SCOPE, "an observation is outside the replay scope")
    if any(OBSERVATION_ID.fullmatch(row.observation_id) is None for row in rows):
        raise _refuse(OBSERVATION_MALFORMED, "an observation has an invalid observation id")
    return rows


def _read_rows(  # noqa: PLR0912 - each refusal is an independent fail-closed contract check
    session: Session,
    backend: StorageBackend,
    rows: Sequence[ObservationRecord],
    *,
    project_id: str | None,
    principal: str,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    case_records: dict[str, dict[str, Any]] = {}
    model_ids: set[str] = set()
    for row in rows:
        if row.content_size < 1 or row.content_size > MAX_OBSERVATION_BYTES:
            raise _refuse(OBSERVATION_LIMIT, "an observation exceeds the replay byte bound")
        try:
            verified = read_observation(
                session,
                backend,
                observation_id=row.observation_id,
                project_id=project_id,
                principal=principal,
            )
        except ObservationScopeError as exc:
            raise _refuse(OBSERVATION_SCOPE, "an observation is outside the replay scope") from exc
        except ObservationIntegrityError as exc:
            raise _refuse(
                OBSERVATION_INTEGRITY, "an observation failed its storage integrity check"
            ) from exc
        except ObservationError as exc:
            raise _refuse(OBSERVATION_INTEGRITY, "an observation could not be read") from exc
        payload = _json_payload(verified)
        if row.observation_kind == "model":
            if (
                not isinstance(payload, dict)
                or set(payload) != _MODEL_KEYS
                or not isinstance(payload["tool_calls"], list)
            ):
                raise _refuse(
                    OBSERVATION_UNSUPPORTED,
                    "the model observation is not the supported runtime shape",
                )
            model_ids.add(row.observation_id)
            continue
        if row.observation_kind != "read" or not isinstance(payload, dict):
            raise _refuse(
                OBSERVATION_UNSUPPORTED,
                "the observation kind is not supported by historical replay",
            )
        projected = case_record_from_observation(payload)
        if projected is None:
            raise _refuse(
                OBSERVATION_UNSUPPORTED, "the read observation is not a supported case record"
            )
        case_id = projected.get("case_id")
        requests = projected.get("document_requests")
        if type(case_id) is not str or not case_id.strip() or not isinstance(requests, list):
            raise _refuse(
                OBSERVATION_MALFORMED, "the read observation has no supported case-record shape"
            )
        if len(requests) > MAX_LISTED_REQUESTS or any(
            not isinstance(item, dict) for item in requests
        ):
            raise _refuse(
                OBSERVATION_MALFORMED, "the read observation has an invalid case-request list"
            )
        try:
            key = json.dumps(
                projected,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise _refuse(
                OBSERVATION_MALFORMED, "the read observation is not canonical JSON"
            ) from exc
        previous = case_records.get(case_id)
        if previous is not None:
            previous_key = json.dumps(
                previous, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            if previous_key != key:
                raise _refuse(
                    OBSERVATION_UNSUPPORTED, "multiple recorded reads disagree for one case"
                )
        else:
            case_records[case_id] = projected
    if not case_records:
        raise _refuse(OBSERVATIONS_MISSING, "the replay has no recorded case read")
    return case_records, model_ids


def _manifest(session: Session, run: CaliberWorkflowRun, project_id: str | None) -> Any:
    if run.manifest_legacy:
        raise _refuse(MANIFEST_UNSUPPORTED, "legacy runs do not have replayable admission pins")
    try:
        loaded = load_run_manifest(session, run.workflow_run_id)
    except PinsError as exc:
        raise _refuse(MANIFEST_CORRUPT, "the stored admission manifest cannot be verified") from exc
    except ManifestError as exc:
        raise _refuse(MANIFEST_CORRUPT, "the stored admission manifest is malformed") from exc
    if loaded is None:
        raise _refuse(MANIFEST_MISSING, "the admitted run has no stored admission manifest")
    record = loaded.record
    if record.get("legacy") is not False or record.get("project_id") != project_id:
        raise _refuse(
            MANIFEST_UNSUPPORTED, "the admission manifest is not bound to this replay scope"
        )
    pins = record.get("pins")
    if not isinstance(pins, dict) or pins.get("workflow_version") != run.workflow_version_id:
        raise _refuse(
            PINS_MISMATCH, "the admission manifest does not pin this run's workflow version"
        )
    return loaded


def _original_decisions(session: Session, run: CaliberWorkflowRun) -> dict[str, AcceptanceDecision]:
    summary = run.summary if isinstance(run.summary, dict) else None
    acceptance = summary.get("acceptance") if summary is not None else None
    entries = acceptance.get("decisions") if isinstance(acceptance, dict) else None
    if not isinstance(entries, list) or not entries:
        raise _refuse(DECISIONS_MISSING, "the run does not name its acceptance decisions")
    result: dict[str, AcceptanceDecision] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _SUMMARY_DECISION_KEYS:
            raise _refuse(DECISIONS_CORRUPT, "the run's acceptance decision summary is malformed")
        decision_id = entry["decision_id"]
        candidate_id = entry["candidate_id"]
        if type(decision_id) is not str or type(candidate_id) is not str:
            raise _refuse(DECISIONS_CORRUPT, "the run's acceptance decision summary is malformed")
        try:
            decision = read_decision(session, decision_id)
        except (KeyError, AcceptanceError) as exc:
            raise _refuse(DECISIONS_CORRUPT, "a named acceptance decision cannot be read") from exc
        if decision.workflow_run_id != run.workflow_run_id or decision.candidate_id != candidate_id:
            raise _refuse(DECISIONS_CORRUPT, "a named acceptance decision is bound to another run")
        if (
            entry["outcome"] != decision.outcome
            or entry["observation_digest"] != decision.observation_digest
            or not isinstance(entry["failed_criteria"], list)
            or tuple(entry["failed_criteria"]) != decision.failed_criteria
        ):
            raise _refuse(
                DECISIONS_CORRUPT, "the acceptance decision summary disagrees with its row"
            )
        if candidate_id in result:
            raise _refuse(DECISIONS_CORRUPT, "the run names one candidate more than once")
        result[candidate_id] = decision
    return result


def _decision_semantics(decision: AcceptanceDecision | Any) -> tuple[Any, ...]:
    criteria = tuple(
        (item.predicate_id, item.passed, item.evidence_ref, item.rule_ids)
        for item in decision.criteria
    )
    return (decision.criteria_version, decision.outcome, criteria, decision.reason_code)


def replay_historical(
    session: Session,
    backend: StorageBackend,
    *,
    run: CaliberWorkflowRun,
    project_id: str | None,
    principal: str,
    observation_ids: Sequence[str] | None = None,
) -> ReplayReport:
    """Replay one admitted run from verified observations without changing SQL or calling a live dependency."""

    if not isinstance(run, CaliberWorkflowRun):
        raise _refuse(REPLAY_ERROR, "run must be a workflow-run record", status_code=400)
    if run.project_id != project_id:
        raise _refuse(OBSERVATION_SCOPE, "the run is outside the replay scope")
    if type(principal) is not str or not principal.strip():
        raise _refuse(OBSERVATION_SCOPE, "the replay principal is invalid", status_code=400)
    loaded = _manifest(session, run, project_id)
    ids = _validate_ids(observation_ids)
    rows = _verified_rows(
        session,
        run=run,
        project_id=project_id,
        principal=principal,
        observation_ids=ids,
    )
    case_records, _model_ids = _read_rows(
        session,
        backend,
        rows,
        project_id=project_id,
        principal=principal,
    )
    originals = _original_decisions(session, run)
    proposal_rows = session.execute(
        select(CaliberEffectProposal.proposal_id, CaliberEffectProposal.connector)
        .where(
            CaliberEffectProposal.workflow_run_id == run.workflow_run_id,
            CaliberEffectProposal.connector.in_(tuple(CRITERIA_BY_CONNECTOR)),
        )
        .order_by(
            CaliberEffectProposal.node_id,
            CaliberEffectProposal.occurrence_id,
            CaliberEffectProposal.proposal_id,
        )
    ).all()
    if not proposal_rows:
        raise _refuse(CRITERIA_UNSUPPORTED, "the run has no supported governed effect to replay")
    if set(originals) != {proposal_id for proposal_id, _connector in proposal_rows}:
        raise _refuse(
            DECISIONS_CORRUPT, "the run's acceptance decisions do not match its governed effects"
        )

    decisions: list[ReplayDecision] = []
    versions: set[str] = set()
    for proposal_id, connector in proposal_rows:
        criteria = CRITERIA_BY_CONNECTOR.get(connector)
        if criteria is None:
            raise _refuse(CRITERIA_UNSUPPORTED, "the governed effect has no replay criteria")
        original = originals[proposal_id]
        if original.criteria_version != criteria.version:
            raise _refuse(CRITERIA_UNSUPPORTED, "the stored criteria version is not replayable")
        base = observations_for_proposal(session, proposal_id)
        proposal = base.get("proposal")
        case_id = proposal.get("case_id") if isinstance(proposal, Mapping) else None
        if not isinstance(case_id, str) or case_id not in case_records:
            raise _refuse(
                OBSERVATIONS_MISSING, "the replay is missing the case read for a governed effect"
            )
        observations = observations_for_proposal(
            session,
            proposal_id,
            case_record=case_records[case_id],
        )
        try:
            verdict = judge(criteria.predicates, observations)
        except PredicateError as exc:
            raise _refuse(
                REPLAY_ERROR, "the pinned acceptance criteria could not be replayed"
            ) from exc
        replay_semantics = _decision_semantics(verdict)
        original_semantics = _decision_semantics(original)
        semantic_match = replay_semantics == original_semantics
        observation_match = verdict.observation_digest == original.observation_digest
        decisions.append(
            ReplayDecision(
                candidate_id=proposal_id,
                criteria_version=verdict.criteria_version,
                outcome=verdict.outcome,
                observation_digest=verdict.observation_digest,
                failed_criteria=tuple(
                    item.predicate_id for item in verdict.criteria if not item.passed
                ),
                reason_code=verdict.reason_code,
                original_decision_id=original.decision_id,
                semantic_match=semantic_match,
                observation_match=observation_match,
            )
        )
        versions.add(verdict.criteria_version)
    return ReplayReport(
        schema_version=REPLAY_SCHEMA_VERSION,
        workflow_run_id=run.workflow_run_id,
        manifest_id=loaded.manifest_id,
        manifest_digest=loaded.digest,
        observation_count=len(rows),
        observation_ids=tuple(row.observation_id for row in rows),
        criteria_version=",".join(sorted(versions)),
        decisions=tuple(decisions),
        matches_original=all(item.semantic_match and item.observation_match for item in decisions),
    )


__all__ = [
    "CODES",
    "MANIFEST_CORRUPT",
    "MANIFEST_MISSING",
    "MANIFEST_UNSUPPORTED",
    "MAX_OBSERVATIONS",
    "MAX_OBSERVATION_BYTES",
    "OBSERVATION_ID",
    "OBSERVATION_INTEGRITY",
    "OBSERVATION_LIMIT",
    "OBSERVATION_MALFORMED",
    "OBSERVATION_SCOPE",
    "OBSERVATION_UNSUPPORTED",
    "ReplayDecision",
    "ReplayError",
    "ReplayReport",
    "replay_historical",
]
