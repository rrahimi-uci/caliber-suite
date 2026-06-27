# Source documents for SCN-04 — stand-ins + how to make real Office files

The extraction pipeline runs on **real binary Office files** (`.docx` / `.pptx`
/ `.xlsx`). Those are ZIP containers and **cannot be authored as text**, so this
folder ships **stand-in source content** you *can* read and edit:

| Stand-in file | Represents | Dataset row(s) | Expected outcome |
| --- | --- | --- | --- |
| [`invoice-clean.md`](invoice-clean.md) | clean invoice (all fields) | D01 (`.docx`) | `validation_status: pass` |
| [`invoice-clean.csv`](invoice-clean.csv) | same invoice as a sheet | D02 (`.xlsx`) | `validation_status: pass` |
| [`invoice-partial.md`](invoice-partial.md) | invoice missing `total` | D03 (`.docx`) | `partial`, `missing_fields:["total"]` |

The dataset ([`../extraction-cases.jsonl`](../extraction-cases.jsonl)) also
references three files that are **deliberately not provided as content** because
they only exist to exercise error paths — create them with the one-liners below:

- `invoice-no-vendor.docx` (D04) — copy `invoice-clean.md`, delete the
  **Vendor** line, save as `.docx`. Expect `partial`, `missing_fields:["vendor"]`.
- `invoice-legacy.doc` (D05) — a **legacy** Word doc (the unsupported-format
  negative case). See "Making the unsupported `.doc`" below.
- `invoice-corrupt.docx` (D06) — a deliberately broken `.docx`. See "Making the
  corrupt file" below.

## Dry run (no conversion) — fastest

`caliber.workflows.ingestion_tools:extract_document` reads `.md`, `.csv`, and
`.txt` as text directly. To smoke-test the **prompt + validator** wiring without
producing Office binaries, upload the `.md`/`.csv` stand-ins to the `doc-intake`
bucket and point the workflow at them. The text the structurer sees is
equivalent; only the `format` field of the extract output differs
(`markdown`/`text` instead of `docx`/`xlsx`). The golden/partial assertions in
the dataset still hold.

> Note: a dry run does **not** cover the negative cases (D05/D06) — those need a
> real legacy `.doc` and a real corrupt `.docx` (see below).

## Make the real Office files

### Option A — LibreOffice headless (no code; converts in place)

```sh
cd cookbooks/04-document-extraction-structuring-lab/assets/dataset/sources
# .md -> .docx  (D01, and the basis for D04 after you remove the Vendor line)
soffice --headless --convert-to docx invoice-clean.md
soffice --headless --convert-to docx invoice-partial.md
# .csv -> .xlsx (D02)
soffice --headless --convert-to xlsx invoice-clean.csv
```

### Option B — Python (`python-docx` + `openpyxl`, from the `caliber[ingest]` extra)

```python
# invoice-clean.docx (D01)
from docx import Document
doc = Document()
for line in open("invoice-clean.md", encoding="utf-8"):
    line = line.rstrip("\n")
    if line.strip().startswith("<!--"):
        break  # stop at the trailing HTML comment
    doc.add_paragraph(line)
doc.save("invoice-clean.docx")

# invoice-clean.xlsx (D02)
import csv
from openpyxl import Workbook
wb = Workbook(); ws = wb.active; ws.title = "Invoice"
with open("invoice-clean.csv", newline="", encoding="utf-8") as fh:
    for row in csv.reader(fh):
        ws.append(row)
wb.save("invoice-clean.xlsx")
```

Then land each produced file on the platform host at the **exact absolute
local path** named in the dataset's `doc_path` (e.g. the
`.../assets/dataset/sources/invoice-clean.docx` placeholder). You can also upload
the files to the `doc-intake` bucket (`Object Store → doc-intake → Upload`) for
preview, but the extractor reads them by local path, not by bucket key.

## Making the unsupported `.doc` (negative case D05)

The extractor tool does **not** raise a typed error for a `.doc` — it would read
the binary as best-effort UTF-8 (garbage). The **readable** `kind:"unsupported"`
diagnostic is produced by the **Object Store extract endpoint**, which is the
surface this case targets. Produce a genuine legacy binary `.doc` and confirm
the endpoint rejects it:

```sh
# Real legacy .doc (binary Word 97-2003), NOT a renamed .docx:
soffice --headless --convert-to doc invoice-clean.md   # -> invoice-clean.doc
mv invoice-clean.doc invoice-legacy.doc
```

Upload `invoice-legacy.doc`, then in the UI open it and click **Extract** (or
call `GET /object-store/buckets/doc-intake/object/extract?key=invoice-legacy.doc`).
Expect a readable `kind:"unsupported"` error naming the format — that is the
`unsupported_format_returns_readable_error` rule check, and the workflow run
should surface it cleanly (status `fail`) rather than crash.

> Do **not** simply rename a `.docx` to `.doc`; that produces a Zip-with-a-`.doc`
> name, not a real legacy OLE document, and won't represent the case faithfully.

## Making the corrupt file (negative case D06)

Truncate a valid `.docx` so the parser fails mid-read:

```sh
# after producing invoice-clean.docx above:
head -c 2048 invoice-clean.docx > invoice-corrupt.docx   # truncated ZIP -> parse error
```

Upload `invoice-corrupt.docx`. The `extract_document` tool raises
`IngestionError("failed to extract docx ...")`; the workflow run ends `fail`
with that message attributed to the **extract** node (not the validate node) —
which is exactly the extraction-vs-validation separation the gate asks for.
