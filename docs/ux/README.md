# UX evidence

Artifacts backing `ux-analysis-report.md`. Everything here is generated or
hand-written evidence, not plan text — the plan lives in the report.

## Structural census

`baseline-<sha>.json` / `.md` are the output of
`caliber/caliber-ui/scripts/ux-census.mjs` at that commit. Regenerate with:

```bash
cd caliber/caliber-ui
npm run ux:census                                   # Markdown to stdout
node scripts/ux-census.mjs --out ../../docs/ux/baseline-$(git rev-parse --short HEAD).json
node scripts/ux-census.mjs --diff ../../docs/ux/baseline-ff6d18c414.json
```

The census reads source only: no network, no credentials, no running app, so
it is safe to run in CI and deterministic across machines.

The committed `baseline-ff6d18c414.*` is the point-in-time record §15.1
documents. Diffing current `main` against it now reports movement (page lines
are up, from the merged Wave-1 fixes) — that is the tool working, not drift to
correct. Regenerate a new baseline when a wave completes rather than after each
PR, or the diffs stop meaning anything.

### What it does and does not establish

It measures **structure**: how many routes exist, how many pages use the shared
chrome, how heavy the largest files are, how many top-level helpers are
duplicated across the workflow surfaces, and how many mutating API call sites
the pages contain.

It measures **no user outcome**. Per the report's §14.5, component size and
call-site counts are engineering guardrails; task completion, error recovery,
evidence visibility, and forbidden-action prevention are the user outcomes, and
none of them can be read off a line count. A downward trend here is not
evidence that anything got easier to use.

Two figures need reading carefully:

- **Mutating call sites** is a coarse proxy, not §7.4's "viewer-visible
  forbidden mutations". It counts `caliberApi.<write-verb>` calls in page
  components; it does not know which scope each endpoint requires, nor whether
  the control is already gated. Turning it into a real measurement needs the
  endpoint-to-affordance matrix (UX-06's remaining half), which pairs each
  control with its endpoint's required scope.
- **Unrouted page components** reports `Overview.tsx` because it exports
  `Dashboard`. That is an alias, not a dead file. The census reports what it
  sees rather than special-casing it.

Route counts are given twice — registrations and distinct paths — because
`/login` is registered twice (unauthenticated, and as an authenticated
redirect). Both answers are defensible; picking one silently is how a census
ends up disagreeing with the document it measures.

## Ledger consistency check

`scripts/check-ux-ledger.mjs` (`npm run ux:check-ledger`) verifies §15.2's
merge-state claims against `git log`: a row marked **Landed** must cite a PR
present in the log, a row marked **In review** must not, and a row marked
**Open** must cite none. Git-only, like the census — no network, no `gh`.

It exists because those claims went stale five times in one review cycle, each
correction of one sentence leaving an adjacent one contradicting it.

**It verifies non-contradiction, not currency.** A row marked `Open` with no PR
is internally consistent even if that package shipped last week, because
nothing links a work-package id to a commit. A green result means "no row
lies", not "the ledger is up to date" — bringing it up to date is still a human
edit, and §15.2 is the only place in the report that states merge state, so it
is a single edit.

## Still outstanding from UX-00

This is the first of UX-00's deliverables. Not yet built:

- seeded viewer / operator / approver / admin fixtures;
- the five persona journeys (prompt regression, failed-workflow recovery, judge
  review, KB build + calibration, first run) at desktop and narrow widths;
- keyboard-only traversal capture;
- `evidence-limits.md` naming what the harness does not establish.

Until those exist, §14.2's **G0** gate is not met, and the structural work
behind it (UX-17 navigation, UX-19/UX-20 extraction and deletion, UX-21
wayfinding) cannot clear G4 on this evidence alone.
