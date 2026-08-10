# CALIBER docs-site refactor proposal

Date: 2026-08-10

## 1. Scope and intent

This proposal is a repository-grounded review of the current `docs-site/` and a
detailed plan to make CALIBER documentation more professional, easier to
navigate, and materially more useful for the full set of intended audiences:

- developers and integrators
- system users
- operators and admins
- evaluators and governance users
- architects
- decision-makers

The goal is not to replace the current documentation stack. The current static
generator, shared design system, and publication/test pipeline are already real
assets. The goal is to keep those strengths and refactor the information
architecture, content model, and navigation so the docs feel like a product
documentation system rather than a high-quality architecture corpus.

## 2. What exists today

The current documentation system is built from:

- Markdown sources under `docs/` plus the repository-root `ARCHITECTURE.md`
- a dependency-light site generator in `docs-site/build-docs.mjs`
- shared client behavior in `docs-site/docs.js`
- shared styling in `docs-site/docs.css`
- sync into served/package copies through `caliber/caliber-ui/scripts/sync-docs.mjs`
- publication and contract validation via:
  - `caliber/tests/test_docs_generation_contract.py`
  - `caliber/tests/test_ci_published_site_gate_contract.py`

The current site is also hybrid in authorship:

- the `m-*.html` and `m-*.md` module corpus is generated from Markdown sources
- several important surfaces remain hand-authored HTML under `docs-site/`, including:
  - `index.html`
  - `interactive-layered-architecture.html`
  - `walkthrough.html`
  - `presentation.html`
  - `presentation_timed.html`

The current top-level documentation groups in the generated site are:

- Documentation
- Platform docs
- REST API
- Python SDK
- Examples
- Strategy & roadmap

Current navigable counts from `docs-site/docs-nav.js`:

- Documentation: 1 page (`Overview`)
- Platform docs: 20 pages
- REST API: 4 pages
- Python SDK: 5 pages
- Examples: 20 pages
- Strategy & roadmap: 2 pages

That is 52 navigable entries in one global navigation tree.

## 3. Current strengths

The current docs-site already has several professional qualities that should be
preserved.

### 3.1 Strong build and publishing discipline

- The core module corpus is generated from source-of-truth Markdown.
- Generated files carry provenance banners, which reduces silent drift.
- The sync pipeline copies the built site into both served and packaged UI
  locations.
- Contract tests verify:
  - generated Markdown and HTML stay in sync with source
  - flattened links resolve
  - the published Pages gate is still wired correctly

This is unusually strong documentation engineering discipline and should remain
part of the design.

Important nuance: not every page is generated today. The landing page,
interactive architecture explorer, walkthrough, and presentation collateral are
still hand-authored HTML surfaces. That is not inherently wrong, but it means the
refactor should account for a mixed generated/manual estate rather than assuming a
pure Markdown pipeline.

### 3.2 Clear documentation styling contract

`docs/STYLE.md` already defines a consistent page contract:

- plain-language intro
- `At a glance`
- signature diagram
- `Reference` tier split
- deep implementation detail below the split

That gives the docs a recognizable voice and structure.

### 3.3 High-value technical depth

The documentation is not shallow. It covers:

- platform architecture
- asset-family architecture
- REST API
- Python SDK
- cookbooks
- operations runbook
- guided walkthrough
- strategy and roadmap

For maintainers, advanced operators, and serious evaluators, this depth is a
major asset.

### 3.4 Good visual and interaction baseline

The rendered site already has:

- a polished design system
- dark mode
- keyboard-accessible search trigger
- contextual TOC
- previous/next navigation
- Mermaid support
- interactive architecture visualization
- responsive behavior

The recent toolbar cleanup improved the top-level header and should be treated as
a good baseline, not reverted.

### 3.5 Good developer-doc foundations

The SDK guide is already stronger than typical early-stage product docs:

- clear install/auth/config/error sections
- many code blocks
- content aligned to executable examples

The REST API section already distinguishes:

- overview
- auth/conventions
- resource catalog
- HTTP reference

### 3.6 Machine-readable output exists

`docs-site/llms.txt` plus flattened `m-*.md` outputs are valuable. They make the
docs consumable by automation and future AI-assisted workflows.

## 4. Current weaknesses

