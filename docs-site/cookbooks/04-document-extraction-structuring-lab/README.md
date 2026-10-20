# Document-to-JSON Pipeline

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery (a preview area, off by default). This lab needs no other flag. It also needs an S3-compatible object store behind the server; the setup below starts a private one.

## What this lab demonstrates

A workflow reads one invoice document from an **object-store bucket**, extracts structured JSON from it, checks the JSON against a **JSON Schema**, and then either writes it to an **output bucket** (valid) or stops at a **human approval gate** (invalid). A second person approves the document, and the approved JSON is written under `reviewed/`, or rejects it, and nothing is written. Every step is visible in the run debugger.

Nothing in the pipeline calls a model. The extractor is a deterministic `python_code` node and the check is a `data_transform` `json_schema` node, so the result is the same under the default **simulated** providers (`CALIBER_LLM_PROVIDER=fake`) as under a real one. A real model would only matter if you replaced the extractor with an agent node, which this lab does not do.

You are done when:

- all 16 valid invoices are in `doc-extracted/invoices/<invoice number>.json` and each matches the expected JSON in `assets/dataset/extraction-cases.jsonl`;
- each of the 13 invalid documents is held at `review_gate` and reports exactly the schema errors the dataset lists;
- the duplicate invoice is refused by the output bucket, and the account that started a run is refused when it tries to approve it;
- one held document is approved (its JSON lands under `reviewed/`) and one is rejected (nothing is written), and `doc-extracted` holds exactly 17 objects.

## Prerequisites and setup

Follow [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md) for a real install. For this lab the repository's disposable environment does the same thing from a checkout, with the cookbooks flag on and a private S3 endpoint (`moto_server`, in memory) behind both the Object Store console and the workflow storage backend. Run everything from the repository root.

```bash
CALIBER_APPROVER_USERS=admin,reviewer CALIBER_OPERATOR_USERS=admin,reviewer \
  scripts/cookbook-env.sh up lab04 --flags cookbooks --s3
source .tmp/cookbook-env/lab04/env.sh
```

`CALIBER_APPROVER_USERS` and `CALIBER_OPERATOR_USERS` give a second account, `reviewer`, the two platform scopes it needs: **approver** to approve or reject, **operator** to resume the run afterwards. The `--s3` option is described in the header of `scripts/cookbook-env.sh`; objects in it do not survive `scripts/cookbook-env.sh restart lab04`.

Create the `reviewer` account and a token for the admin account (the API path uses it):

```bash
curl -s -c "$CALIBER_STATE_DIR/cookies" -X POST "$CALIBER_API/auth/login" \
  -H 'content-type: application/json' -d '{"user_id":"admin","password":"admin"}'
curl -s -b "$CALIBER_STATE_DIR/cookies" -X POST "$CALIBER_API/auth/accounts" \
  -H 'content-type: application/json' -d '{"user_id":"reviewer","password":"reviewer-pass-123"}'
export CALIBER_TOKEN=$(curl -s -b "$CALIBER_STATE_DIR/cookies" -X POST "$CALIBER_API/auth/tokens" \
  -H 'content-type: application/json' \
  -d '{"name":"cookbook-04","scopes":["caliber.admin","caliber.operator","caliber.approver"]}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["token"])')
```

Check the object store and the queue are ready (expected: both `True`):

```bash
curl -s "$CALIBER_API/readiness" -H "Authorization: Bearer $CALIBER_TOKEN" | python3 -c \
 "import sys,json;d=json.load(sys.stdin)['data'];print({c['name']:(c['ready'],c['detail']) for c in d['checks'] if c['name'] in ('object_store','workflow_queue')})"
# {'object_store': (True, 'bucket listable'), 'workflow_queue': (True, 'queue draining, worker heartbeat fresh')}
```

The provider banner in the web app says the providers are simulated; that is expected and does not affect this lab.

## Dataset and sample files

`assets/dataset/sources/` holds 30 documents and `assets/dataset/extraction-cases.jsonl` holds one line per document: `id`, `tags`, `note`, `inputs.key` (the object key, `incoming/<file name>`), `source_bytes` and `expectations`. For a valid invoice `expectations.record` is the JSON the pipeline must write; for a held one `expectations.errors` is the exact set of schema error paths. The expectations were written from the document specifications, not copied from the extractor's output.

