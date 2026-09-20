"""The two-week alpha plan's Day 1 seed fixture: one flagged trace, one
prompt, one eval set.

See ``docs/two-week-alpha-plan.md`` (Week 1, Day 1) and
``docs/reports/two-week-alpha-day1-decision.md`` for the plan and the
path/provider decisions this fixture implements. In short: this is the
*one* fixed scenario the two-week alpha journey (trace -> diagnose ->
candidate -> evaluate -> apply -> release/rollback) will be driven through,
targeting the roadmap's own single-tenant release-governance path (not the
separate multi-tenant "Workspace" subsystem) with the OpenAI provider
profile.

Contents (all committed under ``day1_seed/`` next to this module):

* ``intake_classifier_prompt.md`` -- the ``intake-classifier`` prompt
  template (frontmatter + body), reused byte-for-byte from
  ``docs-site/cookbooks/01-prompt-regression-lab/assets/prompts/intake-classifier.md``
  (the first-milestone pilot fixture pack's "Fixture A"). Copied rather than
  referenced in place so this seed fixture stays self-contained and doesn't
  silently drift if the cookbook asset changes.
* ``intake_classifier_eval_set.jsonl`` -- the matching 12-case eval set
  (P01-P12: golden, edge, and prompt-injection cases), reused byte-for-byte
  from the same cookbook's
  ``assets/dataset/intake-classifier.jsonl``.
* ``flagged_trace.json`` -- a recorded, replayable :class:`TraceSummary` for
  one flagged production trace of the intake-classifier prompt: the model
  classified a compound ticket ("I was double charged AND the page keeps
  crashing...") as a single billing issue and set ``needs_review=false``,
  when the ticket's ambiguity should have forced review. This mirrors the
  eval set's own P07-P09 ambiguous/edge cases, which is exactly why the
  DSPyBootstrapFewShot optimizer (bootstrapping few-shot demonstrations from
  that same eval set) is the fix this scenario wants to demonstrate.
* ``verification_item.json`` -- the flagged item's metadata (category,
  severity, free-text description, artifact-type hint) used to seed the
  ``CaliberVerificationItem`` row that starts the pipeline.

Offline-safe by construction: the "trace" is a recorded :class:`TraceSummary`
loaded into a :class:`FakeTraceClient`, not a live ``mlflow.get_trace`` call.
This follows the same convention ``caliber/tests/test_evidence_trace.py``
already uses to test the evidence stage without a real MLflow server --
production still calls :class:`caliber.trace_client.MLflowTraceClient`;
only the fixture that stands in for "a real trace" is replayed from disk.

Callers (tests today, a Day 2-3 backend-wiring script next) get:

* :func:`load_prompt_template` -- the raw prompt file contents.
* :func:`load_eval_examples` -- the parsed JSONL rows.
* :func:`eval_examples_as_dataset_rows` -- the same rows reshaped to the
  ``{"input", "expected", "weight", "tags"}`` shape
  ``CaliberEvalDatasetExample`` (and DSPy's trainset loader in
  ``orchestrator/candidate.py::_load_trainset``) expect.
* :func:`load_flagged_trace_summary` / :func:`build_fake_trace_client` --
  the trace, as a :class:`TraceSummary` and as a ready-to-inject
  :class:`FakeTraceClient`.
* :func:`seed_flagged_job` -- seeds a verified ``CaliberVerificationItem`` +
  queued ``CaliberRefinementJob`` directly into the DB, the same pattern
  ``caliber/tests/test_e2e_pipeline.py::_seed_verified_job`` uses, except
  this seed also pins ``job.optimizer_type = "DSPyBootstrapFewShot"``
  explicitly. That pin matters: per
  ``orchestrator/optimizer_select.py::select_optimizer``, automatic
  optimizer selection would pick ``MetaPrompt`` for this diagnosis, not
  DSPy -- the DSPy/candidate path this plan wants to exercise is opt-in,
  reachable only through an explicit job/agent override.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from caliber.db.models import CaliberRefinementJob, CaliberVerificationItem
from caliber.ids import new_item_id, new_job_id
from caliber.trace_client import FakeTraceClient, TraceSummary

FIXTURE_DIR = Path(__file__).parent / "day1_seed"

PROMPT_PATH = FIXTURE_DIR / "intake_classifier_prompt.md"
EVAL_SET_PATH = FIXTURE_DIR / "intake_classifier_eval_set.jsonl"
TRACE_PATH = FIXTURE_DIR / "flagged_trace.json"
VERIFICATION_ITEM_PATH = FIXTURE_DIR / "verification_item.json"

# The one fixed optimizer this seed fixture exercises. Explicit override (see
# module docstring) -- automatic selection would not reach DSPy for this
# diagnosis.
SEED_OPTIMIZER_TYPE = "DSPyBootstrapFewShot"

# Default agent id used when a caller doesn't need a different one. Kept in
# one place so tests and the Day 2-3 seed script agree on the same value.
DEFAULT_AGENT_ID = "intake-classifier-day1-seed"


@dataclass(frozen=True)
class EvalExample:
    """One row of the committed ``intake_classifier_eval_set.jsonl`` fixture."""

    id: str
    tags: list[str]
    inputs: dict[str, Any]
    expectations: dict[str, Any]


def load_prompt_template() -> str:
    """Return the raw ``intake-classifier`` prompt file (frontmatter + body)."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def load_eval_examples() -> list[EvalExample]:
    """Parse the committed eval-set JSONL into :class:`EvalExample` rows.

    Raises ``ValueError`` (via the assertions below surfacing as part of a
    dict-shape check) if a row is missing a required key -- callers should
    treat a parse failure here as the fixture itself being broken, not a
    recoverable runtime condition.
    """
    examples: list[EvalExample] = []
    text = EVAL_SET_PATH.read_text(encoding="utf-8")
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{EVAL_SET_PATH}:{line_no}: invalid JSON: {exc}") from exc
        for key in ("id", "inputs", "expectations"):
            if key not in row:
                raise ValueError(f"{EVAL_SET_PATH}:{line_no}: missing required key {key!r}")
        examples.append(
            EvalExample(
                id=row["id"],
                tags=list(row.get("tags", [])),
                inputs=dict(row["inputs"]),
                expectations=dict(row["expectations"]),
            )
        )
    return examples