The weaknesses are mostly not about missing effort. They are about how strong
material is currently organized and surfaced.

### 4.1 The site is still architecture-first, not role-first

The current docs are excellent at explaining what CALIBER is internally. They are
weaker at answering:

- I am a developer. How do I integrate with this quickly?
- I am an operator. How do I install, configure, and run this safely?
- I am a system user. How do I use the product to accomplish a task?

The current top-level groups are organized mostly by internal documentation type,
not by reader job-to-be-done.

### 4.2 One global navigation tree serves very different audiences

On an SDK page, the sidebar still leads with the full 20-page Platform docs
section before the user reaches the local Python SDK section. That makes the
developer path feel secondary inside its own page.

The same issue affects REST API readers and system-user readers.

### 4.3 Search is not real full-text documentation search

Today:

- sidebar search filters only nav link titles
- landing-page search filters only visible reference cards

It does not search:

- page body content
- headings across pages
- API symbols
- endpoint paths
- class/method names
- error names

This is one of the largest practical usability gaps for developers.

### 4.4 The landing page is overloaded

The landing page currently combines:

- hero branding
- design principles
- documentation directory
- multiple playbooks
- FAQ

This is rich, but it asks too much of one page. It reads partly like product
positioning, partly like onboarding, and partly like a documentation index.

Professional documentation sites usually separate:

- orientation
- first success
- role-based paths
- reference browsing

### 4.5 The current sectioning mixes product docs with collateral

The `Examples` group in primary navigation currently includes:

- SDK recipes
- cookbook gallery
- individual cookbook pages
- guided walkthrough
- presentation

These are not the same kind of artifact.

`presentation.html` in particular is useful collateral, but it should not compete
with operational and developer documentation in primary navigation. Another
published collateral page, `presentation_timed.html`, is already off-nav and
belongs to the same collateral/media class.

### 4.6 REST API reference is still “representative”, not exhaustive

The current HTTP reference explicitly describes:

- core endpoints
- representative operations
- OpenAPI entry points

That is useful, but professional API documentation typically expects:

- endpoint-level discoverability
- request/response model visibility
- auth and error details in context
- parameter detail
- example payloads

The site currently explains the contract better than it exposes it.

### 4.7 System-user and operator documentation are under-signaled

There is a strong guided walkthrough and a strong operations runbook, but they
are not surfaced as a first-class “Operate CALIBER” track.

There is no top-level section that clearly says:

- install
- configure
- run
- troubleshoot
- recover

That path exists in fragments, but not as a clear operating journey.

### 4.8 The site lacks visible page metadata for trust and scannability

Most pages do not visibly expose metadata such as:

- audience
- doc type (`tutorial`, `how-to`, `concept`, `reference`, `runbook`)
- prerequisites
- stability
- last reviewed date
- version applicability

For a product at CALIBER’s complexity, those signals matter.

### 4.9 TOC and pager behavior are too coarse

Current behavior in `docs.js`:

- TOC indexes only `h2` sections
- page-to-page navigation is built from the flattened global docs order

Practical effect:

- deep pages lose useful `h3`/`h4` discoverability
- previous/next can jump across audience boundaries in ways that feel arbitrary

### 4.10 The site has a missing middle layer

Today the spectrum is:

- very high-level landing page
- very detailed architecture/reference pages

What is thinner than it should be:

- task-oriented how-to guides
- feature guides for system users
- admin/operator setup guides
- short “do this now” pages between overview and deep reference

### 4.11 Strategy content is valuable but too prominent for the main user journey

`Competitive analysis` and `Roadmap` are useful documents, but for the primary
developer/system-user experience they are not core day-one docs.

They should remain available, but not compete equally with integration,
installation, and operating guidance.

## 5. Diagnosis

The current docs-site is best understood as:

- a high-quality technical corpus
- with good publishing discipline
- and a polished static shell
- but without a mature audience-and-task information architecture

The refactor should therefore not start with a framework migration.

It should start with:

1. audience separation
2. task-oriented navigation
3. generated full-text search
4. better reference surfacing
5. clearer documentation artifact types

## 6. Refactor principles

The documentation refactor should follow these principles.

### 6.1 Preserve the current build pipeline

Do not replace `docs-site/build-docs.mjs` with a heavier stack as the first move.

Reasons:

