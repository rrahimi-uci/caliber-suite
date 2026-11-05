# CALIBER documentation index

Use this index to find documentation and choose a home for new material.
The [repository README](../README.md) introduces CALIBER; the
[architecture overview](../ARCHITECTURE.md) explains the platform as a whole.

## Start with your audience

| Reader / goal | Start here | Continue with |
| --- | --- | --- |
| New user | [Choose your path](start/choose-your-path.md) | [Five-minute quickstart](start/five-minute-quickstart.md), [user guides](use/) |
| Decision-maker / product reviewer | [Product overview](start/decision-maker-overview.md) | [PRD](product/prd.md), [competitive analysis](product/competitive-analysis.md) |
| Architect | [Architecture overview](../ARCHITECTURE.md) | [Reader guide](architecture/index.md), [refinement loop](architecture/refinement-loop.md) |
| Developer / integrator | [SDK or REST API](build/sdk-vs-rest-api.md) | [SDK guide](sdk/guide.md), [API overview](api/overview.md), [cookbooks](../docs-site/cookbooks/README.md) |
| Operator | [Local bring-up](operate/local-bring-up.md) | [Runbook](operate/runbook.md), [recovery runbook](operate/recovery-runbook.md) |
| UX contributor | [UX analysis](ux/ux-analysis-report.md) | [Version-management specification](ux/version-management-ux-spec.md), [UX evidence](ux/) |
| Researcher / evaluator | [Autonomous enterprise](research/autonomous-enterprise.md) | [Supporting evidence](research/autonomous-enterprise-evidence/README.md), [evaluation architecture](14-evaluation/architecture.md) |
| Repository contributor / coding agent | [Contributing](../CONTRIBUTING.md) | [Engineering framework](../.agents/README.md), [engineering policy](../.agents/engineering-policy.md) |

## Authority and currency

