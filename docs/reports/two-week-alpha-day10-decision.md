---
audience:
  - decision-maker
  - architect
  - developer
doc_type: strategy
product_area: strategy
stability: draft
summary: Day 10 of docs/two-week-alpha-plan.md -- everything short of cutting the actual release tag. Reproduces CI's wheel build locally, installs the built wheel into a fresh, non-editable virtualenv and proves the real ASGI app (API routes plus the bundled SPA, including Day 6-8's Diagnosis tab) starts and serves from it, re-verifies the specific "wheel shipped without a bundled SPA" failure mode is not currently happening, and publishes the scope-and-limitations note. Explicitly does not create, push, or publish any git tag or GitHub Release -- that is the repo owner's own deliberate, explicit decision, not an oversight.
prerequisites:
  - Read docs/two-week-alpha-plan.md's Week 2 table (Day 10 row) and "What makes this an alpha release, not just a demo" for the deliverable and acceptance this record satisfies
  - Read docs/reports/two-week-alpha-day1-decision.md through -day9-decision.md for everything this slice builds on
  - Read docs/reports/two-week-alpha-release-notes.md, the scope-and-limitations note this slice publishes
tags:
  - roadmap
  - alpha
  - two-week-plan
---

# Two-week alpha plan -- Day 10 decision record

Last updated: 2026-09-20

## What this delivers, and what it deliberately does not

The plan's own Week 2 table, Day 10 row: "Cut the release: git tag, wheel
build from the tag, a second person installs from the wheel/tag (not the
dev checkout) and runs the journey; publish the one-page scope note
alongside the tag." Acceptance: "A second person reproduces the journey from
the tagged wheel using only the scope note and README."

**This slice deliberately stops short of the tag itself.** The repo owner
was asked directly whether this session should create and push the git tag,
build the release wheel from it, and have "a second person" verify it, and
explicitly decided those three actions are theirs to perform personally:
they are consequential, externally-visible actions (a public tag, a release
artifact, a claimed second-person verification), and the "second person"
requirement structurally cannot be satisfied by an AI agent claiming to be a
second person -- doing so would produce exactly the kind of fabricated
verification this whole plan's "honestly scoped in writing" principle
exists to prevent. This record documents everything that can be genuinely
verified up to that boundary, and lists precisely what remains.

## What was actually verified, and how

