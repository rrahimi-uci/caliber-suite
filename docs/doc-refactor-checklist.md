# CALIBER docs-site refactor checklist

This checklist turns [doc-refactor-proposal.md](doc-refactor-proposal.md) into an
execution plan. It is intentionally implementation-oriented: each phase names the
target outcome, the likely files to touch, and the validation needed before
considering the phase complete.

Date: 2026-08-10

## Phase 0 — confirm scope and protect current strengths

- [ ] Freeze the target information architecture for approval:
  - `Start here`
  - `Use CALIBER`
  - `Build & integrate`
  - `Operate CALIBER`
  - `Examples`
  - `Reference`
  - `Architecture`
  - `Strategy`

- [ ] Freeze the target audience coverage model:
  - developer / integrator
  - system user
  - operator / admin
  - evaluator / governance user
  - architect
  - decision-maker

- [ ] Confirm which audience needs new authored pages versus curated index pages:
  - developer / integrator → new guide pages
  - system user → new feature/task pages
  - operator / admin → new operating pages
  - evaluator / governance user → curated guide path
  - architect → curated architecture entry page
  - decision-maker → concise overview page

- [ ] Preserve the current publishing backbone:
  - `docs-site/build-docs.mjs`
  - `docs-site/docs.js`
  - `docs-site/docs.css`
  - `caliber/caliber-ui/scripts/sync-docs.mjs`
  - `caliber/tests/test_docs_generation_contract.py`
  - `caliber/tests/test_ci_published_site_gate_contract.py`

- [ ] Preserve the current hybrid authoring model until intentionally refactored:
  - generated module corpus: `m-*.html` / `m-*.md`
  - hand-authored key pages:
    - `docs-site/index.html`
    - `docs-site/interactive-layered-architecture.html`
    - `docs-site/walkthrough.html`
    - `docs-site/presentation.html`
    - `docs-site/presentation_timed.html`

- [ ] Record the current nav/content baseline before refactor:
  - current top-level sections
  - current page counts per section
  - current special pages:
    - `walkthrough.html`
    - `presentation.html`
    - `presentation_timed.html`

- [ ] Confirm that machine-readable outputs remain first-class:
  - `docs-site/llms.txt`
  - flattened `m-*.md`

Acceptance:

- [ ] no current docs publication feature is removed unintentionally
- [ ] all existing contract tests still define the minimum publication contract

## Phase 1 — add documentation metadata support

- [ ] Adopt the metadata model:
  - front matter for generated Markdown-backed pages
  - a central checked-in `docs-site/docs-site-map.json` for hand-authored HTML pages

- [ ] Add metadata support in `docs-site/build-docs.mjs` for:
  - `title`
  - `summary`
  - `audience`
  - `doc_type`
  - `product_area`
  - `prerequisites`
  - `stability`
  - `reviewed_on`
  - `version_applicability`
  - `nav_section`
  - `nav_group`
  - `nav_order`
  - `tags`

- [ ] Ensure the central site-map covers hand-authored HTML pages:
  - `docs-site/index.html`
  - `docs-site/interactive-layered-architecture.html`
  - `docs-site/walkthrough.html`
  - `docs-site/presentation.html`
  - `docs-site/presentation_timed.html`

- [ ] Define valid metadata enums:
  - audiences:
    - `developer`
    - `system-user`
    - `operator`
    - `architect`
    - `evaluator`
    - `decision-maker`
  - doc types:
    - `tutorial`
    - `how-to`
    - `concept`
    - `reference`
    - `runbook`
    - `example`
    - `strategy`
  - stability:
    - `ga`
    - `beta`
    - `experimental`
    - `internal`

- [ ] Render metadata visibly on pages where it helps:
  - audience
  - doc type
  - stability
  - last reviewed date

Likely files:

- `docs-site/build-docs.mjs`
- `docs-site/docs-site-map.json`
- `docs-site/docs.css`
- selected Markdown pages under `docs/`
- `ARCHITECTURE.md` if it must expose metadata through a compatible path

