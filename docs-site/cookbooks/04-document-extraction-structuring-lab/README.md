# Document-to-JSON Pipeline

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery (a preview area, off by default). This lab needs no other flag.

## Demo objective

A workflow that ingests Office docs, extracts text/tables, normalizes to JSON,
and validates output before storage or retrieval.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). Key points:

- ✅ **Object Store** upload/preview/extract is real and backed by S3/MinIO.
  Extract supports `.docx` / `.pptx` / `.xlsx` (**not** legacy `.doc/.ppt/.xls`).
- ✅ Two different extraction surfaces. The Object Store **extract endpoint**
  (`GET /object-store/buckets/{bucket}/object/extract?key=...`, shown as the
  preview dialog's content) is for inspecting a stored file. Inside a workflow,
  use the shipped tool `caliber.workflows.ingestion_tools:extract_document`
  (handles PDF/DOCX/PPTX/XLSX/MD + OCR), which opens a **local file path**
  (`ref`), not a bucket key.
- ⚠️ The `input_bucket` node does **not** feed `extract_document`: it decodes each
  object as UTF-8 text and caps it at 100 KB by default, so binary Office files
  cannot flow through it. Use it only for text objects (`.md`/`.csv`/`.txt`).
- ✅ JSON Schema validation is a visual **Data Transform → JSON Schema** node
  using Draft 2020-12; it returns `{valid, metadata.errors}` (each error has
  `path`, `message`, `validator`), fails the run when invalid by default, or
  publishes `valid=false` for routing with `fail_on_invalid` off. It does not
  compute `validation_status` or `missing_fields`; the `python_code` validator
  in the assets does.
- The `SchemaFidelity` judge typed `deterministic` is **not a judge** — it is the
  Data Transform validator's pass/fail plus an `exact_match`/`contains_expected`
  scorer in Evaluations.

## Prerequisites & seed

- DOCX/PPTX/XLSX samples from [`test-data.yaml`](test-data.yaml); the object
  store and the files on the platform host (the extractor reads local paths).
- A target JSON schema for `extracted_fields`.

## Recipe (UI-first, with API fallbacks)

1. **Create a bucket + upload.** `Object Store`: type `doc-intake` into the
   new-bucket field and click **+** (Create bucket; admin scope), then **Upload**
   the sample DOCX/PPTX/XLSX.
   - API: `POST /object-store/buckets`, `POST /object-store/buckets/{b}/objects`
     (multipart `file`).
2. **Verify extraction (preview).** Click each file's preview (eye) icon; the
   dialog runs extraction by itself. Confirm text for DOCX/PPTX and sheet tables
   (`sheets[].rows`) for XLSX, and that an unsupported `.doc` shows a **readable**
   `kind:"unsupported"` message (this is your negative case). There is no
   separate Extract button.
   - API: `GET /object-store/buckets/doc-intake/object/extract?key=<name>`.
3. **Register the extractor tool (optional).** `Tools → Register Tool`
   `extract_document` → `module_path=caliber.workflows.ingestion_tools`,
   `callable_name=extract_document`, `side_effect_level=read`. Sandbox-test it.
4. **Author the structuring prompt.** `Prompts → New prompt`
   `doc-structurer`: system from build.yaml (*"Extract only verifiable facts …
   emit JSON matching the target schema; never invent missing values; list
   missing_fields explicitly."*).
5. **Build the workflow.** `Workflows → New Workflow`, template **`blank`**.
   Add nodes: `extract_document` (tool; map the run input's local `doc_path` to
   `ref`) → `agent`/`template` (apply `doc-structurer`) → a validator →
   `output`. The validator is either a `data_transform` node (operation
   `json_schema`: required keys + types, emits `valid` and `metadata.errors`) or
   the `python_code` body [`validate_document_json.py`](assets/tools/validate_document_json.py),
   which sets `validation_status` (`pass`/`partial`/`fail`) and `missing_fields`.
6. **Preview + real runs.** Run the golden file (valid → `validation_status:pass`),
   an edge file (partial → `missing_fields` populated), and the corrupt `.docx`
   (extraction error surfaced cleanly at the extract node, not a crash). A legacy
   `.doc` is not a clean failure in the workflow: `extract_document` reads it as
   best-effort UTF-8 text, so its readable diagnostic is the one from step 2.
7. **Observe failures.** `Observability` → open each run's trace and
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
  extraction vs normalization vs validation (`unsupported_format_error_readability = 1.0`:
  the Object Store preview for a legacy `.doc`, the extract node for a corrupt `.docx`).