| Case | Document | Expected outcome | What it exercises |
| --- | --- | --- | --- |
| D01 | `inv-2026-0101-acme.txt` (792 B) | `invoices/INV-2026-0101.json` (USD 646.92) | Clean aligned-text invoice; currency inferred from the $ symbols. |
| D02 | `inv-2026-0102-brightline.txt` (532 B) | `invoices/BL-77120.json` (USD 4,820.5) | Item descriptions wrap onto a second line; the currency is only the code USD beside the total. |
| D03 | `inv-2026-0103-northwind.csv` (268 B) | `invoices/INV-2026-0103.json` (EUR 1,085.76) | Invoice exported as CSV: key,value header rows, then a line-item table. |
| D04 | `inv-2026-0104-kessler.txt` (465 B) | `invoices/KP-2026-554.json` (EUR 2,694.5) | Euro amounts written the European way (1.250,00); the date is '14 March 2026'. |
| D05 | `inv-2026-0105-harrow.txt` (539 B) | `invoices/HB-8812.json` (GBP 1,572.0) | Pound sterling with VAT; the number follows 'INVOICE #'. |
| D06 | `inv-2026-0106-lumen.md` (298 B) | `invoices/LW-30415.json` (USD 773.61) | Markdown invoice with a pipe table and a bold total. |
| D07 | `inv-2026-0107-tokyo.txt` (427 B) | `invoices/TP-20260317-02.json` (JPY 55,500.0) | Yen amounts without decimals. |
| D08 | `inv-2026-0108-maple.txt` (542 B) | `invoices/MLT-4410.json` (CAD 618.0) | A negative discount line; currency given by an explicit Currency: CAD row. |
| D09 | `inv-2026-0109-ridge.txt` (589 B) | `invoices/RO-2201.json` (USD 2,342.12) | Tax percentage in the label and a shipping line; the total reconciles only if both are counted. |
| D10 | `inv-2026-0110-bulkparts.txt` (1,947 B) | `invoices/BP-60012.json` (USD 422.5) | 24 line items, to show the table parser does not stop early. |
| D11 | `inv-2026-0111-mueller.txt` (353 B) | `invoices/MS-2026-031.json` (EUR 2,400.0) | Non-ASCII characters that ARE valid UTF-8 pass through intact (contrast with D22). |
| D12 | `inv-2026-0112-orchard.csv` (200 B) | `invoices/OH-3009.json` (USD 151.5) | CSV whose descriptions contain commas inside quotes. |
| D13 | `inv-2026-0113-sable.txt` (415 B) | `invoices/SBL-5521.json` (USD 1,412.4) | Fractional quantity (7.5 hours) and labels 'Vendor:' / 'Inv. No:' / 'Issued:'. |
| D14 | `inv-2026-0114-delta.txt` (456 B) | `invoices/DS-9004.json` (USD 2,189.0) | Subtotal, tax and a 'Grand Total' label; the grand total (not the subtotal) is the invoice total. |
| D15 | `inv-2026-0129-harbor.txt` (390 B) | `invoices/HM-1188.json` (USD 900.0) | A PO number and a due date sit beside the invoice number and date and must not replace them. |
| D16 | `inv-2026-0130-quillnet.txt` (294 B) | `invoices/QN-0001.json` (USD 1,000.0) | A total written without cents. |
| D17 | `inv-2026-0115-no-total.txt` (378 B) | held: errors at `total` | The document never states a total, so the extractor leaves it null and the schema requires it. |
| D18 | `inv-2026-0116-no-number.txt` (323 B) | held: errors at `invoice_number` | No invoice number anywhere in the document. |
| D19 | `inv-2026-0117-no-vendor.txt` (319 B) | held: errors at `vendor` | The page starts at the INVOICE title; no vendor name is stated. |
| D20 | `inv-2026-0118-bad-date.txt` (353 B) | held: errors at `invoice_date` | The date is prose, not a date, so it is not extracted rather than guessed. |
| D21 | `inv-2026-0119-mismatch.txt` (407 B) | held: errors at `total_reconciles` | Lines add up to 690.00 but the printed total is 1,000.00. |
| D22 | `inv-2026-0120-no-currency.txt` (408 B) | held: errors at `currency` | Plain numbers: no symbol, no code, no Currency row, so the currency is not guessed. |
| D23 | `inv-2026-0121-bad-amount.txt` (408 B) | held: errors at `line_items.1.amount`, `line_items.1.unit_price` | A line item whose price and amount are 'N/A'. |
| D24 | `inv-2026-0122-latin1.txt` (354 B) | held: errors at `encoding_clean` | Saved as ISO-8859-1, not UTF-8: the u-umlaut decodes to U+FFFD, which the encoding_clean check rejects. |
| D25 | `inv-2026-0123-oversized.txt` (120,293 B) | held: errors at `total`, `truncated` | More than 100 KB: input_bucket keeps the first 100,000 bytes and marks the object truncated, so the total at the end is cut off. |
| D26 | `purchase-order-7731.txt` (405 B) | held: all seven required fields fail (`document_type` is not `invoice`) | A purchase order with a table and a total: it looks like an invoice but is not one. |
| D27 | `meeting-notes-2026-03-12.txt` (310 B) | held: all seven required fields fail (`document_type` is not `invoice`) | Meeting notes dropped into the intake bucket by mistake. |
| D28 | `empty.txt` (0 B) | held: all seven required fields fail (`document_type` is not `invoice`) | A zero-byte object. |
| D29 | `inv-2026-0127-no-items.txt` (192 B) | held: errors at `line_items` | A total but no line items. |
| D30 | `inv-2026-0101-acme-resend.txt` (792 B) | refused: the output bucket will not overwrite `invoices/INV-2026-0101.json` | The same invoice sent twice under another file name; the output bucket refuses to overwrite invoices/INV-2026-0101.json. |

