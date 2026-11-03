"""Bounded, read-only historical replay (B09.4, #805).

Historical replay is deliberately smaller than a workflow execution. It reads one already-admitted run manifest, verifies
the immutable observation index and object bytes, projects recorded case reads through the acceptance package's existing
normalization, and calls the pure acceptance judge. It never calls the live case reader, a provider, a dispatcher, or
``record_decision``. A shape this contract cannot interpret is a refusal, not an invitation to infer a replacement input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Final, Protocol

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
    record_decision,
)
from caliber.acceptance.predicates import PredicateError
from caliber.db.models import CaliberEffectProposal, CaliberRunManifest, CaliberWorkflowRun
from caliber.ids import new_workflow_run_id
from caliber.replay.observations import (
    ObservationError,
    ObservationIntegrityError,
    ObservationRecord,
    ObservationScopeError,
    list_observation_rows,
    read_observation,
    record_observation,
)
from caliber.run_manifest import (
    ManifestError,
    canonical_bytes,
    digest_of,
    new_manifest_id,
    read_manifest,
)
from caliber.storage.base import StorageBackend
from caliber.workflows.run_pins import PinsError, load_run_manifest

REPLAY_SCHEMA_VERSION: Final = "v1"
MAX_OBSERVATIONS: Final = 256
MAX_OBSERVATION_BYTES: Final = 1 << 20
_MAX_PRINCIPAL_LENGTH: Final = 256
_MAX_ADAPTER_ID_LENGTH: Final = 128
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
FORK_AUTHORIZATION: Final = "counterfactual_authorization_required"
FORK_SOURCE: Final = "counterfactual_source_invalid"
FORK_PINS: Final = "counterfactual_pins_invalid"
FORK_BUDGET: Final = "counterfactual_budget_exceeded"
FORK_ADAPTER: Final = "counterfactual_adapter_refused"

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
        FORK_AUTHORIZATION,
        FORK_SOURCE,
        FORK_PINS,
        FORK_BUDGET,
        FORK_ADAPTER,
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


class CounterfactualCandidateAdapter(Protocol):
    """The isolated candidate seam used by :func:`fork_counterfactual`.

    An adapter receives only a verified source case and immutable pins. It
    has no session, dispatcher, provider client, or source-run object, which
    makes the default scripted path deterministic and incapable of protected
    writes. A live adapter must opt in with ``requires_live_authorization``
    and the caller must pass ``authorize_live_evaluation=True``.
    """

    name: str
    version: str
    requires_live_authorization: bool

    def evaluate(
        self,
        *,
        case_id: str,
        source_case: Mapping[str, Any],
        pins: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ScriptedCandidateAdapter:
    """Deterministic candidate adapter for ordinary CI and offline evaluation."""

    responses: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    name: ClassVar[str] = "scripted"
    version: ClassVar[str] = "v1"
    requires_live_authorization: ClassVar[bool] = False

    def evaluate(
        self,
        *,
        case_id: str,
        source_case: Mapping[str, Any],
        pins: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del pins
        response = self.responses.get(case_id, source_case)
        return deepcopy(dict(response))


@dataclass(frozen=True)
class CounterfactualBudget:
    """Independent bounds for one fork; no source-run budget is reused."""

    max_cases: int = MAX_OBSERVATIONS
    max_adapter_calls: int = MAX_OBSERVATIONS

    def as_json(self) -> dict[str, int]:
        return {
            "max_cases": self.max_cases,
            "max_adapter_calls": self.max_adapter_calls,
        }


@dataclass(frozen=True)
class CounterfactualDecision:
    """One candidate decision produced in the fork's isolated run."""

    candidate_id: str
    source_candidate_id: str
    case_id: str
    observation_id: str
    outcome: str
    criteria_version: str
    observation_digest: str
    failed_criteria: tuple[str, ...]
    decision_id: str


