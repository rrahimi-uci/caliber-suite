# Document-to-JSON Pipeline

## Demo objective

A workflow that ingests Office docs, extracts text/tables, normalizes to JSON,
and validates output before storage or retrieval.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ **Object Store** upload/preview/extract is real and backed by S3/MinIO.
  Extract supports `.docx` / `.pptx` / `.xlsx` (**not** legacy `.doc/.ppt/.xls`).
- ✅ Two ways to extract inside a workflow: the Object Store **extract endpoint**
  (`GET /object-store/buckets/{bucket}/object/extract?key=...`) or the shipped
  tool `caliber.workflows.ingestion_tools:extract_document` (handles
  PDF/DOCX/PPTX/XLSX/MD + OCR).
- ✅ JSON Schema validation is a visual **Data Transform → JSON Schema** node
  using Draft 2020-12; it can fail closed or publish `valid=false` for routing.
- The `SchemaFidelity` judge typed `deterministic` is **not a judge** — it is the
  Data Transform validator's pass/fail plus an `exact_match`/`contains_expected`
  scorer in Evaluations.

## Prerequisites & seed

- DOCX/PPTX/XLSX samples from [`test-data.yaml`](test-data.yaml).
- A target JSON schema for `extracted_fields`.

## Recipe (UI-first, with API fallbacks)

1. **Create a bucket + upload.** `Object Store → New bucket` (`doc-intake`),
   then upload the sample DOCX/PPTX/XLSX.
   - API: `POST /object-store/buckets`, `POST /object-store/buckets/{b}/object`.
2. **Verify extraction (preview).** Open each file → **Extract**; confirm text
   for DOCX/PPTX and `sheets[].rows` for XLSX, and that an unsupported `.doc`
   returns a **readable** `kind:"unsupported"` error (this is your negative case).
   - API: `GET /object-store/buckets/doc-intake/object/extract?key=<name>`.
3. **Register the extractor tool (optional).** `Tools → New tool`
   `extract_document` → `module_path=caliber.workflows.ingestion_tools`,
   `callable_name=extract_document`, `side_effect_level=read`. Sandbox-test it.
4. **Author the structuring prompt.** `Prompts → New prompt`
   `doc-structurer`: system from build.yaml (*"Extract only verifiable facts …
   emit JSON matching the target schema; never invent missing values; list
   missing_fields explicitly."*).
5. **Build the workflow.** `Compose → Workflows → New`, template **`blank`**.
   Add nodes: `input_bucket` (fetch the object) → `extract_document` (tool) →
   `agent`/`template` (apply `doc-structurer`) → `data_transform`
   (operation `json_schema`: validate required keys + types, set
   `validation_status` and `missing_fields`) → `output`.
6. **Preview + real runs.** Run the golden file (valid → `validation_status:pass`),
   an edge file (partial → `missing_fields` populated), and the unsupported file
   (extraction error surfaced cleanly, not a crash).
7. **Observe failures.** `Observe → Observability` → open each run's trace and
   separate **extraction** failures (the extract node) from **schema** failures
   (the validate node). Capture a readable validation error.
8. **Tune.** Adjust the prompt or schema where failures are systemic; re-run.

## Demo evidence to capture

- Object keys for each input document.
- Workflow run ids for pass / partial / fail.
- One readable validation-error payload (which node, which field).

## Done when / gate

- Golden cases produce schema-valid JSON (`schema_pass_rate_min ≥ 0.95`).
- Unsupported/invalid files fail with a readable diagnostic that identifies
  extraction vs normalization vs validation (`unsupported_format_error_readability = 1.0`).
