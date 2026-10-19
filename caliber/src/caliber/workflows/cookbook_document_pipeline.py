"""Cookbook 04 (Document-to-JSON Pipeline): the installable intake workflow.

The cookbook installs a real pipeline, not a placeholder: a text document in an object-store
bucket is read, turned into an invoice record, checked against a JSON Schema, and then either
written to an output bucket or held for a person to approve. Nothing here calls a model, so the
pipeline behaves the same under the default simulated providers as under a real one.

One run handles one document: the run input is the object key (for example
``incoming/inv-2026-0101-acme.txt``) and ``read_document`` lists exactly that key.

    start -> read_document (input_bucket)
          -> extract_fields (python_code)  -> validate_schema (data_transform json_schema)
          -> package_result (python_code: record + schema errors in one JSON text)
          -> route (router on ``valid``)
                valid   -> emit_extracted -> write_extracted (output_bucket, never overwrites) -> final
                invalid -> review_gate (human_approval) -> emit_reviewed -> write_reviewed -> final

``package_result`` exists because a run that resumes after an approval only re-delivers what flows
through the gate: ``emit_reviewed`` therefore takes the record and the schema errors from the one
text that passes through ``review_gate``, not from a second edge out of ``validate_schema``.

The python_code sandbox pre-loads ``re``, ``json`` and ``datetime`` and refuses ``import``, so the
node bodies below have none. The docs copy of the extractor
(``docs-site/cookbooks/04-document-extraction-structuring-lab/assets/tools/extract_invoice_fields.py``)
is checked against ``EXTRACT_FIELDS_CODE`` by ``tests/test_cookbook_document_pipeline.py``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

INPUT_BUCKET = "doc-intake"
OUTPUT_BUCKET = "doc-extracted"
EXTRACTED_PREFIX = "invoices"
REVIEWED_PREFIX = "reviewed"

#: Draft 2020-12 schema the extracted record must satisfy to skip review.
INVOICE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Extracted invoice",
    "type": "object",
    "required": [
        "document_type",
        "invoice_number",
        "vendor",
        "invoice_date",
        "currency",
        "line_items",
        "total",
    ],
    "properties": {
        "document_type": {"const": "invoice"},
        "invoice_number": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$"},
        "vendor": {"type": "string", "minLength": 2},
        "invoice_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
        "currency": {"enum": ["USD", "EUR", "GBP", "JPY", "CAD"]},
        "line_items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["description", "quantity", "unit_price", "amount"],
                "properties": {
                    "description": {"type": "string", "minLength": 1},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number"},
                },
            },
        },
        "total": {"type": "number", "exclusiveMinimum": 0},
        "total_reconciles": {"enum": [True, None]},
        "encoding_clean": {"const": True},
        "truncated": {"const": False},
    },
}

EXTRACT_FIELDS_CODE = r'''"""extract_invoice_fields - body of the `extract_fields` python_code node.

Reads the one text object the `read_document` (input_bucket) node loaded and turns it into a
structured invoice record. It is deterministic and uses no model: labelled header fields, a
line-item table in one of three layouts (aligned text, Markdown table, CSV) and a totals block are
found with regular expressions. It never invents a value: a field the document does not state is
returned as null (or an empty list) and the `validate_schema` node reports it.

The python_code sandbox pre-loads `re`, `json` and `datetime` and refuses `import`, so this file has
no imports. It is the whole node body: the platform keeps a body that defines
`run_python_node(input, context, inputs, run_input)` as written.

Inputs:  inputs["files"]  the input_bucket node's `files` port: [{key, bytes, truncated, text}, ...]
         run_input        the run's input text (the object key), used in error messages
Returns: the record itself (a dict), which the node publishes on its `result` port and, as JSON,
         on its `text` port
