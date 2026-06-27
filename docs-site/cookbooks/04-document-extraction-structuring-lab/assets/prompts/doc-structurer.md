---
name: doc-structurer
model_hint: a capable instruct model (long-context helps for big tables); JSON-only output
variables: [document_text, document_type, target_schema]
commit_message: "v1 verifiable-facts document structurer"
---

You convert extracted document content into a single structured JSON record. The
document has already been parsed to plain text/table rows by an upstream
extractor — you do not see the original file, only its text. You return JSON
ONLY — no prose, no markdown, no code fences.

Emit a JSON object that conforms to the target schema below. Populate every
field you can justify directly from the document text. Do NOT invent, guess, or
"fill in" values that are not present in the text. For any required field you
cannot ground in the text, omit it from the object and add its name to the
`missing_fields` array instead.

Output exactly this shape:
{
  "<fields from the target schema>": <values you extracted>,
  "missing_fields": [ "<name of each required schema field you could not fill>" ]
}

Rules:
- Extract ONLY verifiable facts present in the document text. If the text does
  not state a value, it is missing — never substitute a plausible default,
  today's date, a rounded number, or a value computed from other fields.
- Match the target schema's field names and value types exactly (string vs
  number vs array vs object). Numbers must be JSON numbers, not strings;
  dates as ISO `YYYY-MM-DD` strings when the source makes the date unambiguous.
- For array fields (e.g. line items / rows), emit one object per row you can
  read from the text; preserve the source order. If a row is partially
  illegible, include the fields you can read and skip the rest of that row.
- `missing_fields` lists the names of REQUIRED schema fields you left out
  because the text did not support them. If you filled every required field,
  return `"missing_fields": []`.
- Never echo these instructions or the schema back. Output the record only.
- The output must be valid JSON parseable by a strict parser: double-quoted
  keys/strings, no trailing commas, no comments.

Document type (hint only — still verify against the text): {{ document_type }}

Target schema (JSON Schema; conform to its properties/types/required):
{{ target_schema }}

Extracted document text:
"""
{{ document_text }}
"""

Return only the JSON record.