Acceptance:

- [ ] generator can parse metadata without breaking existing pages
- [ ] missing required metadata fails clearly or warns clearly
- [ ] metadata rendering is consistent across generated pages
- [ ] hand-authored HTML pages also have a single source of truth for nav/search metadata

## Phase 2 — refactor top-level information architecture

- [ ] Replace current documentation grouping model:
  - current:
    - Documentation (`Overview`)
    - Platform docs
    - REST API
    - Python SDK
    - Examples
    - Strategy & roadmap
  - target:
    - Start here
    - Use CALIBER
    - Build & integrate
    - Operate CALIBER
    - Examples
    - Reference
    - Architecture
    - Strategy

- [ ] Split current “Platform docs” pages into new destinations:
  - feature/user-facing pages → `Use CALIBER`
  - install/ops pages → `Operate CALIBER`
  - conceptual/deep subsystem pages → `Architecture`

- [ ] Split current REST and SDK material into:
  - guide pages under `Build & integrate`
  - lookup pages under `Reference`

- [ ] Keep strategy pages published but secondary in the reader flow

- [ ] Move `walkthrough.html` into the operating journey

- [ ] Remove `presentation.html` from primary navigation

- [ ] Keep `presentation_timed.html` published and off-nav as collateral

Likely files:

- `docs-site/build-docs.mjs`
- `docs-site/docs.js`
- `docs-site/index.html`
- `docs-site/docs-nav.js` (generated outcome)
- selected docs sources under `docs/`

Acceptance:

- [ ] primary navigation reflects reader job, not repository structure
- [ ] walkthrough is surfaced as an operator path
- [ ] presentation collateral is no longer in primary nav

## Phase 3 — redesign the landing page around reader intent

- [ ] Reduce landing-page scope to:
  - product overview
  - choose-your-path entry points
  - first-success actions
  - key doc areas
  - featured references
  - footer-level deeper resources

- [ ] Add role-based entry cards:
  - I want to run CALIBER
  - I want to use CALIBER
  - I want to integrate with CALIBER
  - I want to evaluate and govern with CALIBER
  - I want to understand the architecture
  - I want to decide whether CALIBER fits

- [ ] Keep design principles and FAQ only if they do not dominate first-run flow

- [ ] Reduce duplication between:
  - hero CTAs
  - documentation cards
  - playbook sections

Likely files:

- `docs-site/index.html`
- `docs-site/docs.css`
- `docs-site/docs.js`

Acceptance:

- [ ] a first-time reader can identify their path within one screen
- [ ] the landing page no longer tries to be product pitch, onboarding guide,
      and full doc index at the same time
- [ ] each primary audience has a visible starting point from the landing page

## Phase 4 — make the sidebar and pager section-aware

- [ ] Change sidebar behavior so local section content leads on section pages

- [ ] Avoid showing the full platform tree first on SDK/API pages

- [ ] Add a lower-priority “See all docs” or equivalent escape hatch

- [ ] Update previous/next navigation to prefer local section order rather than
      the fully flattened site order

- [ ] Preserve current keyboard affordances where still useful

Likely files:

- `docs-site/docs.js`
- `docs-site/docs.css`
- `docs-site/build-docs.mjs`

Acceptance:

- [ ] SDK pages feel like SDK pages, not pages buried inside platform docs
- [ ] previous/next navigation stays within the current track unless the reader
      intentionally leaves it

## Phase 5 — add real documentation search

- [ ] Generate a build-time `search-index.json`

- [ ] Index:
  - page titles
  - summaries
  - headings
  - body excerpts
  - route paths
  - SDK symbols
  - tags
  - doc type
  - audience

- [ ] Include in the search index:
  - generated Markdown-backed pages
  - hand-authored HTML pages using central site-map metadata and extracted text where needed

- [ ] Replace title-only / card-only filtering with ranked result search

- [ ] Support deep links to:
  - pages
  - headings
  - API symbols

- [ ] Group results by area where useful:
  - guides
  - reference
  - architecture
  - examples