- the current generator is already integrated with tests and Pages
- it already supports CALIBER’s diagrams and page contract
- the current stack is deterministic and easy to publish

Refactor the content model and navigation on top of the current stack first.

### 6.2 Organize by reader job, not repository structure

The primary top-level experience should answer:

- get started
- use the product
- integrate programmatically
- operate safely
- inspect deep reference

It should also guarantee that every primary audience has a deliberate entry
point, not just an implied place somewhere in the corpus.

### 6.3 Separate document types explicitly

A professional docs system should distinguish:

- tutorial
- how-to
- concept
- reference
- runbook
- example
- strategy/internal context

The current content already partly behaves this way; the site should make it
visible.

### 6.4 Keep deep implementation detail, but stop making it the default path

The architecture material is a strength. The site should not remove it. It
should place it behind clearer entry points and better contextual linking.

### 6.5 Build a better developer discovery surface

The docs should let a developer find:

- auth
- first SDK call
- endpoint path
- request model
- error behavior
- waiters / long-running operations
- examples

in seconds, not by browsing the whole sidebar.

## 7. Proposed future information architecture

### 7.1 Top-level structure

Proposed primary navigation:

```text
CALIBER Docs
├─ Start here
├─ Use CALIBER
├─ Build & integrate
├─ Operate CALIBER
├─ Examples
├─ Reference
├─ Architecture
└─ Strategy
```

#### 7.1.1 Start here

Purpose: orientation and first success.

Contents:

- Product overview
- Choose your path
- 5-minute quickstart
- Installation options
- “What CALIBER is / is not”
- Audience entry pages:
  - developer/integrator
  - operator/admin
  - system user
  - evaluator/governance user
  - architect
  - decision-maker

#### 7.1.2 Use CALIBER

Purpose: system-user guides for product features.

Contents:

- Prompts
- Tools
- Skills
- MCP servers
- Workflows
- Knowledge bases
- Evaluations
- Calibration
- Aria
- Review and release flows

These should be feature guides, not architecture pages.

#### 7.1.3 Build & integrate

Purpose: developer entry path.

Contents:

- SDK quickstart
- SDK guides
- REST API guides
- Plugin guide
- Auth and scoping
- Workflow-as-service integration
- API/SDK compatibility and stability

#### 7.1.4 Operate CALIBER

Purpose: admin/operator path.

Contents:

- Install and run
- Configuration
- Environment variables and provider setup
- Health checks
- Backup/recovery
- Operations runbook
- Troubleshooting

The current walkthrough should move here.

#### 7.1.5 Examples

Purpose: scenario-driven learning.

Contents:

- Cookbook gallery
- SDK recipes
- End-to-end product workflows
- Role-based example journeys

Do not place presentations in this section.

#### 7.1.6 Reference

Purpose: lookup material.

Contents:

- REST API endpoint reference
- SDK API reference
- Workflow component catalog
- Config/env-var reference
- Error/reference tables
- Capability/stability matrix

#### 7.1.7 Architecture

Purpose: conceptual and implementation-deep understanding.

Contents:

- Layered architecture
- Interactive architecture
- Platform architecture
- Refinement loop
- Deep subsystem architecture pages

#### 7.1.8 Strategy

Purpose: non-core but valuable context.

Contents:

- Competitive analysis
- Roadmap

This should be reachable, but visually secondary.

### 7.2 Mapping from current sections

| Current area | Proposed destination |
| --- | --- |
| `Documentation` (`Overview`) | `Start here` |
| `Platform docs` | split between `Use CALIBER`, `Operate CALIBER`, and `Architecture` |
| `REST API` | `Build & integrate` + `Reference` |
| `Python SDK` | `Build & integrate` + `Reference` |
| `Examples` | keep as `Examples`, but remove collateral and split walkthrough into `Operate CALIBER` |
| `Strategy & roadmap` | keep as `Strategy`, secondary in navigation |

## 8. Proposed content model

### 8.1 Adopt an explicit doc-type model

Every documentation page should declare metadata such as:

- `title`
- `summary`
- `audience`
- `doc_type`
- `product_area`
- `prerequisites`
- `stability`
- `reviewed_on`
- `version_applicability`
- `tags`
- `nav_section`
- `nav_group`
- `nav_order`

Recommended `doc_type` values:

