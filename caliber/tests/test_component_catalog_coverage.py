"""Targeted coverage for workflow component-catalog helper branches.

These exercise the scattered edge/error/empty branches in the schema- and
port-normalization helpers near the end of ``component_catalog`` that the
public ``build_workflow_component_catalog`` path does not naturally reach.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from caliber.workflows import component_catalog as cc
from caliber.workflows.component_catalog import build_workflow_component_catalog
from caliber.workflows.manifest import PortSpec, ToolNode

# --- _component_docs --------------------------------------------------------


def test_component_docs_known_node_uses_fallback_description() -> None:
    """Empty (whitespace-only) docstring -> _FALLBACK_DESCRIPTIONS for a known type."""

    class WhitespaceDoc(BaseModel):
        """ """

    description, docs = cc._component_docs(node_type="note", model_cls=WhitespaceDoc)
    assert description == cc._FALLBACK_DESCRIPTIONS["note"]
    assert docs == list(cc._COMPONENT_DOCS.get("note", ()))


def test_component_docs_unknown_node_humanizes_type() -> None:
    """No fallback entry -> humanized node type with a trailing period."""

    class WhitespaceDoc(BaseModel):
        """ """

    description, _ = cc._component_docs(node_type="my_node_xyz", model_cls=WhitespaceDoc)
    assert description == "My Node Xyz."


def test_component_docs_ignores_inherited_docstring() -> None:
    """A model with no docstring of its own must publish the fallback, not its base's."""

    class DocumentedBase(BaseModel):
        """Inherited prose that must never reach the designer."""

    class Undocumented(DocumentedBase):
        x: int = 1

    # The trap this guards: the class dict has no docstring, but ``inspect.getdoc``
    # walks the MRO and happily returns the base's.
    assert Undocumented.__dict__.get("__doc__") is None
    assert inspect.getdoc(Undocumented) == "Inherited prose that must never reach the designer."

    description, docs = cc._component_docs(node_type="note", model_cls=Undocumented)
    assert description == cc._FALLBACK_DESCRIPTIONS["note"]
    assert docs == list(cc._COMPONENT_DOCS["note"])


def test_component_docs_uses_own_summary_line_only() -> None:
    """A model's own docstring supplies the description; its body is not a tip source.

    The body is written for someone reading the module, so appending it line by line
    published mid-sentence fragments and reST markup as designer copy. Tips come from
    the curated table alone.
    """

    class OwnDoc(BaseModel):
        """Summary line.

        Body detail mentioning ``literals`` and :class:`SomeNode` that designers
        should never be shown.
        """

    description, docs = cc._component_docs(node_type="note", model_cls=OwnDoc)
    assert description == "Summary line."
    assert docs == list(cc._COMPONENT_DOCS["note"])


def test_catalog_docstring_free_nodes_use_curated_copy() -> None:
    """Every node model lacking its own docstring falls back to curated designer copy."""

    undocumented = [
        node_type
        for node_type, model_cls in cc._COMPONENT_ORDER
        if model_cls.__dict__.get("__doc__") is None
    ]
    assert undocumented, "expected at least one node model without its own docstring"

    catalog = {item["type"]: item for item in build_workflow_component_catalog()["components"]}
    for node_type in undocumented:
        component = catalog[node_type]
        # Assert the curated entry exists before reading it, so a node shipped with
        # neither a docstring nor curated copy names itself instead of raising KeyError.
        fallback = cc._FALLBACK_DESCRIPTIONS.get(node_type)
        assert fallback is not None, (
            f"{node_type} has no docstring of its own and no _FALLBACK_DESCRIPTIONS entry, "
            f"so it would publish the bare humanized type name"
        )
        assert component["description"] == fallback
        assert component["docs"] == list(cc._COMPONENT_DOCS.get(node_type, ()))


def test_catalog_never_publishes_source_markup() -> None:
    """Designer copy must never carry text written for someone reading the source.

    Two distinct leaks produced this: ``inspect.getdoc`` walking the MRO into
    pydantic's ``BaseModel`` docstring, and the docstring body being appended one
    tip per physical source line. Both surfaced reST roles, literals, and internal
    references in the component palette.
    """

    markers = (
        # pydantic's BaseModel docstring
        "Usage Documentation",
        "A base class for creating Pydantic models",
        "__pydantic",
        "__class_vars__",
        "__fields_set__",
        "__private_attributes__",
        "../concepts/models.md",
        # reST / Sphinx markup and internal references from CALIBER docstrings
        "``",
        ":class:",
        ":mod:",
        ":func:",
        ":meth:",
        ":attr:",
        "(plan:",
    )

    for component in build_workflow_component_catalog()["components"]:
        for text in [component["description"], *component["docs"]]:
            for marker in markers:
                assert marker not in text, f"{component['type']} leaks {marker!r}: {text!r}"


def test_catalog_copy_reads_as_whole_sentences() -> None:
    """No description or tip may be a fragment chopped at a source-line boundary.

    The docstring body was published one tip per physical line, so a wrapped
    sentence arrived as several tips each ending mid-clause. Requiring terminal
    punctuation catches a regression to that shape.
    """

    for component in build_workflow_component_catalog()["components"]:
        for text in [component["description"], *component["docs"]]:
            assert text.rstrip().endswith((".", "!", "?")), (
                f"{component['type']} publishes a sentence fragment: {text!r}"
            )


# --- _default_ports ---------------------------------------------------------


def test_default_ports_prefers_starter_node_ports() -> None:
    """A starter-node dict with ports for the field short-circuits to those ports."""

    ports = cc._default_ports(node_type="start", model_cls=ToolNode, field_name="outputs")
    assert "user_message" in ports