Other assets:

- [`assets/tools/extract_invoice_fields.py`](assets/tools/extract_invoice_fields.py): the body of the `extract_fields` node. Deterministic, no imports (the sandbox refuses `import` and pre-loads `re`, `json` and `datetime`), and it never invents a value: what a document does not state is `null`.
- [`assets/schema/invoice.schema.json`](assets/schema/invoice.schema.json): the Draft 2020-12 schema the `validate_schema` node applies.

Both are byte-for-byte what the Cookbooks gallery installs (a test pins this). The oversized file is above the `input_bucket` node's 100,000-byte read cap; the ISO-8859-1 file is not valid UTF-8, which the node decodes with replacement characters.

## Configuration and environment

| Setting | Value used | Why |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` (`--flags cookbooks`) | the Cookbooks gallery and install route |
| `CALIBER_WORKFLOW_STORAGE_BACKEND` / `_BUCKET` | `s3` / `caliber-workspaces` | the bucket nodes read and write through this backend; readiness probes it |
| `CALIBER_WORKFLOW_STORAGE_INTERNAL_ENDPOINT_URL` | `$CALIBER_S3_ENDPOINT` (`http://127.0.0.1:<port>`) | the private S3 endpoint |
| `CALIBER_WORKFLOW_STORAGE_FORCE_PATH_STYLE`, `_ACCESS_KEY_SOURCE`, `_SECRET_KEY_SOURCE` | `true`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | path-style addressing and credentials (`minioadmin` / `minioadmin`) |
| `CALIBER_OBJECT_STORE_ENDPOINT_URL`, `_FORCE_PATH_STYLE`, `_ACCESS_KEY_SOURCE`, `_SECRET_KEY_SOURCE` | the same endpoint and credentials | the Object Store page in the web app |
| `CALIBER_WORKFLOW_RUN_QUEUE_ENABLED`, `_CHECKPOINTING_ENABLED`, `_RUNTIME_APPROVALS_ENABLED` | `true` | a run with an approval node must be queued and must be able to pause |
| `CALIBER_OPERATOR_USERS`, `CALIBER_APPROVER_USERS` | `admin,reviewer` | the second person's platform scopes |
| `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL` | unset | the account that starts a run cannot approve it |
| `CALIBER_LLM_PROVIDER` | `fake` (simulated) | unused by this lab |

