# INVOICE

**Invoice ID:** INV-2026-0099
**Vendor:** Contoso Office Supplies
**Issue Date:** 2026-05-10
**Currency:** USD

Bill To: Caliber Demo Co., 500 Market St, Seattle WA

| # | Description                  | Quantity | Unit Price | Amount  |
|---|------------------------------|----------|------------|---------|
| 1 | Managed print service (May)  | 1        | 300.00     | 300.00  |
| 2 | Toner cartridge (black)      | 2        | 45.00      | 90.00   |

Notes: This is a draft invoice. The grand total has NOT yet been finalized and
is intentionally omitted from this document — do not compute or infer it.

<!--
STAND-IN for the EDGE / missing-field case (dataset row D03).
Required fields invoice_id, vendor, line_items[], currency, issue_date are
present and verifiable, but `total` is deliberately absent. The doc-structurer
prompt must NOT invent a total; it should list "total" in missing_fields, and
validate_document_json must then return validation_status: partial with
missing_fields == ["total"]. Convert to a real .docx per sources/README.md, or
extract this .md directly for a dry run.
-->