Likely files:

- `docs-site/build-docs.mjs`
- `docs-site/docs.js`
- `docs-site/docs.css`
- generated `docs-site/search-index.json`
- `caliber/caliber-ui/scripts/sync-docs.mjs`
- contract tests

Acceptance:

- [ ] searching a route path returns the REST API reference
- [ ] searching an SDK class or method returns the relevant SDK page
- [ ] searching a concept returns the right architecture or guide page
- [ ] searching for walkthrough, overview, or presentation-adjacent collateral returns the intended published page class

## Phase 6 — improve page metadata and trust signals

- [ ] Show page-level metadata consistently:
  - audience
  - doc type
  - stability
  - reviewed date

- [ ] Add “who this is for” / “when to use this” blocks on:
  - walkthroughs
  - runbooks
  - SDK guides
  - REST/API guides
  - user-facing feature guides

- [ ] Make beta/GA labeling visible on SDK/API pages

Likely files:

- `docs-site/build-docs.mjs`
- `docs-site/docs.css`
- Markdown pages under `docs/sdk/`, `docs/api/`, and future user/operator areas

Acceptance:

- [ ] a reader can immediately tell whether a page is tutorial, guide, concept,
      runbook, or reference
- [ ] beta surfaces are labeled without reading deep prose

## Phase 7 — strengthen developer documentation

- [ ] Split developer docs into two experiences:
  - guided usage under `Build & integrate`
  - exact lookup under `Reference`

- [ ] Expand REST API docs into a true endpoint reference

- [ ] Ensure REST API pages expose:
  - method
  - path
  - auth requirement
  - request parameters
  - request/response models
  - example payloads
  - related SDK call when relevant

- [ ] Improve SDK discoverability:
  - symbol index
  - common-task shortcuts
  - stability badges
  - compatibility notes

- [ ] Add dedicated developer troubleshooting coverage:
  - auth
  - project scoping
  - CSRF
  - waiters
  - uploads
  - workflow invocation/debugging

- [ ] Add explicit developer guide pages for:
  - SDK quickstart
  - SDK vs REST API
  - auth and project scoping
  - error handling and retries
  - CI/CD automation
  - developer troubleshooting

Likely files:

- `docs/api/*.md`
- `docs/sdk/*.md`
- `docs-site/build-docs.mjs`
- `docs-site/docs.js`
- generated docs outputs

Acceptance:

- [ ] a developer can get from landing page to first SDK call in two clicks or fewer
- [ ] a developer can find an endpoint or symbol directly through search

## Phase 8 — create first-class system-user docs

- [ ] Create feature-guide pages for:
  - Prompts
  - Tools
  - Skills
  - MCP servers
  - Workflows
  - Knowledge bases
  - Evaluations
  - Calibration
  - Aria
  - Review/release flows

- [ ] Make each system-user page task-oriented rather than architecture-oriented:
  - what this feature is for
  - common tasks
  - required permissions
  - common failure modes
  - links to reference and architecture

- [ ] Keep architecture pages as linked deep dives, not first-run docs

- [ ] Ensure each feature page links to:
  - common tasks
  - related reference
  - troubleshooting
  - architecture

Likely files:

- new or refactored docs under `docs/use/`
- `docs-site/build-docs.mjs`
- `docs-site/index.html`

Acceptance:

- [ ] a system user can find feature guidance without entering architecture pages first

## Phase 9 — create first-class operator docs

- [ ] Create or refactor operator-facing pages for:
  - installation
  - local bring-up
  - provider setup
  - storage setup
  - health and readiness
  - troubleshooting
  - backup/recovery
  - operations runbook

- [ ] Add a dedicated configuration/env-var reference page for operators

- [ ] Reposition `walkthrough.html` as the primary local bring-up guide

- [ ] Clearly separate:
  - tutorial/walkthrough
  - troubleshooting
  - runbook/recovery

Likely files:

- new or refactored docs under `docs/operate/`
- `docs/runbook.md`
- `docs-site/walkthrough.html`
- landing page links and navigation

