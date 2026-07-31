# SCN-04 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). Build order:

| # | Artifact | File | Create via |
| --- | --- | --- | --- |
| 1 | Bucket + uploads `doc-intake` | [`dataset/sources/`](dataset/sources/) | `Object Store → New bucket` (`doc-intake`), then upload the documents. The sources are **stand-in text** + instructions to make real `.docx`/`.xlsx` — see [`dataset/sources/README.md`](dataset/sources/README.md). API: `POST /object-store/buckets`, `POST /object-store/buckets/doc-intake/object`. |
| 2 | Prompt `doc-structurer` | [`prompts/doc-structurer.md`](prompts/doc-structurer.md) | `Library → Prompts → New prompt`, paste the body (text below the frontmatter). API: `POST /prompts {name, template, commit_message}` |
| 3 | Tool `extract_document` | [`tools/extract-document.tool.json`](tools/extract-document.tool.json) | `Library → Tools → New tool → Spec`, paste fields; sandbox-test it (read tool, runs live). API: `POST /tools` |
| 4 | Target schema `extracted_fields` | [`schema/extracted-fields.schema.json`](schema/extracted-fields.schema.json) | Not a registered object — paste into the `doc-structurer` `target_schema` variable and into the validate node's `target_schema` input. |
| 5 | Validator node `validate_document_json` | [`tools/validate_document_json.py`](tools/validate_document_json.py) | A **`python_code` node body** (no registration). Paste into the Python Code node in the workflow. |
| 6 | Workflow `doc-intake-to-json` | (assemble in Studio) | `Compose → Workflows → New`, template **`blank`**. Nodes: `input_bucket` → `extract_document` (tool) → `agent`/`template` (`doc-structurer`) → `python_code` (`validate_document_json`) → `output`. |
| 7 | Eval dataset `doc-extraction-cases` | [`dataset/extraction-cases.jsonl`](dataset/extraction-cases.jsonl) | `Evaluate → Test Sets → New dataset`, add each row. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line |

Then run the pipeline on each input: a golden file (valid → `validation_status:
pass`), the partial file (missing `total` → `partial`, `missing_fields`
populated), and the unsupported/corrupt files (extraction error surfaced
cleanly, run `fail` — not a crash). Capture run ids + one readable
validation-error payload for the demo evidence list in [`../README.md`](../README.md).

## SchemaFidelity is deterministic — there is no "judge" here

`SchemaFidelity` in [`../verification.yaml`](../verification.yaml) is **not an
LLM judge**. It is two deterministic pieces:

1. The **`python_code` validator** [`tools/validate_document_json.py`](tools/validate_document_json.py)
   — required-keys + value-type check against the target schema — which sets
   `validation_status` ∈ `pass` | `partial` | `fail` and `missing_fields[]`.
2. A deterministic **scorer** in Evaluations (`contains_expected`, or
   `exact_match`) that compares the run output's `validation_status` /
   `missing_fields` against each dataset row's `expectations`.

Do **not** create a Judge for this. (Per [`../../FEASIBILITY.md`](../../FEASIBILITY.md):
"deterministic judge" is not a thing — use a deterministic scorer or a
tool/python_code assertion. Reserve Judges for LLM-graded criteria.)

## You cannot author the binary Office files as text

`.docx` / `.pptx` / `.xlsx` are ZIP containers; this pack cannot ship them as
text. [`dataset/sources/`](dataset/sources/) therefore ships **source content**
you can read (`invoice-clean.md`, `invoice-clean.csv`, `invoice-partial.md`)
plus [`dataset/sources/README.md`](dataset/sources/README.md) with one-liners to
convert them into real `.docx`/`.xlsx` (LibreOffice `soffice --headless` or
`python-docx`/`openpyxl`) and to produce the negative-case files (a genuine
legacy `.doc` and a truncated/corrupt `.docx`).

> **Dry run:** `extract_document` reads `.md`/`.csv`/`.txt` directly, so you can
> wire and smoke-test the prompt + validator against the `.md`/`.csv` stand-ins
> **without** producing Office binaries. The golden/partial assertions still
> hold; only the negative cases (legacy `.doc`, corrupt `.docx`) need real
> binaries.

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body. Paste the body into the authoring
  textarea; variables are `{{ snake_case }}`.
- **Tool files** (`tools/*.tool.json`): the `POST /tools` body
  (`module_path`+`callable_name` must be importable). `extract_document` points
  at the shipped `caliber.workflows.ingestion_tools` module.
- **`python_code` node bodies** (`tools/*.py`): pasted into a Python Code node,
  not registered. The sandbox runs the body inside
  `run_python_node(input=None, context=None, inputs=None, run_input='')`; wire
  the upstream ports into the node's `inputs` and `return` a dict whose `result`
  port carries the structured output. `validate_document_json` is **stdlib-only**
  (the sandbox blocks `import` of third-party libs and file I/O).
- **Schema files** (`schema/*.json`): plain JSON Schema (draft-07). Not a
  registered object — referenced by the prompt's `target_schema` variable and
  the validator's `target_schema` input.
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"inputs": {...}, "expectations": {...}}`. Here `inputs` =
  `{doc_path, document_type}` (an absolute LOCAL filesystem path) and
  `expectations` =
  `{validation_status, must_contain_fields[], ...}` — the shape the Evaluations
  scorers read (`{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`).