For the script: `CALIBER_BASE_URL` (the server root, set by `env.sh`), `CALIBER_TOKEN` (the admin token above) and optionally `LAB04_SUFFIX` (names are suffixed with the current time by default, so the script can be re-run).

## Run it through the API

[`cookbook_04_document_to_json_pipeline.py`](../../../sdk/caliber-sdk/examples/cookbooks/cookbook_04_document_to_json_pipeline.py) performs the whole lab with the Python SDK, checks every outcome against the dataset and exits 0 only if all of them hold. From the repository root:

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_04_document_to_json_pipeline
```

It takes about 75 seconds (30 queued runs). Expected output, trimmed (one `ok` line per check, 90 in all; ids and the port vary):

```text
== 0. prerequisites
  ok  the cookbook's configuration checks pass (blocked by: [])
  ok  the object store answers at http://127.0.0.1:62881
== 1. workspace and reviewer
== 2. buckets and documents
  created bucket doc-intake
  created bucket doc-extracted
  ok  30 documents are in doc-intake/incoming/
== 3. install the pipeline and resume it
  ok  the cookbook installs paused
  ok  the draft version validates
== 4. one run per document
  ok  D01 incoming/inv-2026-0101-acme.txt completed
  ok  D01 invoices/INV-2026-0101.json matches the expected JSON
  ...
  ok  D17 incoming/inv-2026-0115-no-total.txt is held for approval
  ok  D17 reports schema errors at exactly ['total'] (got ['total'])
  ...
  ok  D23 reports schema errors at exactly ['line_items.1.amount', 'line_items.1.unit_price'] (got [...])
== 5. a duplicate invoice is refused
  ok  it says why: ToolExecutionError: output_bucket write failed for 'invoices/INV-2026-0101.json': object already exists: 'invoices/INV-2026-0101.json'
== 6. a key that matches no object
  ok  fails clearly: ToolExecutionError: python_code node 'extract_fields' failed: ValueError: expected exactly one object under 'incoming/does-not-exist.txt', found 0
== 7. the approval gate
  ok  the account that started the run cannot approve it (HTTP 403)
  ok  D17 completes after approval and resume
  ok  reviewed/inv-2026-0115-no-total.json records the approval
  ok  D19 fails after rejection
  ok  its error code is approval_rejected
== 8. what reached the output bucket
  ok  doc-extracted holds exactly the 17 expected objects
== done
{ "documents": 30, "extracted": ["D01", ... "D16"], "held": ["D17", ... "D29"], "still_waiting": 11, ... }
```

What it does, by hand, for one document (all calls are in the SDK's `object_store`, `cookbooks` and `workflows.runs` resources):

```bash
A="Authorization: Bearer $CALIBER_TOKEN"; J='content-type: application/json'
# install the built-in pipeline (it is created paused), then resume it
curl -s -X POST "$CALIBER_API/cookbooks/04/install" -H "$A" -H "$J" \
  -d '{"name":"doc-intake-to-json-curl","acknowledge_prerequisites":true}'      # workflow.status "paused", version.status "draft"
curl -s -X PATCH "$CALIBER_API/workflows/<workflow_id>" -H "$A" -H "$J" -d '{"status":"active"}'
# one queued run per document; the run input is the object key
curl -s -X POST "$CALIBER_API/workflow-runs" -H "$A" -H "$J" \
  -d '{"workflow_version_id":"<version_id>","input":"incoming/inv-2026-0114-delta.txt"}'   # queued, then completed