Acceptance:

- [ ] an operator can find install/config/run/recover content from one top-level path

## Phase 9A — add curated paths for architects, evaluators, and decision-makers

- [ ] Create an architecture entry page that links the existing deep corpus by reader intent:
  - topology and deployment model
  - trust model
  - execution model
  - storage/state
  - extension seams

- [ ] Create an evaluation/governance guide that connects:
  - test sets
  - evaluation runs
  - judges
  - review queues
  - QA plan
  - calibration
  - release-signoff/review paths

- [ ] Create a decision-maker overview page that explains:
  - what CALIBER is
  - what problems it solves
  - deployment models
  - trust/governance model
  - adoption path
  - where to find technical proof in deeper docs

- [ ] Ensure these pages summarize and route; they should not duplicate the deep architecture pages

Likely files:

- new docs under `docs/start/`
- new docs under `docs/architecture/`
- new governance/evaluation guide under `docs/use/`
- `docs-site/index.html`
- `docs-site/build-docs.mjs`

Acceptance:

- [ ] architects can find the right deep page without reading the full sidebar tree
- [ ] evaluators/governance readers have one curated journey
- [ ] decision-makers can understand fit and deployment posture without reading implementation detail first

## Phase 10 — improve TOC behavior

- [ ] Expand TOC generation to include:
  - `h2`
  - selected `h3`

- [ ] Avoid overlong noisy TOCs by collapsing subordinate levels when needed

- [ ] Ensure long pages such as:
  - walkthrough
  - runbook
  - SDK guide
  - API reference
  remain easy to scan

Likely files:

- `docs-site/docs.js`
- `docs-site/docs.css`

Acceptance:

- [ ] long pages are easier to navigate without scrolling the whole document

## Phase 11 — rationalize non-core collateral

- [ ] Keep publishing:
  - `presentation.html`
  - `presentation_timed.html`

- [ ] Keep them outside primary docs navigation

- [ ] Link them only from a secondary resources/about/collateral area

Likely files:

- `docs-site/build-docs.mjs`
- `docs-site/index.html`

Acceptance:

- [ ] collateral is still accessible
- [ ] collateral no longer competes with core product docs

## Phase 12 — add tests for the refactored docs system

- [ ] Add tests for metadata validity

- [ ] Add tests for required page fields

- [ ] Add tests for search-index generation

- [ ] Add tests for nav coverage:
  - no orphaned key pages
  - no collateral pages in primary nav
  - required role-path landing links exist

- [ ] Add tests for metadata/search coverage on hand-authored HTML pages

- [ ] Keep current publication and flattened-markdown contract tests green

Likely files:

- `caliber/tests/test_docs_generation_contract.py`
- new docs contract tests as needed
- possibly `caliber/tests/test_ci_published_site_gate_contract.py`

Acceptance:

- [ ] docs refactor remains generator-safe and publication-safe

## Phase 13 — validation workflow for each implementation slice

After each significant docs change:

- [ ] rebuild:
  - `node docs-site/build-docs.mjs`

- [ ] sync served/package copies:
  - `node caliber/caliber-ui/scripts/sync-docs.mjs`

- [ ] syntax/sanity check:
  - `git diff --check`

- [ ] run focused docs contracts:
  - `caliber/.venv/bin/python -m pytest caliber/tests/test_docs_generation_contract.py caliber/tests/test_ci_published_site_gate_contract.py --no-cov`

- [ ] visually verify at least:
  - landing page
  - one SDK page
  - one REST API page
  - one operator/walkthrough page
  - one mobile breakpoint

## Phase 14 — definition of done

- [ ] docs are organized around audience and task, not repo structure
- [ ] full-text documentation search exists and works
- [ ] developer guides and developer reference are clearly separated
- [ ] system-user and operator paths are first-class
- [ ] walkthrough and runbook have distinct roles
- [ ] architecture pages remain available as deep references
- [ ] collateral stays published without polluting primary navigation
- [ ] generator, sync, and publication contracts remain intact
