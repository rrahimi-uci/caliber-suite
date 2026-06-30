"""Prompt-builder catalog plus compile/preview helpers.

This module powers the prompt-template builder surfaced in the Prompts page.
It exposes three layers:

1. Core builder templates for generic archetypes.
2. Imported library templates that preserve the user's original
   instruction/context/examples/input/output structure as first-class
   prompt templates.
3. Optional modifiers that can be fused onto either kind of template.
"""

from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

from caliber.assistant.models import ValidationReport
from caliber.assistant.validators import validate_prompt_draft

CATALOG_VERSION = "2.0.0"

_ELEMENT_ORDER = (
    "instruction",
    "context",
    "examples",
    "input",
    "output_indicator",
)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LIBRARY_OVERRIDE_ENV = "CALIBER_PROMPT_TEMPLATE_LIBRARY_FILE"
_LIBRARY_DATA_DIR = Path(__file__).resolve().parent / "data"
_DEFAULT_LIBRARY_PATH = _LIBRARY_DATA_DIR / "template_library.json"
_LEGACY_LIBRARY_PATH = _LIBRARY_DATA_DIR / "prompt_template_library.json"


_CORE_BASE_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "classify-input",
        "kind": "base",
        "source_kind": "core",
        "title": "Classify Input",
        "summary": "Route or label incoming content using a fixed label set.",
        "domain": "classification",
        "technique": "zero-shot",
        "recommended_modifiers": ["few-shot-examples", "json-output", "safety-guardrails"],
        "recommended_scorers": ["Correctness", "RelevanceToQuery", "Guidelines"],
        "sections": {
            "instruction": "You are a careful classification assistant. {{role_statement}}",
            "context": (
                "Classification task:\n"
                "{{task_description}}\n\n"
                "Allowed labels:\n"
                "{{labels}}\n\n"
                "Decision rules:\n"
                "{{decision_rules}}"
            ),
            "examples": None,
            "input": "Classify the incoming user content into exactly one allowed label.",
            "output_indicator": "Return {{classification_output_format}}.",
        },
        "variables": [
            {
                "name": "task_description",
                "label": "Task description",
                "description": "What the classifier should determine.",
                "required": True,
            },
            {
                "name": "labels",
                "label": "Allowed labels",
                "description": "One label per line or comma-separated.",
                "required": True,
            },
            {
                "name": "role_statement",
                "label": "Role statement",
                "description": "Optional high-level guidance for the classifier.",
                "required": False,
                "default": "Do not invent new labels or extra explanation unless asked.",
            },
            {
                "name": "decision_rules",
                "label": "Decision rules",
                "description": "Tie-break or fallback rules.",
                "required": False,
                "default": (
                    "- Pick the label that best matches the primary user intent.\n"
                    "- If the input is ambiguous, use the fallback behavior described in the task."
                ),
            },
            {
                "name": "classification_output_format",
                "label": "Output format",
                "description": "How the label should be returned.",
                "required": False,
                "default": "only the label",
            },
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "output_format": "enum",
        "sampling_policy": "eval=100%",
        "composable_with": [],
        "source_url": None,
        "owner": "caliber",
        "status": "active",
        "version": CATALOG_VERSION,
    },
    {
        "id": "extract-structured-data",
        "kind": "base",
        "source_kind": "core",
        "title": "Extract Structured Data",
        "summary": "Pull named fields or schema-aligned values from source content.",
        "domain": "information-extraction",
        "technique": "zero-shot",
        "recommended_modifiers": ["json-output", "few-shot-examples", "safety-guardrails"],
        "recommended_scorers": ["Correctness", "Guidelines"],
        "sections": {
            "instruction": "You extract structured information from incoming content.",
            "context": (
                "Extraction goal:\n"
                "{{task_description}}\n\n"
                "Target schema:\n"
                "{{schema}}\n\n"
                "Missing-data policy:\n"
                "{{missing_data_policy}}"
            ),
            "examples": None,
            "input": "Populate every field you can justify from the source content.",
            "output_indicator": "Return {{extraction_output_format}}.",
        },
        "variables": [
            {
                "name": "task_description",
                "label": "Extraction goal",
                "description": "What to extract and why.",
                "required": True,
                "default": (
                    "Extract the key customer, invoice, and payment fields so a downstream "
                    "workflow can ingest the record without manual cleanup."
                ),
            },
            {
                "name": "schema",
                "label": "Target schema",
                "description": "Field list or JSON schema.",
                "required": True,
                "default": (
                    "{\n"
                    '  "customer_name": "string | null",\n'
                    '  "invoice_number": "string | null",\n'
                    '  "due_date": "string | null",\n'
                    '  "amount_due": "number | null",\n'
                    '  "payment_status": "string | null"\n'
                    "}"
                ),
            },
            {
                "name": "missing_data_policy",
                "label": "Missing-data policy",
                "description": "How to handle absent fields.",
                "required": False,
                "default": "Use null for missing fields and never guess.",
            },
            {
                "name": "extraction_output_format",
                "label": "Output format",
                "description": "How the extracted fields should be returned.",
                "required": False,
                "default": "only valid JSON matching the target schema",
            },
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "output_format": "json",
        "sampling_policy": "eval=100%",
        "composable_with": [],
        "source_url": None,
        "owner": "caliber",
        "status": "active",
        "version": CATALOG_VERSION,
    },
    {
        "id": "summarize-for-audience",
        "kind": "base",
        "source_kind": "core",
        "title": "Summarize For Audience",
        "summary": "Condense content for a specific audience, tone, and length target.",
        "domain": "summarization",
        "technique": "zero-shot",
        "recommended_modifiers": ["directional-hints", "markdown-output", "rag-context"],
        "recommended_scorers": ["RelevanceToQuery", "Guidelines"],
        "sections": {
            "instruction": "You create concise summaries for the intended audience.",
            "context": (
                "Summarization goal:\n"
                "{{task_description}}\n\n"
                "Audience:\n"
                "{{audience}}\n\n"
                "Length target:\n"
                "{{length_target}}\n\n"
                "Tone:\n"
                "{{tone}}"
            ),
            "examples": None,
            "input": "Preserve the most important facts, decisions, and action items.",
            "output_indicator": "Return a summary that stays within the requested length and tone.",
        },
        "variables": [
            {
                "name": "task_description",
                "label": "Summarization goal",
                "description": "What the summary should optimize for.",
                "required": True,
            },
            {
                "name": "audience",
                "label": "Audience",
                "description": "Who the summary is for.",
                "required": True,
            },
            {
                "name": "length_target",
                "label": "Length target",
                "description": "Sentence or bullet budget.",
                "required": False,
                "default": "4 to 6 concise bullets",
            },
            {
                "name": "tone",
                "label": "Tone",
                "description": "Writing style for the summary.",
                "required": False,
                "default": "clear, executive, and direct",
            },
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "output_format": "markdown",
        "sampling_policy": "runtime=sampled",
        "composable_with": [],
        "source_url": None,
        "owner": "caliber",
        "status": "active",
        "version": CATALOG_VERSION,
    },
    {
        "id": "grounded-answer",
        "kind": "base",
        "source_kind": "core",
        "title": "Grounded Answer",
        "summary": "Answer directly while staying constrained by available evidence.",
        "domain": "question-answering",
        "technique": "zero-shot",
        "recommended_modifiers": ["rag-context", "markdown-output", "safety-guardrails"],
        "recommended_scorers": ["Correctness", "RelevanceToQuery", "Guidelines"],
        "sections": {
            "instruction": "You answer questions using the available instructions and evidence.",
            "context": (
                "Answering goal:\n"
                "{{task_description}}\n\n"
                "Missing-answer policy:\n"
                "{{missing_answer_policy}}"
            ),
            "examples": None,
            "input": "Answer the user's question directly and stay grounded in the supplied evidence.",
            "output_indicator": "Use {{answer_style}}.",
        },
        "variables": [
            {
                "name": "task_description",
                "label": "Answering goal",
                "description": "What a good answer should optimize for.",
                "required": True,
            },
            {
                "name": "missing_answer_policy",
                "label": "Missing-answer policy",
                "description": "What to do when the available context is insufficient.",
                "required": False,
                "default": (
                    "If the answer is not supported by the available evidence, say that directly "
                    "and ask for the missing source."
                ),
            },
            {
                "name": "answer_style",
                "label": "Answer style",
                "description": "Preferred shape of the response.",
                "required": False,
                "default": "concise prose with clear supporting rationale",
            },
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "output_format": "prose",
        "sampling_policy": "runtime=sampled",
        "composable_with": [],
        "source_url": None,
        "owner": "caliber",
        "status": "active",
        "version": CATALOG_VERSION,
    },
    {
        "id": "custom-prompt",
        "kind": "base",
        "source_kind": "system",
        "title": "Custom Prompt",
        "summary": (
            "Start from a freeform prompt and layer optional retrieval, output, or safety "
            "modifiers."
        ),
        "domain": "freeform",
        "technique": "manual",
        "recommended_modifiers": [
            "rag-context",
            "few-shot-examples",
            "markdown-output",
            "json-output",
            "format-enforce",
            "self-critique",
            "safety-guardrails",
        ],
        "recommended_scorers": ["Correctness", "RelevanceToQuery", "Guidelines"],
        "sections": {
            "instruction": "{{custom_prompt}}",
            "context": None,
            "examples": None,
            "input": None,
            "output_indicator": None,
        },
        "variables": [
            {
                "name": "custom_prompt",
                "label": "Custom prompt",
                "description": (
                    "Write the full prompt text. You can include runtime placeholders such as "
                    "{{user_input}} or {{retrieved_docs}}."
                ),
                "required": True,
                "default": (
                    "You are a careful assistant.\n\n"
                    "Use any supplied context when it is available.\n"
                    "If the answer is not supported, say what is missing instead of guessing.\n\n"
                    "User request:\n"
                    "{{user_input}}"
                ),
            }
        ],
        "runtime_variables": [
            {
                "name": "user_input",
                "label": "User input",
                "description": "Primary user request supplied at runtime.",
            }
        ],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "output_format": "variable",
        "sampling_policy": "runtime=sampled",
        "composable_with": [],
        "source_url": None,
        "owner": "caliber",
        "status": "active",
        "version": CATALOG_VERSION,
    },
    {
        "id": "code-generator",
        "kind": "base",
        "source_kind": "core",
        "title": "Code Generator",
        "summary": "Produce implementation-ready code with explicit constraints and tests.",
        "domain": "coding",
        "technique": "zero-shot",
        "recommended_modifiers": ["markdown-output", "safety-guardrails"],
        "recommended_scorers": ["Correctness", "Guidelines"],
        "sections": {
            "instruction": "You write production-minded code that favors clarity and correctness.",
            "context": (
                "Programming language:\n"
                "{{language}}\n\n"
                "Task:\n"
                "{{task_description}}\n\n"
                "Constraints:\n"
                "{{constraints}}\n\n"
                "Testing expectations:\n"
                "{{testing_expectations}}"
            ),
            "examples": None,
            "input": "Produce the requested code and handle the described edge cases.",
            "output_indicator": "Return {{code_output_format}}.",
        },
        "variables": [
            {
                "name": "language",
                "label": "Programming language",
                "description": "Target language or framework.",
                "required": True,
            },
            {
                "name": "task_description",
                "label": "Task description",
                "description": "What the code must do.",
                "required": True,
            },
            {
                "name": "constraints",
                "label": "Constraints",
                "description": "Libraries, performance, or environment constraints.",
                "required": False,
                "default": "Favor readability, small helpers, and explicit edge-case handling.",
            },
            {
                "name": "testing_expectations",
                "label": "Testing expectations",
                "description": "Any required test coverage or validation.",
                "required": False,
                "default": "Include the key edge cases the implementation should handle.",
            },
            {
                "name": "code_output_format",
                "label": "Output format",
                "description": "How code should be returned.",
                "required": False,
                "default": (
                    "only the code in a single fenced block with brief inline comments when helpful"
                ),
            },
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "output_format": "code",
        "sampling_policy": "runtime=sampled",
        "composable_with": [],
        "source_url": None,
        "owner": "caliber",
        "status": "active",
        "version": CATALOG_VERSION,
    },
)


_MODIFIERS: tuple[dict[str, Any], ...] = (
    {
        "id": "few-shot-examples",
        "kind": "modifier",
        "title": "Few-Shot Examples",
        "summary": "Append concrete examples that demonstrate the desired behavior.",
        "domain": "evaluation",
        "technique": "few-shot",
        "recommended_modifiers": [],
        "recommended_scorers": ["Correctness"],
        "variables": [
            {
                "name": "few_shot_examples",
                "label": "Few-shot examples",
                "description": "Examples in a readable input/output format.",
                "required": True,
            }
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "operations": [
            {
                "element": "examples",
                "mode": "append",
                "content": "Examples:\n{{few_shot_examples}}",
            }
        ],
    },
    {
        "id": "rag-context",
        "kind": "modifier",
        "title": "RAG Context",
        "summary": "Ground answers or summaries in retrieved source material.",
        "domain": "question-answering",
        "technique": "rag",
        "recommended_modifiers": [],
        "recommended_scorers": ["Correctness", "Guidelines"],
        "variables": [],
        "runtime_variables": [
            {
                "name": "retrieved_docs",
                "label": "Retrieved docs",
                "description": "Runtime-provided source material.",
            }
        ],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "operations": [
            {
                "element": "context",
                "mode": "append",
                "content": (
                    "When retrieved source material is supplied, treat it as the primary evidence.\n\n"
                    "Retrieved context:\n{{retrieved_docs}}"
                ),
            }
        ],
    },
    {
        "id": "json-output",
        "kind": "modifier",
        "title": "JSON Output",
        "summary": "Force the response into a strict JSON contract.",
        "domain": "evaluation",
        "technique": "zero-shot",
        "recommended_modifiers": [],
        "recommended_scorers": ["Correctness"],
        "variables": [
            {
                "name": "json_schema",
                "label": "JSON schema or shape",
                "description": "The structure the output must satisfy.",
                "required": True,
            }
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": ["markdown-output"],
        "operations": [
            {
                "element": "output_indicator",
                "mode": "append",
                "content": (
                    "Return only valid JSON matching this schema:\n{{json_schema}}\n"
                    "No markdown fences."
                ),
            }
        ],
    },
    {
        "id": "format-enforce",
        "kind": "modifier",
        "title": "Format Enforce",
        "summary": "Apply an arbitrary output contract beyond the built-in JSON or markdown helpers.",
        "domain": "evaluation",
        "technique": "zero-shot",
        "recommended_modifiers": [],
        "recommended_scorers": ["Correctness", "Guidelines"],
        "variables": [
            {
                "name": "required_format",
                "label": "Required format",
                "description": "The exact output contract the response must satisfy.",
                "required": True,
            }
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": ["json-output", "markdown-output"],
        "operations": [
            {
                "element": "output_indicator",
                "mode": "append",
                "content": (
                    "Respond ONLY in the required format. No preamble, no explanation, "
                    "and no markdown fences unless the required format explicitly asks for them.\n"
                    "Required format:\n{{required_format}}"
                ),
            }
        ],
    },
    {
        "id": "self-critique",
        "kind": "modifier",
        "title": "Self-Critique",
        "summary": "Add an inline self-review pass before the model returns its final answer.",
        "domain": "evaluation",
        "technique": "reflexion",
        "recommended_modifiers": [],
        "recommended_scorers": ["Correctness", "Guidelines"],
        "variables": [
            {
                "name": "critique_focus",
                "label": "Critique focus",
                "description": "What the self-review pass should check before finalizing.",
                "required": False,
                "default": "errors, unsupported claims, and gaps against the user's task",
            }
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "operations": [
            {
                "element": "output_indicator",
                "mode": "append",
                "content": (
                    "Before finalizing, review your draft for {{critique_focus}}. "
                    "Revise silently, then return only the corrected final answer."
                ),
            }
        ],
    },
    {
        "id": "markdown-output",
        "kind": "modifier",
        "title": "Markdown Output",
        "summary": "Force the response into named markdown sections.",
        "domain": "evaluation",
        "technique": "zero-shot",
        "recommended_modifiers": [],
        "recommended_scorers": ["Guidelines"],
        "variables": [
            {
                "name": "markdown_sections",
                "label": "Markdown sections",
                "description": "Named sections or headings to emit.",
                "required": False,
                "default": "Summary, Key Evidence, Final Answer",
            }
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": ["json-output"],
        "operations": [
            {
                "element": "output_indicator",
                "mode": "append",
                "content": "Use markdown with these sections:\n{{markdown_sections}}",
            }
        ],
    },
    {
        "id": "directional-hints",
        "kind": "modifier",
        "title": "Directional Hints",
        "summary": "Bias the response toward specific themes, risks, or priorities.",
        "domain": "summarization",
        "technique": "dsp",
        "recommended_modifiers": [],
        "recommended_scorers": ["RelevanceToQuery"],
        "variables": [
            {
                "name": "hints",
                "label": "Directional hints",
                "description": "Keywords, angles, or priorities to emphasize.",
                "required": True,
            }
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "operations": [
            {
                "element": "context",
                "mode": "append",
                "content": "Emphasize these priorities or angles:\n{{hints}}",
            }
        ],
    },
    {
        "id": "safety-guardrails",
        "kind": "modifier",
        "title": "Safety Guardrails",
        "summary": "Add explicit refusal, escalation, or safe-completion constraints.",
        "domain": "safety",
        "technique": "zero-shot",
        "recommended_modifiers": [],
        "recommended_scorers": ["Guidelines", "Safety"],
        "variables": [
            {
                "name": "safety_policies",
                "label": "Safety policies",
                "description": "Guardrails or forbidden behavior.",
                "required": True,
            },
            {
                "name": "escalation_rule",
                "label": "Escalation rule",
                "description": "How the prompt should fail closed.",
                "required": False,
                "default": (
                    "If the request is out of policy or missing required facts, refuse and "
                    "explain what is needed."
                ),
            },
        ],
        "runtime_variables": [],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "operations": [
            {
                "element": "context",
                "mode": "append",
                "content": (
                    "Safety and policy guardrails:\n{{safety_policies}}\n\n"
                    "Escalation rule:\n{{escalation_rule}}"
                ),
            }
        ],
    },
)


def list_prompt_template_catalog() -> dict[str, Any]:
    """Return a UI-friendly prompt template catalog."""
    base_templates = [*get_library_templates(), *_CORE_BASE_TEMPLATES]
    starter_recipes = _build_starter_recipes(get_library_templates())
    return {
        "catalog_version": CATALOG_VERSION,
        "base_templates": [_serialize_template(item) for item in base_templates],
        "modifiers": [_serialize_template(item) for item in _MODIFIERS],
        "starter_recipes": [_serialize_starter_recipe(item) for item in starter_recipes],
    }


def preview_prompt_template(
    *,
    base_template_id: str,
    modifier_ids: list[str] | tuple[str, ...] | None = None,
    builder_values: dict[str, str] | None = None,
    preview_variables: dict[str, str] | None = None,
    runtime_variables: list[str] | tuple[str, ...] | None = None,
    template_override: str | None = None,
    section_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compile a single prompt from a base template plus inline modifiers.

    ``section_overrides`` lets the builder replace any of the five canonical
    prompt elements (instruction/context/examples/input/output_indicator)
    wholesale after the base + modifiers are composed but before variable
    substitution. This is what powers the element-level editor: editing one
    element overrides just that element instead of the whole prompt the way
    ``template_override`` does. Unknown element keys are ignored.
    """
    modifier_ids = list(modifier_ids or [])
    builder_values = {str(k): str(v) for k, v in (builder_values or {}).items()}
    preview_variables = {str(k): str(v) for k, v in (preview_variables or {}).items()}
    custom_runtime_variables = _normalize_runtime_variables(runtime_variables or [])
    section_overrides = {
        str(k): str(v) for k, v in (section_overrides or {}).items() if str(k) in _ELEMENT_ORDER
    }

    base = _lookup_base_template(base_template_id)
    if base is None:
        raise ValueError(f"unknown base template id: {base_template_id}")

    selected_modifiers: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    seen_modifiers: set[str] = set()
    for modifier_id in modifier_ids:
        modifier = _lookup_template(_MODIFIERS, modifier_id)
        if modifier is None:
            errors.append(f"Unknown modifier: {modifier_id}")
            continue
        if modifier_id in seen_modifiers:
            warnings.append(
                f"Modifier {modifier_id!r} was selected more than once; duplicates were ignored."
            )
            continue
        seen_modifiers.add(modifier_id)
        selected_modifiers.append(modifier)

    _validate_compatibility(base, selected_modifiers, errors)

    (
        generated_template,
        design_specs,
        runtime_specs,
        composed_sections,
        applied_overrides,
    ) = _compile_generated_template(
        base=base,
        modifiers=selected_modifiers,
        builder_values=builder_values,
        section_overrides=section_overrides,
    )

    template_text = generated_template
    if isinstance(template_override, str) and template_override.strip():
        template_text = template_override.strip()

    design_names = {spec["name"] for spec in design_specs}
    runtime_names = {spec["name"] for spec in runtime_specs}
    runtime_names.update(custom_runtime_variables)

    missing_required_design = [
        spec["name"]
        for spec in design_specs
        if spec.get("required") and spec["name"] not in builder_values and not spec.get("default")
    ]
    for name in missing_required_design:
        errors.append(f"Builder field {name!r} is required.")

    unresolved_design = sorted(
        name
        for name in _find_placeholders(template_text)
        if name in design_names and name not in runtime_names
    )
    for name in unresolved_design:
        if name not in missing_required_design:
            errors.append(f"Builder field {name!r} is still unresolved in the prompt template.")

    declared_variables = sorted(runtime_names | set(unresolved_design))
    base_report = validate_prompt_draft(
        {
            "template": template_text,
            "variables": declared_variables,
        }
    )
    errors.extend(base_report.errors)
    warnings.extend(base_report.warnings)

    rendered_preview, preview_applied = _render_preview(template_text, preview_variables)
    detected_variables = _find_placeholders(template_text)
    unresolved_variables = _find_placeholders(rendered_preview)
    validation_report = ValidationReport(
        valid=len(_dedupe_messages(errors)) == 0,
        errors=_dedupe_messages(errors),
        warnings=_dedupe_messages(warnings),
    )

    return {
        "catalog_version": CATALOG_VERSION,
        "base_template": _serialize_template(base),
        "modifiers": [_serialize_template(item) for item in selected_modifiers],
        "generated_template": generated_template,
        "compiled_template": template_text,
        "rendered_preview": rendered_preview,
        "composed_sections": composed_sections,
        "overridden_sections": applied_overrides,
        "builder_variables": _serialize_variables(design_specs, builder_values),
        "runtime_variables": _serialize_runtime_variables(runtime_specs, custom_runtime_variables),
        "detected_variables": detected_variables,
        "unresolved_variables": unresolved_variables,
        "preview_variables_applied": preview_applied,
        "validation_report": validation_report.model_dump(mode="json"),
        "word_count": len(rendered_preview.split()),
        "char_count": len(rendered_preview),
        "recommended_scorers": _merge_recommended_scorers(base, selected_modifiers),
    }


@lru_cache(maxsize=1)
def _load_raw_library() -> dict[str, Any]:
    override = os.getenv(_LIBRARY_OVERRIDE_ENV)
    if override:
        path = Path(override).expanduser()
    else:
        path = _DEFAULT_LIBRARY_PATH if _DEFAULT_LIBRARY_PATH.exists() else _LEGACY_LIBRARY_PATH
    with path.open("r", encoding="utf-8") as handle:
        data: dict[str, Any] = json.load(handle)
    return data


@lru_cache(maxsize=1)
def get_library_templates() -> tuple[dict[str, Any], ...]:
    raw = _load_raw_library()
    templates: list[dict[str, Any]] = []
    for item in raw.get("templates", []):
        if not isinstance(item, dict):
            continue
        converted = _build_library_template(item)
        if converted is not None:
            templates.append(converted)
    return tuple(templates)


def _build_library_template(item: dict[str, Any]) -> dict[str, Any] | None:
    template_id = str(item.get("id") or "").strip()
    if not template_id:
        return None
    variable_specs = item.get("variables", [])
    variable_names = [
        str(spec.get("name")).strip()
        for spec in variable_specs
        if isinstance(spec, dict)
        and _VARIABLE_NAME_RE.fullmatch(str(spec.get("name") or "").strip())
    ]
    sections = {
        "instruction": _normalize_library_section(item.get("instruction"), variable_names),
        "context": _normalize_library_section(item.get("context"), variable_names),
        "examples": _normalize_library_section(item.get("examples"), variable_names),
        "input": _normalize_library_section(item.get("input"), variable_names),
        "output_indicator": _normalize_library_section(
            item.get("output_indicator"), variable_names
        ),
    }
    composable_with = [
        str(value) for value in item.get("composable_with", []) if str(value).strip()
    ]
    return {
        "id": template_id,
        "kind": "base",
        "source_kind": "library",
        "title": template_id,
        "summary": _derive_library_summary(item),
        "domain": str(item.get("domain") or "general"),
        "technique": str(item.get("technique") or "unknown"),
        "recommended_modifiers": _exact_composable_modifier_ids(composable_with),
        "recommended_scorers": _recommended_scorers_for_domain(
            str(item.get("domain") or "general")
        ),
        "variables": [],
        "runtime_variables": [
            {
                "name": str(spec["name"]),
                "label": str(spec["name"]),
                "description": str(
                    spec.get("description") or "Runtime value supplied when the prompt runs."
                ),
                "required": bool(spec.get("required")),
            }
            for spec in variable_specs
            if isinstance(spec, dict) and str(spec.get("name") or "").strip()
        ],
        "compatible_base_ids": [],
        "incompatible_modifier_ids": [],
        "sections": sections,
        "output_format": str(item.get("output_format") or "variable"),
        "sampling_policy": str(item.get("sampling_policy") or "runtime=sampled"),
        "composable_with": composable_with,
        "source_url": item.get("source"),
        "owner": str(item.get("owner") or "platform"),
        "status": str(item.get("status") or "active"),
        "version": str(item.get("version") or raw_library_version()),
        "execution_note": None,
        "is_wrapper": bool(item.get("is_wrapper")),
    }


@lru_cache(maxsize=1)
def raw_library_version() -> str:
    raw = _load_raw_library()
    return str(raw.get("library_version") or "1.0.0")


def _build_starter_recipes(
    library_templates: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    recipes: list[dict[str, Any]] = []
    for template in library_templates:
        runtime_names = [spec["name"] for spec in template.get("runtime_variables", [])]
        recipes.append(
            {
                "id": template["id"],
                "title": template["title"],
                "summary": template["summary"],
                "domain": template["domain"],
                "technique": template["technique"],
                "support_level": "builder",
                "support_reason": "Imported directly from template_library.json.",
                "base_template_id": template["id"],
                "modifier_ids": [],
                "builder_values": {},
                "runtime_variables": runtime_names,
                "preview_variables": _build_preview_variables_for_template(template),
                "template_override": None,
                "suggested_modifier_ids": list(template.get("recommended_modifiers", [])),
                "source_label": "Prompting Guide",
                "source_url": template.get("source_url"),
                "composable_with": list(template.get("composable_with", [])),
                "execution_note": None,
            }
        )
    return tuple(recipes)


def _build_preview_variables_for_template(template: dict[str, Any]) -> dict[str, str]:
    preview: dict[str, str] = {}
    for spec in template.get("runtime_variables", []):
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        preview[name] = _preview_value_for_variable(name)
    return preview


def _preview_value_for_variable(name: str) -> str:
    samples = {
        "instruction": "Summarize the incident update for the on-call lead.",
        "input": (
            "The rollout is paused, one region was rolled back, and the next decision is whether "
            "to retry after patch validation."
        ),
        "question": "Why do teams use staging before production?",
        "problem": (
            "A service processes 48 requests in 6 minutes. At the same rate, how many requests "
            "will it process in 15 minutes?"
        ),
        "sample_index": "1",
        "n_samples": "5",
        "k": "3",
        "retrieved_docs": (
            "[doc_1] Refunds take 5-7 business days after approval.\n"
            "[doc_2] Only approved refunds can be expedited."
        ),
        "tools": "search_docs(query), fetch_ticket(id), calculator(expression)",
        "scratchpad": "Thought: I should inspect the refund policy first.",
        "task": "Write a SQL query that returns the five most recent failed jobs.",
        "prior_attempt": "SELECT * FROM jobs;",
        "feedback": "Does not filter failed jobs or limit to the five most recent rows.",
        "b": "3",
        "path": "1. Compare possible branches.\n2. Drop impossible paths.",
        "task_description": (
            "Create a support-triage prompt that classifies tickets and asks clarifying "
            "questions only when necessary."
        ),
        "hints": "timeline, customer impact, next decision",
        "schema": (
            "{\n"
            '  "invoice_number": "string | null",\n'
            '  "customer_name": "string | null",\n'
            '  "payment_status": "string | null"\n'
            "}"
        ),
        "text": (
            "Invoice INV-1042 for Dana Kim is still pending. The customer asked whether the "
            "payment posted after the June 14 transfer."
        ),
        "labels": "billing\ntechnical_issue\naccount_access",
        "spec": "Build a Python function that returns the five most recent failed jobs.",
        "constraints": "Python 3.12, SQLAlchemy available, prefer readability.",
        "concept": "Staging deployments",
        "audience": "new engineering manager",
        "n": "3",
        "context_docs": (
            "Refund approvals take 5-7 business days. Expedited refunds require approval first."
        ),
        "answer": "Refunds always arrive the same day they are requested.",
        "examples": (
            "Input: I was charged twice for the same invoice.\n"
            "Output: billing\n\n"
            "Input: My reset link expired and I still cannot access my account.\n"
            "Output: account_access"
        ),
        "format": '{"answer": string, "confidence": number}',
        "output": "Refunds are always instant and never require approval.",
        "step_instruction": "Extract the action items from the prior draft.",
        "prev_output": "The incident summary notes a rollback, a patch, and two follow-up owners.",
    }
    return samples.get(name, f"example_{name}")


def _exact_composable_modifier_ids(values: list[str]) -> list[str]:
    available_modifier_ids = {item["id"] for item in _MODIFIERS}
    return [value for value in values if value in available_modifier_ids]


def _recommended_scorers_for_domain(domain: str) -> list[str]:
    scorer_map = {
        "classification": ["Correctness", "RelevanceToQuery", "Guidelines"],
        "information-extraction": ["Correctness", "Guidelines"],
        "question-answering": ["Correctness", "RelevanceToQuery", "Guidelines"],
        "reasoning": ["Correctness", "Guidelines"],
        "coding": ["Correctness", "Guidelines"],
        "summarization": ["RelevanceToQuery", "Guidelines"],
        "truthfulness": ["Correctness", "Guidelines"],
        "evaluation": ["Correctness", "Guidelines"],
    }
    return list(scorer_map.get(domain, ["Correctness", "Guidelines"]))


def _normalize_library_section(value: Any, variable_names: list[str]) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(item).strip() for item in value if str(item).strip()]
        if not parts:
            return None
        text = "\n\n".join(parts)
    else:
        text = str(value).strip()
    if not text:
        return None
    for name in variable_names:
        text = text.replace(f"{{{name}}}", f"{{{{{name}}}}}")
    return text


def _derive_library_summary(item: dict[str, Any]) -> str:
    for key in ("instruction", "output_indicator", "input", "context"):
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            text = " ".join(str(part).strip() for part in value if str(part).strip())
        else:
            text = str(value).strip()
        if text:
            if re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", text):
                continue
            return re.sub(r"\s+", " ", text)
    return str(item.get("technique") or "template")


def _compile_generated_template(
    *,
    base: dict[str, Any],
    modifiers: list[dict[str, Any]],
    builder_values: dict[str, str],
    section_overrides: dict[str, str] | None = None,
) -> tuple[
    str,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, str],
    list[str],
]:
    section_overrides = section_overrides or {}
    sections: dict[str, str | None] = deepcopy(base["sections"])
    design_specs = _merge_variable_specs(base.get("variables", []), [])
    runtime_specs = _merge_variable_specs(base.get("runtime_variables", []), [])

    for modifier in modifiers:
        design_specs = _merge_variable_specs(design_specs, modifier.get("variables", []))
        runtime_specs = _merge_variable_specs(runtime_specs, modifier.get("runtime_variables", []))
        for operation in modifier.get("operations", []):
            element = str(operation["element"])
            mode = str(operation["mode"])
            content = str(operation["content"]).strip()
            current = (sections.get(element) or "").strip()
            if mode == "replace":
                sections[element] = content
            elif mode == "prepend":
                sections[element] = content if not current else f"{content}\n\n{current}"
            else:
                sections[element] = content if not current else f"{current}\n\n{content}"

    # Snapshot the composed (post-modifier, pre-override) elements so the
    # element editor can show what each element resolves to and "Reset" can
    # restore it. Placeholders are left intact; substitution happens below.
    composed_sections = {element: (sections.get(element) or "") for element in _ELEMENT_ORDER}

    # Apply per-element overrides wholesale. An override (even to an empty
    # string, which clears the element) counts as a deliberate edit.
    applied_overrides: list[str] = []
    for element in _ELEMENT_ORDER:
        if element in section_overrides:
            sections[element] = section_overrides[element]
            applied_overrides.append(element)

    defaults: dict[str, str] = {}
    for spec in design_specs:
        default = spec.get("default")
        if isinstance(default, str):
            defaults[spec["name"]] = default

    compiled_values = {**defaults, **builder_values}
    generated_parts: list[str] = []
    for element in _ELEMENT_ORDER:
        raw = sections.get(element)
        if not raw:
            continue
        generated_parts.append(_substitute_known_variables(str(raw), compiled_values))
    return (
        "\n\n".join(generated_parts).strip(),
        design_specs,
        runtime_specs,
        composed_sections,
        applied_overrides,
    )


def _validate_compatibility(
    base: dict[str, Any],
    modifiers: list[dict[str, Any]],
    errors: list[str],
) -> None:
    modifier_ids = {item["id"] for item in modifiers}
    for modifier in modifiers:
        compatible = set(modifier.get("compatible_base_ids", []))
        if compatible and base["id"] not in compatible:
            errors.append(
                f"Modifier {modifier['title']!r} is not supported for base template {base['title']!r}."
            )
        for blocked in modifier.get("incompatible_modifier_ids", []):
            if blocked in modifier_ids:
                errors.append(
                    f"Modifier {modifier['title']!r} cannot be combined with {blocked!r}."
                )


def _lookup_base_template(template_id: str) -> dict[str, Any] | None:
    return _lookup_template((*get_library_templates(), *_CORE_BASE_TEMPLATES), template_id)


def _lookup_template(items: tuple[dict[str, Any], ...], template_id: str) -> dict[str, Any] | None:
    for item in items:
        if item["id"] == template_id:
            return item
    return None


def _serialize_template(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "kind": item["kind"],
        "source_kind": item.get("source_kind", "core"),
        "title": item["title"],
        "summary": item["summary"],
        "domain": item["domain"],
        "technique": item["technique"],
        "recommended_modifiers": list(item.get("recommended_modifiers", [])),
        "recommended_scorers": list(item.get("recommended_scorers", [])),
        "variables": deepcopy(item.get("variables", [])),
        "runtime_variables": deepcopy(item.get("runtime_variables", [])),
        "compatible_base_ids": list(item.get("compatible_base_ids", [])),
        "incompatible_modifier_ids": list(item.get("incompatible_modifier_ids", [])),
        "sections": deepcopy(item.get("sections", {})),
        "output_format": item.get("output_format"),
        "sampling_policy": item.get("sampling_policy"),
        "composable_with": list(item.get("composable_with", [])),
        "source_url": item.get("source_url"),
        "owner": item.get("owner"),
        "status": item.get("status"),
        "version": item.get("version"),
        "execution_note": item.get("execution_note"),
        "is_wrapper": bool(item.get("is_wrapper")),
    }


def _serialize_starter_recipe(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item["id"],
        "title": item["title"],
        "summary": item["summary"],
        "domain": item["domain"],
        "technique": item["technique"],
        "support_level": item["support_level"],
        "support_reason": item["support_reason"],
        "base_template_id": item.get("base_template_id"),
        "modifier_ids": list(item.get("modifier_ids", [])),
        "builder_values": deepcopy(item.get("builder_values", {})),
        "runtime_variables": list(item.get("runtime_variables", [])),
        "preview_variables": deepcopy(item.get("preview_variables", {})),
        "template_override": item.get("template_override"),
        "suggested_modifier_ids": list(item.get("suggested_modifier_ids", [])),
        "source_label": item["source_label"],
        "source_url": item["source_url"],
        "composable_with": list(item.get("composable_with", [])),
        "execution_note": item.get("execution_note"),
    }


def _serialize_variables(
    specs: list[dict[str, Any]], values: dict[str, str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in specs:
        item = deepcopy(spec)
        item["value"] = values.get(spec["name"], str(spec.get("default") or ""))
        out.append(item)
    return out


def _serialize_runtime_variables(
    specs: list[dict[str, Any]], custom_names: set[str]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    known = {spec["name"] for spec in specs}
    for spec in specs:
        out.append(deepcopy(spec))
    for name in sorted(custom_names - known):
        out.append(
            {
                "name": name,
                "label": _humanize_identifier(name),
                "description": "Custom runtime placeholder declared in the builder.",
            }
        )
    return out


def _merge_variable_specs(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [deepcopy(item) for item in left]
    seen = {item["name"] for item in out}
    for item in right:
        name = item["name"]
        if name in seen:
            continue
        out.append(deepcopy(item))
        seen.add(name)
    return out


def _substitute_known_variables(template: str, values: dict[str, str]) -> str:
    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template)


def _render_preview(template: str, preview_variables: dict[str, str]) -> tuple[str, dict[str, str]]:
    applied: dict[str, str] = {}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in preview_variables:
            applied[name] = preview_variables[name]
            return preview_variables[name]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_sub, template), applied


def _find_placeholders(template: str) -> list[str]:
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def _merge_recommended_scorers(base: dict[str, Any], modifiers: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for name in list(base.get("recommended_scorers", [])) + [
        scorer for modifier in modifiers for scorer in modifier.get("recommended_scorers", [])
    ]:
        if name not in out:
            out.append(name)
    return out


def _normalize_runtime_variables(values: list[str] | tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for value in values:
        name = str(value).strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            out.add(name)
    return out


def _dedupe_messages(messages: list[str]) -> list[str]:
    out: list[str] = []
    for message in messages:
        clean = message.strip()
        if clean and clean not in out:
            out.append(clean)
    return out


def _humanize_identifier(value: str) -> str:
    parts = [part for part in value.replace("_", " ").replace("-", " ").split() if part]
    return " ".join(part.capitalize() for part in parts) if parts else value
