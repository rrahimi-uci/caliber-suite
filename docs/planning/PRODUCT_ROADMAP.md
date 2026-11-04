# CALIBER product roadmap

CALIBER helps teams turn AI workflows into controlled, observable work: people can approve consequential actions, understand results, and recover safely when something fails. The product grows from a narrow evaluation experience to repeatable operation by a customer team.

This roadmap is published in [Project #3](https://github.com/users/rrahimi-uci/projects/3). The repository copy is reviewed through PRs; the project README is the product-facing entry point. The [execution plan](BETA_MVP_EXECUTION_PLAN.md), [release mapping](PREVIEW_TO_MVP_RELEASE_TRAIN.md) and task briefs provide the engineering detail. These are delivery goals, not claims that the releases have shipped.

## Product goals and release decisions

| Release | Intended users and product goal | What users should be able to do | Evidence required to move forward |
| --- | --- | --- | --- |
| **Beta — prove a useful, trustworthy workflow** | Early evaluators and design partners test whether CALIBER solves one meaningful workflow problem. | Install the supported reference deployment; run one supported workflow; approve consequential actions; inspect its result and evidence; understand refusals and recover from interruption. UI, API, SDK and CLI describe the same supported experience. | The complete supported journey works on installed artifacts. Required approval, denial, retry, recovery, upgrade and restore checks pass. A bounded authorized real-provider trial and independent security review support D5 PASS. Useful feedback is recorded; no unapproved product-quality threshold is invented. |
| **Pilot — prove it works in real operations** | A small, named group of real users and an independent operator use CALIBER under agreed support and operating limits. | Operate two controlled use cases with a second supported connector; manage credentials and budgets; diagnose failures; restore service; evaluate changes without depending on the original developer for each routine operation. | D6–D9 pass. An independent operator completes installation/restore and a controlled 4–6 week trial demonstrates the owner-approved quality, cost, reliability and support targets. Actual incidents, outcomes and user feedback are retained. Pilot is bounded use, not a general-availability or unlimited-scale promise. |
| **MVP — make the product repeatably usable and supportable** | A customer team can adopt the defined product and use it routinely with a documented operating model. | Onboard users and administrators; configure approved workflows and access; manage knowledge and releases; operate, upgrade and recover using documented procedures. The supported scope and support responsibilities are clear. | D10–D15 and the retained enterprise contracts pass, with independent final review and one exact validated release candidate. Representative users/operators demonstrate the supported journeys and owner-approved product outcomes. MVP does not automatically mean commercial GA, a contractual SLA or shared-customer hosting. |

The current MVP backlog retains identity, regions, Knowledge Hub, memory, federation, catalog, governance and autonomy. That is an **enterprise-sized scope**. H16 in [#608](https://github.com/rrahimi-uci/caliber-suite/issues/608) asks the product owner to fund that scope or explicitly define a smaller first product and move named capabilities to later releases. This roadmap does not silently remove those requirements. One organization per installation, with multiple projects/users, remains the agreed boundary.

## Names for the three-week delivery cycles

| Work leading to | Cycle names | Final product gate |
| --- | --- | --- |
| Beta | **Preview 1, Preview 2, Preview 3** — the existing names remain | **Beta** |
| Pilot | **Pilot Preview 01, Pilot Preview 02, … Pilot Preview N** | **Pilot** |
| MVP | **MVP Preview 01, MVP Preview 02, … MVP Preview N** | **MVP** |

```text
Alpha → Preview 1 → Preview 2 → Preview 3 → Beta
      → Pilot Preview 01 → Pilot Preview 02 → … → Pilot
      → MVP Preview 01 → MVP Preview 02 → … → MVP
```

A preview is a small, demonstrated increment toward its destination release. Its name does not imply Pilot or MVP acceptance. Use a descriptive suffix in the cycle goal, such as “Pilot Preview 01 — safer connector access”; keep the numbered cycle identity stable. Avoid “RC” for ordinary feature work: a release candidate is appropriate only after the intended release scope is complete and under final validation. Do not reuse the existing M01 package IDs or P1/P2/P3 lane names as cycle IDs.

## Consistent size and delivery rhythm

Every development preview is a **three-week timebox**:

1. **Week 1:** implement the agreed user outcome and its producing contracts, with focused tests.
2. **Week 2:** finish and integrate the complete supported journey, exercise failure paths, update documentation and freeze the candidate.
3. **Week 3:** QA, bug bash, fixes, regression checks, installed-journey validation and an accept/HOLD decision. No planned feature work is hidden in the QA week.

Equal duration requires active scope control; it does not mean equal task counts. Select one primary user outcome, estimate the complete slice including implementation tests/docs, and include only work whose dependencies and start decisions can be ready in time. Use measured team capacity and an explicit integration owner. Split an oversized epic across previews while preserving all its final acceptance criteria. Keep the next cycle detailed and later cycles as a reforecastable queue.

The historical capacity assumption is 1.8 planned base person-weeks per development week. Under that assumption, each preview has at most **3.6 base person-weeks of planned development**, plus its protected QA week. This is a provisional sizing budget, not confirmed staffing or a mandate to fill every slot. Closed issue counts and the number of agents are not throughput measurements.

At kickoff, each cycle record must contain: one product outcome; included task links and estimates; producing dependencies; explicit exclusions; owner/capacity; demo and acceptance criteria; required automated tests and manual QA; and candidate/evidence links. A blocked required outcome stays HOLD. Replan the incomplete slice into a subsequent cycle and shift dependent release dates; never mark the missed preview accepted or waive its release criteria.

The Pilot's controlled operator trial is a separate evidence window after its prerequisites pass. Allow two three-week checkpoints (six calendar weeks) for the retained 4–6 week trial and its review; observation/support activity replaces new feature development during that trial. This does not require a public software release every three weeks. The last qualifying preview can supply the release candidate, but only the release control can declare Beta, Pilot or MVP PASS.

## Product outcome sequence within the preview series

These are ordered **themes**, not a claim that an entire D milestone fits one three-week cycle. Allocate as many bounded previews as the estimated tasks require. Native producing dependencies permit safe parallel preparation; cumulative release gates still apply.

| Preview series / existing control | Product outcome to demonstrate | Engineering scope retained |
| --- | --- | --- |
| Pilot / D6 | Operators can control access, approve changes and inspect trustworthy operational evidence. | Credential leases, signed promotion, protected audit, approval/stop controls and evaluation foundations. |
| Pilot / D7 | Teams can configure and evaluate workflows with governed knowledge and isolated execution. | Knowledge lifecycle, isolation, trajectory evaluation, feedback, installation and conformance foundations. |
| Pilot / D8 | Teams can extend to a second real use case while keeping spend, concurrency and recovery bounded. | Second connector, budgets, child tasks, upgrade/restore preparation and second-use-case predicates. |
| Pilot / D9 | An independent operator can run the product within measured limits. | Integrated evaluation, conformance, measured operating envelope, install/restore and controlled operator trial; Pilot decision. |
| MVP / D10 | Administrators can govern how workflows and resources are released and used. | Governance, routing, platform contracts and bounded autonomy. |
| MVP / D11 | A customer team can onboard and remove people through its approved identity system. | OIDC/SCIM, project permissions and timely deprovisioning. |
| MVP / D12 | Operators can place and recover data/work in approved locations. | Same-organization regional cells, residency and measured recovery. |
| MVP / D13 | Domain users can turn evidence into reviewed, reusable knowledge. | Typed extraction verdicts, approved ontology, grounded release gates and Knowledge Hub journeys. |
| MVP / D14 | Teams can reuse approved memory and collaborate with approved peer systems. | Consent/purpose/TTL/deletion and scope-constrained federation with recovery. |
| MVP / D15 | A customer can adopt a documented, independently reviewed supported release. | Certified adapter/catalog, companion traceability, final security review, install/upgrade and exact-artifact release decision. |

The [release mapping](PREVIEW_TO_MVP_RELEASE_TRAIN.md#epic-and-task-assignment) assigns every epic and child to these controls. Changing the cadence does not change their dependencies or make future scope optional.

## Timeline and sizing correction

The previous 36–44 week Beta-to-Pilot and 72–96 week Pilot-to-MVP ranges assumed long development stretches followed by separate integration/QA reserves. They are **historical estimates**, not commitments under the repeated two-development-plus-one-QA cadence.

For illustration only, if the historical 53.5 and 108.5 base person-week scope estimates are treated as remaining development effort at the same capacity:

| Destination | Capacity arithmetic | What the result does and does not include |
| --- | --- | --- |
| Pilot | `ceil(53.5 / 3.6) = 15` development previews → **45 weeks** | Normal cycle QA is included. A separate six-week operator-trial allowance brings the illustration to **51 weeks**, before additional contingency or unavailable prerequisites. |
| MVP | `ceil(108.5 / 3.6) = 31` development previews → **93 weeks** | Normal cycle QA is included. Additional independent review, external dependencies and contingency still need explicit allowance. |

These are arithmetic illustrations, **not an approved 15/31-cycle schedule or new delivery promises**. Avoid double-counting QA already present in an estimate. H01 must confirm remaining effort, named staffing and measured throughput; H16 must confirm product scope. Then publish the actual cycle count and dates, with operator/reviewer bookings. Until then, later tasks remain unscheduled rather than receiving invented dates. The original Beta estimate also remains conditional on its own remaining work and acceptance inputs.

## GitHub roadmap and planning fields

Use the **Project #3 README** for this product roadmap and the repository document for reviewed detail. Keep a single roadmap source rather than a separately maintained wiki. GitHub supports project descriptions/READMEs with Markdown and links. [GitHub project documentation](https://docs.github.com/en/issues/planning-and-tracking-with-projects/creating-projects/creating-a-project#updating-your-project-description-and-readme).

- **Release** remains the destination: Preview 1/2/3, Beta, Pilot or MVP.
- **Delivery cycle** holds a committed cycle name such as `Pilot Preview 01`. A blank value means **unscheduled**, not ready or overdue. Existing milestone/Release assignments are not cycle commitments.
- **Readiness** and **Human input** remain separate: cycle assignment cannot override a producing blocker or a pending start decision.
- The **Delivery cycles** table shows open Pilot/MVP tasks with these fields; populate cycles only after kickoff scope/capacity checks. Existing release views retain the full backlog.

Once H01 supplies a real kickoff date, create a 21-day iteration field and a dated roadmap view. GitHub timelines require date or iteration values; an empty or invented timeline would be misleading. The product roadmap is available immediately in the project README. [Iteration fields](https://docs.github.com/en/issues/planning-and-tracking-with-projects/understanding-fields/about-iteration-fields), [roadmap layout](https://docs.github.com/en/issues/planning-and-tracking-with-projects/customizing-views-in-your-project/customizing-the-roadmap-layout).