All commands below were run from a fresh worktree at commit
`a553384f6aacaa0aa383237eb5a72167a2f7ad86` (`origin/main` at the time this
slice started -- Day 9's own merge commit, #426), reproducing
`.github/workflows/ci.yml`'s `package` ("Wheel build (with bundled SPA)")
job step by step, not a paraphrase of it.

### 1. The SPA build (CI's "Rebuild the SPA" step)

```
cd caliber/caliber-ui && npm ci && npm run build
```

Succeeded (`vite build`, ~6s). The only warning
(`INEFFECTIVE_DYNAMIC_IMPORT` on `caliberApi.ts`) is the same pre-existing,
already-documented warning Day 6-8's own decision record names -- not new.

Directly confirmed the built `dist/assets/Prompts-*.js` chunk contains Day
6-8's Diagnosis tab source (`grep -l "PromptDiagnosisTab\|No flagged items
for this prompt" dist/assets/Prompts-*.js` matched), before packaging
anything -- proving the SPA build this session produced genuinely includes
the newest UI slice, not a stale cached bundle.

### 2. Staging + build (CI's "Stage SPA into Python package" + "Build sdist + wheel" steps)

```
cd caliber
test -f caliber-ui/dist/index.html
mkdir -p src/caliber/ui && cp -R caliber-ui/dist/. src/caliber/ui/
uv venv --python 3.11 .venv-build          # matches CI's actions/setup-python@v7 python-version: "3.11"
uv pip install --python .venv-build/bin/python build twine
.venv-build/bin/python -m build
```

Produced both `caliber_suite-0.1.0.dev0-py3-none-any.whl` (13,355,684
bytes) and `caliber_suite-0.1.0.dev0.tar.gz` (25,518,650 bytes) in
`caliber/dist/`.

### 3. Distribution validation (CI's "Check distribution metadata and contents" step, reproduced verbatim)

```
.venv-build/bin/python -m twine check dist/*
```
-> `PASSED` for both the wheel and the sdist.

The exact Python content-assertion block CI's `package` job runs inline was
copied verbatim into a scratch script and run against the same `dist/`:
confirms the wheel contains `caliber/ui/index.html`, at least one
`caliber/ui/assets/*` file, and both required architecture-doc files
(`caliber/ui/docs/m-00-layered-architecture.{html,md}`); confirms the sdist
contains none of the forbidden local-cache/evidence paths (`node_modules`,
`.pytest_cache`, `.venv*`, etc.). Result: `validated
caliber_suite-0.1.0.dev0-py3-none-any.whl and
caliber_suite-0.1.0.dev0.tar.gz` (742 wheel entries, 1718 sdist entries).

**Beyond what CI checks:** this slice added one extra assertion CI does not
make -- that the wheel's bundled `Prompts-*.js` chunk specifically contains
the Diagnosis tab's own rendered string (`"No flagged items for this prompt
yet"`). This directly re-verifies, on the actual packaged artifact rather
than the pre-staging `dist/` directory, that Day 6-8's UI slice is really in
what the wheel ships -- confirmed present.

### 4. Re-verifying the known "wheel shipped without the bundled SPA" failure mode is not happening

`docs/reports/product-completeness-report.md` §3c records a real,
previously-shipped defect: `[tool.hatch.build.targets.wheel]` had the
`artifacts = [...]` prose comment but not the key itself, so hatchling
honored `.gitignore` and silently dropped `src/caliber/ui` from the wheel
for **12 consecutive CI runs**, undetected, because the one check that
would have caught it runs last, behind six other jobs. Reading
`caliber/pyproject.toml`'s current `[tool.hatch.build.targets.wheel]` block
today confirms the fix is in place (`artifacts = ["src/caliber/ui/**"]` is
present, not just described in a comment -- the comment now narrates the
fix, including this exact incident, rather than a behavior that was never
implemented). Step 3 above is a live, current re-verification of that fix,
not a re-reading of the config: the built wheel genuinely contains the SPA,
confirmed by opening the real archive and reading real entries, not by
trusting the config.

### 5. Installing the built wheel into a fresh, non-editable virtualenv

This is the substance of Day 10's own acceptance criterion this session can
verify directly, short of a literal second machine.

```
uv venv --python 3.11 <scratch>/caliber-wheel-install-test
uv pip install --python <scratch>/caliber-wheel-install-test/bin/python \
    dist/caliber_suite-0.1.0.dev0-py3-none-any.whl
```

No `-e`, no editable install, a virtualenv independent of this worktree's
own `.venv`. Confirmed genuinely non-editable: no `__editable__` shim, no
`.pth` redirect back to this checkout -- `site-packages/caliber/` is a real,
independent copy of the package, and
`importlib.metadata.version("caliber-suite")` reports `0.1.0.dev0` (this
dev-cycle's pre-tag version; a real tag build would report the tag's own
version once the repo owner bumps `pyproject.toml`'s `version` as part of
cutting the release -- see "Remaining handoff steps" below).

**A genuine, unplanned finding:** `uvicorn` is not declared anywhere in
`pyproject.toml` (checked directly -- no `dependencies` entry, no extra).
The base wheel install nonetheless pulled in `uvicorn==0.53.0` as a
transitive dependency (through the `mcp`/`sse-starlette` dependency chain).
This means the README's own documented entrypoint,
```
uvicorn caliber.server:create_app --factory --host 127.0.0.1 --port 5001
```
is runnable from the **base wheel alone**, no extras -- which was not
obvious from reading `pyproject.toml` in isolation and is worth the repo
owner's attention: it currently works by transitive luck, not an explicit
declaration, so an unrelated dependency change could silently break the
one command the README tells a new installer to run.

### 6. Starting the real ASGI app from the installed wheel and proving it serves

```
CALIBER_DATABASE_URL=sqlite:///<scratch>/wheel-install-test.db \
CALIBER_AUTH_SESSION_COOKIE_SECURE=false \
CALIBER_AUTH_BOOTSTRAP_ALLOW_INSECURE_DEFAULT=true \
MLFLOW_TRACKING_URI=http://127.0.0.1:59999 \
<scratch>/caliber-wheel-install-test/bin/uvicorn caliber.server:create_app \
    --factory --host 127.0.0.1 --port 5911
```

(`MLFLOW_TRACKING_URI` points at an intentionally unbound port -- this check
is about whether the CALIBER app itself boots and serves from the wheel, not
about standing up a live MLflow server, which is a separate, already-proven
concern per Day 9's own record.)

After the app finished its startup sequence (~25s, migrations + worker
bootstrap), direct `curl` probes against the running process confirmed, all
against `127.0.0.1:5911`:

| Check | Result |
| --- | --- |
| `GET /caliber/` (SPA shell) | `200`, real `<!doctype html>` CALIBER shell |
| `GET /caliber/assets/Prompts-qKL4Am7o.js` | `200`; body contains `"No flagged items for this prompt yet"` -- the Diagnosis tab, genuinely served from the installed wheel, not the dev checkout |
| `GET /caliber/docs/index.html` (packaged docs bundle) | `200` |
| `GET /ajax-api/2.0/mlflow/caliber/csrf` (real API route) | `200`, genuine JSON body (`{"data":{"enabled":false,"token":null,"ttl_seconds":0}}`) -- proves the backend API, not just static files, is live |
| `python -c "import caliber; print(caliber.__file__)"` inside the venv | resolves to `.../site-packages/caliber/__init__.py` -- the installed copy, not this worktree's `src/` |

The process was then stopped (`pkill`) once verification completed; no
server was left running.

**What this proves:** the built wheel is genuinely installable outside a
dev checkout, and the documented entrypoint the README gives a new
installer starts the real application -- API and bundled SPA (including
Day 6-8's Diagnosis tab) both -- from that install alone.

**What this does not prove, and is not claimed to prove:** that the
Diagnosis-tab-to-Apply-to-rollback *journey itself* runs correctly from
this wheel install. That would require standing up MLflow, seeding the Day
1 fixture, and driving the full browser journey against this
freshly-installed copy -- structurally the same shape of work Day 9's own
Playwright journey already did against the dev checkout. Re-running that
full seeded journey against a wheel install, with a real second person at
the keyboard, is exactly the verification the repo owner reserved for
themselves (see below), not a gap this session overlooked.

## Remaining handoff steps for the repo owner

Everything below is intentionally **not done** by this session, per the
explicit instruction that follows from the repo owner's own decision:

1. **Bump the release version.** `caliber/pyproject.toml`'s
   `[project].version` is currently `0.1.0.dev0`. Update it to the intended
   alpha version (e.g. `0.1.0a1`) in a small commit before tagging, so the
   built wheel's own metadata reflects the tag it ships from rather than a
   `.dev0` placeholder.
2. **Create and push the git tag**, e.g.:
   ```
   git tag v0.1.0-alpha.1 a553384f6aacaa0aa383237eb5a72167a2f7ad86
   git push origin v0.1.0-alpha.1
   ```
   (or the current tip of `main` at the time of tagging, if this PR and any
   version-bump commit have landed by then -- use `git rev-parse main` to
   confirm the exact commit before tagging).
3. **Build the release wheel from that tag.** No tag-triggered release
   workflow exists in this repository today -- checked directly: none of
   `.github/workflows/{ci,codeql,pages,trustabl}.yml` trigger on `push:
   tags:`; `ci.yml`'s own `package` job runs only on `push`/`pull_request`
   against `main`. The repo owner can either (a) check out the tag locally
   and run the exact sequence Step 1-3 of "What was actually verified"
   above reproduces, or (b) add a tag-triggered workflow that runs the same
   `package` job steps and uploads the wheel as a release asset -- a
   reasonable follow-up, but a scope decision for the repo owner, not
   something this slice should add unasked.
4. **Have an actual second person install from that tagged wheel/release
   asset and run the full journey**, using only
   `docs/reports/two-week-alpha-release-notes.md` and the README -- the one
   piece of Day 10's acceptance criterion that structurally requires a
   second human, which this session cannot supply.
5. **Publish/attach the scope note alongside the tag** (e.g. as the GitHub
   Release body, or linked from it) --
   `docs/reports/two-week-alpha-release-notes.md`, added by this slice's PR
   (merged once the repo owner reviews and merges it, per this repo's own
   review process), is ready to be pointed to or pasted in as-is once on
   `main`.

## Validation performed for this slice's own changes

This slice adds two new files under `docs/reports/` and touches nothing
under `src/`, `caliber-ui/src/`, migrations, or CI config -- a
documentation/verification-only slice, matching the task's own framing.

- `git diff --check` -- clean.
- Docs contract tests (`.github/workflows/ci.yml`'s `docs-validation` job
  list): `pytest caliber/tests/test_docs_generation_contract.py
  caliber/tests/test_docs_mermaid_contract.py
  caliber/tests/test_sdk_docs_contract.py
  caliber/tests/test_cookbook_doc_contract.py
  caliber/tests/test_cookbook_steps_contract.py
  caliber/tests/test_design_principles_contract.py
  caliber/tests/test_ci_published_site_gate_contract.py
  caliber/tests/test_docs_executable_spec_contract.py --no-cov -q` --
  confirmed no regression from the two new files (they are audit/report
  documents under `docs/reports/`, not entries in
  `docs-site/build-docs.mjs`'s module manifest, so
  `test_generated_markdown_sources_define_front_matter_metadata` -- the one
  test that requires specific front-matter keys -- does not apply to them,
  matching every prior Day 1-9 decision record's own precedent).
- No `src/caliber` production code, migration, UI component, or CI config
  changed, so the backend/UI/lint/type-check/security/compose gates this
  repo's own `CLAUDE.md` lists are not applicable beyond the docs checks
  above and `git diff --check`; this mirrors the "documentation-only
  changes" carve-out those instructions themselves describe.
