"""DSPy BootstrapFewShot and MIPROv2 bridge for candidate generation.

CALIBER's automatic selector can route an opted-in prompt job to
``"DSPyBootstrapFewShot"``; an explicit job/agent override can also select
``"DSPyMIPRO"``. The OpenAI provider lazily loads this module for either name.

What this does
--------------
DSPy optimizes a *program* (a ``dspy.Module``), not a raw prompt string. We
bridge the two by wrapping the agent's current prompt as a single-field
``question -> answer`` :class:`dspy.Signature` whose *instructions* are the
prompt body, then running DSPy's ``BootstrapFewShot`` teleprompter over the
agent's eval dataset. BootstrapFewShot runs the prompt as a teacher over each
trainset example, keeps the traces whose output passes the metric, and emits
them as few-shot *demonstrations*. We render the selected demos into a
``# Few-shot examples`` block appended to the prompt — that rendered prompt is
the candidate, which then flows through CALIBER's normal eval gate and
promotion path unchanged.

Deliberate Phase-1 simplifications (documented so they're not mistaken for
bugs):

* **Single ``question -> answer`` signature.** Arbitrary CALIBER prompts are
  treated as one input → one output task. Multi-field / structured tasks remain
  a later milestone for both teleprompters.
* **Deterministic metric.** A normalized containment check between the gold
  ``expected`` text and the model output decides whether a demo is kept. This
  keeps bootstrapping cheap (no judge-LLM cost) and reproducible. Swapping in
  an LLM-judge metric built from the eval scorer suite is the obvious Phase-2
  upgrade. MIPROv2 currently uses the same deterministic metric while jointly
  searching the instruction and demonstrations.

Packaging
---------
``dspy`` ships in the dedicated ``[dspy]`` extra on top of the OpenAI-backed
provider. The core / ``FakeLLMProvider`` path never imports this module, and
the OpenAI provider only loads it when a DSPy optimizer is actually selected,
so the base ``caliber-suite[llm]`` install stays dspy-free.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import dspy
from dspy.teleprompt import BootstrapFewShot, MIPROv2

from caliber.llm.provider import CandidateContext, LLMUsage, PromptCandidate

logger = logging.getLogger("caliber.llm.dspy_optimizer")


class EmptyTrainsetError(RuntimeError):
    """Raised when the trainset has no usable ``(input -> expected)`` rows.

    The provider catches this and falls back to MetaPrompt — BootstrapFewShot
    can't select demonstrations without examples to bootstrap from.
    """


def run_bootstrap_fewshot(
    *,
    context: CandidateContext,
    model: str,
    max_bootstrapped_demos: int,
    max_labeled_demos: int,
) -> tuple[PromptCandidate, LLMUsage]:
    """Run DSPy ``BootstrapFewShot`` and return a few-shot-augmented candidate.

    Parameters
    ----------
    context:
        The candidate context. ``context.trainset`` supplies the
        ``[{"input": ..., "expected": ...}]`` rows; ``current_artifact_content``
        becomes the program's instructions.
    model:
        Plain OpenAI model id (e.g. ``"gpt-4o-mini"``). Wrapped as
        ``"openai/<model>"`` for DSPy's litellm-backed ``dspy.LM``.
    max_bootstrapped_demos / max_labeled_demos:
        Teleprompter caps — how many self-generated vs. raw labeled demos may
        be added.

    Raises
    ------
    EmptyTrainsetError
        If no usable trainset rows are present.
    """
    lm, program, examples, instructions = _prepare_program(context, model)

    teleprompter = BootstrapFewShot(
        metric=_demo_metric,
        max_bootstrapped_demos=max_bootstrapped_demos,
        max_labeled_demos=max_labeled_demos,
    )
    compiled = teleprompter.compile(program, trainset=examples)

    return _build_candidate(
        context=context,
        compiled=compiled,
        lm=lm,
        n_examples=len(examples),
        instructions=instructions,
        optimizer_label="BootstrapFewShot",
        optimize_instruction=False,
    )


def run_mipro(
    *,
    context: CandidateContext,
    model: str,
    max_bootstrapped_demos: int,
    max_labeled_demos: int,
    auto: str = "light",
) -> tuple[PromptCandidate, LLMUsage]:
    """Run DSPy ``MIPROv2`` and return an instruction+demo-optimized candidate.

    Where BootstrapFewShot only *adds* few-shot demonstrations, MIPROv2 jointly
    searches over candidate **instructions** (proposed by a prompt model) and
    demonstrations, scoring each combination on the trainset with the metric and
    keeping the best. The candidate therefore carries the *optimized*
    instruction body, not just the original one with demos appended.

    Parameters
    ----------
    auto:
        MIPROv2's built-in budget preset — ``"light"`` / ``"medium"`` /
        ``"heavy"``. Higher presets try more instruction/demo candidates and
        cost proportionally more LLM calls (instruction proposal + minibatch
        evaluation), so ``"light"`` is the default.

    Raises
    ------
    EmptyTrainsetError
        If no usable trainset rows are present.
    """
    lm, program, examples, instructions = _prepare_program(context, model)

    teleprompter = MIPROv2(
        metric=_demo_metric,
        auto=auto,
        max_bootstrapped_demos=max_bootstrapped_demos,
        max_labeled_demos=max_labeled_demos,
    )
    # MIPROv2 no longer gates on an interactive cost confirmation (the
    # ``requires_permission_to_run`` flag is deprecated in dspy 3.x), so compile
    # runs unattended — correct for the orchestrator worker (no TTY).
    compiled = teleprompter.compile(program, trainset=examples)

    return _build_candidate(
        context=context,
        compiled=compiled,
        lm=lm,
        n_examples=len(examples),
        instructions=instructions,
        optimizer_label="MIPROv2",
        optimize_instruction=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prepare_program(context: CandidateContext, model: str) -> tuple[Any, Any, list[Any], str]:
    """Shared setup for the DSPy teleprompters.

    Builds the ``dspy.Example`` trainset, configures the LM, and wraps the
    current prompt as a ``question -> answer`` :class:`dspy.Predict` program.
    Returns ``(lm, program, examples, instructions)``. Raises
    :class:`EmptyTrainsetError` when no usable trainset rows exist.
    """
    examples = _build_examples(context.trainset or [])
    if not examples:
        raise EmptyTrainsetError(
            f"{context.optimizer_type}: no usable trainset rows for job {context.job_id!r}"
        )

    label = context.skill_name or context.agent_id
    instructions = (context.current_artifact_content or f"You are {label}.").strip()
    if not instructions:
        instructions = f"You are {label}."

    lm = dspy.LM(f"openai/{model}")
    dspy.configure(lm=lm)

    signature = dspy.Signature("question -> answer").with_instructions(instructions)
    program = dspy.Predict(signature)
    return lm, program, examples, instructions


def _build_candidate(
    *,
    context: CandidateContext,
    compiled: Any,
    lm: Any,
    n_examples: int,
    instructions: str,
    optimizer_label: str,
    optimize_instruction: bool,
) -> tuple[PromptCandidate, LLMUsage]:
    """Assemble a :class:`PromptCandidate` from a compiled DSPy program.

    Renders the selected demonstrations into a few-shot block. When
    ``optimize_instruction`` is set (MIPRO), the optimized instruction replaces
    the original prompt body; otherwise (BootstrapFewShot) the original
    instruction is kept and demos are appended.
    """
    demos = _extract_demos(compiled)

    final_instructions = instructions
    instruction_changed = False
    if optimize_instruction:
        optimized = _extract_instruction(compiled, instructions).strip()
        if optimized and optimized != instructions.strip():
            final_instructions = optimized
            instruction_changed = True

    block = _render_demos_block(demos)
    content = f"{final_instructions}\n\n{block}" if block else final_instructions

    rationale = (
        f"DSPy {optimizer_label} selected {len(demos)} few-shot demonstration(s) "
        f"from {n_examples} trainset example(s)"
    )
    if optimize_instruction:
        rationale += "; instruction " + ("rewritten" if instruction_changed else "unchanged")
    root_cause = getattr(context.diagnosis, "root_cause", "") or ""
    if root_cause:
        rationale = f"{rationale}: {root_cause}"
    if not demos and not instruction_changed:
        rationale = (
            f"{rationale} (no change selected; the eval gate will reject a "
            "no-improvement candidate)"
        )

    diff_parts: list[str] = []
    if instruction_changed:
        diff_parts.append("instruction rewritten")
    diff_parts.append(f"+{len(demos)} few-shot demo(s)" if demos else "no demos selected")

    candidate = PromptCandidate(
        artifact_type=context.artifact_type,
        content=content,
        rationale=rationale,
        diff_summary=", ".join(diff_parts),
    )
    return candidate, _usage_from_history(lm)


def _build_examples(trainset: list[dict[str, Any]]) -> list[Any]:
    """Map CALIBER trainset rows to ``dspy.Example`` objects.

    Rows without a non-empty input are skipped — DSPy needs an input to run the
    teacher over. A missing ``expected`` is tolerated (the metric treats an
    unlabeled example as auto-pass so its trace can still seed a demo).
    """
    examples: list[Any] = []
    for row in trainset:
        if not isinstance(row, dict):
            continue
        question = _example_text(row.get("input"))
        if not question:
            continue
        answer = _example_text(row.get("expected"))
        examples.append(dspy.Example(question=question, answer=answer).with_inputs("question"))
    return examples


def _demo_metric(example: Any, prediction: Any, trace: Any = None) -> bool:  # noqa: ARG001 -- ``trace`` is part of DSPy's required metric signature
    """Deterministic keep/drop metric for a bootstrapped demonstration.

    Returns ``True`` (keep) when the gold answer and the model output overlap
    by normalized containment, or when the example is unlabeled. Cheap and
    reproducible — no judge LLM. See the module docstring for the rationale.
    """
    gold = _normalize(getattr(example, "answer", ""))
    pred = _normalize(getattr(prediction, "answer", ""))
    if not gold:
        return True
    if not pred:
        return False
    return gold in pred or pred in gold


def _extract_demos(compiled: Any) -> list[dict[str, str]]:
    """Pull the selected demonstrations off a compiled DSPy program.

    DSPy has moved this around across versions, so we try the documented
    surfaces in order: ``compiled.demos`` (single-predictor programs), then the
    first entry of ``compiled.predictors()``, then ``named_predictors()``.
    """
    raw = getattr(compiled, "demos", None)
    if not raw:
        raw = _first_predictor_demos(compiled)
    demos: list[dict[str, str]] = []
    for demo in raw or []:
        question = _attr_or_key(demo, "question")
        answer = _attr_or_key(demo, "answer")
        if question or answer:
            demos.append({"question": question, "answer": answer})
    return demos


def _extract_instruction(compiled: Any, fallback: str) -> str:
    """Pull the optimized instruction off a compiled DSPy program (MIPRO).

    MIPROv2 rewrites the signature's ``instructions``. We read it off the first
    predictor's signature, falling back to the program-level signature, then to
    the original instruction when neither is present.
    """
    predictors_fn = getattr(compiled, "predictors", None)
    predictors: list[Any] = []
    if callable(predictors_fn):
        try:
            predictors = list(predictors_fn())
        except Exception:  # pragma: no cover - defensive across dspy versions
            predictors = []
    for predictor in predictors:
        signature = getattr(predictor, "signature", None)
        instruction = getattr(signature, "instructions", None)
        if isinstance(instruction, str) and instruction.strip():
            return instruction

    signature = getattr(compiled, "signature", None)
    instruction = getattr(signature, "instructions", None)
    if isinstance(instruction, str) and instruction.strip():
        return instruction
    return fallback


def _first_predictor_demos(compiled: Any) -> list[Any]:
    predictors_fn = getattr(compiled, "predictors", None)
    if callable(predictors_fn):
        try:
            predictors = list(predictors_fn())
        except Exception:  # pragma: no cover - defensive across dspy versions
            predictors = []
        for predictor in predictors:
            demos = getattr(predictor, "demos", None)
            if demos:
                return list(demos)
    named_fn = getattr(compiled, "named_predictors", None)
    if callable(named_fn):
        try:
            for _name, predictor in named_fn():
                demos = getattr(predictor, "demos", None)
                if demos:
                    return list(demos)
        except Exception:  # pragma: no cover - defensive across dspy versions
            return []
    return []


def _render_demos_block(demos: list[dict[str, str]]) -> str:
    if not demos:
        return ""
    lines = ["# Few-shot examples (selected by DSPy BootstrapFewShot)", ""]
    for index, demo in enumerate(demos, start=1):
        lines.append(f"## Example {index}")
        lines.append(f"Input: {demo.get('question', '').strip()}")
        lines.append(f"Output: {demo.get('answer', '').strip()}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _usage_from_history(lm: Any) -> LLMUsage:
    """Best-effort token accounting from the DSPy LM call history."""
    history = getattr(lm, "history", None) or []
    input_tokens = 0
    output_tokens = 0
    for entry in history:
        usage = entry.get("usage") if isinstance(entry, dict) else None
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("prompt_tokens", 0) or 0)
        output_tokens += int(usage.get("completion_tokens", 0) or 0)
    return LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=0.0)


def _example_text(value: Any) -> str:
    """Flatten a JSON ``input``/``expected`` payload to a single string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in (
            "input",
            "text",
            "question",
            "query",
            "prompt",
            "content",
            "output",
            "answer",
            "expected",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _attr_or_key(obj: Any, name: str) -> str:
    """Read ``name`` from a dspy.Example (attr) or a plain dict (key)."""
    if isinstance(obj, dict):
        value = obj.get(name, "")
    else:
        value = getattr(obj, name, "")
        if value == "" and hasattr(obj, "get"):
            try:
                value = obj.get(name, "")
            except Exception:  # pragma: no cover - defensive
                value = ""
    return value if isinstance(value, str) else (json.dumps(value) if value else "")


def _normalize(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else str(value).strip().lower()
