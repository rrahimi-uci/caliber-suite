"""Immutable evidence for an evaluation run.

The review's finding: ``CaliberEvalRun.results`` persists the evaluated rows, but
the run records "no cryptographic content/run digest, full pre-truncation
inventory or sampling decision, or a resolved bundle of skill content/version,
prompt content/alias, draft workflow manifest, judge definition/model, and
provider configuration". A pinned run was therefore "reproducible by convention,
not by proof", and the per-scorer aggregates had no durable denominators, so a
future asynchronous consumer could not tell how many rows a mean was over.

This module builds the missing block. Four independent claims, each verifiable
without re-running anything:

``digests``
    A ``dataset_digest`` over the graded *inputs* and a ``content_digest`` over
    the complete scored rows. The first answers "was this the same data?", the
    second "is this the same result?". Separating them matters: a changed
    prediction with an unchanged dataset digest is a model/subject change, while
    a changed dataset digest invalidates the comparison entirely.

``sampling``
    How many examples were available *before* truncation, how many were scored,
    the cap that produced that, and the deterministic order used. A bounded run
    that cannot say it was bounded reads as exhaustive.

``denominators``
    Per-scorer valid-row count and weight sum. The aggregates are weighted means
    that exclude errored rows, so without the denominator a consumer cannot
    reconstruct — or safely combine — them.

``slices``
    Per-tag aggregates. Dataset tags were carried through to rows but never
    grouped, so a curated "hard cases" tag could not be read off a run.

``resolved``
    Identity of everything that produced the predictions: subject content digest,
    resolved version, judge definitions, model, and provider. A ``subject_ref`` of
    ``support-agent@3`` proves nothing if the prompt body changed underneath it.

Everything here is derived — no new inputs are required from the caller beyond
what the run already resolved — and it is written once, with the run, so it is
immutable in the same sense the rows are.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

#: Bump when the shape changes so a consumer can tell an old bundle from a new
#: one instead of guessing from which keys happen to be present.
EVIDENCE_SCHEMA_VERSION = 1

#: The deterministic load order the pre-truncation inventory refers to. Recorded
#: rather than implied: "the first 50" only means something with an order.
SAMPLE_ORDER = "created_at asc, example_id asc"


def _digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def content_digest_of_text(text: str | None) -> str | None:
    """Digest a subject's body (prompt/skill content) so a silent edit is visible."""
    if text is None:
        return None
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def dataset_digest(rows: Sequence[Mapping[str, Any]]) -> str:
    """Digest the graded inputs, independent of any prediction.

    Deliberately excludes the prediction and the scores: this is the "was this the
    same data?" claim, which must be comparable across two runs of different
    subjects over one dataset.
    """
    return _digest(
        [
            {
                "example_id": row.get("example_id"),
                "input": row.get("input"),
                "expected": row.get("expected"),
                "weight": row.get("weight", 1.0),
                "tags": sorted(str(tag) for tag in (row.get("tags") or [])),
            }
            for row in rows
        ]
    )