@dataclass(frozen=True)
class CounterfactualReport:
    """The durable result of one separately authorized counterfactual run."""

    schema_version: str
    workflow_run_id: str
    source_workflow_run_id: str
    manifest_id: str
    manifest_digest: str
    adapter: str
    adapter_version: str
    changed_pins: Mapping[str, Any]
    budget: CounterfactualBudget
    observation_ids: tuple[str, ...]
    decisions: tuple[CounterfactualDecision, ...]
    protected_writes: bool


class CounterfactualError(ReplayError):
    """A fail-closed counterfactual authorization, input, or budget refusal."""


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


def _fork_refuse(code: str, message: str, *, status_code: int = 409) -> CounterfactualError:
    return CounterfactualError(code, message, status_code=status_code)


def _fork_budget(value: CounterfactualBudget | Mapping[str, Any] | None) -> CounterfactualBudget:
    if value is None:
        return CounterfactualBudget()
    if isinstance(value, CounterfactualBudget):
        result = value
    elif isinstance(value, Mapping):
        allowed = {"max_cases", "max_adapter_calls"}
        if set(value) != allowed:
            raise _fork_refuse(FORK_BUDGET, "the counterfactual budget has unsupported fields")
        result = CounterfactualBudget(
            max_cases=value["max_cases"],  # type: ignore[arg-type]
            max_adapter_calls=value["max_adapter_calls"],  # type: ignore[arg-type]
        )
    else:
        raise _fork_refuse(FORK_BUDGET, "the counterfactual budget is malformed")
    if any(
        type(item) is not int or item < 1 or item > MAX_OBSERVATIONS
        for item in (result.max_cases, result.max_adapter_calls)
    ):
        raise _fork_refuse(
            FORK_BUDGET,
            f"counterfactual budget values must be integers from 1 through {MAX_OBSERVATIONS}",
        )
    return result


def _fork_pins(
    source_pins: Mapping[str, Any], changed_pins: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        not isinstance(source_pins, Mapping)
        or not isinstance(changed_pins, Mapping)
        or not changed_pins
    ):
        raise _fork_refuse(FORK_PINS, "changed_pins must be a non-empty object")
    changed = dict(changed_pins)
    if any(type(key) is not str or not key.strip() for key in changed):
        raise _fork_refuse(FORK_PINS, "changed pin names must be non-empty strings")
    try:
        canonical_bytes(changed)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fork_refuse(FORK_PINS, "changed pins must be finite JSON") from exc
    merged = deepcopy(dict(source_pins))
    merged.update(deepcopy(changed))
    if merged == dict(source_pins):
        raise _fork_refuse(FORK_PINS, "changed_pins must change at least one pinned value")
    try:
        canonical_bytes(merged)
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fork_refuse(FORK_PINS, "the resulting pin set must be finite JSON") from exc
    return merged, changed


def _adapter_info(
    adapter: CounterfactualCandidateAdapter | Callable[..., Mapping[str, Any]] | None,
    *,
    authorize_live_evaluation: bool,
) -> tuple[Any, str, str, bool]:
    selected: Any = ScriptedCandidateAdapter() if adapter is None else adapter
    name = getattr(selected, "name", type(selected).__name__)
    version = getattr(selected, "version", "v1")
    declared_live = getattr(selected, "requires_live_authorization", None)
    if declared_live is None:
        declared_live = getattr(selected, "live", None)
    if declared_live is None:
        # A bare callable is not provably provider-free. Treat it as live so a
        # caller cannot smuggle a network/provider side effect through the
        # convenience callable form without the explicit authorization gate.
        live = callable(selected)
    elif type(declared_live) is not bool:
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter live marker is malformed")
    else:
        live = declared_live
    if (
        type(name) is not str
        or not name.strip()
        or len(name) > _MAX_ADAPTER_ID_LENGTH
        or type(version) is not str
        or not version.strip()
        or len(version) > _MAX_ADAPTER_ID_LENGTH
    ):
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter identity is malformed")
    if live and not authorize_live_evaluation:
        raise _fork_refuse(
            FORK_AUTHORIZATION,
            "a live candidate adapter requires explicit live-evaluation authorization",
            status_code=403,
        )
    evaluate = getattr(selected, "evaluate", None)
    if not callable(evaluate) and not callable(selected):
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter has no evaluation callable")
    return evaluate if callable(evaluate) else selected, name, version, live


