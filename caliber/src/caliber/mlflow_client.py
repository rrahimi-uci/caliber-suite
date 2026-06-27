"""MLflow assessment client.

The feedback poller never imports ``mlflow`` directly. It depends on the
:class:`MLflowAssessmentClient` Protocol, which has two implementations:

* :class:`MLflowAssessmentClientImpl` — the production client that calls
  ``mlflow.search_traces`` and ``Trace.search_assessments``.
* :class:`FakeMLflowAssessmentClient` — an in-memory test double.

This boundary lets tests exercise the poller end-to-end without standing up
an MLflow tracking server, while keeping the production code path
straightforward against the real MLflow 3.12 API.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

logger = logging.getLogger("caliber.mlflow_client")


@dataclass(frozen=True)
class AssessmentInfo:
    """A single MLflow assessment, normalized for CALIBER's use.

    Only the fields the poller needs to create a verification-queue row.
    Rich MLflow metadata (full feedback payload, span IDs, run IDs) is
    intentionally not surfaced here — if CALIBER needs more, it joins back to
    MLflow via the ``trace_id``.
    """

    assessment_id: str
    trace_id: str
    experiment_id: str
    created_at: datetime
    category: str
    free_text: str
    severity: str
    session_id: str | None = None


class MLflowAssessmentClient(Protocol):
    """The polling surface CALIBER actually depends on.

    Implementations are responsible for translating between MLflow's
    Assessment objects and :class:`AssessmentInfo`. Implementations must
    return assessments newest-first (so the poller can checkpoint
    incrementally) and must be safe to call with an empty experiment list.
    """

    def list_assessments_since(
        self,
        *,
        experiment_ids: list[str],
        since: datetime,
        max_results: int = 200,
    ) -> Iterable[AssessmentInfo]: ...


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------


class MLflowAssessmentClientImpl:
    """Production client against the installed MLflow 3.12 SDK.

    The query strategy:

    1. ``mlflow.search_traces(experiment_ids=..., return_type="list")`` returns
       ``list[Trace]`` for the relevant experiments.
    2. For each trace, call ``Trace.search_assessments(type="feedback")`` to
       get the trace's feedback-typed assessments. Filter out assessments
       created before ``since`` client-side.
    3. Map each ``Assessment`` into an :class:`AssessmentInfo`.

    Why client-side filtering on ``create_time_ms``? MLflow's ``filter_string``
    targets *trace* attributes, but assessments can be added to an old trace
    after a reviewer reads it — filtering by trace timestamp would miss
    those. The trade-off is that we re-scan recent traces' assessments each
    tick; acceptable at expected scale (single-org deployments).
    """

    def list_assessments_since(
        self,
        *,
        experiment_ids: list[str],
        since: datetime,
        max_results: int = 200,
    ) -> Iterable[AssessmentInfo]:
        if not experiment_ids:
            return []

        # Lazy import is deliberate: tests that only use FakeMLflowAssessmentClient
        # should not load MLflow at import time. The Impl is only constructed
        # in production paths where MLflow is already a hard dependency anyway.
        import mlflow  # noqa: PLC0415

        search_experiment_ids, experiment_aliases = _resolve_search_experiment_ids(
            mlflow,
            experiment_ids,
        )
        if not search_experiment_ids:
            return []

        # Pull recent traces. We don't filter by since at this layer for the
        # reason above; the caller's interval keeps the working set small.
        traces = mlflow.search_traces(
            experiment_ids=search_experiment_ids,
            max_results=max_results,
            order_by=["timestamp_ms DESC"],
            return_type="list",
        )

        since_ms = int(since.replace(tzinfo=timezone.utc).timestamp() * 1000)
        results: list[AssessmentInfo] = []
        for trace in traces or []:
            # MLflow's ``Trace`` carries the trace's ``info`` block —
            # ``experiment_id`` lives there. We resolve it once per trace
            # rather than once per assessment because every feedback on
            # the trace shares it.
            trace_experiment_id = _experiment_id_from_trace(trace)
            configured_experiment_id = experiment_aliases.get(
                trace_experiment_id,
                trace_experiment_id,
            )
            try:
                feedbacks = trace.search_assessments(type="feedback")
            except Exception:
                logger.exception("trace=%s: search_assessments failed; skipping", trace)
                continue
            for assessment in feedbacks:
                if assessment.create_time_ms is None or assessment.create_time_ms <= since_ms:
                    continue
                results.append(
                    _to_assessment_info(assessment, experiment_id=configured_experiment_id)
                )
        return results


def _resolve_search_experiment_ids(
    mlflow_mod: object,
    experiment_ids: list[str],
) -> tuple[list[str], dict[str, str]]:
    """Resolve configured MLflow experiment names to IDs for search_traces.

    CALIBER stores the configured experiment identifier on agents. Operators may
    provide either the numeric MLflow experiment ID or the human-readable
    experiment name. MLflow ``search_traces`` wants experiment IDs, while the
    poller still needs to route returned assessments back to the configured
    value. The returned alias map is keyed by resolved MLflow ID and points back
    to the original configured value.
    """
    resolved: list[str] = []
    aliases: dict[str, str] = {}
    get_by_name = getattr(mlflow_mod, "get_experiment_by_name", None)

    for raw in experiment_ids:
        value = str(raw).strip()
        if not value:
            continue
        if value.isdigit():
            resolved.append(value)
            aliases[value] = value
            continue
        if not callable(get_by_name):
            logger.warning("mlflow.get_experiment_by_name unavailable; skipping %s", value)
            continue
        experiment = get_by_name(value)
        experiment_id = (
            getattr(experiment, "experiment_id", None) if experiment is not None else None
        )
        if experiment_id is None:
            continue
        resolved_id = str(experiment_id).strip()
        if not resolved_id:
            continue
        resolved.append(resolved_id)
        aliases[resolved_id] = value
    return resolved, aliases


def _to_assessment_info(assessment: object, *, experiment_id: str) -> AssessmentInfo:
    """Translate an MLflow ``Assessment`` into the CALIBER record.

    ``experiment_id`` is resolved by the caller from the enclosing
    trace (MLflow's ``Assessment`` object doesn't carry it directly);
    the caller is responsible for passing the correct value. Passing
    an empty string leads to the poller silently dropping the
    assessment because no agent maps to ``""`` — pre-Phase-4-fix
    behavior we now have explicit coverage for.

    Defensive: MLflow's ``Assessment`` object is loosely typed (the
    feedback value can be a bool, a number, or a string), so we
    coerce things to sensible defaults rather than assuming a shape
    that may change in a future MLflow release.
    """
    # Use getattr so the function works both for real Assessment instances and
    # for any duck-typed test stand-in.
    assessment_id = str(getattr(assessment, "assessment_id", ""))
    trace_id = str(getattr(assessment, "trace_id", ""))
    create_time_ms = int(getattr(assessment, "create_time_ms", 0) or 0)
    rationale = str(getattr(assessment, "rationale", "") or "")
    metadata = getattr(assessment, "metadata", None) or {}

    feedback_obj = getattr(assessment, "feedback", None)
    feedback_value = getattr(feedback_obj, "value", None) if feedback_obj is not None else None

    severity = _severity_from_feedback(feedback_value)
    raw_category = metadata.get("category")
    category = raw_category if isinstance(raw_category, str) and raw_category else "feedback"
    session_id_raw = metadata.get("session_id")
    session_id = str(session_id_raw) if session_id_raw is not None else None

    return AssessmentInfo(
        assessment_id=assessment_id,
        trace_id=trace_id,
        experiment_id=experiment_id,
        created_at=datetime.fromtimestamp(create_time_ms / 1000.0, tz=timezone.utc),
        category=category,
        free_text=rationale,
        severity=severity,
        session_id=session_id,
    )


def _experiment_id_from_trace(trace: object) -> str:
    """Pull ``experiment_id`` from a Trace's ``.info`` block.

    MLflow's ``Trace`` objects expose trace metadata under ``.info``
    (a ``TraceInfo`` instance) — including ``experiment_id``. Older
    MLflow releases also expose it directly on the trace object. We
    accept either shape via duck-typing so a minor MLflow upgrade
    doesn't break ingest, returning ``""`` only when neither path
    resolves (which the poller already handles as "no matching agent
    → skip").
    """
    direct = getattr(trace, "experiment_id", None)
    if isinstance(direct, str) and direct:
        return direct
    info = getattr(trace, "info", None)
    info_experiment = getattr(info, "experiment_id", None) if info is not None else None
    if isinstance(info_experiment, str) and info_experiment:
        return info_experiment
    return ""


def _severity_from_feedback(value: object) -> str:
    """Map MLflow feedback values onto CALIBER's severity vocabulary.

    The mapping is conservative — anything non-negative (thumbs-up, numeric
    score, missing value) is "standard"; explicit negative signal becomes
    "critical". Phase 3 may add a third level once the eval framework
    distinguishes them.
    """
    if (
        value is False
        or value == 0
        or (isinstance(value, str) and value.lower() in {"down", "negative", "bad"})
    ):
        return "critical"
    return "standard"


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class FakeMLflowAssessmentClient:
    """In-memory implementation for tests.

    Holds a list of :class:`AssessmentInfo` records and filters them on
    ``experiment_ids`` / ``since`` exactly the way the protocol promises.
    Tests construct one of these and pre-seed the records, then exercise
    the poller end-to-end.
    """

    def __init__(self, records: list[AssessmentInfo] | None = None) -> None:
        self._records: list[AssessmentInfo] = list(records or [])

    def add(self, record: AssessmentInfo) -> None:
        self._records.append(record)

    def list_assessments_since(
        self,
        *,
        experiment_ids: list[str],
        since: datetime,
        max_results: int = 200,
    ) -> Iterable[AssessmentInfo]:
        wanted = set(experiment_ids)
        results = [r for r in self._records if r.experiment_id in wanted and r.created_at > since]
        results.sort(key=lambda r: r.created_at, reverse=True)
        return results[:max_results]
