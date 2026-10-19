# Cookbook 04 assets

Everything the lab uses, and how each file is loaded. The README's "Dataset and sample files" section describes the corpus case by case.

| Artifact | File | How it is used |
| --- | --- | --- |
| 30 sample documents | [`dataset/sources/`](dataset/sources/) | Uploaded to the `doc-intake` bucket under `incoming/` (UI: **Object Store** > `doc-intake` > New folder `incoming` > **Upload**, select all; API: `caliber.object_store.upload("doc-intake", filename=..., content=..., key="incoming/<file>")`). UTF-8 text, CSV and Markdown, plus an ISO-8859-1 file, a file over 100 KB and an empty file. |
| Cases and expected outcomes | [`dataset/extraction-cases.jsonl`](dataset/extraction-cases.jsonl) | One line per document: `inputs.key`, `expectations.outcome` (`extracted`, `needs_review` or `duplicate`) and either the exact expected `record` or the exact set of schema error `errors`. Read by the SDK script, the Playwright spec and the offline tests. |
| Extractor | [`tools/extract_invoice_fields.py`](tools/extract_invoice_fields.py) | The body of the `extract_fields` `python_code` node. No imports (the sandbox pre-loads `re`, `json`, `datetime`). |
| Invoice schema | [`schema/invoice.schema.json`](schema/invoice.schema.json) | The Draft 2020-12 schema in the `validate_schema` node. |

The workflow itself is not a file here: the Cookbooks gallery installs it (`caliber.workflows.cookbook_document_pipeline`), and `caliber/tests/test_cookbook_document_pipeline.py` checks that the extractor and the schema above are byte-for-byte what it installs.

## Conventions

- **Keys.** A run's input is one object key, for example `incoming/inv-2026-0101-acme.txt`. The `input_bucket` node lists that exact key, so a key that is a prefix of several objects fails (`expected exactly one object ... found 2`).
- **Dataset rows.** `{"id", "tags", "note", "inputs": {"key"}, "expectations": {...}, "source_bytes"}`. A valid invoice's `expectations.record` lists only the fields the lab checks (the extractor also writes `subtotal`, `tax`, `source_key`, ...).
- **Nulls.** An absent field is `null` in the extractor's output, never a guess; the schema then names the type it wanted (`None is not of type 'number'`).
- **Adding a document.** Add the file, add a line to `extraction-cases.jsonl` with what you expect, run `caliber/.venv/bin/python -m pytest caliber/tests/test_cookbook_document_pipeline.py`, fix the extractor until it passes, and update the counts in the README.
