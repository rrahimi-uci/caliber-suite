"""Infer request/response contracts from CALIBER route handlers.

The management OpenAPI document is generated from the live route table, but the
route table alone only tells us *that* an operation exists. This module recovers
the rest of the contract from the handler source itself:

* request bodies from existing Pydantic request models, multipart helpers, or
  the keys a manual JSON parser reads;
* success responses from the envelope helpers, direct ``JSONResponse`` payloads,
  text/binary responses, and explicit 204s;
* component schemas from the actual Pydantic models already used by handlers.

The goal is complete, honest coverage. Where a handler remains deliberately
dynamic, the document still names the content type and emits a permissive object
schema instead of pretending the route has no body at all.
"""

from __future__ import annotations

# This route-table inference is a bounded AST interpreter; its branches map
# directly to supported Starlette/Pydantic response patterns.
# ruff: noqa: PLR0911, PLR0912, PLR0915, PLR2004, SIM102, RET504, ARG001
import ast
import inspect
import json
import textwrap
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from http import HTTPStatus
from typing import Any, get_args, get_origin, get_type_hints

from pydantic import BaseModel, TypeAdapter


def infer_operation_contract(
    endpoint: Any,
    *,
    method: str,
    components_schemas: dict[str, Any],
) -> dict[str, Any]:
    """Return ``requestBody`` / ``responses`` for one route handler."""

    target = _unwrap_endpoint(endpoint)
    analysis = _analyze(target, components_schemas=components_schemas)
    request_body = _request_body(
        analysis=analysis, method=method, components_schemas=components_schemas
    )
    responses = _responses(analysis=analysis, components_schemas=components_schemas)
    if not responses:
        responses = {"200": _json_response(_generic_object_schema())}
    contract: dict[str, Any] = {"responses": responses}
    if request_body is not None:
        contract["requestBody"] = request_body
    return contract


@dataclass
class _ResponseVariant:
    status_code: str
    media_type: str
    schema: dict[str, Any] | None


@dataclass
class _EndpointAnalysis:
    globals_ns: dict[str, Any]
    request_model: type[BaseModel] | None = None
    request_schema: dict[str, Any] | None = None
    request_media_type: str | None = None
    request_required: bool = True
    responses: list[_ResponseVariant] = field(default_factory=list)


def _unwrap_endpoint(endpoint: Any) -> Any:
    """Prefer the real handler over transport wrappers such as CORS shims."""

    target = inspect.unwrap(endpoint)
    closure = getattr(target, "__closure__", None) or ()
    for cell in closure:
        try:
            value = cell.cell_contents
        except ValueError:
            continue
        if callable(value) and getattr(value, "__name__", None) == getattr(
            target, "__name__", None
        ):
            return inspect.unwrap(value)
    return target


def _analyze(endpoint: Any, *, components_schemas: dict[str, Any]) -> _EndpointAnalysis:
    source = textwrap.dedent(inspect.getsource(endpoint))
    tree = ast.parse(source)
    fn = next(
        (node for node in tree.body if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))),
        None,
    )
    if fn is None:
        return _EndpointAnalysis(globals_ns=getattr(endpoint, "__globals__", {}))
    analysis = _EndpointAnalysis(globals_ns=getattr(endpoint, "__globals__", {}))
    var_types: dict[str, list[Any]] = defaultdict(list)
    var_schemas: dict[str, list[dict[str, Any]]] = defaultdict(list)
    _walk_statements(
        fn.body,
        analysis=analysis,
        var_types=var_types,
        var_schemas=var_schemas,
        components_schemas=components_schemas,
    )
    return analysis