"""

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
SYMBOL_CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
CURRENCY_CODES = ("USD", "EUR", "GBP", "JPY", "CAD", "AUD", "CHF")
SYMBOLS = "$€£¥"

# One money-looking token: optional sign/parenthesis, optional symbol, digits with , and . groups.
MONEY = r"-?\(?\s*[" + SYMBOLS + r"]?\s*-?\d[\d.,]*\)?"
ITEM_ROW = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s+((?:[" + SYMBOLS + r"]\s?)?-?\d[\d.,]*|N/A)"
    r"\s+((?:[" + SYMBOLS + r"]\s?)?-?\d[\d.,]*|N/A)\s*$"
)
TOTAL_LABEL = re.compile(
    r"^\s*(?:(grand\s+total|total\s+due|amount\s+due|balance\s+due)|(total))\b(.*)$", re.I
)
TOTALS_START = re.compile(
    r"^\s*(?:sub\s*total|total|(?:sales\s+)?tax|vat|gst|shipping|freight|delivery|discount|grand|"
    r"amount\s+due|balance|payment)\b",
    re.I,
)
TOTALS_CELL = re.compile(
    r"^(?:sub\s*total|total|(?:sales\s+)?tax|vat|gst|shipping|freight|discount|grand\s+total|"
    r"amount\s+due|balance\s+due)$",
    re.I,
)
OTHER_LABELS = (
    "bill to", "ship to", "due date", "po number", "po", "payment terms", "terms", "customer",
    "date", "invoice date", "issue date", "currency", "invoice no", "invoice number", "invoice",
    "invoice num", "inv no", "inv num",
)
HEADER_FIELD = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z ._#]*?)\s*(?:[:,=]|\s#)\s*(?P<value>\S.*?)\s*$")


def split_csv(line):
    cells, cell, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            cells.append(cell.strip())
            cell = ""
        else:
            cell += ch
    cells.append(cell.strip())
    return cells


def parse_amount(token):
    """'$1,250.00' -> 1250.0, '1.250,00' -> 1250.0, '(50.00)' -> -50.0, 'N/A' -> None."""
    if token is None:
        return None
    text = token.strip()
    negative = text.startswith("-") or "(" in text or "-" in text.replace(" ", "")[:3]
    digits = re.sub(r"[^\d.,]", "", text)
    if not re.search(r"\d", digits):
        return None
    if "," in digits and "." in digits:
        if digits.rfind(".") > digits.rfind(","):
            digits = digits.replace(",", "")
        else:
            digits = digits.replace(".", "").replace(",", ".")
    elif "," in digits:
        if digits.count(",") == 1 and re.search(r",\d{1,2}$", digits):
            digits = digits.replace(",", ".")
        else:
            digits = digits.replace(",", "")
    elif digits.count(".") > 1:
        digits = digits.replace(".", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def last_money(text):
    tokens = re.findall(MONEY, text)
    return parse_amount(tokens[-1]) if tokens else None


def parse_date(value):
    text = value.strip().rstrip(".")
    found = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if found:
        year, month, day = int(found.group(1)), int(found.group(2)), int(found.group(3))
    else:
        found = re.match(r"^([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})$", text)
        if found:
            month, day, year = MONTHS.get(found.group(1).lower()), int(found.group(2)), int(found.group(3))
        else:
            found = re.match(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})$", text)
            if not found:
                return None
            day, month, year = int(found.group(1)), MONTHS.get(found.group(2).lower()), int(found.group(3))
    if not month:
        return None
    try:
        return datetime.date(year, month, day).isoformat()
    except ValueError:
        return None


def cell_index(header_cells):
    index = {}
    for position, cell in enumerate(header_cells):
        name = re.sub(r"[^a-z]", "", cell.lower())
        if name in ("description", "item", "details") and "description" not in index:
            index["description"] = position
        elif name in ("qty", "quantity") and "quantity" not in index:
            index["quantity"] = position
        elif name in ("unitprice", "unit", "price", "rate") and "unit_price" not in index:
            index["unit_price"] = position
        elif name in ("amount", "total", "linetotal") and "amount" not in index:
            index["amount"] = position
    return index if "description" in index and "amount" in index else None


def row_item(cells, index):
    def cell(name):
        position = index.get(name)
        return cells[position] if position is not None and position < len(cells) else None

    quantity = parse_amount(cell("quantity")) if cell("quantity") else None
    return {
        "description": (cell("description") or "").strip(),
        "quantity": quantity,
        "unit_price": parse_amount(cell("unit_price")) if cell("unit_price") else None,
        "amount": parse_amount(cell("amount")) if cell("amount") else None,
    }


def parse_items(lines):
    """Find the line-item table: a CSV or Markdown table with a header row, or aligned text."""
    items = []
    mode = None
    index = None
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if mode is None:
            if stripped.startswith("|"):
                cells = [c.strip() for c in stripped.strip("|").split("|")]
                found = cell_index(cells)
                if found:
                    mode, index = "md", found
            elif "," in stripped:
                found = cell_index(split_csv(stripped))
                if found:
                    mode, index = "csv", found
            elif re.match(r"^\s*(qty|quantity)\b.*\bdescription\b.*\bamount\b", line, re.I):
                mode = "text"
            continue
        if re.match(r"^[\s|:\-=+]*$", line):
            if stripped and mode != "text":
                continue  # a Markdown separator row or a ruled line
            if items:
                break  # a blank line after the first item ends the table
            continue
        if mode == "md":
            if not stripped.startswith("|"):
                break
            items.append(row_item([c.strip() for c in stripped.strip("|").split("|")], index))
        elif mode == "csv":
            cells = split_csv(stripped)
            if TOTALS_CELL.match(cells[0]):
                break
            items.append(row_item(cells, index))
        else:
            if not line[:1].isspace() and TOTALS_START.match(stripped):
                break
            row = ITEM_ROW.match(line)
            if row:
                items.append(
                    {
                        "description": row.group(2).strip(),
                        "quantity": parse_amount(row.group(1)),
                        "unit_price": parse_amount(row.group(3)),
                        "amount": parse_amount(row.group(4)),
                    }
                )
            elif items:
                items[-1]["description"] = items[-1]["description"] + " " + stripped
    return items


def extract_record(text, key, size, truncated):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    record = {
        "source_key": key,
        "source_bytes": size,
        "document_type": "invoice" if re.search(r"invoice", text, re.I) else "unknown",
        "invoice_number": None,
        "vendor": None,
        "invoice_date": None,
        "currency": None,
        "line_items": [],
        "subtotal": None,
        "tax": None,
        "shipping": None,
        "discount": None,
        "total": None,
        "total_reconciles": None,
        "encoding_clean": "�" not in text,
        "truncated": truncated,
    }
    if record["document_type"] != "invoice":
        return record

    explicit_currency = None
    first_line = None
    priority_total = None
    plain_total = None
    for raw in lines:
        stripped = raw.replace("*", "").strip()
        if not stripped:
            continue
        if first_line is None:
            first_line = re.sub(r"^#+\s*", "", stripped)
        field = HEADER_FIELD.match(stripped)
        label = re.sub(r"[ ._#]+", " ", field.group("label")).strip().lower() if field else ""
        value = field.group("value") if field else ""
        if label in ("invoice no", "invoice number", "invoice num", "invoice", "inv no", "inv num"):
            token = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)
            if token and record["invoice_number"] is None:
                record["invoice_number"] = token.group(0)
        elif label in ("invoice date", "date", "issue date", "date of issue", "issued"):
            if record["invoice_date"] is None:
                record["invoice_date"] = parse_date(value)
        elif label in ("vendor", "from", "supplier", "seller", "issued by", "vendor name"):
            if record["vendor"] is None:
                record["vendor"] = value
        elif label == "currency":
            code = value.strip().upper()
            explicit_currency = code if re.match(r"^[A-Z]{3}$", code) else explicit_currency
        total = TOTAL_LABEL.match(stripped)
        if total:
            amount = last_money(total.group(3))
            if amount is not None:
                if total.group(1):
                    priority_total = (amount, total.group(3))
                else:
                    plain_total = (amount, total.group(3))
            continue
        for name, pattern in (
            ("subtotal", r"^sub\s*total\b"),
            ("tax", r"^(?:sales\s+)?(?:tax|vat|gst)\b"),
            ("shipping", r"^(?:shipping|freight|delivery)\b"),
            ("discount", r"^discount\b"),
        ):
            if re.match(pattern, stripped, re.I):
                amount = last_money(re.sub(r"\([^)]*%[^)]*\)|\d+(?:\.\d+)?\s*%", "", stripped))
                if amount is not None:
                    record[name] = -abs(amount) if name == "discount" else amount

    if record["vendor"] is None and first_line is not None:
        found = HEADER_FIELD.match(first_line)
        first_label = re.sub(r"[ ._#]+", " ", found.group("label")).strip().lower() if found else ""
        if (
            first_label not in OTHER_LABELS
            and not re.match(r"^invoice\b", first_line, re.I)
            and not first_line.startswith("|")
        ):
            record["vendor"] = first_line

    chosen = priority_total or plain_total
    if chosen:
        record["total"] = chosen[0]
    record["line_items"] = parse_items(lines)

    if explicit_currency:
        record["currency"] = explicit_currency
    elif chosen:
        found = re.search(r"\b(" + "|".join(CURRENCY_CODES) + r")\b", chosen[1])
        if found:
            record["currency"] = found.group(1)
    if record["currency"] is None:
        for ch in text:
            if ch in SYMBOL_CURRENCY:
                record["currency"] = SYMBOL_CURRENCY[ch]
                break

    amounts = [item["amount"] for item in record["line_items"]]
    if record["total"] is not None and amounts and all(a is not None for a in amounts):
        expected = sum(amounts) + (record["tax"] or 0) + (record["shipping"] or 0) + (record["discount"] or 0)
        record["total_reconciles"] = abs(expected - record["total"]) < 0.01
    return record


def run_python_node(input=None, context=None, inputs=None, run_input=""):
    files = (inputs or {}).get("files") or []
    if len(files) != 1:
        raise ValueError(
            "expected exactly one object under %r, found %d" % (run_input, len(files))
        )
    document = files[0]
    record = extract_record(
        document.get("text") or "",
        document.get("key") or run_input,
        document.get("bytes") or 0,
        bool(document.get("truncated")),
    )
    return record
'''

PACKAGE_RESULT_CODE = r"""def run_python_node(input=None, context=None, inputs=None, run_input=""):
    inputs = inputs or {}
    problems = (inputs.get("errors") or {}).get("errors") or []
    packaged = {"record": inputs.get("record"), "schema_errors": problems}
    return {"text": json.dumps(packaged, sort_keys=True), "valid": inputs.get("valid") is True}
