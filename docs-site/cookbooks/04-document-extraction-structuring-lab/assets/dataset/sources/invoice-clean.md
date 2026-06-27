# INVOICE

**Invoice ID:** INV-2026-0042
**Vendor:** Northwind Traders, Inc.
**Issue Date:** 2026-05-01
**Due Date:** 2026-05-31
**Currency:** USD

Bill To: Caliber Demo Co., 500 Market St, Seattle WA

| # | Description                       | Quantity | Unit Price | Amount  |
|---|-----------------------------------|----------|------------|---------|
| 1 | Standard Widget (part WGT-100)    | 10       | 12.50      | 125.00  |
| 2 | Premium Widget (part WGT-200)     | 5        | 30.00      | 150.00  |
| 3 | On-site installation (per hour)   | 4        | 95.00      | 380.00  |

Subtotal: 655.00
Tax (8%): 52.40
**Total: 707.40 USD**

Payment terms: Net 30. Please reference the invoice ID on remittance.

<!--
STAND-IN for the GOLDEN case (dataset rows D01/D02).
Every required field of schema/extracted-fields.schema.json is present and
verifiable: invoice_id, vendor, line_items[] (3 rows), total (707.40),
currency (USD), issue_date (2026-05-01). Expected validation_status: pass.
Convert to a real .docx (D01) and .xlsx (D02) per sources/README.md before the
live extract path; for a dry run you can extract this .md directly.
-->
