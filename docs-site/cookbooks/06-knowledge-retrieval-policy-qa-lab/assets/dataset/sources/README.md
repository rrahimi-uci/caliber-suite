# Policy corpus: the cookbook 06 documents

Eleven company policies for a fictional software company (CALIBER Cloud), written to be read by a person and chunked by a build: real length (320 to 660 words), numbered sections, tables, owners, versions and effective dates. They are plain Markdown, uploaded to a bucket under `policies/`. [`../qa-eval.jsonl`](../qa-eval.jsonl) holds the 48 questions about them and, for every answerable one, the literal passage that answers it (`expected.evidence`), which `caliber/tests/test_cookbook_06_knowledge_lab.py` and the lab script check against these files, so a policy edit cannot silently orphan a question.

| File | Document ID | Domain | Effective | What is in it that a retriever must handle |
| --- | --- | --- | --- | --- |
| `refund-policy.md` | POL-BIL-004 v3.2 | billing | 2026-03-01 | Supersedes v3.1: the window went from **14 to 30 days** for charges on or after 1 March 2026; a table of charge types; Enterprise plans are deferred to an order form. |
| `refund-faq.md` | FAQ-BIL-001 | billing | last reviewed 2025-11-12 | **Stale and contradictory on purpose**: still says 14 days, and says annual plans are refunded for unused months where the policy gives no refund after day 30. |
| `information-security-policy.md` | POL-SEC-001 v5.0 | security | 2026-01-15 | MFA, 14-character passwords, AES-256 / TLS, a remediation-deadline table, a 24-hour reporting rule and a 72-hour customer-notification rule. |
| `data-retention-policy.md` | POL-SEC-007 v2.4 | security | 2025-07-01 | A retention table (audit logs 365 days next to application logs 90, authentication logs 180, backups 35); content deleted within 30 days of closure; legal holds. |
| `privacy-requests-policy.md` | POL-PRV-002 v1.3 | privacy | 2026-02-01 | A 30-day response deadline (the same number as the refund window), a 60-day extension, a 72-hour breach rule. |
| `leave-and-time-off-policy.md` | POL-HR-003 v4.1 | hr | 2026-01-01 | Parental leave raised from 12/6 to **16/8 weeks** for births on or after 1 January 2026; PTO accrual by tenure; carry-over. |
| `remote-work-policy.md` | POL-HR-010 v2.0 | hr | 2025-09-01 | Hybrid days, a one-time $600 stipend after 90 days, 20 days a year abroad; says home-office equipment is not claimed through expenses. |
| `travel-and-expense-policy.md` | POL-FIN-002 v3.0 | finance | 2026-07-01 | Supersedes v2.5: hotel **$180 to $220**, meals $65 to $75 for travel from 1 July 2026; receipts over $25; points back at the stipend above. |
| `acceptable-use-and-device-policy.md` | POL-IT-001 v3.3 | it | 2026-04-01 | A lost device is reported within **4 hours**, which overlaps the 24-hour incident rule of the security policy. |
| `support-sla-policy.md` | POL-SUP-001 v2.2 | support | 2026-05-01 | A first-response table for Standard and Premium; **no targets for Enterprise** (order form); service credits. |
| `vendor-procurement-policy.md` | POL-FIN-009 v1.5 | finance | 2025-11-01 | Quote thresholds, a signing-authority table, a 10-business-day security review. |

## What each kind of question is for

- **Table rows** (Q11, Q15 to Q18, Q20, Q34, Q38): the answer is one row of a table, next to rows with different numbers.
- **Effective dates** (Q01, Q26, Q31): the answer depends on a date, and an older rule is still in the text.
- **Overlap traps** (Q10, Q14, Q16, Q19, Q22, Q36): a number or a topic that two documents share.
- **A contradiction** (Q40, Q41): `refund-policy.md` and `refund-faq.md` disagree; a grounded answerer must show both, not blend them. The policy says to quote it over the FAQ.
- **Unanswerable** (Q44 to Q48): nothing in the corpus; and **traps** (Q42, Q43): the policy exists, the clause does not (Enterprise terms are deferred to an order file nobody uploaded).

## Loading it

`Agent resources > Object Store`: create a bucket (`policy-corpus`), a folder `policies`, upload the eleven `*.md` files (not this README). API: `POST /object-store/buckets`, then one multipart `POST /object-store/buckets/{bucket}/objects` per file with `prefix=policies/`. In [`../unreadable/`](../unreadable/) is one file no parser can read, for the build-failure scenario.