def _row_weight(row: Any) -> float:
    raw = getattr(row, "weight", 1.0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 1.0
    value = float(raw)
    return value if value >= 0 and math.isfinite(value) else 1.0


def scorer_denominators(rows: Sequence[Any]) -> dict[str, dict[str, float]]:
    """Valid-row count and weight sum behind each per-scorer mean.

    A row with any scorer error is excluded from every per-scorer aggregate (so a
    partial failure cannot create survivorship bias). That policy is only
    auditable if the denominator it produced is recorded.
    """
    denominators: dict[str, dict[str, float]] = {}
    for row in rows:
        if getattr(row, "error", None):
            continue
        weight = _row_weight(row)
        for name in getattr(row, "scores", {}) or {}:
            entry = denominators.setdefault(name, {"valid_rows": 0.0, "weight_sum": 0.0})
            entry["valid_rows"] += 1
            entry["weight_sum"] += weight
    return {
        name: {"valid_rows": int(entry["valid_rows"]), "weight_sum": round(entry["weight_sum"], 6)}
        for name, entry in sorted(denominators.items())
    }


def tag_slices(rows: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Weighted aggregates grouped by dataset tag.

    A row with several tags contributes to each of them, so the slice weight sums
    do not add up to the run total — that is inherent to overlapping tags, not a
    bug, and is why each slice reports its own denominator.
    """
    slices: dict[str, dict[str, Any]] = {}
    for row in rows:
        weight = _row_weight(row)
        errored = bool(getattr(row, "error", None))
        for tag in sorted({str(tag) for tag in (getattr(row, "tags", None) or [])}):
            entry = slices.setdefault(
                tag,
                {
                    "n_examples": 0,
                    "weight_sum": 0.0,
                    "passed_count": 0,
                    "errored_count": 0,
                    "_score_weight": 0.0,
                    "_pass_weight": 0.0,
                },
            )
            entry["n_examples"] += 1
            entry["weight_sum"] += weight
            if errored:
                entry["errored_count"] += 1
            if getattr(row, "passed", False):
                entry["passed_count"] += 1
                entry["_pass_weight"] += weight
            entry["_score_weight"] += weight * float(getattr(row, "score", 0.0) or 0.0)

    out: dict[str, dict[str, Any]] = {}
    for tag, entry in sorted(slices.items()):
        weight_sum = float(entry["weight_sum"])
        out[tag] = {
            "n_examples": entry["n_examples"],
            "weight_sum": round(weight_sum, 6),
            "passed_count": entry["passed_count"],
            "errored_count": entry["errored_count"],
            # Weighted means, matching how the headline overall/pass_rate are
            # computed. Zero total weight yields ``None`` rather than 0.0: an
            # explicitly excluded slice has no mean, it does not score zero.
            "overall": round(float(entry["_score_weight"]) / weight_sum, 6) if weight_sum else None,
            "pass_rate": round(float(entry["_pass_weight"]) / weight_sum, 6)
            if weight_sum
            else None,
        }
    return out


def build_evidence(
    *,
    scored_rows: Sequence[Mapping[str, Any]],
    result: Any,
    available_examples: int,
    sample_cap: int | None,
    scorers: Sequence[str],
    pass_threshold: float,
    dataset_id: str,
    dataset_version: int,
    predict_target: str,
    subject_ref: str | None,
    model: str | None,
    resolved: Mapping[str, Any] | None = None,
    latencies_ms: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Assemble the immutable evidence block for one evaluation run.

    ``scored_rows`` is the loader's row dicts *as passed to the scorer* (so the
    dataset digest covers what was actually graded, not what the dataset holds
    now). ``result`` is the :class:`caliber.eval.scorecard.ScorecardResult`.
    """
    rows = list(getattr(result, "rows", []) or [])
    evaluated = len(rows)
    latency_values = [float(value) for value in (latencies_ms or []) if value is not None]
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "digests": {
            "dataset": dataset_digest(scored_rows),
            "content": _digest(
                [row.to_dict() if hasattr(row, "to_dict") else dict(row) for row in rows]
            ),
        },
        "sampling": {
            "available_examples": int(available_examples),
            "evaluated_examples": evaluated,
            "cap": sample_cap,
            # The single fact a bounded run must not omit.
            "truncated": bool(available_examples > evaluated),
            "order": SAMPLE_ORDER,
        },
        "denominators": scorer_denominators(rows),
        "slices": tag_slices(rows),
        "policy": {
            "scorers": list(scorers),
            "pass_threshold": pass_threshold,
            "incomplete_row_policy": (
                "a row with any scorer error scores 0, cannot pass, and is excluded "
                "from every per-scorer aggregate while remaining in the overall and "
                "pass-rate denominators"
            ),
        },
        "resolved": {
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "predict_target": predict_target,
            "subject_ref": subject_ref,
            "model": model,
            **dict(resolved or {}),
        },
        "cost": {
            # Latency is measured per prediction; token/cost accounting for the
            # generic evaluation path depends on provider usage reporting, which is
            # recorded per row when the provider supplies it. Absent values are
            # omitted rather than defaulted to zero, which would understate spend.
            "avg_latency_ms": round(sum(latency_values) / len(latency_values), 3)
            if latency_values
            else None,
            "max_latency_ms": round(max(latency_values), 3) if latency_values else None,
            "total_latency_ms": round(sum(latency_values), 3) if latency_values else None,
        },
    }


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "SAMPLE_ORDER",
    "build_evidence",
    "content_digest_of_text",
    "dataset_digest",
    "scorer_denominators",
    "tag_slices",
]
