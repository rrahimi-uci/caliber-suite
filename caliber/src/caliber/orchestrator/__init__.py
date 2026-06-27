"""Refinement-pipeline orchestrator.

Phase 2 lands the 6-stage pipeline (triage → evidence → diagnosis → candidate
→ eval → approval) one stage at a time, so the state machine for each stage
can be designed and tested in isolation before its expensive LLM work is
wired in.

This module currently exposes :func:`run_triage` only; the rest follow in
subsequent milestones. The orchestrator does *not* yet have a worker — jobs
sit in ``queued`` state until something invokes a stage function. The first
worker lands once all six stage functions exist.
"""

from __future__ import annotations

from caliber.orchestrator.candidate import run_candidate
from caliber.orchestrator.diagnosis import run_diagnosis
from caliber.orchestrator.eval_stage import run_eval
from caliber.orchestrator.evidence import run_evidence
from caliber.orchestrator.optimizer_select import select_optimizer
from caliber.orchestrator.triage import run_triage
from caliber.orchestrator.worker import RefinementWorker

__all__ = [
    "RefinementWorker",
    "run_candidate",
    "run_diagnosis",
    "run_eval",
    "run_evidence",
    "run_triage",
    "select_optimizer",
]
