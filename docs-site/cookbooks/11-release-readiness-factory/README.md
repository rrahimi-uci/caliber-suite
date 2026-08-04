# Release Signoff Factory

## Demo objective

Create a durable release candidate, compute an evidence-backed readiness score,
resolve blockers through accountable waivers, record an immutable go/no-go
decision, and retain an Allure-compatible report.

## Feasibility & substitutions

- ✅ Release candidates, weighted criteria, evidence references, blocking
  thresholds, waivers, signoffs, planned actions, and rollback targets are
  first-class persisted objects on **Releases**.
- ✅ The server recomputes the weighted score and blockers; the browser does not
  submit a trusted aggregate.
- ✅ Waivers and final signoff are admin-only and audited. A go decision requires
  a ready candidate, planned action, and rollback target.
- ✅ **Generate Allure evidence** creates a durable in-product job with
  Allure-compatible JSON and a SHA-256 identity for the candidate snapshot.
- The separate static Allure HTML site remains CI evidence; it is not required
  to create or sign a release candidate.

## Prerequisites & seed

- Evaluation, review, and trace evidence references from prior Cookbooks.
- A named artifact/version, planned release action, and rollback target.

## Recipe

1. Open **Operate → Releases → Release Signoff Factory**.
2. Enter the candidate name, artifact type/reference/version, required score,
   planned action JSON, and rollback-target JSON.
3. Add criteria JSON with stable keys, weights, observed scores, thresholds,
   blocking flags, and evidence references. Add the evidence inventory JSON.
4. Click **Create candidate**. Inspect the server-computed weighted score,
   blockers, evidence, action, and rollback target.
5. Click **Re-evaluate** after evidence changes. Do not inflate an observed score
   to clear a blocker.
6. If policy permits an exception, an admin records a criterion-specific waiver
   reason and optional expiry. Confirm the blocker and audit state update.
7. Click **Generate Allure evidence** and inspect the completed durable job.
8. An admin records **Sign go** or **Sign no-go** with rationale. A final candidate
   is immutable; create a new candidate for a changed release package.

## Demo evidence to capture

- Candidate id, exact artifact/version, weighted score, and blocker list.
- Every criterion's evidence links and any waiver provenance.
- Planned action and rollback target.
- Final signoff id/decision/rationale/actor and frozen candidate snapshot.
- Report-job id, Allure-compatible results, and snapshot SHA-256.

## Done when / gate

- The candidate meets its required score and has no unwaived blocker before go.
- Every waiver has a reason and accountable admin.
- Go has both a planned action and rollback target.
- Final signoff and report evidence remain queryable after page reload.
