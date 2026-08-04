# Release Signoff Factory assets

These files are example inputs for the in-product **Releases** domain. The
runtime record—not a filled Markdown worksheet—is the release source of truth.

| Order | Asset | File | Use in CALIBER |
|---:|---|---|---|
| 1 | Evidence references | `dataset/required-run-ids.jsonl` | Add the relevant run/trace/review references to the candidate evidence JSON. |
| 2 | Weighted criteria | `rubric/release-rubric.yaml` | Translate stable keys, weights, thresholds, and blocking flags into criteria JSON; CALIBER recomputes the score. |
| 3 | Decision template | `decision/decision-record.template.md` | Optional preparation aid for rationale; final go/no-go is an immutable signoff row. |
| 4 | Allure-compatible evidence | generated in Releases | Click **Generate Allure evidence**; retain the report-job id and candidate snapshot SHA-256. |

Do not paste secrets into evidence references or workflow headers. A go decision
requires a ready candidate, planned action, and rollback target.
