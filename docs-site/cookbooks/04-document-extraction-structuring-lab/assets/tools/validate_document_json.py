"""validate_document_json — Caliber workflow `python_code` node body.

Paste this into the **Python Code** node `validate_document_json` (no tool
registration; the python_code sandbox runs it). It performs a minimal,
stdlib-only JSON-Schema-ish check of the structurer's output against the target
schema and reports whether the record is schema-valid.

CONTRACT (how the sandbox calls this)
  The node body runs inside:
      run_python_node(input=None, context=None, inputs=None, run_input='')
  Wire the upstream ports into this node's `inputs` so it receives:
      inputs["extracted_json"]  -> the doc-structurer node output (dict, or a
                                   JSON string; both are accepted)
      inputs["target_schema"]   -> the JSON Schema (schema/extracted-fields.schema.json),
                                   as a dict or a JSON string
  If `inputs` is not populated, it falls back to reading those keys off `input`
  / `context` so the node still works when the upstream emits a single dict.

OUTPUTS (returned on the node's `result` port)
  {
    "validation_status": "pass" | "partial" | "fail",
    "missing_fields":    [<required schema fields absent from extracted_json>],
    "errors":            [<human-readable type/shape problems>]
  }
    - "pass"    : every required field present AND all type checks pass.
    - "partial" : some required fields missing (or echoed via the structurer's
                  own `missing_fields`) BUT no type errors on what IS present.
                  This is the README's "missing_fields populated" edge case
                  (a.k.a. pass_with_warnings).
    - "fail"    : at least one type/shape error, or the payload is not a JSON
                  object at all (unparseable / wrong root type).
  The same dict is also returned on `text` (as JSON) so the node's text port and
  the downstream `contains_expected` scorer can read `validation_status` /
  `missing_fields` directly.
"""

import json

# Map JSON Schema "type" -> Python types for a basic isinstance check.
# bool is excluded from "number"/"integer" on purpose (bool is a subclass of int).
_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _coerce_obj(value):
    """Accept a dict directly or a JSON string; anything else -> None."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _type_ok(value, json_type):
    py = _JSON_TYPES.get(json_type)
    if py is None:
        return True  # unknown declared type -> don't fail the record on it
    if json_type in ("number", "integer") and isinstance(value, bool):
        return False  # booleans are not numbers here
    if json_type == "number":
        return isinstance(value, (int, float))
    return isinstance(value, py)


def _check_value(name, value, prop_schema, errors):
    """Type/shape-check one value against its property subschema (one level deep
    into array item objects, which is enough for the invoice line_items case)."""
    declared = prop_schema.get("type")
    if declared and not _type_ok(value, declared):
        errors.append(
            "field {!r}: expected type {!r}, got {!r}".format(
                name, declared, type(value).__name__
            )
        )
        return  # don't descend into a value of the wrong type

    if declared == "array":
        item_schema = prop_schema.get("items") or {}
        item_required = item_schema.get("required") or []
        item_props = item_schema.get("properties") or {}
        for idx, item in enumerate(value):
            if item_schema.get("type") and not _type_ok(item, item_schema["type"]):
                errors.append(
                    "field {!r}[{}]: expected item type {!r}, got {!r}".format(
                        name, idx, item_schema["type"], type(item).__name__
                    )
                )
                continue
            if isinstance(item, dict):
                for req in item_required:
                    if req not in item or item.get(req) is None:
                        errors.append(
                            "field {!r}[{}]: missing required item key {!r}".format(
                                name, idx, req
                            )
                        )
                for key, sub in item_props.items():
                    if key in item and item.get(key) is not None:
                        _check_value("{}[{}].{}".format(name, idx, key), item[key], sub, errors)


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    src = inputs if isinstance(inputs, dict) else {}

    def _pick(key):
        if key in src:
            return src[key]
        if isinstance(input, dict) and key in input:
            return input[key]
        if isinstance(context, dict) and key in context:
            return context[key]
        return None

    extracted = _coerce_obj(_pick("extracted_json"))
    schema = _coerce_obj(_pick("target_schema")) or {}

    errors = []
    missing_fields = []

    # Root must be a JSON object; otherwise the structurer broke the contract.
    if extracted is None:
        result = {
            "validation_status": "fail",
            "missing_fields": [],
            "errors": ["extracted_json is missing or is not a JSON object"],
        }
        return {"text": json.dumps(result), "result": result}

    required = schema.get("required") or []
    props = schema.get("properties") or {}

    # 1) Required-key presence (treat null as absent).
    for field in required:
        if field not in extracted or extracted.get(field) is None:
            missing_fields.append(field)

    # Honor the structurer's self-declared missing_fields (the prompt is told to
    # list required fields it could not ground). Union, preserving order.
    declared_missing = extracted.get("missing_fields")
    if isinstance(declared_missing, list):
        for field in declared_missing:
            if isinstance(field, str) and field not in missing_fields:
                missing_fields.append(field)

    # 2) Type/shape checks for fields that ARE present.
    for field, prop_schema in props.items():
        if field in extracted and extracted.get(field) is not None:
            _check_value(field, extracted[field], prop_schema, errors)

    if errors:
        status = "fail"
    elif missing_fields:
        status = "partial"
    else:
        status = "pass"

    result = {
        "validation_status": status,
        "missing_fields": missing_fields,
        "errors": errors,
    }
    return {"text": json.dumps(result), "result": result}
