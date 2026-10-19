# Release Signoff Factory assets

Example inputs for the in-product **Releases** domain (**Prompt lifecycle › Releases**). The runtime record, not a filled Markdown
worksheet, is the release source of truth, and the page is a preview, record-only: signing off records a decision and does not deploy or
roll back anything.

| File | Use in CALIBER |
|---|---|
| `dataset/release-candidates.jsonl` | 20 candidates, one JSON object per line. `name`, `artifact_type`, `artifact_ref`, `version_ref`, `rollback_version`, `required_score`, `criteria` and `evidence` become the `POST /releases/candidates` body (the form fills `planned_action` and `rollback_target` from the type and the rollback version). `note` says what the row teaches; `expect` is the weighted score, status and blockers the server must produce, worked out independently. `id` is for this lab only |
| `decision/decision-record.template.md` | An optional drafting aid for the signoff rationale; the final go/no-go is an immutable signoff row |

A criterion is `{key, title, weight, score, threshold, blocking, evidence_refs}`: `weight` is greater than 0, `score` and `threshold` are
0 to 1, `key` is letters, digits, `_` and `-`. An evidence entry is `{evidence_type, evidence_ref, label}`; CALIBER stores the reference
and never opens it. Do not paste secrets into evidence references.

A go decision requires a `ready` candidate, a planned action and a rollback target. The report job (**Generate Allure**) is a snapshot of
the candidate at that moment: generate the one you intend to keep after the last waiver.