curl -s "$CALIBER_API/object-store/buckets/doc-extracted/object?key=invoices/DS-9004.json" -H "$A"
# an invalid document pauses at review_gate: status waiting_approval, current_node_id review_gate
curl -s -X POST "$CALIBER_API/workflow-runs/<run_id>/approval/approve" -H "$A" -H "$J" -d '{}'
# {"detail":"the account that triggered this run cannot approve it; a second person must decide (set CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true for a single-operator install)","status_code":403}
```

The schema errors of a held run are in its events: the `workflow.run.step` event of `validate_schema` carries `output_by_port.valid` and `output_by_port.metadata.errors`. For `inv-2026-0119-mismatch.txt` it is `[{"path": "total_reconciles", "message": "False is not one of [True, None]", "validator": "enum"}]`. The approver (a different account) then calls `.../approval/approve` (or `/reject`) and, to continue after an approval, `POST /workflow-runs/<run_id>/resume`.

A valid invoice's output, `invoices/INV-2026-0101.json` (trimmed):

```json
{ "currency": "USD", "document_type": "invoice", "encoding_clean": true,
  "invoice_date": "2026-03-04", "invoice_number": "INV-2026-0101",
  "line_items": [ { "amount": 346.0, "description": "Hydraulic hose assembly, 2 m", "quantity": 4.0, "unit_price": 86.5 }, "..." ],
  "source_key": "incoming/inv-2026-0101-acme.txt", "subtotal": 599.0, "tax": 47.92,
  "total": 646.92, "total_reconciles": true, "truncated": false, "vendor": "Acme Industrial Supply Co." }
```

An approved document's output, `reviewed/inv-2026-0115-no-total.json`, is the same record plus `"review": {"decision": "approved", "schema_errors": [{"message": "None is not of type 'number'", "path": "total", "validator": "type"}]}`.

Preview is not available for this workflow: the editor's Preview (and `POST /workflow-versions/<id>/preview-run`) answers `preview blocked before execution because ordinary Preview cannot isolate these capability nodes: ... (python_code), ... (input_bucket), ... (output_bucket)`. Run it for real (queued), as above.

## Recipe: run it through the UI

[`04-document-extraction.spec.ts`](../../../caliber/caliber-ui/e2e/cookbooks/labs/04-document-extraction.spec.ts) performs these steps with real clicks. Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`.