def _walk_statements(
    statements: list[ast.stmt],
    *,
    analysis: _EndpointAnalysis,
    var_types: dict[str, list[Any]],
    var_schemas: dict[str, list[dict[str, Any]]],
    components_schemas: dict[str, Any],
) -> None:
    for statement in statements:
        if isinstance(statement, ast.Assign):
            inferred_type = _infer_python_type(
                statement.value,
                globals_ns=analysis.globals_ns,
                var_types=var_types,
            )
            inferred_schema = _infer_schema(
                statement.value,
                globals_ns=analysis.globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            for target in statement.targets:
                for name in _assigned_names(target):
                    if inferred_type is not None:
                        var_types[name].append(inferred_type)
                    if inferred_schema is not None:
                        var_schemas[name].append(inferred_schema)
            _maybe_capture_request_model(statement, analysis=analysis, var_types=var_types)
        elif isinstance(statement, ast.AnnAssign):
            inferred_type = _infer_python_type(
                statement.value,
                globals_ns=analysis.globals_ns,
                var_types=var_types,
            )
            inferred_schema = _infer_schema(
                statement.value,
                globals_ns=analysis.globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            for name in _assigned_names(statement.target):
                if inferred_type is not None:
                    var_types[name].append(inferred_type)
                if inferred_schema is not None:
                    var_schemas[name].append(inferred_schema)
            _maybe_capture_request_model(statement, analysis=analysis, var_types=var_types)
        elif isinstance(statement, ast.Return):
            variant = _infer_response_variant(
                statement.value,
                globals_ns=analysis.globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            if variant is not None:
                analysis.responses.append(variant)
        elif isinstance(statement, ast.If):
            _walk_statements(
                statement.body,
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            _walk_statements(
                statement.orelse,
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
        elif isinstance(statement, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor)):
            _walk_statements(
                statement.body,
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            _walk_statements(
                getattr(statement, "orelse", []),
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
        elif isinstance(statement, ast.Try):
            _walk_statements(
                statement.body,
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            for handler in statement.handlers:
                _walk_statements(
                    handler.body,
                    analysis=analysis,
                    var_types=var_types,
                    var_schemas=var_schemas,
                    components_schemas=components_schemas,
                )
            _walk_statements(
                statement.orelse,
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            _walk_statements(
                statement.finalbody,
                analysis=analysis,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
        elif isinstance(statement, ast.Expr):
            _maybe_capture_form_request(statement.value, analysis=analysis)

    if analysis.request_model is None and analysis.request_media_type is None:
        request = _infer_manual_request_schema(statements)
        if request is not None:
            analysis.request_media_type, analysis.request_schema, analysis.request_required = (
                request
            )


def _assigned_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for item in target.elts:
            out.extend(_assigned_names(item))
        return out
    return []


def _maybe_capture_request_model(
    statement: ast.Assign | ast.AnnAssign,
    *,
    analysis: _EndpointAnalysis,
    var_types: dict[str, list[Any]],
) -> None:
    value = statement.value
    if not isinstance(value, ast.Call):
        return
    if not _looks_like_json_body_model(value):
        return
    model = _model_from_call(value, globals_ns=analysis.globals_ns, var_types=var_types)
    if model is not None:
        analysis.request_model = model
        analysis.request_media_type = "application/json"
        analysis.request_required = not _call_has_allow_empty(value)


def _looks_like_json_body_model(call: ast.Call) -> bool:
    return not (not call.args and not call.keywords)


def _call_has_allow_empty(call: ast.Call) -> bool:
    for arg in ast.walk(call):
        if not isinstance(arg, ast.Call):
            continue
        name = _call_name(arg.func)
        if name != "parse_json_object":
            continue
        for keyword in arg.keywords:
            if keyword.arg == "allow_empty" and isinstance(keyword.value, ast.Constant):
                return bool(keyword.value.value)
    return False


def _maybe_capture_form_request(call: ast.AST, *, analysis: _EndpointAnalysis) -> None:
    if analysis.request_media_type is not None:
        return
    if not isinstance(call, ast.Call):
        return
    name = _call_name(call.func)
    if name not in {"_read_upload"}:
        return
    analysis.request_media_type = "multipart/form-data"
    analysis.request_schema = {
        "type": "object",
        "properties": {
            "file": {"type": "string", "format": "binary"},
            "path": {"type": "string"},
            "kind": {"type": "string"},
            "media_type": {"type": "string"},
            "metadata": {"type": "string"},
        },
        "required": ["file"],
    }


def _infer_manual_request_schema(
    statements: list[ast.stmt],
) -> tuple[str, dict[str, Any], bool] | None:
    body_names: dict[str, bool] = {}
    form_names: set[str] = set()

    for statement in ast.walk(ast.Module(body=statements, type_ignores=[])):
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Await):
            awaited = statement.value.value
            if isinstance(awaited, ast.Call) and _call_name(awaited.func) == "parse_json_object":
                allow_empty = False
                for keyword in awaited.keywords:
                    if keyword.arg == "allow_empty" and isinstance(keyword.value, ast.Constant):
                        allow_empty = bool(keyword.value.value)
                for target in statement.targets:
                    for name in _assigned_names(target):
                        body_names[name] = allow_empty
            if isinstance(awaited, ast.Call) and isinstance(awaited.func, ast.Attribute):
                if awaited.func.attr == "form":
                    for target in statement.targets:
                        for name in _assigned_names(target):
                            form_names.add(name)
        elif isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            if (
                isinstance(statement.value.func, ast.Attribute)
                and statement.value.func.attr == "form"
            ):
                for target in statement.targets:
                    for name in _assigned_names(target):
                        form_names.add(name)

    if body_names:
        properties: dict[str, Any] = {}
        required: set[str] = set()
        aliases: dict[str, str] = {}

        for node in ast.walk(ast.Module(body=statements, type_ignores=[])):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                key = _body_key_access(node.value, body_names)
                if key is not None:
                    inferred = _schema_from_validation_context(node, key)
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = key
                    properties.setdefault(key, inferred or {})
            key = _body_key_access(node, body_names)
            if key is not None:
                properties.setdefault(key, _schema_from_validation_context(node, key) or {})
            key = _body_key_subscript(node, body_names)
            if key is not None:
                properties.setdefault(key, {})
                required.add(key)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                inferred = _schema_from_isinstance(node)
                key = _key_from_alias_check(node.args[0], aliases)
                if key is not None and inferred is not None:
                    properties[key] = inferred
            elif isinstance(node, ast.Call):
                key = _key_from_alias_check(node, aliases)
                inferred = _schema_from_validation_context(node, key)
                if key is not None and inferred is not None:
                    properties[key] = inferred
        return (
            "application/json",
            {
                "type": "object",
                "properties": properties or {},
                "required": sorted(required),
                "additionalProperties": True,
            },
            not any(body_names.values()),
        )

    if form_names:
        form_properties: dict[str, Any] = {}
        form_required: set[str] = set()
        for node in ast.walk(ast.Module(body=statements, type_ignores=[])):
            key = _form_key_access(node, form_names)
            if key is not None:
                form_properties.setdefault(key, {"type": "string"})
                if key == "file":
                    form_properties[key] = {"type": "string", "format": "binary"}
                    form_required.add("file")
        return (
            "multipart/form-data",
            {
                "type": "object",
                "properties": form_properties or {"file": {"type": "string", "format": "binary"}},
                "required": sorted(form_required),
            },
            True,
        )
    return None


def _body_key_access(node: ast.AST, body_names: dict[str, bool]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if not isinstance(node.func.value, ast.Name) or node.func.value.id not in body_names:
        return None
    if (
        not node.args
        or not isinstance(node.args[0], ast.Constant)
        or not isinstance(node.args[0].value, str)
    ):
        return None
    return node.args[0].value


def _body_key_subscript(node: ast.AST, body_names: dict[str, bool]) -> str | None:
    if not isinstance(node, ast.Subscript):
        return None
    if not isinstance(node.value, ast.Name) or node.value.id not in body_names:
        return None
    key = _slice_string(node.slice)
    return key


def _form_key_access(node: ast.AST, form_names: set[str]) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if not isinstance(node.func.value, ast.Name) or node.func.value.id not in form_names:
        return None
    if (
        not node.args
        or not isinstance(node.args[0], ast.Constant)
        or not isinstance(node.args[0].value, str)
    ):
        return None
    return node.args[0].value


def _slice_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _key_from_alias_check(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id)
    return None


def _schema_from_validation_context(node: ast.AST, key: str | None) -> dict[str, Any] | None:
    if key is None:
        return None
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name == "str":
            return {"type": "string"}
        if name == "int":
            return {"type": "integer"}
        if name == "float":
            return {"type": "number"}
        if name == "bool":
            return {"type": "boolean"}
    return None


def _schema_from_isinstance(node: ast.Call) -> dict[str, Any] | None:
    if len(node.args) < 2:
        return None
    target = node.args[1]
    if isinstance(target, ast.Name):
        return _schema_for_builtin_type_name(target.id)
    if isinstance(target, ast.Tuple):
        options = [
            _schema_for_builtin_type_name(item.id)
            for item in target.elts
            if isinstance(item, ast.Name)
        ]
        options = [option for option in options if option is not None]
        if not options:
            return None
        return {"oneOf": options}
    return None


def _schema_for_builtin_type_name(name: str) -> dict[str, Any] | None:
    mapping = {
        "str": {"type": "string"},
        "int": {"type": "integer"},
        "float": {"type": "number"},
        "bool": {"type": "boolean"},
        "dict": _generic_object_schema(),
        "list": {"type": "array", "items": {}},
    }
    return mapping.get(name)


def _request_body(
    *,
    analysis: _EndpointAnalysis,
    method: str,
    components_schemas: dict[str, Any],
) -> dict[str, Any] | None:
    if method == "GET":
        return None
    if analysis.request_model is not None:
        schema = _schema_for_type(analysis.request_model, components_schemas=components_schemas)
        return {
            "required": analysis.request_required,
            "content": {analysis.request_media_type or "application/json": {"schema": schema}},
        }
    if analysis.request_schema is not None and analysis.request_media_type is not None:
        return {
            "required": analysis.request_required,
            "content": {
                analysis.request_media_type: {
                    "schema": analysis.request_schema,
                }
            },
        }
    return None


def _responses(
    *,
    analysis: _EndpointAnalysis,
    components_schemas: dict[str, Any],
) -> dict[str, Any]:
    grouped: dict[str, list[_ResponseVariant]] = defaultdict(list)
    for variant in analysis.responses:
        grouped[variant.status_code].append(variant)
    responses: dict[str, Any] = {}
    for status_code, variants in grouped.items():
        first = variants[0]
        if first.schema is None:
            responses[status_code] = {
                "description": HTTPStatus(int(status_code)).phrase
                if status_code.isdigit()
                else "Success."
            }
            continue
        if len(variants) == 1:
            responses[status_code] = _response_for_variant(first)
            continue
        media_types = {variant.media_type for variant in variants}
        if len(media_types) == 1:
            responses[status_code] = {
                "description": HTTPStatus(int(status_code)).phrase
                if status_code.isdigit()
                else "Success.",
                "content": {
                    first.media_type: {
                        "schema": _one_of(
                            [variant.schema for variant in variants if variant.schema is not None]
                        )
                    }
                },
            }
        else:
            content: dict[str, Any] = {}
            for variant in variants:
                if variant.schema is None:
                    continue
                content[variant.media_type] = {"schema": variant.schema}
            responses[status_code] = {
                "description": HTTPStatus(int(status_code)).phrase
                if status_code.isdigit()
                else "Success.",
                "content": content,
            }
    for code in ("400", "401", "403", "404"):
        responses.setdefault(code, _error_ref(code))
    return dict(
        sorted(responses.items(), key=lambda item: int(item[0]) if item[0].isdigit() else 999)
    )


def _response_for_variant(variant: _ResponseVariant) -> dict[str, Any]:
    if variant.schema is None:
        return {"description": HTTPStatus(int(variant.status_code)).phrase}
    return {
        "description": HTTPStatus(int(variant.status_code)).phrase
        if variant.status_code.isdigit()
        else "Success.",
        "content": {variant.media_type: {"schema": variant.schema}},
    }


def _infer_response_variant(
    node: ast.AST | None,
    *,
    globals_ns: dict[str, Any],
    var_types: dict[str, list[Any]],
    var_schemas: dict[str, list[dict[str, Any]]],
    components_schemas: dict[str, Any],
) -> _ResponseVariant | None:
    if node is None:
        return _ResponseVariant(status_code="204", media_type="application/json", schema=None)
    if not isinstance(node, ast.Call):
        return None
    name = _call_name(node.func)
    if name == "envelope_response":
        payload = (
            _infer_schema(
                node.args[0] if node.args else None,
                globals_ns=globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            or _generic_object_schema()
        )
        return _ResponseVariant(
            status_code=_status_code(node, default="200"),
            media_type="application/json",
            schema=_envelope_schema(payload),
        )
    if name == "envelope_response_dict":
        payload = (
            _infer_schema(
                node.args[0] if node.args else None,
                globals_ns=globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            or _generic_object_schema()
        )
        return _ResponseVariant(
            status_code=_status_code(node, default="200"),
            media_type="application/json",
            schema=_envelope_schema(payload),
        )
    if name == "JSONResponse":
        json_payload = _jsonresponse_payload(node)
        schema = (
            _infer_schema(
                json_payload,
                globals_ns=globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            or _generic_object_schema()
        )
        status_code = _status_code(node, default="200")
        if status_code == "204":
            return _ResponseVariant(
                status_code=status_code, media_type="application/json", schema=None
            )
        return _ResponseVariant(
            status_code=status_code, media_type="application/json", schema=schema
        )
    if name in {
        "Response",
        "PlainTextResponse",
        "HTMLResponse",
        "StreamingResponse",
        "FileResponse",
    }:
        status_code = _status_code(node, default="200")
        if status_code == "204" or (name == "Response" and _response_has_no_content(node)):
            return _ResponseVariant(
                status_code=status_code, media_type="application/json", schema=None
            )
        media_type = _response_media_type(name, node)
        schema = _response_payload_schema(
            name=name,
            node=node,
            globals_ns=globals_ns,
            var_types=var_types,
            var_schemas=var_schemas,
            components_schemas=components_schemas,
        )
        return _ResponseVariant(status_code=status_code, media_type=media_type, schema=schema)
    return None


def _jsonresponse_payload(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg in {"content", "payload"}:
            return keyword.value
    return None


def _response_has_no_content(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg == "status_code" and _literal_status(keyword.value) == "204":
            return True
        if (
            keyword.arg == "content"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is None
        ):
            return True
    return False


def _response_media_type(name: str, node: ast.Call) -> str:
    if name == "PlainTextResponse":
        return "text/plain"
    if name == "HTMLResponse":
        return "text/html"
    if name == "StreamingResponse":
        value = _keyword_value(node, "media_type")
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return "application/octet-stream"
    if name == "FileResponse":
        return "application/octet-stream"
    value = _keyword_value(node, "media_type")
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return "application/octet-stream"


def _response_payload_schema(
    *,
    name: str,
    node: ast.Call,
    globals_ns: dict[str, Any],
    var_types: dict[str, list[Any]],
    var_schemas: dict[str, list[dict[str, Any]]],
    components_schemas: dict[str, Any],
) -> dict[str, Any]:
    if name == "PlainTextResponse":
        return {"type": "string"}
    if name == "HTMLResponse":
        return {"type": "string"}
    if name in {"FileResponse", "StreamingResponse"}:
        return {"type": "string", "format": "binary"}
    payload = _keyword_value(node, "content")
    if payload is None and node.args:
        payload = node.args[0]
    return _infer_schema(
        payload,
        globals_ns=globals_ns,
        var_types=var_types,
        var_schemas=var_schemas,
        components_schemas=components_schemas,
    ) or {"type": "string", "format": "binary"}


def _keyword_value(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _status_code(node: ast.Call, *, default: str) -> str:
    keyword = _keyword_value(node, "status_code")
    if keyword is None:
        return default
    literal = _literal_status(keyword)
    return literal or default


def _literal_status(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return str(node.value)
    if isinstance(node, ast.Call) and _call_name(node.func) == "int" and node.args:
        return _literal_status(node.args[0])
    if isinstance(node, ast.Attribute):
        try:
            value = getattr(HTTPStatus, node.attr, None)
            if isinstance(value, IntEnum):
                return str(int(value))
        except Exception:
            return None
    return None


def _infer_schema(
    node: ast.AST | None,
    *,
    globals_ns: dict[str, Any],
    var_types: dict[str, list[Any]],
    var_schemas: dict[str, list[dict[str, Any]]],
    components_schemas: dict[str, Any],
) -> dict[str, Any] | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        if node.id in var_schemas:
            return _one_of(var_schemas[node.id])
        model = _as_model(globals_ns.get(node.id))
        if model is not None:
            return _schema_for_type(model, components_schemas=components_schemas)
        return None
    if isinstance(node, ast.Constant):
        return _schema_for_constant(node.value)
    if isinstance(node, ast.List):
        item_schemas = [
            _infer_schema(
                item,
                globals_ns=globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            for item in node.elts
        ]
        items = _one_of([item for item in item_schemas if item is not None]) if item_schemas else {}
        return {"type": "array", "items": items}
    if isinstance(node, ast.ListComp):
        item = (
            _infer_schema(
                node.elt,
                globals_ns=globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
            or _generic_object_schema()
        )
        return {"type": "array", "items": item}
    if isinstance(node, ast.Dict):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            if key_node is None:
                extra = _infer_schema(
                    value_node,
                    globals_ns=globals_ns,
                    var_types=var_types,
                    var_schemas=var_schemas,
                    components_schemas=components_schemas,
                )
                if isinstance(extra, dict):
                    properties.update(extra.get("properties", {}))
                    required.extend(extra.get("required", []))
                continue
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            key = key_node.value
            properties[key] = (
                _infer_schema(
                    value_node,
                    globals_ns=globals_ns,
                    var_types=var_types,
                    var_schemas=var_schemas,
                    components_schemas=components_schemas,
                )
                or {}
            )
            required.append(key)
        return {"type": "object", "properties": properties, "required": sorted(set(required))}
    if isinstance(node, ast.Call):
        model = _model_from_call(node, globals_ns=globals_ns, var_types=var_types)
        if model is not None:
            return _schema_for_type(model, components_schemas=components_schemas)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "model_dump",
            "model_dump_json",
        }:
            return _infer_schema(
                node.func.value,
                globals_ns=globals_ns,
                var_types=var_types,
                var_schemas=var_schemas,
                components_schemas=components_schemas,
            )
        return_type = _infer_python_type(node, globals_ns=globals_ns, var_types=var_types)
        if return_type is not None:
            return _schema_for_type(return_type, components_schemas=components_schemas)
    if isinstance(node, ast.IfExp):
        left = _infer_schema(
            node.body,
            globals_ns=globals_ns,
            var_types=var_types,
            var_schemas=var_schemas,
            components_schemas=components_schemas,
        )
        right = _infer_schema(
            node.orelse,
            globals_ns=globals_ns,
            var_types=var_types,
            var_schemas=var_schemas,
            components_schemas=components_schemas,
        )
        return _one_of([schema for schema in (left, right) if schema is not None])
    return None


def _infer_python_type(
    node: ast.AST | None,
    *,
    globals_ns: dict[str, Any],
    var_types: dict[str, list[Any]],
) -> Any | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        types = var_types.get(node.id)
        if types:
            return types[0]
        value = globals_ns.get(node.id)
        if value is not None:
            return value
        return None
    if isinstance(node, ast.Call):
        model = _model_from_call(node, globals_ns=globals_ns, var_types=var_types)
        if model is not None:
            return model
        callable_obj = _resolve_callable(node.func, globals_ns=globals_ns, var_types=var_types)
        if callable_obj is None:
            return None
        if inspect.isclass(callable_obj):
            return callable_obj
        try:
            hints = get_type_hints(
                callable_obj,
                globalns=getattr(callable_obj, "__globals__", globals_ns),
                include_extras=True,
            )
        except Exception:
            return None
        return hints.get("return")
    return None


def _model_from_call(
    node: ast.Call,
    *,
    globals_ns: dict[str, Any],
    var_types: dict[str, list[Any]],
) -> type[BaseModel] | None:
    if isinstance(node.func, ast.Attribute) and node.func.attr == "model_validate":
        target = _resolve_object(node.func.value, globals_ns=globals_ns, var_types=var_types)
        return _as_model(target)
    target = _resolve_object(node.func, globals_ns=globals_ns, var_types=var_types)
    return _as_model(target)


def _resolve_callable(
    node: ast.AST, *, globals_ns: dict[str, Any], var_types: dict[str, list[Any]]
) -> Any | None:
    if isinstance(node, ast.Name):
        return globals_ns.get(node.id)
    if isinstance(node, ast.Attribute):
        owner = _resolve_object(node.value, globals_ns=globals_ns, var_types=var_types)
        if owner is None:
            return None
        return getattr(owner, node.attr, None)
    return None


def _resolve_object(
    node: ast.AST, *, globals_ns: dict[str, Any], var_types: dict[str, list[Any]]
) -> Any | None:
    if isinstance(node, ast.Name):
        types = var_types.get(node.id)
        if types:
            return types[0]
        return globals_ns.get(node.id)
    if isinstance(node, ast.Call):
        return _infer_python_type(node, globals_ns=globals_ns, var_types=var_types)
    if isinstance(node, ast.Attribute):
        owner = _resolve_object(node.value, globals_ns=globals_ns, var_types=var_types)
        if owner is None:
            return None
        return getattr(owner, node.attr, None)
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _as_model(value: Any) -> type[BaseModel] | None:
    if inspect.isclass(value) and issubclass(value, BaseModel):
        return value
    return None


def _schema_for_type(py_type: Any, *, components_schemas: dict[str, Any]) -> dict[str, Any]:
    if py_type in {Any, object}:
        return _generic_object_schema()
    origin = get_origin(py_type)
    if origin is dict:
        args = get_args(py_type)
        value_schema = (
            _schema_for_type(args[1], components_schemas=components_schemas)
            if len(args) == 2 and args[1] not in {Any, object}
            else {}
        )
        return {"type": "object", "additionalProperties": value_schema or True}
    if origin in {list, tuple, set, frozenset}:
        args = get_args(py_type)
        item_schema = (
            _schema_for_type(args[0], components_schemas=components_schemas) if args else {}
        )
        return {"type": "array", "items": item_schema}
    if origin is not None and origin is not type(None):
        try:
            schema = TypeAdapter(py_type).json_schema(ref_template="#/components/schemas/{model}")
        except Exception:
            return _generic_object_schema()
        return _hoist_defs(schema, components_schemas=components_schemas)
    model = _as_model(py_type)
    if model is not None:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        return _hoist_defs(schema, components_schemas=components_schemas)
    if py_type is str:
        return {"type": "string"}
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    try:
        schema = TypeAdapter(py_type).json_schema(ref_template="#/components/schemas/{model}")
    except Exception:
        return _generic_object_schema()
    return _hoist_defs(schema, components_schemas=components_schemas)


def _hoist_defs(schema: dict[str, Any], *, components_schemas: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(json.dumps(schema))
    defs = payload.pop("$defs", {})
    if isinstance(defs, dict):
        for name, value in defs.items():
            components_schemas.setdefault(name, _rewrite_refs(value))
    rewritten = _rewrite_refs(payload)
    return rewritten if isinstance(rewritten, dict) else {}


def _rewrite_refs(node: Any) -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                out[key] = value.replace("#/$defs/", "#/components/schemas/")
            else:
                out[key] = _rewrite_refs(value)
        return out
    if isinstance(node, list):
        return [_rewrite_refs(value) for value in node]
    return node


def _schema_for_constant(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if value is None:
        return {"type": "null"}
    return _generic_object_schema()


def _generic_object_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": True}


def _one_of(schemas: Iterable[dict[str, Any]]) -> dict[str, Any]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for schema in schemas:
        key = json.dumps(schema, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(schema)
    if not unique:
        return _generic_object_schema()
    if len(unique) == 1:
        return unique[0]
    return {"oneOf": unique}


def _envelope_schema(payload_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"data": payload_schema},
        "required": ["data"],
    }


def _json_response(schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": "OK",
        "content": {"application/json": {"schema": schema}},
    }


def _error_ref(status_code: str) -> dict[str, Any]:
    mapping = {
        "400": "ValidationFailed",
        "401": "Unauthenticated",
        "403": "Forbidden",
        "404": "NotFound",
    }
    name = mapping.get(status_code, "ValidationFailed")
    return {"$ref": f"#/components/responses/{name}"}