- `tutorial`
- `how-to`
- `concept`
- `reference`
- `runbook`
- `example`
- `strategy`

Recommended `audience` values:

- `developer`
- `system-user`
- `operator`
- `architect`
- `evaluator`
- `decision-maker`

### 8.2 Use front matter plus a thin site map

Today the module manifest is hard-coded in `build-docs.mjs`. That is manageable
for the current size, but it will become increasingly rigid as the docs grow.

Recommended approach:

- use front matter for generated Markdown-backed pages
- keep a thin central site-map file at `docs-site/docs-site-map.json` for:
  - top-level grouping
  - ordering overrides
  - metadata for hand-authored HTML pages such as:
    - `index.html`
    - `interactive-layered-architecture.html`
    - `walkthrough.html`
    - `presentation.html`
    - `presentation_timed.html`

This keeps the generator deterministic without making `build-docs.mjs` the place
where every page fact must be edited.

It also matches the current hybrid repository model: Markdown-backed modules can
carry their own metadata, while hand-authored HTML pages still need one checked-
in source of truth for navigation, search inclusion, and audience labeling.

### 8.3 Audience-coverage requirement

The refactor should explicitly distinguish two different actions:

- reorganizing existing content
- authoring missing audience-facing content

Both are required. Reorganization alone will not make the docs complete for all
audiences.

Required audience coverage after the refactor:

| Audience | Current state | Required additions |
| --- | --- | --- |
| Developer / integrator | Strong technical depth, but fragmented first-run path. | Add a short integration path: SDK quickstart, API vs SDK choice guide, auth/project-scoping guide, error/retry guide, CI/CD guide, and developer troubleshooting. |
| System user | Weakest current audience; feature detail exists mostly as architecture, not user guidance. | Add task-oriented feature guides for prompts, tools, skills, MCP, workflows, knowledge bases, evaluations, calibration, Aria, and review/release. |
| Operator / admin | Strong detail exists, but fragmented across walkthrough, runbook, and subsystem pages. | Add a coherent operating track: install, local bring-up, config, provider setup, storage, health, troubleshooting, backup/recovery, runbook. |
| Architect | Strong deep architecture already exists. | Add an architecture entry/index page that explains where to start: topology, trust model, execution model, storage, and extension seams. |
| Evaluator / governance user | Strong deep material exists, but not as a curated path. | Add a trust/governance guide that connects test sets, evaluation, judges, review queues, QA plan, calibration, and release-signoff flows. |
| Decision-maker | Only partial coverage today; the root architecture is still too technical for this role. | Add a concise overview that explains what CALIBER is, where it fits, deployment models, trust model, adoption path, and where to go for technical proof. |

This means the project should plan to create some new pages, some role-home
pages, and some curated index pages that connect existing deep references.

### 8.4 Introduce page templates by doc type

### Tutorial template

Use for:

- quickstart
- install first-run
- first SDK integration

Structure:

1. goal
2. prerequisites
3. estimated time
4. steps
5. success check
6. next steps
7. troubleshooting

### How-to template

Use for:

- create a workflow
- connect an MCP server
- publish a workflow service

Structure:

1. task summary
2. when to use this
3. prerequisites
4. steps
5. verification
6. related reference

### Concept template

Use for:

- layered architecture
- refinement loop
- product mental models

Structure:

1. what it is
2. why it exists
3. key concepts
4. diagram
5. related tasks
6. deep reference

### Reference template

Use for:

- API endpoints
- SDK symbols
- workflow components
- environment variables

Structure:

1. concise summary
2. lookup table / index
3. examples
4. compatibility/stability
5. related guides

### Runbook template

Use for:

- queue failures
- release rollback
- webhook dead-lettering

Structure:

1. symptoms
2. severity
3. evidence to gather
4. recovery steps
5. what not to assume
6. escalation path

## 9. Proposed navigation and UX changes

### 9.1 Replace the current global sidebar with a sectional sidebar model

Recommended behavior:

- primary header chooses the top-level doc area
- sidebar then shows:
  - local section pages first
  - optionally a collapsed “See all docs” group below

Example on an SDK page:

- sidebar should lead with SDK pages and developer guides
- Platform docs should not occupy the first screenful

### 9.2 Add a real generated search index

Current search is only filtering titles/cards.

Recommended:

- generate a `search-index.json` at build time
- index:
  - page title
  - summary
  - headings
  - body excerpt
  - API symbols
  - route paths
  - tags
- cover both:
  - generated Markdown-backed pages
  - hand-authored HTML pages via the central site-map or extracted text
- support:
  - keyboard search
  - ranked results
  - result grouping by doc area
  - heading-level deep links

This is the highest-leverage UX improvement for developers.

### 9.3 Make TOC hierarchical

Current TOC behavior is `h2` only.

Recommended:

- include `h2` and selected `h3`
- collapse/expand long TOCs
- preserve current sticky behavior

This is especially important for:

- SDK reference
- REST API reference
- walkthrough
- runbook

### 9.4 Make page-to-page navigation section-aware

Current pager walks the flattened global nav order.

Recommended:

- previous/next should default to the local section
- optionally provide “next in this track”
- optionally provide “related concept/reference”

That prevents developer pages from feeling chained to unrelated architecture
pages.

### 9.5 Remove collateral from primary documentation flow

Recommended:

- remove `presentation.html` from main docs navigation
- keep it published, but link it from:
  - a secondary “Resources” or “About” area
  - release notes / marketing collateral section

`presentation_timed.html` is already off the main nav today and should remain a
secondary collateral page rather than being promoted into the main reader flow.

### 9.6 Improve landing page structure

Proposed landing page sections:

1. concise product overview
2. choose your path
3. first-success cards
4. key doc areas
5. featured references
6. footer links to architecture and strategy

Recommended role-path cards:

- I want to run CALIBER
- I want to use CALIBER
- I want to integrate with CALIBER
- I want to evaluate and govern with CALIBER
- I want to understand the architecture
- I want to decide whether CALIBER fits

The current design principles and FAQ can remain, but should move lower and stop
competing with onboarding.

## 10. Proposed developer-doc improvements

### 10.1 Split “Build & integrate” into guides and reference

Developers need two different experiences:

- guided usage
- exact contract lookup

Recommended structure:

```text
Build & integrate
├─ SDK quickstart
├─ SDK guides
├─ REST API quickstart
├─ Auth and project scoping
├─ Workflow service integration
├─ Plugin development
└─ Compatibility and stability

Reference
├─ SDK API reference
├─ SDK symbol index
├─ REST API endpoint reference
├─ OpenAPI entry points
├─ Error model
└─ Config/env var reference
```

### 10.2 Turn REST API reference into a real endpoint reference

Current strength:

- it explains the contract shape well

Current weakness:

- it is still representative rather than exhaustive

Recommended:

- generate or ingest endpoint metadata from the served OpenAPI contract
- expose:
  - path
  - method
  - summary
  - auth requirement
  - parameters
  - request body
  - response models
  - example request/response
  - related SDK call

### 10.3 Make SDK reference more discoverable

Recommended:

- dedicated symbol index page
- grouped module pages
- search hits on class/method/model names
- “common tasks” shortcuts:
  - authenticate
  - create prompt version
  - run workflow
  - wait for job
  - upload file
  - invoke service

### 10.4 Show stability and compatibility clearly

Add visible badges and metadata for:

- GA
- beta
- experimental
- internal
- server compatibility where relevant

This is especially important because CALIBER already distinguishes stable and
less-stable SDK/API surfaces.

### 10.5 Add a developer troubleshooting layer

Recommended pages:

- auth failures
- project scoping issues
- CSRF behavior
- long-running waiter behavior
- file upload constraints
- workflow invocation debugging

Right now these details exist inside deep guides, but not as a dedicated
troubleshooting surface.

## 11. Proposed system-user and operator doc improvements

### 11.1 Create a first-class “Use CALIBER” area

These pages should explain product features from the user’s perspective, not the
codebase’s.

Recommended feature guides:

- Workflows Studio
- Prompts
- Tools
- Skills
- MCP servers
- Knowledge bases
- Evaluations and judges
- Calibration
- Aria
- Review and release flows

Each page should link to:

- common tasks
- permissions/roles
- troubleshooting
- deep architecture

### 11.2 Create a first-class “Operate CALIBER” area

Recommended structure:

- Install CALIBER
- Local development setup
- Provider configuration
- Storage configuration
- Health and readiness
- Troubleshooting
- Operations runbook
- Backup/recovery

