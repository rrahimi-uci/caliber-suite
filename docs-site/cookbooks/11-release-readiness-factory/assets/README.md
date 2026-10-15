# Release Signoff Factory assets

These files are example inputs for the in-product **Releases** domain
(**Core journey › Releases**). The runtime record—not a filled Markdown
worksheet—is the release source of truth, and the page is Preview, record-only:
signing off records a decision and does not deploy or roll back anything.

| Order | Asset | File | Use in CALIBER |
|---:|---|---|---|
| 1 | Evidence references | `dataset/run-id-manifest.json` | Staging sheet for the run/trace/review references you add to the candidate's evidence JSON (`[{"evidence_type": …, "evidence_ref": …, "label": …}]`) and to each criterion's `evidence_refs`. |
| 2 | Weighted criteria | `rubric/release-rubric.json` | Translate its dimensions (stable key, weight, blocking flag) into the criteria JSON; you enter the observed `score` and `threshold` for each, and CALIBER computes the weighted score and blockers. |
| 3 | Blocker triage questions (optional) | `review/blocker-questions.json` | Question schema for an optional `release-blocker-triage` review queue that helps decide which issues are true blockers and who owns them. |
| 4 | Rationale drafter (optional) | `prompts/release-risk-summarizer.md` | Prompt that narrates evidence you supply into a draft rationale; it never scores the release. |
| 5 | Decision template | `decision/decision-record.template.md` | Optional preparation aid for the signoff rationale; the final go/no-go is an immutable signoff row. |
| 6 | Allure-compatible evidence | generated in Releases | Click **Generate Allure**; retain the report-job id and candidate snapshot SHA-256. |

Do not paste secrets into evidence references or workflow headers. A go decision
requires a ready candidate, planned action, and rollback target (the UI records the
planned action for you).