"""

EMIT_EXTRACTED_CODE = r"""def run_python_node(input=None, context=None, inputs=None, run_input=""):
    record = json.loads((inputs or {}).get("text") or "{}")["record"]
    name = record["invoice_number"] + ".json"
    return {
        "text": "extracted " + record["invoice_number"],
        "artifacts": {name: json.dumps(record, indent=2, sort_keys=True)},
    }
"""

EMIT_REVIEWED_CODE = r"""def run_python_node(input=None, context=None, inputs=None, run_input=""):
    packaged = json.loads((inputs or {}).get("text") or "{}")
    record = packaged["record"]
    record["review"] = {"decision": "approved", "schema_errors": packaged["schema_errors"]}
    stem = (record.get("source_key") or "document").split("/")[-1].rsplit(".", 1)[0]
    return {
        "text": "approved " + stem,
        "artifacts": {stem + ".json": json.dumps(record, indent=2, sort_keys=True)},
    }
"""


def _ports(**ports: str) -> dict[str, dict[str, str]]:
    return {name: {"type": kind} for name, kind in ports.items()}


def document_pipeline_graph() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Nodes and edges of the intake workflow (the start and final nodes included)."""

    nodes: dict[str, dict[str, Any]] = {
        "start": {"id": "start", "type": "start", "outputs": _ports(object_key="string")},
        "read_document": {
            "id": "read_document",
            "type": "input_bucket",
            "label": "Read the document",
            "bucket": INPUT_BUCKET,
            "prefix": "",
            "recursive": False,
            "max_files": 5,
            "max_bytes_per_file": 100_000,
            "encoding": "utf-8",
            "inputs": _ports(prefix="string"),
            "outputs": _ports(text="string", files="structured", metadata="structured"),
        },
        "extract_fields": {
            "id": "extract_fields",
            "type": "python_code",
            "label": "Extract invoice fields",
            "code": EXTRACT_FIELDS_CODE,
            "timeout_seconds": 20,
            "inputs": _ports(files="structured"),
            "outputs": _ports(text="string", result="structured", metadata="structured"),
        },
        "validate_schema": {
            "id": "validate_schema",
            "type": "data_transform",
            "label": "Validate against the invoice schema",
            "operation": "json_schema",
            "config": {"schema": deepcopy(INVOICE_SCHEMA)},
            "fail_on_invalid": False,
            "inputs": _ports(value="structured", text="string"),
            "outputs": _ports(
                text="string", result="structured", valid="boolean", metadata="structured"
            ),
        },
        "package_result": {
            "id": "package_result",
            "type": "python_code",
            "label": "Bundle the record with its schema errors",
            "code": PACKAGE_RESULT_CODE,
            "inputs": _ports(record="structured", valid="boolean", errors="structured"),
            "outputs": _ports(
                text="string", result="structured", valid="boolean", metadata="structured"
            ),
        },
        "route": {
            "id": "route",
            "type": "router",
            "label": "Valid or needs review",
            "inputs": _ports(valid="boolean", text="string"),
            "outputs": _ports(text="string"),
            "branches": [
                {
                    "condition": {"field": "valid", "op": "equals", "value": "true"},
                    "to": "emit_extracted",
                },
                {"condition": None, "to": "review_gate"},
            ],
        },
        "emit_extracted": {
            "id": "emit_extracted",
            "type": "python_code",
            "label": "Name the output object",
            "code": EMIT_EXTRACTED_CODE,
            "inputs": _ports(text="string"),
            "outputs": _ports(text="string", result="structured", metadata="structured"),
        },
        "write_extracted": {
            "id": "write_extracted",
            "type": "output_bucket",
            "label": "Write the extracted JSON",
            "bucket": OUTPUT_BUCKET,
            "prefix": EXTRACTED_PREFIX,
            "overwrite": False,
            "inputs": _ports(input="string"),
            "outputs": _ports(keys="structured", metadata="structured"),
        },
        "review_gate": {
            "id": "review_gate",
            "type": "human_approval",
            "label": "Approve or reject the invalid document",
            "required_role": "caliber.approver",
            "inputs": _ports(request="string"),
            "outputs": _ports(approved="string"),
        },
        "emit_reviewed": {
            "id": "emit_reviewed",
            "type": "python_code",
            "label": "Record the approval",
            "code": EMIT_REVIEWED_CODE,
            "inputs": _ports(text="string"),
            "outputs": _ports(text="string", result="structured", metadata="structured"),
        },
        "write_reviewed": {
            "id": "write_reviewed",
            "type": "output_bucket",
            "label": "Write the approved JSON",
            "bucket": OUTPUT_BUCKET,
            "prefix": REVIEWED_PREFIX,
            "overwrite": True,
            "inputs": _ports(input="string"),
            "outputs": _ports(keys="structured", metadata="structured"),
        },
        "final": {"id": "final", "type": "output", "inputs": _ports(response="structured")},
    }

    def edge(source: str, target: str, **mapping: str) -> dict[str, Any]:
        return {"id": f"e_{source}_{target}", "from": source, "to": target, "map": mapping}

    edges = [
        edge("start", "read_document", object_key="prefix"),
        edge("read_document", "extract_fields", files="files"),
        edge("extract_fields", "validate_schema", result="value"),
        edge(
            "validate_schema",
            "package_result",
            result="record",
            valid="valid",
            metadata="errors",
        ),
        edge("package_result", "route", valid="valid", text="text"),
        edge("route", "emit_extracted", text="text"),
        edge("emit_extracted", "write_extracted", text="input"),
        edge("write_extracted", "final", metadata="response"),
        edge("route", "review_gate", text="request"),
        edge("review_gate", "emit_reviewed", approved="text"),
        edge("emit_reviewed", "write_reviewed", text="input"),
        edge("write_reviewed", "final", metadata="response"),
    ]
    return nodes, edges


__all__ = [
    "EMIT_EXTRACTED_CODE",
    "EMIT_REVIEWED_CODE",
    "EXTRACTED_PREFIX",
    "EXTRACT_FIELDS_CODE",
    "INPUT_BUCKET",
    "INVOICE_SCHEMA",
    "OUTPUT_BUCKET",
    "PACKAGE_RESULT_CODE",
    "REVIEWED_PREFIX",
    "document_pipeline_graph",
]