def eval_examples_as_dataset_rows() -> list[dict[str, Any]]:
    """Reshape the eval examples into ``CaliberEvalDatasetExample`` row shape.

    Matches the ``{"input": ..., "expected": ..., "weight": ...}`` shape
    ``orchestrator/candidate.py::_load_trainset`` reads for DSPy's trainset,
    and the ``input``/``expected`` columns ``CaliberEvalDatasetExample``
    itself stores (the JSONL fixture's own field names, ``inputs`` /
    ``expectations``, are the cookbook/UI-facing names; the DB columns are
    singular).
    """
    return [
        {
            "input": example.inputs,
            "expected": example.expectations,
            "weight": 1.0,
            "tags": example.tags,
        }
        for example in load_eval_examples()
    ]


def load_flagged_trace_summary() -> TraceSummary:
    """Load the committed flagged-trace fixture as a :class:`TraceSummary`."""
    payload = json.loads(TRACE_PATH.read_text(encoding="utf-8"))
    return TraceSummary(
        status=payload["status"],
        request_preview=payload["request_preview"],
        response_preview=payload["response_preview"],
        span_count=payload["span_count"],
        tool_calls=list(payload.get("tool_calls", [])),
        error=payload.get("error"),
    )


def flagged_trace_id() -> str:
    """Return the fixed trace id the flagged-trace fixture is keyed under."""
    payload: dict[str, Any] = json.loads(TRACE_PATH.read_text(encoding="utf-8"))
    trace_id = payload["trace_id"]
    if not isinstance(trace_id, str) or not trace_id:
        raise ValueError(f"{TRACE_PATH}: 'trace_id' must be a non-empty string")
    return str(trace_id)


def build_fake_trace_client() -> FakeTraceClient:
    """Build a :class:`FakeTraceClient` pre-loaded with the flagged trace.

    This is the offline-safe stand-in for a live ``mlflow.get_trace`` call --
    the same pattern ``test_evidence_trace.py`` uses -- so the evidence stage
    can be exercised against this fixture without a real MLflow server.
    """
    return FakeTraceClient({flagged_trace_id(): load_flagged_trace_summary()})


def _verification_item_metadata() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(VERIFICATION_ITEM_PATH.read_text(encoding="utf-8"))
    return payload


def seed_flagged_job(
    session: Session,
    *,
    agent_id: str = DEFAULT_AGENT_ID,
) -> str:
    """Seed the fixture's verified feedback item + queued refinement job.

    Mirrors ``caliber/tests/test_e2e_pipeline.py::_seed_verified_job``
    (a verified ``CaliberVerificationItem`` plus a ``queued``
    ``CaliberRefinementJob`` on the ``triage`` stage), pointed at this
    fixture's flagged trace and free-text, with one addition:
    ``job.optimizer_type`` is pinned to :data:`SEED_OPTIMIZER_TYPE` so the
    candidate stage takes the explicit-override path in
    ``orchestrator/optimizer_select.py::select_optimizer`` (step 1) instead
    of falling through to automatic selection, which would pick
    ``MetaPrompt`` for this diagnosis.

    The caller is responsible for registering ``agent_id`` (e.g. via
    ``POST /agents`` or a directly-inserted ``CaliberAgentConfig`` row)
    before calling this -- ``agent_id`` is FK-constrained.

    Returns the new ``job_id``.
    """
    metadata = _verification_item_metadata()
    item = CaliberVerificationItem(
        item_id=new_item_id(),
        agent_id=agent_id,
        category=metadata["category"],
        free_text=metadata["free_text"],
        severity=metadata["severity"],
        status="verified",
        verified_by="@day1-seed-fixture",
        trace_id=flagged_trace_id(),
        artifact_type_hint=metadata.get("artifact_type_hint"),
    )
    session.add(item)
    session.flush()

    job = CaliberRefinementJob(
        job_id=new_job_id(),
        agent_id=agent_id,
        primary_item_id=item.item_id,
        artifact_type="prompt",
        optimizer_type=SEED_OPTIMIZER_TYPE,
        status="queued",
        current_stage="triage",
        bundle_targets=[],
    )
    session.add(job)
    session.commit()
    return job.job_id
