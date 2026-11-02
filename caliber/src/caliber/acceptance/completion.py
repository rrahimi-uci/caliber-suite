"""Acceptance at run completion: the platform reads the system of record and judges every governed effect of a run (B09.2b, #791).

A queued run that the worker would mark ``completed`` is first judged here. The worker calls :func:`evaluate_run` with the
run row it owns; nothing in the model's text, the manifest, the request or the downstream's HTTP status is an input. For every
effect proposal of the run whose connector has a criteria set (:data:`CRITERIA_BY_CONNECTOR`: today ``document-request``,
judged by :data:`~caliber.acceptance.predicates.HARBORLIGHT_V1`) the platform builds the observations itself:

* ``proposal``: the case (from the proposal's recorded ``resource``), the ``document_type`` (from its recorded arguments) and
  the ``expected_version``, converted strictly from the recorded text (:func:`~caliber.effects.connectors.document_request.expected_version_of`:
  ``"01"``, ``" 1"``, ``"-1"`` and a missing version are *not* converted, and the criterion then fails closed);
* ``receipt``: the ``downstream_request_id`` of the proposal's own verified receipt (read back and recomputed by
  :func:`~caliber.effects.receipts.read_receipt`). A proposal with no receipt (a refused dispatch, a proposal that was never
  sent) has none, and so is not accepted;
* ``case_record``: the downstream's record of that case, read **now** by the platform from the operator's binding for the
  run's project (``CALIBER_EFFECT_CALLBACK_CONNECTORS``, the address the reconciler asks) through the guarded OpenAPI read, and
  projected onto the keys the criteria read (``case_id`` and, per listed request, ``request_id``, ``document_type`` and
  ``case_version``) so a change to an unrelated field never makes the same effect a new observation. A case that cannot be read
  (no binding, an error, a status other than 200, a body that is not a case, an implausibly long list) is simply absent: the
  criteria fail closed and the decision is a recorded rejection. The run is not left waiting on a downstream that may never
  answer.

Each proposal is one **candidate**, and its id is **the proposal's id** (``prop-<32 hex>``). That is the convention for a
decision about a run's own effect; a candidate that is not a recorded proposal (a counterfactual candidate, B09.5, #812, and
the evidence package that resolves the id, B14.2, #826) must use another prefix, so the two never collide in
``candidate_id``, which has no foreign key (:func:`is_proposal_candidate`).

**Which decision settles the run.** The decision this pass obtained for each candidate, returned by
:func:`~caliber.acceptance.decision.record_decision` against :data:`~caliber.acceptance.predicates.HARBORLIGHT_V1` passed
*explicitly* (the criteria are pinned by this registry, never taken from the caller; a row another recorder stored under the same
key with a different verdict is a ``decision_conflict``, not an answer). The run is ``accepted`` if and only if every one is;
its ids are kept in the run's summary so the public record names them. Rows kept under other observation digests are evidence, never the answer: ordering by the clock decides
nothing. The decisions land in the caller's transaction (``record_decision`` never commits), so they commit with the run's
terminal status or roll back with it, and a pass repeated over the same observations returns the same rows.

**Failure is closed.** A different verdict stored under the same key (``decision_conflict``), a stored row that is not
consistent (``decision_corrupt``), a criterion that raises (a bug, ``PredicateError``) and a store that refuses are *raised*
to the caller, which fails the run (``acceptance_error``) and audits it; nothing here swallows them and nothing here turns one
into a pass. Observations that cannot be frozen as JSON are not an error: they are a recorded rejection
(``invalid_observations``) by :func:`~caliber.acceptance.decision.judge`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.acceptance.decision import AcceptanceDecision, record_decision
from caliber.acceptance.predicates import HARBORLIGHT_V1, Predicate
from caliber.db.models import CaliberEffectProposal, CaliberWorkflowRun
from caliber.effects.connectors import document_request
from caliber.effects.receipts import read_receipt
from caliber.effects.records import ProposalError, proposal_state, proposal_view
from caliber.integrations.openapi.executor import execute_openapi_http_tool
from caliber.workflows.run_state import (
    ACCEPTANCE_ACCEPTED,
    ACCEPTANCE_REJECTED,
)

logger = logging.getLogger("caliber.acceptance.completion")

#: The prefix of the id of a recorded effect proposal (:func:`caliber.effects.records.new_proposal_id`), and so of every candidate
#: this module decides about.
PROPOSAL_CANDIDATE_PREFIX: Final = "prop-"
#: A case record that lists more requests than this is not read as an observation (the criteria and the digest are linear in it).
MAX_LISTED_REQUESTS: Final = 10_000
_REQUEST_KEYS: Final = ("request_id", "document_type", "case_version")


@dataclass(frozen=True)
class Criteria:
    """The criteria set that judges the effects of one connector: the predicates, pinned here (their shared criteria version is the set's)."""

    connector: str
    predicates: tuple[Predicate, ...]

    @property
    def version(self) -> str:
        return self.predicates[0].criteria_version

    @property
    def predicate_ids(self) -> tuple[str, ...]:
        return tuple(predicate.id for predicate in self.predicates)


#: The connectors whose effects are judged, and by which set. An effect of any other connector has no criteria set and is not
#: part of acceptance (a run whose only effects are such has no acceptance information).
CRITERIA_BY_CONNECTOR: Final[Mapping[str, Criteria]] = MappingProxyType(
    {document_request.CONNECTOR: Criteria(document_request.CONNECTOR, tuple(HARBORLIGHT_V1))}
)


@dataclass(frozen=True)
class CaseAnswer:
    """What the downstream answered to a read of a case."""

    status_code: int
    body: Any


#: ``(base_url, case_id) -> answer``. It may raise; the caller treats that as an unreadable case.
CaseReader = Callable[[str, str], CaseAnswer]
#: ``(connector, project) -> base url`` (``caliber.effects.reconcile.targets_from_bindings``).
Targets = Mapping[tuple[str, str], str]


def read_case(base_url: str, case_id: str) -> CaseAnswer:
    """Read the downstream's record of ``case_id`` (a guarded ``GET`` through the OpenAPI executor, so the egress policy applies)."""
    found = execute_openapi_http_tool(
        execution_config=document_request.case_config(base_url),
        input_schema=None,
        input_data=document_request.case_input(case_id),
    )
    return CaseAnswer(found["status_code"], found["json"])


def is_proposal_candidate(candidate_id: str) -> bool:
    """Whether ``candidate_id`` names a recorded effect proposal (and not, say, a counterfactual candidate)."""
    return isinstance(candidate_id, str) and candidate_id.startswith(PROPOSAL_CANDIDATE_PREFIX)


@dataclass(frozen=True)
class RunAcceptance:
    """The acceptance of one run: the status, the criteria version and the decision of each candidate."""

    status: str
    criteria_version: str
    decisions: tuple[AcceptanceDecision, ...]

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTANCE_ACCEPTED

    def as_summary(self) -> dict[str, Any]:
        """The compact record kept in the run's summary (the decisions themselves are the rows in the decision table)."""
        return {
            "status": self.status,
            "criteria_version": self.criteria_version,
            "error_code": None,
            "decisions": [
                {
                    "decision_id": decision.decision_id,
                    "candidate_id": decision.candidate_id,
                    "outcome": decision.outcome,
                    "observation_digest": decision.observation_digest,
                    "failed_criteria": list(decision.failed_criteria),
                }
                for decision in self.decisions
            ],
        }


def error_summary(error_code: str) -> dict[str, Any]:
    """The run-summary record of a run that could not be judged: no decision, and the fixed code of why."""
    return {
        "status": "error",
        "criteria_version": None,
        "error_code": error_code,
        "decisions": [],
    }


def _case_record(answer: CaseAnswer) -> dict[str, Any] | None:
    """The part of the downstream's case record the criteria read, or ``None`` if the answer is not a readable case."""
    body = answer.body
    if answer.status_code != 200 or not isinstance(body, dict):  # noqa: PLR2004 - HTTP status
        return None
    record: dict[str, Any] = {}
    if "case_id" in body:
        record["case_id"] = body["case_id"]
    listed = body.get("document_requests")
    if isinstance(listed, list):
        if len(listed) > MAX_LISTED_REQUESTS:
            return None
        record["document_requests"] = [
            {key: item[key] for key in _REQUEST_KEYS if key in item}
            if isinstance(item, dict)
            else item
            for item in listed
        ]
    return record


def _receipt_observation(session: Session, proposal_id: str) -> dict[str, Any] | None:
    receipt_id = proposal_state(session, proposal_id).receipt_id
    if receipt_id is None:
        return None
    try:
        record = read_receipt(session, receipt_id)
    except (KeyError, ProposalError):
        # A receipt that cannot be read back, or is not this proposal's, is not a receipt.
        logger.warning("proposal %s has a receipt that cannot be read back", proposal_id)
        return None
    return {"downstream_request_id": record["downstream_request_id"]}


def _observations(
    session: Session,
    proposal_id: str,
    *,
    project_id: str | None,
    targets: Targets,
    read_once: Callable[[str, str, str], dict[str, Any] | None],
) -> dict[str, Any]:
    view = proposal_view(session, proposal_id)
    args = view.args if view.args is not None else {}
    case_id = document_request.case_id_of_resource(view.resource)
    expected = document_request.expected_version_of(view.expected_resource_version)
    observations: dict[str, Any] = {
        "proposal": {
            "case_id": case_id,
            "document_type": args.get("document_type"),
            # An unconvertible version is passed on as it is (text or ``None``): the criterion that needs an integer fails closed.
            "expected_version": expected
            if expected is not None
            else view.expected_resource_version,
        }
    }
    receipt = _receipt_observation(session, proposal_id)
    if receipt is None:
        return observations
    observations["receipt"] = receipt
    base_url = targets.get((view.connector, project_id)) if project_id is not None else None
    if case_id is None or base_url is None:
        return observations
    record = read_once(base_url, case_id, proposal_id)
    if record is not None:
        observations["case_record"] = record
    return observations


def evaluate_run(
    session: Session,
    run: CaliberWorkflowRun,
    *,
    targets: Targets,
    reader: CaseReader = read_case,
    now: datetime | None = None,
) -> RunAcceptance | None:
    """Judge every governed effect of ``run`` and record one decision per candidate, or ``None`` if it has none to judge.

    Records in ``session``'s transaction and never commits. Every candidate is judged (a rejection does not stop the pass), so
    each has a decision; the result is ``accepted`` only if all are. Raises what
    :func:`~caliber.acceptance.decision.record_decision` raises (see the module text): the caller fails the run closed.
    """
    rows = session.execute(
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
    if not rows:
        return None
    reads: dict[tuple[str, str], dict[str, Any] | None] = {}

    def read_once(base_url: str, case_id: str, proposal_id: str) -> dict[str, Any] | None:
        """One read per distinct case in a pass (each may take its whole timeout), so effects on one case see one record."""
        key = (base_url, case_id)
        if key not in reads:
            try:
                reads[key] = _case_record(reader(base_url, case_id))
            except (
                Exception
            ):  # an unreachable or refusing downstream: not an observation (the text is never kept)
                logger.warning("the case of proposal %s could not be read", proposal_id)
                reads[key] = None
        return reads[key]

    decisions: list[AcceptanceDecision] = []
    versions: set[str] = set()
    for proposal_id, connector in rows:
        criteria = CRITERIA_BY_CONNECTOR[connector]
        observations = _observations(
            session,
            proposal_id,
            project_id=run.project_id,
            targets=targets,
            read_once=read_once,
        )
        recorded = record_decision(
            session,
            workflow_run_id=run.workflow_run_id,
            candidate_id=proposal_id,
            predicates=criteria.predicates,
            observations=observations,
            now=now,
        )
        decisions.append(recorded.decision)
        versions.add(criteria.version)
    status = ACCEPTANCE_ACCEPTED if all(d.accepted for d in decisions) else ACCEPTANCE_REJECTED
    return RunAcceptance(status, ",".join(sorted(versions)), tuple(decisions))


__all__ = [
    "CRITERIA_BY_CONNECTOR",
    "MAX_LISTED_REQUESTS",
    "PROPOSAL_CANDIDATE_PREFIX",
    "CaseAnswer",
    "CaseReader",
    "Criteria",
    "RunAcceptance",
    "Targets",
    "error_summary",
    "evaluate_run",
    "is_proposal_candidate",
    "read_case",
]