1. **Create the reviewer account, the workspace and the membership.** Open **Administration** (Admin group): under **Accounts** enter `reviewer` as *User ID* and `reviewer-pass-123` as *Password*, click **Create account**; `reviewer` appears in *Accounts in this deployment* (the platform scopes it needs come from `CALIBER_APPROVER_USERS` / `CALIBER_OPERATOR_USERS`, set when the environment started). Click the **+** beside the workspace selector in the top bar, name the workspace `invoice-intake`, click **Create and select**. Back on **Administration**, choose the workspace under **Project for access management**, enter `reviewer`, pick **Reviewer**, click **Add member**. The **Project members** table lists `reviewer`.
2. **Create the two buckets.** **Object Store** (Agent resources): type `doc-intake` in *new-bucket-name* and click **+**, then `doc-extracted`. Both appear next to `caliber-workspaces`.
3. **Upload the sample invoices.** Open `doc-intake`, **New folder** `incoming`, **Create**, open the folder, **Upload** and select all 30 files in `assets/dataset/sources/`. The folder lists 30 objects, from `empty.txt` (0 B) to `inv-2026-0123-oversized.txt` (about 120 KB).
4. **Preview a stored document.** Click the eye action on `inv-2026-0101-acme.txt`. The dialog shows `Acme Industrial Supply Co.` and `Invoice No: INV-2026-0101`.
5. **Install the cookbook.** **Cookbooks** (Integrations): on **Document-to-JSON Pipeline** click Install, name it `doc-intake-to-json`, tick the prerequisites acknowledgement, **Install paused draft**. The Studio opens with `read_document`, `extract_fields`, `validate_schema`, `package_result`, `route`, `emit_extracted`, `write_extracted`, `review_gate`, `emit_reviewed`, `write_reviewed` and `final` in the outline; **Validate** answers *Valid.*
6. **Resume the installed workflow.** Open **Workflows**, the workflow's page, click **Resume** (it reads **Pause** afterwards), open the editor again and click **Run monitor**.
7. **Run a valid invoice.** Run input `incoming/inv-2026-0101-acme.txt`, **Run**. The badge reaches **Completed**; the Execution Debugger lists `write_extracted`. Then run `incoming/inv-2026-0107-tokyo.txt`, a yen invoice (JPY 55,500): it is valid under this version, so it also ends **Completed** and its JSON is written (you will run it again on version 2).
8. **Run the same invoice again.** Run input `incoming/inv-2026-0101-acme-resend.txt`. The run ends **Failed** with `output_bucket write failed for 'invoices/INV-2026-0101.json': object already exists`.
9. **Run an invoice that fails validation.** Run input `incoming/inv-2026-0115-no-total.txt`: the badge reads **Awaiting approval** at `review_gate`. Click **Approve**: *the account that triggered this run cannot approve it; a second person must decide* appears and the run stays put. Start a second invalid one, `incoming/inv-2026-0117-no-vendor.txt`.
10. **Approve and reject as the reviewer.** Log out, sign in as `reviewer` / `reviewer-pass-123`, select the workspace, open the workflow editor and **Run monitor**, select the first held run, **Approve**, **Resume**: it ends **Completed** (`write_reviewed` in the step map). Select the second held run and **Reject**: it ends **Failed**.
11. **Check the output bucket.** **Object Store** > `doc-extracted` > `invoices`: preview `INV-2026-0101.json` (`"invoice_number": "INV-2026-0101"`, `"total": 646.92`). `reviewed` holds `inv-2026-0115-no-total.json` and nothing for the rejected document.
12. **Publish version 1.** Sign out of `reviewer`, sign in as `admin`, select the `invoice-intake` workspace and open the workflow's editor (**Workflows** > the workflow > **Open editor**). The header reads `v1 · draft`. Click **Publish**, **Run validation**, tick the acknowledgement if it is shown, **Next**, **Next**, **Publish**. The message reads *Published.*, the header changes to `v1 · published` and a banner says the version is read-only.
13. **Change the rule, save a second version.** Finance now wants yen invoices checked by a person. Click **Restore as draft** in the banner: the editor opens `v2 · draft`. Select the `validate_schema` node, find the schema's `currency` list in the node's config and delete `"JPY"` from it, click **Save** (*Saved*), then **Publish** as in step 12: `v2 · published`.
14. **Re-run the same document on version 2.** Open **Run monitor**, run `incoming/inv-2026-0107-tokyo.txt` again. This time the badge reads **Awaiting approval** at `review_gate` (the same invoice completed under version 1), and nothing new is written to `doc-extracted`.
15. **Compare the versions.** Open the workflow's page, the **Versions** tab, pick `v1 · published` as the base and `v2 · published` to compare: the diff lists `validate_schema` as the modified node. The **Runs** tab lists the two yen runs, one **Completed** (version 1) and one **Awaiting approval** (version 2).

Run the spec against the running environment:

```bash
cd caliber/caliber-ui
CALIBER_LAB_BASE_URL="$CALIBER_URL/caliber" npx playwright test --config=playwright.labs.config.ts e2e/cookbooks/labs/04-document-extraction.spec.ts
```

The spec makes no API calls except its documented cleanup (it empties and removes the two buckets first, so it can be repeated); the `reviewer` account is created on the Administration page, and is reused when it already exists.

## Verify it worked

- **Output bucket.** `GET $CALIBER_API/object-store/buckets/doc-extracted/objects?recursive=true` lists 16 `invoices/*.json` objects plus `reviewed/inv-2026-0115-no-total.json` after the script (the web run leaves one `invoices/INV-2026-0101.json` and one `reviewed/` object).
- **Run states.** 16 runs `completed`; 11 runs `waiting_approval` at `review_gate`; the duplicate and the missing-object runs `failed` with the messages above; the approved run `completed`; the rejected run `failed` with `error_code` `approval_rejected`.
- **Each JSON.** The script compares every written record with `expectations.record`; the schema errors of every held run with `expectations.errors`.
- **Versions.** The workflow has two published versions; the yen invoice's run on v1 is `completed`, its run on v2 is `waiting_approval` at `review_gate`; the Versions-tab diff names `validate_schema`.
- **UI.** Run monitor badge, Execution Debugger step map (`read_document` > `extract_fields` > `validate_schema` > `package_result` > `route` > `emit_extracted` > `write_extracted`, or `... route` > `review_gate`), and the Object Store listing.

