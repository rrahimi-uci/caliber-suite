from caliber.prompt_template_library import (
    list_prompt_template_catalog,
    preview_prompt_template,
)


def test_list_prompt_template_catalog_exposes_expected_shapes() -> None:
    catalog = list_prompt_template_catalog()

    assert catalog["catalog_version"] == "2.0.0"
    assert {item["id"] for item in catalog["base_templates"]} >= {
        "zs-cot-trigger",
        "rag-grounded-qa",
        "react-tool-loop",
        "custom-prompt",
        "grounded-answer",
    }
    assert {item["id"] for item in catalog["modifiers"]} >= {
        "few-shot-examples",
        "rag-context",
        "json-output",
        "format-enforce",
        "markdown-output",
        "directional-hints",
        "self-critique",
        "safety-guardrails",
    }
    recipe_map = {item["id"]: item for item in catalog["starter_recipes"]}
    assert recipe_map["zs-cot-trigger"]["base_template_id"] == "zs-cot-trigger"
    assert recipe_map["zs-cot-trigger"]["support_level"] == "builder"
    assert recipe_map["zs-cot-trigger"]["title"] == "zs-cot-trigger"
    assert recipe_map["react-tool-loop"]["support_level"] == "builder"
    assert recipe_map["react-tool-loop"]["title"] == "react-tool-loop"
    assert recipe_map["check-hallucination"]["modifier_ids"] == []
    assert recipe_map["rag-grounded-qa"]["preview_variables"]["question"]


def test_preview_prompt_template_preserves_library_template_shape() -> None:
    preview = preview_prompt_template(
        base_template_id="zs-cot-trigger",
        preview_variables={"question": "How many releases ship in 8 weeks at 3 per week?"},
    )

    assert preview["validation_report"]["valid"] is True
    assert preview["validation_report"]["errors"] == []
    assert preview["compiled_template"] == "{{question}}\n\nLet's think step by step."
    assert (
        preview["rendered_preview"]
        == "How many releases ship in 8 weeks at 3 per week?\n\nLet's think step by step."
    )
    assert {item["name"] for item in preview["runtime_variables"]} == {"question"}


def test_preview_prompt_template_composes_base_modifiers_and_preview_variables() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        modifier_ids=["rag-context", "markdown-output"],
        builder_values={
            "task_description": "Answer support questions using policy evidence.",
        },
        preview_variables={"retrieved_docs": "Policy doc excerpt"},
    )

    assert preview["validation_report"]["valid"] is True
    assert preview["validation_report"]["errors"] == []
    assert preview["validation_report"]["warnings"] == []
    assert "Answer support questions using policy evidence." in preview["compiled_template"]
    assert "Retrieved context:\n{{retrieved_docs}}" in preview["compiled_template"]
    assert "Policy doc excerpt" in preview["rendered_preview"]
    assert preview["preview_variables_applied"] == {"retrieved_docs": "Policy doc excerpt"}
    assert preview["unresolved_variables"] == []
    assert [item["name"] for item in preview["runtime_variables"]] == ["retrieved_docs"]
    assert preview["recommended_scorers"] == ["Correctness", "RelevanceToQuery", "Guidelines"]


def test_preview_prompt_template_rejects_incompatible_modifiers_and_missing_builder_values() -> (
    None
):
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        modifier_ids=["json-output", "markdown-output"],
    )

    assert preview["validation_report"]["valid"] is False
    assert "Builder field 'task_description' is required." in preview["validation_report"]["errors"]
    assert "Builder field 'json_schema' is required." in preview["validation_report"]["errors"]
    assert any(
        "cannot be combined" in message for message in preview["validation_report"]["errors"]
    )


def test_preview_prompt_template_prefills_extraction_starter_values() -> None:
    preview = preview_prompt_template(base_template_id="extract-structured-data")

    assert preview["validation_report"]["valid"] is True
    assert preview["validation_report"]["errors"] == []
    values = {item["name"]: item["value"] for item in preview["builder_variables"]}
    assert "downstream workflow can ingest the record" in values["task_description"]
    assert '"invoice_number": "string | null"' in values["schema"]
    assert '"payment_status": "string | null"' in preview["compiled_template"]


def test_preview_prompt_template_supports_custom_prompt_template() -> None:
    preview = preview_prompt_template(
        base_template_id="custom-prompt",
        builder_values={
            "custom_prompt": (
                "You are a billing assistant.\n\n"
                "Use the supplied context when it is present.\n\n"
                "Customer request:\n"
                "{{user_input}}"
            )
        },
        preview_variables={"user_input": "Where is my refund?"},
    )

    assert preview["validation_report"]["valid"] is True
    assert preview["validation_report"]["errors"] == []
    assert "You are a billing assistant." in preview["compiled_template"]
    assert "Where is my refund?" in preview["rendered_preview"]
    assert {item["name"] for item in preview["runtime_variables"]} == {"user_input"}


