# Release Signoff Factory

> **Prerequisite flags:** `CALIBER_FEATURE_COOKBOOKS=true` opens the Cookbooks gallery and `CALIBER_FEATURE_RELEASE_CANDIDATES=true` for the **Release signoff factory** section of Releases and the `/releases/candidates` API (record-only; both off by default).

## Demo objective

Create a durable release candidate, compute an evidence-backed readiness score,
resolve blockers through accountable waivers, record an immutable go/no-go
decision, and retain an Allure-compatible report.

## Feasibility & substitutions

- ⚠️ **Preview, record-only.** **Releases** (sidebar **Core journey**) shows a
  *Preview, record-only* note on the **Release signoff factory** section: signing
  off a candidate records a decision; it does **not** deploy or roll back anything
  (the release path on that page is Apply and Rollback on a prompt).
- ✅ Release candidates, weighted criteria, evidence references, blocking
  thresholds, waivers, signoffs, and rollback targets are first-class persisted
  objects on **Releases**. The planned action is stored too, but it is not a form
  field: the UI records `promote_<artifact type>` → `prod` for you (the API accepts
  any `planned_action`).
- ✅ The server recomputes the weighted score (Σ weight × score ÷ Σ weight) and
  the blockers (a `blocking` criterion whose score is below its threshold and is
  not waived); the browser does not submit a trusted aggregate. A candidate is
  `ready` only when the score reaches the required score and no blocker remains.
- ✅ Waivers and final signoff are admin-only and audited. A go decision requires
  a `ready` candidate, a planned action, and a rollback target; the signoff
  rationale needs at least 8 characters.
- ✅ **Generate Allure** creates a durable in-product job with
  Allure-compatible JSON and a SHA-256 identity for the candidate snapshot.
- Scores are entered with the criteria when the candidate is created, and there
  is no route that edits a candidate: **Re-evaluate** recomputes from the stored
  criteria and waivers. To change an observed score, create a new candidate.
- A waiver has a criterion key and a reason in the UI. The API also accepts an
  `expires_at`, but the UI has no expiry field, so write any expiry into the reason.
- The separate static Allure HTML site (**Settings › Allure Report**, produced by
  the test tooling) remains CI evidence; it is not required to create or sign a
  release candidate.

## Prerequisites & seed

- Evaluation, review, and trace evidence references from prior Cookbooks.
- A named artifact/version and a rollback version.
- The operator role to create, evaluate and generate reports; the admin role to
  record a waiver or sign off.

## Recipe

1. With `CALIBER_FEATURE_RELEASE_CANDIDATES=true` set, open **Core journey › Releases** and scroll to the
   **Release signoff factory** section (it carries a Preview badge and the *Preview, record-only* note).
2. Click **New candidate**. Enter the **Release candidate name**, the artifact type
   (Workflow / Prompt / Skill / Knowledge base), the **Artifact ID or registry
   name**, the **Candidate version**, the **Rollback version**, and the required
   score (default 0.8).
3. Edit the criteria JSON (the default shows `quality` and `human_review`): stable
   keys, titles, weights, observed scores, thresholds, blocking flags, and evidence
   references. Add the evidence inventory JSON, for example
   `[{"evidence_type":"evaluation_run","evidence_ref":"EVR-...","label":"Sealed eval"}]`.
4. Click **Create and evaluate candidate**. Inspect the server-computed weighted
   score, status, blockers, evidence count, and rollback version on the candidate
   card.
5. Click **Re-evaluate** after a waiver or other change to the stored candidate;
   it recomputes the score and blockers from what is stored. Do not inflate an
   observed score to clear a blocker; create a new candidate for new evidence.
6. If policy permits an exception, an admin enters the **criterion key** and a
   reason (at least 8 characters, including any expiry and compensating control)
   and clicks **Record waiver**. Confirm the blocker clears and the audit trail
   updates.
7. Click **Generate Allure** and note the report job id the card reports.
8. An admin enters the **Signoff rationale** (at least 8 characters) and clicks
   **Sign off GO** (enabled only while the candidate is `ready`) or **Sign off
   NO-GO**. A final candidate is immutable; create a new candidate for a changed
   release package.

## Demo evidence to capture

- Candidate id, exact artifact/version, weighted score, and blocker list.
- Every criterion's evidence links and any waiver provenance.
- Planned action (as recorded) and rollback target.
- Final signoff id/decision/rationale/actor and frozen candidate snapshot.
- Report-job id, Allure-compatible results, and snapshot SHA-256.

## Done when / gate

- The candidate meets its required score and has no unwaived blocker before go.
- Every waiver has a reason and accountable admin.
- Go has both a planned action and rollback target.
- Final signoff and report evidence remain queryable after page reload.