Offline checks (no server): `caliber/.venv/bin/python -m pytest caliber/tests/test_cookbook_document_pipeline.py` runs all 30 documents through the real sandbox and schema and the installed manifest through the real runtime over an in-process S3; `cd sdk/caliber-sdk && ../../caliber/.venv/bin/python -m pytest tests/test_cookbook_04_example.py` replays the script against a stateful stand-in that runs the same extractor and schema.

## Common errors and troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `404` with `reason_code: feature_disabled` ("Cookbooks is disabled in this deployment") | the flag is off. Start with `--flags cookbooks` (observed). |
| `GET /readiness` answers `503`, `object_store` not ready ("probe timed out after 2s"); `GET /object-store/status` shows `connected: false`; the Object Store page shows *Object store unreachable at ...* | the S3 endpoint is down. A run fails with `input_bucket list failed for 'doc-intake/incoming/...': ... Could not connect to the endpoint URL`. Start the endpoint (observed by stopping it). `restart` of the environment starts an empty one, so upload the documents again. |
| `input_bucket list failed ... bucket not found: 'doc-intake'` or `output_bucket write failed ... bucket not found: 'doc-extracted'` | a bucket was never created (or was deleted). Create it (step 2). Before this lab's fix the message said "object not found" and named the key. |
| `python_code node 'extract_fields' failed: ValueError: expected exactly one object under '<key>', found 0` | the run input is not an object key in `doc-intake`, or it is a prefix of several. Use the full key. |
| `output_bucket write failed ... object already exists` | an invoice with that number is already in `invoices/`. This is the duplicate guard. After a run, empty `doc-extracted` (or let the script do it) before re-running a document. |
| `403` "the account that triggered this run cannot approve it" | the same account started and approved the run. Use the `reviewer` account, or set `CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true` on a single-operator install. |
| `Resume failed: missing required scope; expected one of ['caliber.operator']` | the approver does not hold the operator scope. Add the account to `CALIBER_OPERATOR_USERS`, or let an operator resume the approved run (observed). |
| `404` when `reviewer` opens the run | the workflow is not in a workspace `reviewer` belongs to. Create the workflow in the lab's workspace and add `reviewer` with the Reviewer role (steps 1 and 5). |
| `cannot run workflow: synchronous real runs cannot checkpoint runtime approvals required by human approval node` | the run was started through the synchronous `/workflow-versions/<id>/run`. Submit through `POST /workflow-runs`, as the editor does. |
| *This workflow is paused* in the run monitor | the cookbook installs paused. Click **Resume** on the workflow's page. |
| Held documents report `None is not of type 'string'` for a missing field | expected: the extractor reports an absent field as `null` and the schema names the type it wanted. |
| Scores or "simulated" banners | not relevant: no model is used. |

## Limits and what is simulated

- Nothing is simulated in the pipeline itself; the only simulated parts of the environment are the language-model and evaluator providers, which this lab never calls. The object store is real S3 protocol (moto), not a production service.
- `input_bucket` reads text only: it decodes UTF-8 with replacement characters and keeps 100,000 bytes per object. Binary Office files (`.docx`, `.xlsx`) cannot flow through it; the Object Store page can still preview and extract them for inspection, but this lab does not use that path.
- The extractor understands the layouts in the corpus (aligned text, Markdown table, CSV). A new layout is a new case in the dataset and a new rule in the extractor, not something a model infers.
- After an approval, only what flows through the approval gate reaches later nodes. A node after the gate that also has an input from a node that ran before the gate is not run after the resume and the run still ends `completed`. The installed workflow avoids this on purpose: `package_result` puts the record and its schema errors in the one text that passes through the gate. Keep that shape if you extend the workflow.
- One run handles one document. A batch is a loop in your own script (as the script here does), not a feature of the node.

## Cleanup and reset

```bash
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_04_document_to_json_pipeline --cleanup
```

empties and removes `doc-intake` and `doc-extracted` and archives the `invoice-intake*` workspaces (their workflows go with them). The `reviewer` account stays (accounts are not deleted in the alpha). To reset everything:

```bash
scripts/cookbook-env.sh down lab04
rm -rf .tmp/cookbook-env/lab04
```