def test_preview_prompt_template_accepts_override_with_custom_runtime_variables() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        builder_values={"task_description": "Answer questions from policy excerpts."},
        runtime_variables=["custom_context", "user_question"],
        preview_variables={
            "custom_context": "Only share the minimum necessary data.",
            "user_question": "Can I send this customer record to a vendor?",
        },
        template_override="Use {{custom_context}} to answer: {{user_question}}",
    )

    assert preview["validation_report"]["valid"] is True
    assert preview["validation_report"]["errors"] == []
    assert preview["validation_report"]["warnings"] == []
    assert preview["compiled_template"] == "Use {{custom_context}} to answer: {{user_question}}"
    assert (
        preview["rendered_preview"]
        == "Use Only share the minimum necessary data. to answer: Can I send this customer record to a vendor?"
    )
    assert {item["name"] for item in preview["runtime_variables"]} == {
        "custom_context",
        "user_question",
    }


def test_preview_prompt_template_warns_when_duplicate_modifier_is_selected() -> None:
    preview = preview_prompt_template(
        base_template_id="summarize-for-audience",
        modifier_ids=["rag-context", "rag-context"],
        builder_values={
            "task_description": "Summarize the incident updates for leadership.",
            "audience": "Operations executives",
        },
        preview_variables={"retrieved_docs": "Incident notes and latest mitigations."},
    )

    assert preview["validation_report"]["valid"] is True
    assert [item["id"] for item in preview["modifiers"]] == ["rag-context"]
    assert any(
        "selected more than once" in message for message in preview["validation_report"]["warnings"]
    )


def test_preview_prompt_template_returns_composed_sections() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        builder_values={"task_description": "Answer support questions."},
    )

    composed = preview["composed_sections"]
    # All five canonical elements are always present (empty string when unused).
    assert set(composed) == {
        "instruction",
        "context",
        "examples",
        "input",
        "output_indicator",
    }
    assert composed["instruction"] == (
        "You answer questions using the available instructions and evidence."
    )
    assert "Answering goal:" in composed["context"]
    assert preview["overridden_sections"] == []


def test_preview_prompt_template_applies_section_override() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        builder_values={"task_description": "Answer support questions."},
        section_overrides={"context": "Only use the attached contract."},
    )

    assert preview["validation_report"]["valid"] is True
    assert preview["overridden_sections"] == ["context"]
    assert "Only use the attached contract." in preview["compiled_template"]
    # The base context text is replaced, not appended.
    assert "Answering goal:" not in preview["compiled_template"]
    # composed_sections still reports the pre-override text so the editor can reset.
    assert "Answering goal:" in preview["composed_sections"]["context"]


def test_section_override_substitutes_builder_variables() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        builder_values={"task_description": "triage billing tickets"},
        section_overrides={"instruction": "Your job: {{task_description}}."},
    )

    assert preview["validation_report"]["valid"] is True
    assert "Your job: triage billing tickets." in preview["compiled_template"]
    assert preview["overridden_sections"] == ["instruction"]


def test_section_override_can_clear_an_element() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        builder_values={"task_description": "Answer support questions."},
        section_overrides={"input": ""},
    )

    assert preview["overridden_sections"] == ["input"]
    assert (
        "Answer the user's question directly" not in preview["compiled_template"]
    )
    # The composed snapshot keeps the original element so Reset can restore it.
    assert (
        "Answer the user's question directly"
        in preview["composed_sections"]["input"]
    )


def test_section_override_ignores_unknown_element_keys() -> None:
    preview = preview_prompt_template(
        base_template_id="grounded-answer",
        builder_values={"task_description": "Answer support questions."},
        section_overrides={"not_an_element": "ignored"},
    )

    assert preview["overridden_sections"] == []
    assert "ignored" not in preview["compiled_template"]


def test_builder_supported_starter_recipes_compile_without_validation_errors() -> None:
    catalog = list_prompt_template_catalog()

    for recipe in catalog["starter_recipes"]:
        if recipe["support_level"] != "builder":
            continue

        preview = preview_prompt_template(
            base_template_id=recipe["base_template_id"],
            modifier_ids=recipe["modifier_ids"],
            builder_values=recipe["builder_values"],
            preview_variables=recipe["preview_variables"],
            runtime_variables=recipe["runtime_variables"],
            template_override=recipe["template_override"],
        )

        assert preview["validation_report"]["valid"] is True, recipe["id"]
        assert preview["validation_report"]["errors"] == [], recipe["id"]