Move the guided walkthrough here and treat it as the primary local bring-up path.

### 11.3 Separate “runbook” from “tutorial”

Current walkthrough and runbook are both good, but they solve different jobs:

- walkthrough = learn and bring up the product
- runbook = recover from failure

The new IA should make that distinction obvious.

### 11.4 Add “what this page is for” metadata

For system users especially, every major page should visibly say:

- who should read it
- what task it helps with
- whether it is a tutorial, guide, or reference

That reduces cognitive load immediately.

## 12. Proposed content cleanup and repositioning

### 12.1 Keep architecture pages, but de-emphasize them as first-run docs

Current architecture pages are strong and should remain.

But the primary path for most readers should be:

- tutorial / quickstart
- task guide
- reference
- architecture deep dive

not the reverse.

### 12.2 Reclassify non-core collateral

Recommended treatment:

- `presentation.html`
  - keep published
  - remove from primary docs nav
- `presentation_timed.html`
  - keep published
  - keep off-nav
  - treat as media/collateral
- strategy documents
  - keep published
  - move to secondary navigation or footer-level area

### 12.3 Add a dedicated config/env-var reference

There are many environment and deployment details spread across guides and
walkthroughs.

Create one explicit reference page for:

- environment variables
- required secrets
- defaults
- example deployment values
- where each setting matters

This is a common operator lookup need.

### 12.4 Add explicit audience guide sets

To make the docs complete for all primary audiences, plan the following
additions or curated index pages.

#### Start here / role entry pages

Recommended additions:

- `docs/start/overview.md`
- `docs/start/choose-your-path.md`
- `docs/start/decision-maker-overview.md`

Purpose:

- orient first-time readers
- make role-based entry points explicit
- separate product/value overview from deep technical architecture

#### Developer / integrator guide set

Recommended additions:

- `docs/build/sdk-quickstart.md`
- `docs/build/sdk-vs-rest-api.md`
- `docs/build/auth-and-project-scoping.md`
- `docs/build/error-handling-and-retries.md`
- `docs/build/ci-cd-automation.md`
- `docs/build/developer-troubleshooting.md`

Purpose:

- reduce time to first successful integration
- make common integration decisions explicit
- provide a supportable path between guide material and reference material

#### System-user guide set

Recommended additions:

- `docs/use/prompts.md`
- `docs/use/tools.md`
- `docs/use/skills.md`
- `docs/use/mcp-servers.md`
- `docs/use/workflows.md`
- `docs/use/knowledge-bases.md`
- `docs/use/evaluations.md`
- `docs/use/calibration.md`
- `docs/use/aria.md`
- `docs/use/review-and-release.md`

Purpose:

- explain how to accomplish user-facing tasks
- stop using architecture pages as the default feature guide

#### Operator / admin guide set

Recommended additions:

- `docs/operate/install.md`
- `docs/operate/local-bring-up.md`
- `docs/operate/configuration.md`
- `docs/operate/provider-setup.md`
- `docs/operate/storage.md`
- `docs/operate/health-and-readiness.md`
- `docs/operate/troubleshooting.md`
- `docs/operate/backup-and-recovery.md`

Keep:

- `docs/runbook.md`
- `docs-site/walkthrough.html` until or unless it is intentionally migrated

Purpose:

- create one coherent operating journey
- distinguish tutorial, operations, and recovery content

#### Architect / evaluator / governance / decision-maker guide set

Recommended additions:

- `docs/architecture/index.md`
- `docs/use/trust-and-governance.md`
- `docs/start/decision-maker-overview.md`

Purpose:

- give architects a guided entry into the deep technical corpus
- give evaluators/governance users a curated path through trust surfaces
- give decision-makers a concise non-implementation overview that still links to proof

## 13. Recommended implementation approach

### 13.1 Phase 0 — preserve and codify current strengths

Do first:

- keep the generator
- keep the sync pipeline
- keep the contract tests
- keep `llms.txt`
- keep the style guide

Add:

- doc metadata schema
- `docs-site/docs-site-map.json`

### 13.2 Phase 1 — information architecture refactor

Implement:

- new top-level sections
- new landing page
- sectional navigation model
- relocation of walkthrough/presentation/strategy visibility

No major content rewrite required yet; focus on better grouping and entry points.