def _candidate_case(
    evaluate: Callable[..., Mapping[str, Any]],
    *,
    case_id: str,
    source_case: Mapping[str, Any],
    pins: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        result = evaluate(
            case_id=case_id, source_case=deepcopy(dict(source_case)), pins=deepcopy(dict(pins))
        )
    except CounterfactualError:
        raise
    except Exception as exc:
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter failed closed") from exc
    if not isinstance(result, Mapping):
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter did not return a case object")
    projected = case_record_from_observation(result)
    if projected is None or projected.get("case_id") != case_id:
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter returned an invalid case record")
    requests = projected.get("document_requests")
    if not isinstance(requests, list) or any(not isinstance(item, dict) for item in requests):
        raise _fork_refuse(FORK_ADAPTER, "the candidate adapter returned an invalid request list")
    return deepcopy(projected)


def _fork_manifest(
    loaded: Any,
    *,
    workflow_run_id: str,
    principal: str,
    pins: Mapping[str, Any],
    moment: datetime,
) -> tuple[dict[str, Any], bytes, str]:
    record = deepcopy(loaded.record)
    record.update(
        {
            "manifest_id": new_manifest_id(),
            "workflow_run_id": workflow_run_id,
            "pins": deepcopy(dict(pins)),
            "subject": {"user_id": principal, "auth_method": "counterfactual"},
            "purpose": "counterfactual",
            "grant_id": None,
            "request_digest": None,
            "created_at": moment.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
        }
    )
    try:
        payload = canonical_bytes(record)
        digest = digest_of(payload)
        read_manifest(payload, digest)
    except (ManifestError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fork_refuse(
            FORK_PINS, "the changed pins do not form a supported run manifest"
        ) from exc
    return record, payload, digest


def fork_counterfactual(  # noqa: PLR0912, PLR0915 - this is the transaction-level fork contract
    session: Session,
    backend: StorageBackend,
    *,
    run: CaliberWorkflowRun,
    project_id: str | None,
    principal: str,
    changed_pins: Mapping[str, Any],
    budget: CounterfactualBudget | Mapping[str, Any] | None = None,
    candidate_adapter: CounterfactualCandidateAdapter
    | Callable[..., Mapping[str, Any]]
    | None = None,
    observation_ids: Sequence[str] | None = None,
    source_principal: str | None = None,
    authorize_live_evaluation: bool = False,
    now: datetime | None = None,
) -> CounterfactualReport:
    """Create and execute an isolated, deterministic counterfactual fork.

    The source run, its observations, proposals and decisions are read only.
    The fork gets a fresh run and manifest identity, its own observations and
    acceptance decisions, and a bounded independent budget.  The function
    flushes all new rows but never commits; callers retain transaction control.
    """

    if not isinstance(run, CaliberWorkflowRun):
        raise _fork_refuse(FORK_SOURCE, "the source must be a workflow-run record", status_code=400)
    if run.project_id != project_id:
        raise _fork_refuse(FORK_SOURCE, "the source run is outside the fork scope")
    if (
        type(principal) is not str
        or not principal.strip()
        or len(principal) > _MAX_PRINCIPAL_LENGTH
    ):
        raise _fork_refuse(FORK_AUTHORIZATION, "the fork principal is invalid", status_code=403)
    reader_principal = principal if source_principal is None else source_principal
    if (
        type(reader_principal) is not str
        or not reader_principal.strip()
        or len(reader_principal) > _MAX_PRINCIPAL_LENGTH
    ):
        raise _fork_refuse(
            FORK_SOURCE, "the source observation principal is invalid", status_code=400
        )
    selected_budget = _fork_budget(budget)
    loaded = _manifest(session, run, project_id)
    pins, changed = _fork_pins(loaded.pins, changed_pins)
    evaluate, adapter_name, adapter_version, live = _adapter_info(
        candidate_adapter,
        authorize_live_evaluation=authorize_live_evaluation,
    )
    ids = _validate_ids(observation_ids)
    rows = _verified_rows(
        session,
        run=run,
        project_id=project_id,
        principal=reader_principal,
        observation_ids=ids,
    )
    case_records, _model_ids = _read_rows(
        session,
        backend,
        rows,
        project_id=project_id,
        principal=reader_principal,
    )
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
        raise _fork_refuse(FORK_SOURCE, "the source run has no supported governed effect")
    originals = _original_decisions(session, run)
    if set(originals) != {proposal_id for proposal_id, _connector in proposal_rows}:
        raise _fork_refuse(FORK_SOURCE, "the source acceptance decisions do not match its effects")

    proposal_cases: list[tuple[str, str, str]] = []
    for proposal_id, connector in proposal_rows:
        criteria = CRITERIA_BY_CONNECTOR.get(connector)
        if criteria is None:
            raise _fork_refuse(FORK_SOURCE, "the source effect has no supported criteria")
        if originals[proposal_id].criteria_version != criteria.version:
            raise _fork_refuse(FORK_SOURCE, "the source decision criteria are not supported")
        base = observations_for_proposal(session, proposal_id)
        proposal = base.get("proposal")
        case_id = proposal.get("case_id") if isinstance(proposal, Mapping) else None
        if not isinstance(case_id, str) or case_id not in case_records:
            raise _fork_refuse(FORK_SOURCE, "the source observations omit a governed case")
        proposal_cases.append((proposal_id, connector, case_id))

    required_cases = tuple(
        dict.fromkeys(case_id for _proposal, _connector, case_id in proposal_cases)
    )
    if len(required_cases) > selected_budget.max_cases:
        raise _fork_refuse(FORK_BUDGET, "the source cases exceed the independent fork budget")
    candidate_cases: dict[str, dict[str, Any]] = {}
    for case_id in required_cases:
        if len(candidate_cases) >= selected_budget.max_adapter_calls:
            raise _fork_refuse(FORK_BUDGET, "the candidate adapter call budget was exceeded")
        candidate_cases[case_id] = _candidate_case(
            evaluate,
            case_id=case_id,
            source_case=case_records[case_id],
            pins=pins,
        )

    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise _fork_refuse(FORK_SOURCE, "now must be timezone-aware", status_code=400)
    fork_id = new_workflow_run_id()
    manifest_record, manifest_bytes, manifest_digest = _fork_manifest(
        loaded,
        workflow_run_id=fork_id,
        principal=principal,
        pins=pins,
        moment=moment,
    )
    naive_moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    spec = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "source_run_id": run.workflow_run_id,
        "source_principal": reader_principal,
        "authorized_by": principal,
        "adapter": {"name": adapter_name, "version": adapter_version, "live": live},
        "changed_pins": deepcopy(changed),
        "pins": deepcopy(pins),
        "budget": selected_budget.as_json(),
        "protected_writes": "disabled",
    }
    fork = CaliberWorkflowRun(
        workflow_run_id=fork_id,
        runtime_lineage_id=None,
        workflow_id=run.workflow_id,
        project_id=run.project_id,
        tenant_id=run.tenant_id,
        workflow_version_id=run.workflow_version_id,
        deployment_alias=run.deployment_alias,
        status="completed",
        source="counterfactual",
        created_by=principal,
        priority=0,
        queued_at=naive_moment,
        started_at=naive_moment,
        completed_at=naive_moment,
        attempt_number=1,
        source_run_id=run.workflow_run_id,
        counterfactual_spec=spec,
        manifest_snapshot=deepcopy(run.manifest_snapshot)
        if isinstance(run.manifest_snapshot, dict)
        else None,
        summary={
            "status": "completed",
            "counterfactual": {
                "source_run_id": run.workflow_run_id,
                "adapter": {"name": adapter_name, "version": adapter_version},
                "protected_writes": "disabled",
                "decisions": [],
            },
        },
        manifest_legacy=False,
    )
    session.add(fork)
    session.add(
        CaliberRunManifest(
            manifest_id=manifest_record["manifest_id"],
            workflow_run_id=fork_id,
            schema_version="v1",
            manifest_bytes=manifest_bytes,
            manifest_digest=manifest_digest,
            created_at=naive_moment,
        )
    )
    session.flush()

    observation_ids_by_case: dict[str, str] = {}
    for case_id, candidate_case in candidate_cases.items():
        observation = record_observation(
            session,
            backend,
            workflow_run_id=fork_id,
            project_id=project_id,
            principal=principal,
            node_id="counterfactual.adapter",
            occurrence_id=f"case:{digest_of(case_id.encode('utf-8'))[:32]}",
            attempt=1,
            observation_kind="read",
            request={
                "source_run_id": run.workflow_run_id,
                "case_id": case_id,
                "adapter": adapter_name,
                "adapter_version": adapter_version,
            },
            content=candidate_case,
            provider=f"counterfactual.{adapter_name}",
            provider_version=adapter_version,
        )
        observation_ids_by_case[case_id] = observation.observation_id

    decisions: list[CounterfactualDecision] = []
    versions: set[str] = set()
    for proposal_id, connector, case_id in proposal_cases:
        criteria = CRITERIA_BY_CONNECTOR[connector]
        observations = observations_for_proposal(session, proposal_id)
        observations["case_record"] = candidate_cases[case_id]
        candidate_id = f"cf-{fork_id}-{proposal_id}"
        recorded = record_decision(
            session,
            workflow_run_id=fork_id,
            candidate_id=candidate_id,
            predicates=criteria.predicates,
            observations=observations,
            now=moment,
        )
        decision = recorded.decision
        versions.add(decision.criteria_version)
        decisions.append(
            CounterfactualDecision(
                candidate_id=candidate_id,
                source_candidate_id=proposal_id,
                case_id=case_id,
                observation_id=observation_ids_by_case[case_id],
                outcome=decision.outcome,
                criteria_version=decision.criteria_version,
                observation_digest=decision.observation_digest,
                failed_criteria=decision.failed_criteria,
                decision_id=decision.decision_id,
            )
        )

    acceptance_status = (
        "accepted" if all(item.outcome == "accepted" for item in decisions) else "rejected"
    )
    fork.summary = {
        "status": "completed",
        "counterfactual": {
            "source_run_id": run.workflow_run_id,
            "adapter": {"name": adapter_name, "version": adapter_version},
            "protected_writes": "disabled",
            "acceptance": {
                "status": acceptance_status,
                "criteria_version": ",".join(sorted(versions)),
                "decisions": [
                    {
                        "decision_id": item.decision_id,
                        "candidate_id": item.candidate_id,
                        "outcome": item.outcome,
                        "observation_digest": item.observation_digest,
                        "failed_criteria": list(item.failed_criteria),
                    }
                    for item in decisions
                ],
            },
        },
    }
    session.flush()
    return CounterfactualReport(
        schema_version=REPLAY_SCHEMA_VERSION,
        workflow_run_id=fork_id,
        source_workflow_run_id=run.workflow_run_id,
        manifest_id=manifest_record["manifest_id"],
        manifest_digest=manifest_digest,
        adapter=adapter_name,
        adapter_version=adapter_version,
        changed_pins=deepcopy(changed),
        budget=selected_budget,
        observation_ids=tuple(observation_ids_by_case.values()),
        decisions=tuple(decisions),
        protected_writes=False,
    )


__all__ = [
    "CODES",
    "FORK_ADAPTER",
    "FORK_AUTHORIZATION",
    "FORK_BUDGET",
    "FORK_PINS",
    "FORK_SOURCE",
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
    "CounterfactualBudget",
    "CounterfactualCandidateAdapter",
    "CounterfactualDecision",
    "CounterfactualError",
    "CounterfactualReport",
    "ReplayDecision",
    "ReplayError",
    "ReplayReport",
    "ScriptedCandidateAdapter",
    "fork_counterfactual",
    "replay_historical",
]
