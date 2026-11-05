# Planning archive migration

Retired on 4 November 2026. [GitHub Project #3](https://github.com/users/rrahimi-uci/projects/3) and its linked [Issues](https://github.com/rrahimi-uci/caliber-suite/issues) are the sole current planning authority: roadmap, milestones, scope, dependencies, priorities, acceptance criteria, decisions and progress. Read live Project and Issue data through the canonical `.agents/` workflows reached from the repository agent entry points. If GitHub is unavailable or inaccessible, report the blocker; this archive is not a fallback source for selecting or certifying current work.

The following paths remain stable for citations and provenance:

| Archived source | Retained purpose |
| --- | --- |
| [Execution manifest](beta-mvp-execution.json) | Historical task/epic/control records, architectural constraints, acceptance criteria, evidence paths and dependency graph. Its `archive` metadata records retirement; record contents are preserved. |
| [Execution plan](BETA_MVP_EXECUTION_PLAN.md) | Architectural rationale, original scope decisions and issue traceability index. |
| [Product roadmap](PRODUCT_ROADMAP.md) and [release train](PREVIEW_TO_MVP_RELEASE_TRAIN.md) | Historical outcome, sizing and cadence proposals; no current schedule or commitment. |
| [November audit](PLAN_AUDIT_2026_11.md) and [contract corrections](EXECUTION_CONTRACT_CORRECTIONS.md) | Dated findings and reasons for earlier changes. |
| [Former execution guide](AGENT_EXECUTION_GUIDE.md) | Historical handoff procedure; current workflow definitions live under `.agents/`. |
| [Alpha-era roadmap](../roadmap.md) | Earlier planning and architecture provenance. |

Statements such as “canonical manifest,” “current brief,” “binding correction,” and “source precedence” within archived records describe their historical context. They cannot override current GitHub scope or repository engineering rules. No task or evidence is deleted, reopened, reclassified or marked complete by this migration. Architecture and runtime safeguards still require their own reviewed changes; retiring planning parity does not relax production tests or release evidence requirements.

## Tool compatibility

| Entry point | Behavior after migration |
| --- | --- |
| `node docs/planning/validate-execution-plan.mjs` | Continues to work offline for archive structure, reviewed contracts and the complete historical task/epic/control DAG. Output explicitly disclaims current planning and release validation. Existing CI invocation remains compatible. |
| Validator `--live`, `--snapshot`, or any argument | Exits 2 before reading an external snapshot or invoking GitHub. Comparing live scope to a frozen manifest is retired. |
| `node docs/planning/render-task-brief.mjs <issue>` | Exits 2 with migration guidance; no obsolete current brief is emitted. |
| `node docs/planning/render-task-brief.mjs --archive <issue>` | Renders historical content with an archive warning and archive-specific markers. Unknown IDs and invalid arguments exit 2. Never paste this output over a current issue. |
| `renderTask`, `renderEpic`, `renderControl`, `renderDecisions` imports | Require a final `{ archive: true }` option for historical inspection. Calls without that explicit option throw. Output never carries the old current-brief markers. |
| `managedBody` import | Always throws a retirement error; the old manifest-to-issue update builder cannot be used, even with archive output. |
| Pure lookup/readiness and contract helpers | Retained only to inspect/test historical data. Their result is not current Project readiness or an authorization to claim, close or release work. |

Offline archive validation:

```sh
node docs/planning/validate-execution-plan.mjs
node --test --experimental-test-coverage docs/planning/test-execution-plan-validator.mjs
git diff --check
```

Negative tests preserve historical safety-contract and dependency checks and verify that retired entry points fail without contacting GitHub. They replace the former tests enforcing equality between GitHub and the local manifest. New live tasks, changed milestones and updated issue scope do not require editing this archive.

## Existing references and integration

Older standalone delivery plans are grouped under [archive/](archive/): the
[EGI plan](archive/CALIBER_EGI_DELIVERY_PLAN.md),
[five-person plan](archive/CALIBER_FIVE_PERSON_DELIVERY_PLAN.md) and
[two-week alpha plan](archive/two-week-alpha-plan.md). The gap report is now under
[architecture reports](../reports/architecture/map-gap.md). This folder reorganization
updates only the manifest's top-level report locator; archived task, epic, control,
decision and dependency records retain their original content and baseline references.

Existing architecture, release, PRD, gap and report documents link to these stable archive paths. Follow the archive notice to the live Project before treating an old reference as work to execute. Historical reports and acceptance evidence retain their original provenance and claims; current release readiness requires fresh evidence for the actual candidate.

Callers and contributor instructions must stop describing the local renderer as the current handoff or requesting manifest synchronization. Use live Issue content, Project fields and native dependency links instead. The shared contribution playbook and repository agent entry points are maintained with the canonical `.agents/` framework; this migration does not duplicate their workflow definitions. The CI step may retain its old display name, but its unchanged no-argument command now verifies archive integrity only.

No GitHub Issue or Project content is rewritten by these tools or by this migration. A future correction to historical evidence must be explicit about what changed and why; it must not become a second live planning workflow.