[Live GitHub Project #3](https://github.com/users/rrahimi-uci/projects/3) and its
[linked issues](https://github.com/rrahimi-uci/caliber-suite/issues) own roadmap,
priorities, milestones, dependencies, acceptance criteria, decisions and progress.
Use the [Project integration contract](../.agents/github/project-integration.md)
for current execution context. Repository documents do not form a second backlog.

Guides describe intended use and documented behavior; check their applicability
against the implementation and release you are using. Product requirements and
research proposals do not establish that a capability has shipped.

Reports, archived plans and evidence record their original candidate, date and
scope. Their completion claims are historical, not current release certification.
Follow their evidence and the live Project before relying on an old status.

## Directory ownership

Ownership here means the content each location is responsible for, not a new
maintainer assignment or approval policy. Paths are relative to `docs/`.

| Location | Owns |
| --- | --- |
| [start/](start/) | Reader orientation, installation, quickstart and release-scope guidance |
| [use/](use/) | Product workflows and user-facing how-to guides |
| [build/](build/) | Integration guidance, developer troubleshooting and build procedures |
| [api/](api/), [sdk/](sdk/) | API contracts, SDK usage, CLI and plugin documentation |
| [reference/](reference/), [openapis/](openapis/) | Configuration reference and checked-in OpenAPI inputs |
| [product/](product/) | Product requirements, positioning and competitive analysis |
| [architecture/](architecture/) | Cross-cutting designs, refinement loop, reader guide and ADRs |
| Numbered modules, `01-caliber/`–`16-openapi-integrations/` | Existing platform and feature architecture series |
| [operate/](operate/) | Deployment, configuration, runbooks, intervention and recovery |
| [ux/](ux/) | UX specifications, analysis and supporting census/evidence artifacts |
| [research/](research/) | Research narratives and their supporting evidence collections |
| [reports/](reports/) | Dated assessments, coverage matrices, decisions and delivery evidence |
| [planning/](planning/) | Existing frozen planning records and archive compatibility tools |
| [planning/archive/](planning/archive/) | Relocated older delivery and alpha plans |

### Architecture and published modules

Keep the [architecture reader guide](architecture/index.md) and numbered series
in their existing locations. The [platform module](01-caliber/architecture.md)
provides the implementation reference beneath the root overview; feature modules
own their detailed contracts. Cross-link these layers instead of duplicating them.
The [refinement loop](architecture/refinement-loop.md) belongs with cross-cutting
architecture, alongside the [ADR collection](architecture/adr/README.md).

### Reports by subject

| Group | Contents / entry point |
| --- | --- |
| [alpha/](reports/alpha/) | [Coverage matrix](reports/alpha/alpha-feature-coverage-matrix.md), alpha gaps, release notes and dated decisions |
| [beta/](reports/beta/) | [Beta feature coverage matrix](reports/beta/beta-feature-coverage-matrix.md) |
| [architecture/](reports/architecture/) | [Reference gap analysis](reports/architecture/CALIBER_REFERENCE_ARCHITECTURE_GAP_ANALYSIS.md), map gaps and integration proposals |
| [product/](reports/product/) | [Product completeness](reports/product/product-completeness-report.md), MVP reviews and pilot fixtures |
| [sdk/](reports/sdk/) | [Implementation checklist](reports/sdk/sdk-implementation-checklist.md), historical SDK plans and decisions |
| [ux/](reports/ux/) | [UI completion report](reports/ux/ui-complete-report.md) and delivery assessments |
| [egi/](reports/egi/) | Existing EGI prototype, acceptance, security and reliability evidence |

UX design specifications and the UX analysis with its evidence live in `ux/`;
dated UI delivery reports live in `reports/ux/`. Research evidence stays beside
its narrative under `research/autonomous-enterprise-evidence/`.

### Planning archives

The [planning archive notice](planning/PLANNING_ARCHIVE.md) explains retirement
of local execution state and the retained tooling. Existing core files such as
the [execution plan](planning/BETA_MVP_EXECUTION_PLAN.md),
[execution manifest](planning/beta-mvp-execution.json) and
[product roadmap](planning/PRODUCT_ROADMAP.md) remain frozen at their stable paths.
The [legacy roadmap](roadmap.md) also remains a historical reference.

Older standalone plans are grouped under `planning/archive/`:

- [EGI delivery plan](planning/archive/CALIBER_EGI_DELIVERY_PLAN.md)
- [Five-person delivery plan](planning/archive/CALIBER_FIVE_PERSON_DELIVERY_PLAN.md)
- [Two-week alpha plan](planning/archive/two-week-alpha-plan.md)

Do not update archived schedules to mirror GitHub or treat archive validation
as evidence of current readiness.

## Sources and files that stay in place

[STYLE.md](STYLE.md) defines the contract for registered published sources.
This index is repository navigation, not a registered published module; it does
not require invented publication metadata or the module page structure.

Generated `docs-site/m-*` pages and copies under
`caliber/caliber-ui/public/docs/` and `caliber/src/caliber/ui/docs/` are outputs.
Do not manually move or edit them. Change the canonical source and, when needed,
the generator mapping; use the existing [docs build procedure](build/ci-cd-automation.md).

[Cookbooks](../docs-site/cookbooks/README.md) are an authored exception within
`docs-site/`: keep each recipe, assets, fixtures and verification files colocated.
Keep module diagram assets beside their source pages as well.

Package READMEs and package policies remain with their packages. Root
[README](../README.md), [ARCHITECTURE](../ARCHITECTURE.md), [AGENTS](../AGENTS.md)
and [CLAUDE](../CLAUDE.md) retain their discovery and entry-point roles.
Keep root contributor/security/conduct policies and GitHub-discovered files,
including templates and workflows under `.github/`, in their established locations.

## Placing future documentation

1. Choose the reader's task and the owning directory above; extend an existing
   source when it already owns the subject.
2. Put durable guidance in the relevant guide/reference directory, dated findings
   in the matching report group, and research evidence beside its research source.
3. Keep new tasks, schedules and progress in GitHub. Preserve archived plans as
   provenance, including their original limitations and evidence references.
4. Use relative links for repository content. When moving a source, update its
   incoming links, asset references and any existing generator registration.
5. Apply the published-source style contract only where applicable and use the
   repository's existing validation workflow; this guide adds no CI requirements.
6. Update this index when navigation changes. Add a directory README only when
   it serves a distinct purpose rather than repeating this directory map.