### 13.3 Phase 2 — search and navigation improvements

Implement:

- generated full-text search index
- heading-level results
- hierarchical TOC
- section-scoped pager

### 13.4 Phase 3 — developer-reference improvements

Implement:

- better REST API endpoint reference
- better SDK symbol/reference discovery
- visible stability badges
- compatibility metadata

### 13.5 Phase 4 — system-user and operator guide layer

Author or refactor:

- feature guides for product users
- install/config guides for operators
- explicit troubleshooting pages
- role-home pages for architects, evaluators, and decision-makers

This fills the current missing middle layer.

### 13.6 Phase 5 — governance and quality gates

Add tests for:

- required front matter fields
- search-index generation
- no orphaned nav entries
- no collateral pages in primary nav
- stable page metadata rendering

## 14. Concrete implementation recommendations

### 14.1 Generator changes

Recommended changes to `docs-site/build-docs.mjs`:

- read front matter metadata
- read `docs-site/docs-site-map.json` for hand-authored HTML-page metadata and section ordering
- generate a search index JSON
- support page badges/metadata blocks
- support section-aware nav models
- support document-type styling hooks

### 14.2 Client changes

Recommended changes to `docs-site/docs.js`:

- full-text search UI instead of title filtering only
- hierarchical TOC
- section-aware previous/next
- audience/type badge rendering

Recommended changes to `docs-site/docs.css`:

- compact metadata badges
- search result panel styling
- stronger distinction between guide/reference/runbook pages

### 14.3 Source-tree changes

Recommended source organization under `docs/`:

```text
docs/
├─ start/
├─ use/
├─ operate/
├─ build/
├─ architecture/
├─ reference/
├─ examples/
└─ strategy/
```

This does not require deleting current pages immediately. Existing files can be
remapped in phases.

### 14.4 Content metadata example

Recommended front matter shape:

```yaml
---
title: CALIBER Python SDK
summary: Install, authenticate, and make your first typed API call.
audience:
  - developer
doc_type: tutorial
product_area: sdk
stability: ga
prerequisites:
  - Python 3.10+
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
nav_section: build
nav_group: sdk
nav_order: 10
tags:
  - sdk
  - auth
  - quickstart
---
```

## 15. Acceptance criteria

The refactor should be considered successful only if it improves the actual
reader experience, not just the site map.

### 15.1 Developer acceptance criteria

- a developer can find auth + first SDK call in 2 clicks or fewer
- a developer can search for an endpoint path or SDK symbol and get a direct hit
- API pages expose exact contract details, not only representative summaries
- SDK/API pages clearly show stability and compatibility

### 15.2 System-user/operator acceptance criteria

- an operator can find install/config/troubleshooting/runbook content from one
  clear top-level section
- a system user can find feature guides without entering architecture pages first
- walkthrough and runbook are clearly distinguished by purpose

### 15.3 Documentation-system acceptance criteria

- generated docs remain source-driven and test-validated
- search index generation is deterministic and tested
- published Pages output still passes sync/publication contract checks
- collateral pages remain published without polluting primary navigation

## 16. Recommended immediate next steps

1. Approve the future IA:
   - `Start here`
   - `Use CALIBER`
   - `Build & integrate`
   - `Operate CALIBER`
   - `Examples`
   - `Reference`
   - `Architecture`
   - `Strategy`

2. Add page metadata/front matter support to the generator.

3. Rework the landing page around role-based entry points.

4. Move:
   - `walkthrough.html` under the operating path
   - `presentation.html` out of primary nav and `presentation_timed.html` kept as secondary collateral

5. Add generated full-text search before doing large-scale content rewrites.

6. After the IA and search changes land, refactor the content layer in this
   order:
   - developer reference
   - system-user feature guides
   - operator guides
   - curated architect/evaluator/decision-maker entry pages

## 17. Final recommendation

Do not treat this as a “rewrite the docs” project.

Treat it as a documentation productization project:

- keep the current engineering discipline
- preserve the strong architecture corpus
- separate audiences and document types
- improve search and discoverability
- make installation, integration, usage, and operations first-class journeys

The current docs-site already has the technical backbone of a professional
documentation system. What it needs now is a stronger user-facing information
architecture and a clearer separation between concept, task, reference, and
collateral.
