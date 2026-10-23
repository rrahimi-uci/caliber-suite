# Source documents for cookbook 04

Thirty documents, one per case of [`../extraction-cases.jsonl`](../extraction-cases.jsonl) (D01 to D30). Upload the **30 documents, not this
README**, to the `doc-intake` bucket under `incoming/` (the lab README's UI step 3 and the SDK script both do exactly that).

They are plain text, so a person can read and edit every one:

- 16 valid invoices (`D01` to `D16`): aligned text, a Markdown pipe table and two CSV exports, in USD, EUR, GBP, JPY and CAD, with wrapped item
  descriptions, European number formats, a negative discount line, a 24-line invoice, and a PO number and due date that must not replace the
  invoice number and date.
- 13 documents the pipeline must hold for a person (`D17` to `D29`): a missing total, number, vendor or currency; a date written as prose; a printed
  total that does not match its lines; a line item priced `N/A`; an ISO-8859-1 file; a file over 100 KB; a purchase order, meeting notes and an empty
  file that are not invoices; and an invoice with no line items.
- 1 duplicate (`D30`, `inv-2026-0101-acme-resend.txt`): the same invoice as `D01` under another file name, which the output bucket must refuse to overwrite.

The lab README's "Dataset and sample files" table lists each file with its size and the outcome the dataset expects.

The documents are deliberately **text, CSV and Markdown**. The `input_bucket` node reads text only (UTF-8, first 100,000 bytes), so binary Office
files (`.docx`, `.xlsx`) cannot flow through this pipeline and this lab ships none.