def test_default_ports_falls_back_to_model_field_default() -> None:
    """No starter ports -> derive ports from the model field default."""

    ports = cc._default_ports(node_type="zzz_unknown", model_cls=ToolNode, field_name="inputs")
    assert "input" in ports


def test_default_ports_missing_field_returns_empty() -> None:
    """A model lacking the requested port field yields an empty map."""

    class NoPorts(BaseModel):
        x: int = 1

    assert cc._default_ports(node_type="zzz_unknown", model_cls=NoPorts, field_name="inputs") == {}


# --- _normalize_port_map ----------------------------------------------------


def test_normalize_port_map_accepts_portspec_instance() -> None:
    spec = PortSpec(type="string", description="d")
    result = cc._normalize_port_map({"out": spec})
    assert result == {"out": {"type": "string", "description": "d", "schema": None}}


def test_normalize_port_map_skips_non_dict_spec() -> None:
    assert cc._normalize_port_map({"a": 5}) == {}


def test_normalize_port_map_skips_invalid_dict_spec() -> None:
    assert cc._normalize_port_map({"a": {"type": "NOT_A_REAL_TYPE"}}) == {}


def test_normalize_port_map_rejects_non_mapping_value() -> None:
    assert cc._normalize_port_map("not a dict") == {}


# --- _component_fields ------------------------------------------------------


def test_component_fields_skips_non_dict_property_and_normalizes_sentinel_default() -> None:
    """Cover the non-dict-property continue and the PydanticUndefined default reset."""

    class Crafted(BaseModel):
        good: int = 5
        weird: str = "z"
        nodefault: int = 7

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "properties": {
                    "good": {"type": "integer", "default": 5},
                    "weird": "NOT_A_DICT",
                    "nodefault": {"type": "integer", "default": PydanticUndefined},
                },
                "required": ["good"],
                "$defs": {},
            }

    fields = cc._component_fields(node_type="tool", model_cls=Crafted)
    keys = {field["key"]: field for field in fields}
    assert "weird" not in keys
    assert keys["nodefault"]["default"] is None
    assert keys["good"]["required"] is True


# --- _resolve_schema --------------------------------------------------------


def test_resolve_schema_follows_ref_to_definition() -> None:
    defs = {"Foo": {"type": "object", "title": "Foo"}}
    resolved, nullable = cc._resolve_schema({"$ref": "#/$defs/Foo"}, defs)
    assert resolved == {"type": "object", "title": "Foo"}
    assert nullable is False


def test_resolve_schema_missing_ref_target_is_unchanged() -> None:
    resolved, nullable = cc._resolve_schema({"$ref": "#/$defs/Bar"}, {})
    assert resolved == {"$ref": "#/$defs/Bar"}
    assert nullable is False


def test_resolve_schema_skips_non_dict_union_option() -> None:
    resolved, nullable = cc._resolve_schema(
        {"anyOf": ["not-a-dict", {"type": "string"}, {"type": "null"}]}, {}
    )
    assert resolved == {"type": "string"}
    assert nullable is True


def test_resolve_schema_unwraps_single_allof() -> None:
    resolved, _ = cc._resolve_schema({"allOf": [{"type": "integer"}]}, {})
    assert resolved == {"type": "integer"}


def test_resolve_schema_allof_non_dict_option_is_unchanged() -> None:
    schema = {"allOf": ["not-a-dict"]}
    resolved, _ = cc._resolve_schema(schema, {})
    assert resolved == schema


# --- _schema_type_label -----------------------------------------------------


def test_schema_type_label_list_type_with_null() -> None:
    assert cc._schema_type_label({"type": ["string", "null"]}, {}) == "string"


def test_schema_type_label_list_type_only_null_is_object() -> None:
    assert cc._schema_type_label({"type": ["null"]}, {}) == "object"


def test_schema_type_label_object_with_title_is_humanized() -> None:
    assert cc._schema_type_label({"type": "object", "title": "MyThing"}, {}) == "My Thing"


def test_schema_type_label_resolved_title_without_type() -> None:
    assert cc._schema_type_label({"title": "SomeTitle"}, {}) == "Some Title"


def test_schema_type_label_uses_original_schema_title() -> None:
    """allOf unwraps to a title-less schema; the wire title supplies the label."""

    assert cc._schema_type_label({"allOf": [{}], "title": "WireTitle"}, {}) == "Wire Title"


def test_schema_type_label_object_fallback() -> None:
    assert cc._schema_type_label({}, {}) == "object"


# --- _union_type_labels -----------------------------------------------------


def test_union_type_labels_skips_non_dict_options() -> None:
    labels, nullable = cc._union_type_labels(
        {"anyOf": ["x", {"type": "string"}, {"type": "null"}]}, {}
    )
    assert labels == ["string"]
    assert nullable is True


def test_union_type_labels_all_null_returns_empty() -> None:
    labels, nullable = cc._union_type_labels({"anyOf": [{"type": "null"}]}, {})
    assert labels == []
    assert nullable is True


# --- _normalize_json_value --------------------------------------------------


def test_normalize_json_value_dumps_base_model() -> None:
    assert cc._normalize_json_value(PortSpec(type="void")) == {
        "type": "void",
        "description": "",
        "schema": None,
    }


def test_normalize_json_value_converts_tuple_to_list() -> None:
    assert cc._normalize_json_value((1, 2)) == [1, 2]


def test_normalize_json_value_sentinels_become_none() -> None:
    assert cc._normalize_json_value(Ellipsis) is None
    assert cc._normalize_json_value(inspect._empty) is None


# --- _humanize --------------------------------------------------------------


def test_humanize_skips_empty_parts() -> None:
    assert cc._humanize("__id__") == "ID"


def test_humanize_preserves_known_abbreviations() -> None:
    assert cc._humanize("mcp_server_id") == "MCP Server ID"
