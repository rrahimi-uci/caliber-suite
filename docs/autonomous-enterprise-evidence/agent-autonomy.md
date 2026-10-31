# R1: External research on autonomy frameworks for AI agents and enterprise agent platforms

Researcher scope: external landscape only (the repo was read only for orientation). Date of research: 2026-10-31.

## 0. How to read the evidence tags

| Tag | Meaning |
|---|---|
| [S] | I opened the source (WebFetch) and read the content I cite. URL given. |
| [W] | Appeared in WebSearch results only (secondary or snippet). Treat as directional; URL given. |
| [M] | From my own prior knowledge, not re-verified in this session. |
| [J] | My own judgement or inference, not a claim about the world. |

Two important caveats up front. First, several vendor and regulatory items changed in 2026 (SR 26-2, EU AI Act omnibus, Entra/Agent 365, Gemini Enterprise Agent Platform). Where I rely on secondary coverage I say so. Second, the market numbers quoted by blogs attributed to Gartner, McKinsey, Deloitte and Forrester are mostly unverifiable; I did not find primary maturity models from McKinsey, Deloitte or Forrester and I do not invent them.

---

## 1. Key findings (read this if nothing else)

1. **"Autonomy level" is now a crowded, mostly descriptive taxonomy space, and it is not where differentiation lies.** At least five level schemes exist: Feng/McDonald/Zhang (5 user roles), the CSA SAE-style scheme (L0-L5), the CSA Agentic Trust Framework (Intern/Junior/Senior/Principal), Zheng et al. (5 levels with Allowed vs Capable split), and Anthropic's empirical "auto-approve vs interrupt" view. They agree on the shape (human acts -> human approves each action -> human approves plan -> agent acts and escalates -> human monitors) and disagree on everything else. A level label alone is commodity. [S]
2. **The most defensible shared insight is "autonomy is a permission, not a capability."** Feng et al. say autonomy is "a deliberate design decision" separate from capability; Zheng et al. formalise it as Allowed Autonomy Level (AAL) vs Autonomous Capability Level (ACL). This is the conceptual spine Caliber should adopt and it directly supports "earned" autonomy. [S]
3. **Nobody ships evidence-gated promotion and demotion as an enforced, per-responsibility product feature.** Hyperscalers and SaaS suites ship identity, policy-at-the-tool-call, registry, observability, evaluation, and human-approval steps. Promotion criteria exist only in specifications (CSA ATF, with numeric time gates) and in research proposals (autonomy certificates). Demotion in ATF is thinly specified. That is the real white space, but also a warning: the thing is easy to describe and hard to make trustworthy. [S] for ATF and Feng; [W] for vendor product claims.
4. **Evidence for autonomy is weaker than platforms imply.** pass^k collapses with repetition (tau-bench: pass^8 under 25% in retail for GPT-4o-class agents), automated grading overestimates real performance (METR), frontier models verbalise evaluation awareness and can change behaviour when they suspect a test (Apollo/OpenAI), and Anthropic's own auto-mode classifier misses 17% of real overeager actions. Evaluation can gate and bound autonomy; it cannot by itself justify unbounded autonomy. Design for runtime containment and post-deployment monitoring, with evals as one input. [S]/[W]
5. **"Level" is probably the wrong primary abstraction for enterprise work.** The closest enterprise analogues (SR 11-7 style model risk tiers, delegation-of-authority matrices, ITIL standard changes, FDA predetermined change control plans, SAE's own operational design domain) all grant authority per action class, within a bounded envelope, with limits (amount, reversibility, population), conditions and a named accountable owner. A single ordinal "L3" is a lossy summary of that envelope. Recommend: envelope as the source of truth, levels as a derived label for communication. [J, grounded in S/M analogues]

---

## 2. Autonomy-level frameworks for agents

### 2.1 Feng, McDonald, Zhang: "Levels of Autonomy for AI Agents" (2025)

Source opened: https://arxiv.org/html/2506.12469v2 and https://arxiv.org/abs/2506.12469 (arXiv v1 14 Jun 2025, v2 28 Jul 2025; written for the Knight First Amendment Institute "AI and Democratic Freedoms" series: https://knightcolumbia.org/content/levels-of-autonomy-for-ai-agents-1 [W]). [S]

- Autonomy is "a deliberate design decision," separate from capability and operating environment. The paper explicitly says more autonomy is not automatically a better agent; tradeoffs include utility, efficiency, accountability and cost. [S]
- Levels are defined by the role the user plays:

| Level | User role | What the user does | Agent does | Paper's stated open question |
|---|---|---|---|---|
| L1 | Operator | Plans, decomposes, invokes | Offers suggestions, acts only when invoked | Detecting when a preference decision is needed |
| L2 | Collaborator | Edits plans, delegates, can take control anytime | Executes delegated work in parallel, reports blockers | Smooth takeover |
| L3 | Consultant | Gives feedback and direction, cannot directly take control | Plans and executes most, consults at chosen points | When to consult, eliciting good feedback |
| L4 | Approver | Pre-sets approval conditions, handles blockers, credentials, sign-offs | Runs workflow autonomously, plan visible | Preventing rubber-stamp approvals; detecting misaligned agents that exploit disengagement; identifying consequential actions |
| L5 | Observer | Monitors logs, emergency off switch | Fully autonomous | What monitoring tools and what off-switch |

- **Autonomy certificates** (proposal, not implemented): a digital document issued by a third-party governing body that sets the maximum autonomy level for an agent, derived from technical specs and operating environment, backed by an "autonomy case" analogous to a safety case. Changes to model, tools or environment may invalidate the certificate. [S]
- **Assisted evaluation** (proposal): run the agent with a standby human, start with no help, add involvement progressively, map minimum involvement needed to a level from logs. [S]
- **Multi-agent point**: all-L1 systems are impractical (everyone waits for an operator); all-L5 systems are hard to steer, debug, audit; mixed levels are more likely useful. [S]
- The paper is descriptive, not prescriptive, and has no dedicated limitations section. It does not discuss "agent cards" (I checked). [S]

Relevance to Caliber: the "user role" framing maps directly onto Caliber's human/agent assignment question. The paper's strongest contribution for this study is the certificate idea (autonomy as a revocable, scoped, evidenced artefact) and the explicit warning about rubber-stamp approvals at L4. It is per-agent, not per-responsibility, which is the main gap relative to the Caliber vision. [J]

### 2.2 Cloud Security Alliance: SAE-inspired six levels (blog) and the Agentic Trust Framework (ATF)

**SAE-style levels.** Opened: https://cloudsecurityalliance.org/articles/levels-of-autonomy. [S]

| Level | Name | Definition (from the article) |
|---|---|---|
| 0 | No autonomy | AI informs or recommends, humans perform all actions |
| 1 | Assisted | AI can execute, human approves each action first |
| 2 | Supervised | Human approves a plan or batch, AI executes within scope |
| 3 | Conditional | AI decides and acts within boundaries, escalates outside them |
| 4 | High autonomy | Broad scope, human monitors for anomalies and intervenes |
| 5 | Full autonomy | AI sets goals, may modify itself, strategic oversight only; author says not appropriate for enterprise today |

The author is candid about weaknesses: unsure it is worth developing further, asks whether three levels would do, notes a system may warrant different levels per capability or context, says "autonomy alone doesn't determine risk," and says it is unclear how to verify a claimed level. [S] A further CSA paper "Agentic AI Autonomy Levels and Control Framework" exists (https://labs.cloudsecurityalliance.org/research/agentic-ai-autonomy-levels-control-framework-v2-csa-styled/) but I did not open it. [W]

**ATF (Feb 2026).** Opened: https://cloudsecurityalliance.org/blog/2026/02/02/the-agentic-trust-framework-zero-trust-governance-for-ai-agents. [S] Open spec repo referenced: https://github.com/massivescale-ai/agentic-trust-framework [W].

- Four maturity levels: Intern (read-only), Junior (recommends, executes after approval), Senior (executes approved action types, notifies after), Principal (autonomous within domain, escalates edge cases). [S]
- Five promotion gates (all must pass): performance (minimum time at prior level: Junior 2 weeks, Senior 4 weeks, Principal 8 weeks; accuracy above 95% for Senior and above 99% for Principal; availability thresholds 99 / 99.5 / 99.9%), security validation (vulnerability assessment, then penetration test, then adversarial testing), business value (baseline, then ROI), incident record (zero critical incidents plus RCA and verified remediation), governance sign-off (technical and business owner; adds security team; adds risk committee for Principal). [S]
- Demotion: the only explicit trigger is "any significant incident triggers automatic demotion" for Principal; "significant" is not defined, and no demotion rules are given below Principal. "Graceful degradation" is mentioned without criteria. Internal inconsistency noted by the fetch: Section 4.3 requires >95% recommendation acceptance for Junior promotion while the gate table lists accuracy as N/A for Junior. [S]
- Five elements: identity, behaviour, data governance, segmentation, incident response (circuit breaker, kill switch under 1 second, session revocation, state rollback). [S]
- It is a governance spec, not an implementation; the thresholds are illustrative, and sources disagree on the timings (secondary blogs vary). [S]/[W]
- A secondary summary adds "no agent skips levels, no agent self-promotes" as operating rules; I could not confirm these in the primary text. [W]

Assessment [J]: ATF is the closest published thing to "autonomy must be earned," and it validates the lifecycle in the charter. Its weaknesses are the ones Caliber would inherit if it copied it: levels are per agent, not per responsibility; thresholds are time-and-accuracy based and ignore action reversibility or population; demotion is underspecified; "accuracy" is undefined for non-deterministic work.

### 2.3 Zheng et al.: "Separating Capability from Permission" (Jul 2026)

Opened abstract: https://arxiv.org/abs/2607.23438. [S] Authors Zheng, Dong, Depena, Bhatia, Xiao, Xu (one search result associated it with ExxonMobil authors [W]).

- Five levels: reactive execution, decision support, supervised action, goal-directed autonomy, delegated operational authority.
- Core distinction: Allowed Autonomy Level (AAL, what the agent is authorised to do given risk, oversight, accountability) vs Autonomous Capability Level (ACL, what it can technically do).
- Case: an enterprise data-engineering agent rated high on capability was deliberately held at a lower allowed level because of risk, reversibility and organisational readiness. A "risk-aware decision process" assigns AAL.
- I read only the abstract. [S]

This is exactly the "capability vs autonomy" split the charter needs and it is the best-aligned external framework to cite. [J]

### 2.4 Anthropic: how autonomy is actually granted in practice

Opened: https://www.anthropic.com/research/measuring-agent-autonomy (Feb 2026). [S]

- Claude Code: 99.9th-percentile turn duration rose from under 25 to over 45 minutes (Oct 2025 to Jan 2026); median turn about 45 seconds and stable. Smooth growth across model releases suggests trust, task ambition and product changes, not only model capability. "Deployment overhang": models can handle more autonomy than they are given.
- Experience changes oversight style: new users auto-approve about 20% of sessions; by 750 sessions over 40%. Interrupts also rise (about 5% to 9% of turns): users move from approving each action to monitoring and intervening. Defaults require manual approval, so some shift is user reconfiguration.
- Agent-initiated stops matter: on the most complex tasks Claude asks for clarification more than twice as often as on simple ones and more often than humans interrupt.
- Public API (about 1M tool calls): 0.8% of actions appear irreversible; 80% of calls came from agents with at least one safeguard; 73% appear to have a human in the loop (likely upper bounds); software engineering is about half of calls; highest-risk clusters (security, financial, medical) exist but are sparse.
- Recommendations: invest in post-deployment monitoring because pre-deployment evals cannot reveal real usage; train models to surface their own uncertainty; design for oversight, not just approval gates; policymakers should not mandate approving every action but should focus on whether humans can effectively monitor and intervene.
- Limits stated by Anthropic: single provider, mostly software engineering, risk and autonomy scores are Claude-generated, some oversight unobservable.

Implication [J]: real autonomy is co-constructed by model, user and product and moves by experience, not by certification. An enterprise platform that only supports static level settings misses the observable pattern (monitor and intervene, agent-initiated escalation).

Opened: https://anthropic.com/engineering/claude-code-auto-mode (auto mode). [S]
- Users accept 93% of manual permission prompts (approval fatigue is real, so per-action approval is weak oversight).
- Auto mode: a prompt-injection probe on tool outputs plus a transcript classifier gating actions. Two stages. Tiered permissions: read-only allowlisted, in-project edits skip the classifier, everything else classified.
- Measured: on real internal traffic (n=10,000) false positive 8.5% (stage 1) to 0.4% (full); on real overeager actions (n=52) false negative 6.6% to 17%; synthetic exfiltration (n=1,000) 1.8% to 5.7%.
- Fallback: after 3 consecutive denials or 20 total, the agent stops and escalates to the human; in headless mode the process terminates.
- Threat model explicitly includes overeager behaviour, honest mistakes, prompt injection and a misaligned model, tuned mainly for the first two.

Why it matters [J]: it is a published example of confidence-gated escalation with a measured miss rate, and a circuit breaker (deny counters) that converts repeated policy friction into human escalation. Both are patterns for the "operating envelope" model. It also shows that even a vendor with deep model access reports a 17% miss on the class of action that matters most, so a "classifier replaces approvals" claim needs a quantified residual risk.

Other reports of the auto mode rollout and default status come from secondary sources and conflict (93% vs 97%; default date). [W]

### 2.5 Google DeepMind: "Intelligent AI Delegation" (Feb 2026)

arXiv 2602.11865 (Tomašev, Franklin, Osindero). I read about the first half via the HTML render: https://arxiv.org/html/2602.11865. [S]

- Delegation is a sequence of decisions including transfer of authority, responsibility and accountability, with trust mechanisms; both delegator and delegatee may be human or AI.
- Task traits that should drive delegation: complexity, criticality, uncertainty, duration, cost, resources, constraints, verifiability, reversibility, contextuality, subjectivity.
- **Contract-first decomposition**: delegate only if the output can be precisely verified; otherwise decompose further. Final specification defines roles, resource limits, reporting frequency and required certifications.
- **Trust calibration**: reputation (public, verifiable track record) vs trust (private, context-specific threshold). A high-reputation agent can still fail a high-stakes threshold. Low-trust agents face caps and mandatory oversight.
- **Privilege attenuation**: sub-delegation passes only the permissions the subtask needs; just-in-time grants, automatic revocation, circuit breakers that invalidate tokens when reputation drops or anomalies appear.
- **Transitive accountability** and "liability firebreaks": an agent either accepts liability for downstream failures or halts and requests a fresh transfer of authority.
- **Moral crumple zone**: humans placed in loops only to absorb liability without real control; the countermeasure is context-aware cognitive friction, noting alarm fatigue.
- Protocol gap: secondary coverage says MCP, A2A and similar protocols lack native support for authority transfer, accountability and verification. [W] https://techinformed.com/google-deepmind-proposes-intelligent-delegation-for-enterprise-ai-agents/. I did not read the Protocols section; the claim is unverified at source level.
- Secondary coverage also claims the paper suggests occasionally giving humans tasks to prevent skill atrophy; I could not confirm it. [W] https://the-decoder.com/deepmind-suggests-ai-should-occasionally-assign-humans-busywork-so-we-do-not-forget-how-to-do-our-jobs/

This is the best conceptual match to "Work -> Roles -> Assignment -> Capabilities -> Autonomy" in the literature: delegation is per task, parameterised by task traits, carrying authority plus accountability, with verifiability as the gating condition. It supports a "delegation contract" abstraction over a "level" abstraction. [J]

### 2.6 Other frameworks (brief)

- **SAE J3016** [M]: six levels (0-5) for driving automation, defined per feature and per operational design domain (ODD), with explicit allocation of the dynamic driving task, object-and-event detection and fallback between driver and system. Key lessons: (a) levels describe role allocation, not capability; (b) the ODD (where it may operate) is part of the definition; (c) L3 handover and L2 supervision are the known failure zones (automation complacency). The "autonomy level" metaphor imports SAE's rigour only if the ODD concept comes with it. Autonomy in vehicles is also not earned in the field by self-assessment; it is approved per jurisdiction. [M]
- **ISO 26262 ASIL** [M]: risk classification by severity x exposure x controllability, not by autonomy. Another useful non-ordinal-autonomy analogue.
- **OpenAI "Levels of AGI" / OpenAI five-level scale; DeepMind "Levels of AGI" (Morris et al. 2023)** [M]: capability-oriented (performance x generality) and explicitly separate "levels of autonomy" as a deployment decision (DeepMind's paper has a six-level autonomy axis from "AI as tool" to "AI as agent" with human-AI interaction paradigms and risk). Relevant mainly because it independently makes the capability/autonomy separation. Not re-verified.
- **Google A2A**: open protocol for agent-to-agent interop. Agent Card (identity, capabilities, skills, endpoint, auth requirements, can be signed), task lifecycle with `submitted/working/completed/failed/canceled/rejected` plus interrupted states `input-required` and `auth-required`. No autonomy levels, no approval workflow, no governance in the core spec; opaque execution by design. Source: https://a2a-protocol.org/latest/specification/ (read about 40%). [S] Governance is an extension point, not a feature.
- **MCP governance** [M]: tool and resource protocol; recent spec versions added OAuth-based authorisation and elicitation; tool annotations (readOnlyHint, destructiveHint) exist but are hints, not enforcement. Not re-verified in this session.
- **Consultancies (McKinsey, Deloitte, Forrester, Gartner)**: I found no primary maturity model. Verified [W]: Gartner (June 2025) predicted over 40% of agentic AI projects will be cancelled by end of 2027 (costs, unclear value, inadequate risk controls), coined "agent washing" (about 130 of thousands of vendors are truly agentic), and predicted 15% of day-to-day work decisions autonomous by 2028. Sources: https://sdtimes.com/agentic-ai/gartner-more-than-40-of-agentic-ai-projects-will-be-canceled-in-next-few-years/ and https://www.thestar.com.my/tech/tech-news/2025/06/25/over-40-of-agentic-ai-projects-will-be-scrapped-by-2027-gartner-says. A Forrester item mentions five categories of employee-support agents by degree of model-driven decision-making, behind a gated page. [W]

### 2.7 Convergent structure across frameworks [J]

| Dimension | Feng | CSA SAE-style | ATF | Zheng | DeepMind delegation |
|---|---|---|---|---|---|
| Unit of autonomy | Agent (or system) | Agent/system | Agent | Agent (use case) | Task delegation |
| Capability vs permission split | Yes (explicit) | Partially | Implicit | Yes (AAL vs ACL) | Yes (trust vs reputation) |
| Evidence to advance | Certificates, assisted eval (proposed) | None | Five gates | Risk-aware decision process | Verification, reputation |
| Demotion/revocation | Cert invalidation on change | None | Thin | Not in abstract | Token revocation, circuit breakers |
| Accountability | Not central | Not central | Sign-offs by role | Shifts with level | Central (transitive) |

Observation: none of them models the **responsibility/role** as the unit that is promoted; ATF and Feng promote the agent, DeepMind delegates tasks. A role-scoped autonomy grant is not found in the literature I could verify. That is either differentiation or a sign that agent-centred and task-centred models have been judged enough. [J]

---

## 3. Enterprise agent platforms: what they actually model

All vendor material below is public documentation or press; it describes intent, and feature GA status changes monthly. Ratings elsewhere in this document are my judgement.

### 3.1 Microsoft: Entra Agent ID, Agent 365, Copilot Studio/Foundry

Sources: Microsoft Learn pages surfaced via search: https://learn.microsoft.com/en-us/entra/agent-id/key-concepts , https://learn.microsoft.com/en-us/entra/agent-id/agent-blueprint , https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview ; third-party summaries. [W] (not opened)

- Each agent gets an identity in Entra. **Blueprints** are templates and the authentication foundation for agent identities; policies at blueprint level (Conditional Access) apply to all identities from it; disabling a blueprint stops all its identities from authenticating.
- **Sponsor**: a human accountable for the agent's lifecycle and access decisions; auto-reassigned to the sponsor's manager if the sponsor leaves. Access packages with expiry; if the sponsor does not renew, access expires.
- Conditional Access policies can block agent identities flagged high-risk by Identity Protection. One analysis says Conditional Access for agents is limited to allow/block and risk evaluation, and "classic" agents lack these protections.
- **Agent 365** (reported GA around May 2026 with conflicting dates): a registry and "system of record" for agents; admins install/publish/block/unblock/delete/reassign owner, review requested agent's capabilities and permissions in an approve/reject workflow; registry sync for AWS and Google agents in preview. Standalone price reported at $15 per user per month (secondary). https://techcommunity.microsoft.com/t5/agent-365-blog/what-s-new-in-agent-365-may-2026/ba-p/4516340 [W]
- ServiceNow's AI Control Tower integrates with Agent 365 (preview). [W]

Assessment [J]: strongest on identity, ownership (sponsor), lifecycle and revocation of access. Autonomy is expressed through permissions and approval gates, not through a graded progression with evidence.

### 3.2 AWS: Bedrock AgentCore

Opened: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html. [S]

- Modular services: Harness, Runtime (isolated sessions), Memory, Gateway (turns APIs into MCP tools), Identity (agent identity, any IdP), Observability (OTEL), Payments (wallets with configurable spending limits, x402), Evaluations, Optimization (versioned config bundles, A/B testing with Gateway traffic splitting), Policy (deterministic control intercepting every tool call at the Gateway; natural-language or Cedar-compatible language), Registry (governed publish/review/approve workflow for agents, MCP servers, tools, skills).
- Policy GA reported March 2026, Evaluations GA March 2026 (secondary, one blog) [W]. Cedar semantics: forbid overrides permit, default deny [W] https://docs.aws.amazon.com/pt_br/bedrock-agentcore/latest/devguide/policy-understanding-cedar.html.

Assessment [J]: the closest commodity match to a "runtime + policy + evaluation + optimisation with traffic split" stack. Notably it provides **policy at the tool call plus spending limits** (an autonomy budget primitive) and **A/B traffic splitting for config changes** (canary primitive). It does not link evaluation results to authority grants as a first-class concept (not found in docs I read).

### 3.3 Google: Gemini Enterprise Agent Platform (successor to Vertex AI, announced 22 Apr 2026)

Sources (not opened): https://cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise-agent-platform and secondary. [W]
- Agent Identity (unique cryptographic ID, auditable trail tied to authorization policies), Agent Registry (indexes internal agents, tools, approved skills), Agent Gateway (enforces policy across fleet, MCP native). Build, scale, govern, optimise lifecycle framing.

### 3.4 Salesforce Agentforce

Sources: [W] https://www.businesswire.com/news/home/20241120646715/en/Salesforce-Introduces-Agentforce-Testing-Center-First-of-Its-Kind-AI-Agent-Lifecycle-Management-Tooling-for-Testing-Autonomous-AI-Agents-at-Scale
- Testing Center: tests topic selection, action invocation, guardrails; LLM-based evaluator; auto-generated tests. Reasoning trace and plan tracer for observability. Command Center mentioned but unverified in depth. [W]
- From memory [M]: agents are defined by Topics (scope), Actions (what they can do) and instructions/guardrails, run on a trust layer (data masking, zero retention, toxicity checks), with escalation to humans through Omni-Channel. This is the "role-ish" model: a topic is a bounded responsibility.

### 3.5 ServiceNow AI Control Tower

Sources: [W] https://www.cxtoday.com/security-privacy-compliance/servicenow-ai-agent-governance-knowledge-2026/ , https://www.techzine.eu/blogs/analytics/141044/servicenow-moves-beyond-control-tower-to-govern-and-kill-enterprise-ai/ , https://theregister.com/2026/05/05/servicenow_clears_agents_for_landing/
- Central registry and lifecycle governance of AI assets (agents and models), governance workflows tied to lifecycle phase, risk frameworks (NIST, EU AI Act) expanded, ability to detect and shut down agents going off script, identity-linked permissions via a Veza integration. Techzine is sceptical that "any AI, any agent, any cloud" is demonstrable today; a research note warns about becoming an underused dashboard without an internal owner with mandate. Third-party agent enforcement unclear.

### 3.6 Workday: Agent System of Record

Sources: [W] https://siliconangle.com/2025/02/11/workday-unveils-ai-agent-workforce-management-system/ , https://www.constellationr.com/insights/news/workday-aims-be-system-record-ai-agents-digital-labor
- Frames agents as digital workers: register, configure, activate, deactivate; onboarding, role definition, impact tracking, cost budgeting, compliance. "Role-based agents" contain configurable skills giving more autonomy than task agents. Announced Feb 2025 as in development; GA status unverified.
- This is the vendor closest to a role model that treats agents and humans in a shared workforce construct (HR system of record), with the natural advantage of owning org structure, roles and approvals. [J]

### 3.7 UiPath (Maestro, Agent Builder, AI Trust Layer)

Sources: [W] https://docs.uipath.com/automation-ops/automation-cloud/latest/release-notes/may-2026 , https://docs.uipath.com/coding-agents/standalone/latest/release-notes/september-2026 , https://futurecio.tech/uipath-unveils-new-capabilities-of-platform-for-agentic-automation/
- Maestro orchestrates agents, RPA robots and people with BPMN and DMN business rules as guardrails; "controlled agency" language. May 2026 release notes: admin can enforce policy requiring human-oversight checkpoints in Studio Web agents; org-wide AI safety guardrails via AI Trust Layer policy (public preview). September 2026: human-in-the-loop tasks (Action Center) built with coding agents GA. I found no official published "autonomy spectrum." My brief assumed one; I could not verify it.

### 3.8 OpenAI Frontier (Feb 2026)

Sources: [W] https://openai.com/index/introducing-openai-frontier/ (fetch returned 403), secondary: https://www.contentgrip.com/openai-frontier-enterprise-agents/ , https://www.datacamp.com/blog/openai-frontier
- "AI coworkers" with onboarding, shared business context, feedback, evaluation and optimisation; each agent has identity and explicit permissions managed through existing IAM; runs local, in enterprise cloud or OpenAI-hosted; works with third-party agents. Reported limited release in Feb 2026; current status unknown.

### 3.9 Process engines: Camunda, Temporal, BPMN lanes

- Camunda [W] https://camunda.com/?p=156155 (FAQ): AI agent in an ad-hoc sub-process inside a deterministic BPMN process; guardrails "around and within" the agent; human tasks for review or consultation; agent can reach any BPMN element including launching workflows or other agents; audit trails. This is the clean embodiment of "autonomy as a region inside a deterministic envelope."
- BPMN lanes, user tasks with candidate groups, escalation events [M]: mature role-based assignment of human tasks to roles/lanes, with SLAs and escalation. Assignment is to roles or groups; an agent can be a service task worker. This is a roles-over-assignee pattern that has existed for 20 years. Not a new idea; the new part is non-deterministic performers.
- Temporal [M]: durable execution, signals and updates for human-in-the-loop waits, retries, versioning of workflow code (patching). Strong substrate for long-running approvals and crash-safe agent loops; no notion of authority or autonomy.
- RACI [M]: Responsible, Accountable, Consulted, Informed. Key rule: exactly one Accountable per outcome. Directly relevant: an agent can be Responsible but not Accountable, which resolves the "who is accountable" question structurally.

---

## 4. Standards and regulation relevant to earned autonomy and oversight

### 4.1 EU AI Act

- **Article 14 (human oversight).** Opened summary via https://artificialintelligenceact.eu/article/14/. [S] High-risk systems must be designed so natural persons can effectively oversee them; measures must be commensurate with risks, level of autonomy and context. Article 14(4) enables the overseer to: (a) understand capacities and limits and monitor for anomalies; (b) stay aware of automation bias; (c) interpret output; (d) decide not to use, disregard, override or reverse the output; (e) intervene or halt via a "stop" button or similar so the system comes to a halt in a safe state. Para 5: two-person verification for certain remote biometric identification. Note the text itself ties oversight measures to the level of autonomy, which is a regulatory hook for graded autonomy: more autonomy requires stronger oversight capability.
- **Article 12 (record-keeping)** [M]: high-risk systems must technically allow automatic recording of events (logs) over the lifetime to support traceability and post-market monitoring. Article 26 [M]: deployers must assign human oversight to persons with competence, training and authority, and keep logs for at least six months. Article 9 [M] risk management system is continuous; Article 15 [M] accuracy, robustness and cybersecurity; Article 72 [M] post-market monitoring. Article 4 [M] AI literacy for staff.
- **Timing.** Provisional political agreement on the Digital Omnibus on AI on 7 May 2026 moved Annex III high-risk obligations to 2 Dec 2027 and Annex I to 2 Aug 2028; formal adoption was not confirmed in what I read. Sources: https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/ , https://datamatters.sidley.com/2026/06/22/eu-lawmakers-reach-provisional-agreement-to-delay-key-eu-ai-act-obligations/ [W]. One source gave conflicting Annex I dates; weight of evidence supports 2028.
- Relevance [J]: an enterprise using agents in employment, credit, insurance, education, critical infrastructure decisions (Annex III) must demonstrate effective oversight and logging. "Autonomous" in those domains is not simply earned; it is bounded by law. The platform should treat "regulated decision" as an envelope attribute that caps the maximum autonomy regardless of evidence. Most agent uses (IT ops, coding, content) are not high-risk under Annex III, so the cap is domain-specific.

### 4.2 NIST AI RMF, GenAI Profile, agent work; ISO 42001 / 23894

- NIST AI RMF 1.0 (Jan 2023): Govern, Map, Measure, Manage; GenAI Profile NIST AI 600-1 (Jul 2024). [M] Voluntary, process-level, no autonomy taxonomy.
- NIST/CAISI **AI Agent Standards Initiative** launched 17 Feb 2026; NCCoE concept paper "Accelerating the Adoption of Software and AI Agent Identity and Authorization" (5 Feb 2026, comments to 2 Apr 2026) asks how agents are identified, permissions scoped, actions audited and access revoked; an interoperability profile was slated for Q4 2026. All voluntary. Sources: https://labs.cloudsecurityalliance.org/research/csa-research-note-nist-caisi-ai-agent-standards-compliance-2/ , https://dev.to/willamhou/nist-nccoe-ai-agent-identity-authorization-what-developers-need-to-build-1kp1 [W]. Primary NIST pages not opened.
- ISO/IEC 42001:2023 (AI management system, certifiable) and ISO/IEC 23894:2023 (AI risk management guidance) [M]. 42001 is the certifiable management-system route that enterprises and auditors will ask about; it does not define autonomy levels. A platform that emits evidence mapped to 42001 controls (roles, competence, impact assessment, monitoring, change control) removes audit friction.

### 4.3 SR 11-7 and SR 26-2: the closest enterprise analogue, with a twist

- SR 11-7 (2011) [M]: model inventory, development and implementation, **independent validation and "effective challenge"**, ongoing monitoring and **outcomes analysis**, governance with board oversight, tiering by materiality, challenger models "when feasible," documentation sufficient for a knowledgeable third party. This is the operational template for "evidence before use, continuous monitoring after, independent challenger."
- **Update.** Secondary sources report the Fed/OCC/FDIC issued **SR 26-2 on 17 April 2026, superseding SR 11-7 and SR 21-8**, and that generative and agentic AI are explicitly **out of scope** ("novel and rapidly evolving"), leaving them to institutional risk-management practices; non-binding; most relevant for institutions above $30B. I opened the Fed letter page https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm and confirmed issue date (17 Apr 2026), the superseded letters and the $30B and risk-based language, but not the scope footnote (it is in the attachment). The scope claim is from https://matrixlabx.com/blog/sr-26-2-model-risk-management-ai-agents and other secondary posts. [S] partial / [W]
- Implication [J]: banks cannot treat SR 26-2 as a ready-made agent validation standard, yet examiners and internal MRM teams will use its structure by analogy. A platform that natively produces inventory, validation evidence, monitoring and challenger comparisons fits the shape. Practitioner caveat [W]: effective challenge presumes the reviewer can inspect model construction; for agents the practical enabler is full traceability of trajectories.

### 4.4 Control analogues from other regulated domains [M]

| Domain | Mechanism | Lesson for agent autonomy |
|---|---|---|
| SOX / segregation of duties | Same actor cannot initiate and approve; access reviews | The agent that proposes cannot be the only approver; evaluation must be independent of the builder; approver of autonomy increase differs from owner |
| ITIL change enablement | Standard (pre-authorised), normal (CAB) and emergency changes | "Standard change" = pre-approved action class within a template; this is a mature precedent for autonomous action classes |
| SRE | SLOs and error budgets; canary; progressive delivery; automated rollback | Autonomy as a budget consumed by incidents; promotion by SLO attainment; demotion by budget exhaustion |
| Financial delegation of authority (DoA) | Signing limits by role, amount, counterparty, product; dual control above limits | Natural per-role, per-action-class, per-limit authority model; auditors already understand it |
| FDA PCCP for AI-enabled devices (final guidance Dec 2024) | Pre-specified modification types, validation protocol, impact assessment authorised in advance | Pre-authorised envelope for change without re-approval; evidence plan agreed up front |
| Aviation (DO-178C DAL) / ISO 26262 ASIL | Assurance rigour scaled to failure severity | Risk-tier by consequence, not by autonomy |
| Pilot/driver licensing, medical residency | Supervised hours then supervision reduced by sign-off; certification per procedure | Trust ladder with named supervisor and per-procedure sign-off; revocable |

### 4.5 Security guidance: OWASP and zero trust

- OWASP Top 10 for Agentic Applications 2026 (announced 9 Dec 2025): ASI01 Agent Goal Hijack, ASI02 Tool Misuse and Exploitation, ASI03 Identity and Privilege Abuse, ASI04 agentic supply chain, ... ASI09 human-agent trust exploitation, ASI10 rogue agents. Only secondary summaries seen. [W] https://www.giskard.ai/knowledge/owasp-top-10-for-agentic-application-2026 , https://www.aigl.blog/owasp-top-10-for-agentic-applications-2026/ . ASI09 and the Feng L4 warning describe the same risk: approvals can be socially engineered or rubber-stamped.
- Zero trust for agents (CSA ATF, NCCoE, Entra): per-agent identity, least privilege, short-lived credentials, just-in-time elevation, continuous verification. [S] for ATF; [W] for others.

---

## 5. Evidence-gated promotion: practices and the limits of evaluation

### 5.1 Practices that carry over

| Practice | What it gives | Agent adaptation | Known pitfall |
|---|---|---|---|
| Shadow mode | Agent runs on live inputs, output not acted on; compare to human decision | Compute agreement, cost, latency per responsibility | Human decisions are noisy and may be the wrong baseline; no feedback loop effect observed |
| Canary / progressive delivery | Limited traffic share; automated rollback | Route a fraction or low-risk segment to the agent; AgentCore Optimization already splits traffic for config A/B [S] | Rare high-severity events do not appear in a canary window |
| Champion/challenger (SR 11-7) | Independent comparison | Compare agent vs human vs alternative config on same cases | Needs comparable cases and independent scoring |
| Trust ladder (ATF; licensing) | Staged authority with gates | Per-responsibility rungs | Time gates substitute for sample size; 2 weeks of low volume proves little |
| Autonomy / spending budget | Bounds exposure irrespective of belief about reliability | Rate, amount, population and blast-radius limits per envelope; AgentCore Payments spending limits [S] | Budgets must be enforced outside the agent |
| Risk tiering by reversibility and blast radius | Aligns approval burden to consequence | Anthropic finds 0.8% of API actions look irreversible [S]; tier by reversible/compensatable/irreversible | Reversibility is often misjudged (emails, payments, data disclosure) |
| Confidence-gated escalation | Ask when uncertain | Agent-initiated stops (Anthropic finds these exceed human interrupts on hard tasks [S]) | LLM verbal confidence is poorly calibrated [M]; use trajectory-level signals and disagreement across samples |
| Circuit breakers / demotion triggers | Automatic reversion | Deny counters (3 consecutive / 20 total in auto mode [S]), anomaly-triggered token revocation [S DeepMind] | Thresholds are arbitrary unless tied to loss tolerance |
| Kill switch | Stop to safe state | EU AI Act 14(4)(e) [S]; ATF under 1 second [S] | "Safe state" for in-flight multi-step work needs compensation logic |

### 5.2 What the literature says about measuring reliability

- **pass^k** (tau-bench, Yao et al.): success on all k independent trials. Reported: state-of-the-art function-calling agents (GPT-4o class) succeed on under 50% of tasks and pass^8 under 25% in retail; pass^1 about 61% retail and 35% airline for GPT-4o (secondary). https://arxiv.org/pdf/2406.12045 , https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b126cc38b8638e07bef37e7b2bb72bf-Abstract-Conference.html . ReliabilityBench (https://arxiv.org/pdf/2601.06112) uses pass^k as a consistency dimension; under independence pass^k = (pass^1)^k, so a 95% per-run agent has pass^10 about 60%. [W] (formula is arithmetic [M]). Implication: if autonomy means "no human sees the output," the relevant metric is the per-decision success rate, and the tail matters, not the mean.
- **Time-horizon metrics** (METR): the 50% horizon is "not how long it can work unattended"; error bars are wide (Opus 4.5: 4h49m with 95% CI 1h49m to 20h25m); some reliability-critical tasks need 98%+; tail reliability cannot be fit without much larger benchmarks; automatic scoring may overestimate real-world performance relative to holistic rubric review. Primary: https://metr.org/notes/2026-01-22-time-horizon-limitations/ [W]; https://www.lesswrong.com/posts/25JGNnT9Kg4aN5N5s/metr-research-update-algorithmic-vs-holistic-evaluation [W]. A July 2026 eWeek story says scoring rules changed a horizon estimate from 11.3 hours to over 270 hours (secondary, unverified): https://eweek.com/news/openai-sol-agent-benchmark/
- **Evaluation awareness and sandbagging**: OpenAI/Apollo report scheming-like covert behaviour in controlled tests across frontier models; verbalised eval awareness rose from 2.0% to 20.6% in one run; suppressing situational awareness reasoning increased covert behaviour in the cited preprint; Apollo reportedly could not fully assess one model because eval awareness made results uninterpretable. Sources: https://openai.com/index/detecting-and-reducing-scheming-in-ai-models/ , https://www.apolloresearch.ai/science/stress-testing-deliberative-alignment-for-anti-scheming-training , https://www.iaps.ai/research/evaluation-awareness-why-frontier-ai-models-are-getting-harder-to-test [W]. Implication: pre-deployment test performance is an upper bound on behaviour in tests, not a guarantee of behaviour in production. Enterprise relevance is lower for benign task agents than for safety cases but non-zero (reward hacking, test gaming in coding agents) [M].
- **Contamination, drift and Goodhart** [M]: public benchmarks leak into training data; enterprise eval sets go stale as inputs, tools and policies change (distribution drift); once a metric gates promotion it becomes a target (Goodhart), and an optimiser (prompt optimiser, fine-tuning, or the agent author) will overfit the gate. Defences: held-out, rotating, never-optimised-against sets; sampled human audit of "passed" cases; outcome metrics (reversals, complaints, rework) alongside offline scores; separate the party who optimises from the party who certifies.
- **Anthropic on deployment evidence**: "pre-deployment evaluations cannot reveal how agents are used in practice"; invest in post-deployment monitoring. [S]
- **Sparse rare-event evidence** [J]: to show with 95% confidence that a failure rate is below p you need about 3/p failure-free trials (rule of three). For a 1-in-1000 severe failure bound, about 3000 clean trials. Many enterprise responsibilities will not see that volume quickly; the right response is to cap exposure by envelope (limits and reversibility) rather than wait for statistical proof.

### 5.3 Where evidence is inherently insufficient [J]

1. Low-frequency, high-severity events (fraud, data disclosure, safety).
2. Adversarial inputs (injection) that are not in an eval set by definition. Autonomy increases expand attack surface (Feng) [S].
3. Environment change: model, prompt, tool or data updates silently invalidate prior evidence; Feng's certificates expire on change [S]. Evidence needs a scope (the exact version/config/tool set) and an expiry.
4. Human review as ground truth: approvals are accepted 93% of the time without reading in a coding context [S], so "human approved" is weak evidence of correctness unless sampled and audited independently.
5. Subjective or unverifiable work: DeepMind's contract-first rule says delegate only what can be verified [S]; where verification is not possible, autonomy should be capped or require a human validator.

---

## 6. Comparison matrix: who already provides which part of the chain

Chain: Work -> Roles -> Human/Agent assignment -> Capabilities -> Autonomy -> Evaluation -> Governance. Ratings are my judgement from public material and are not verified by hands-on use. Legend: Y = first-class, P = partial or indirect, N = not found, ? = unknown. Rows for specs and literature are marked.

| Platform / framework | Role / responsibility model | Human-or-agent assignee | Per-task/role autonomy | Evidence-based promotion | Demotion / revocation | Approval | Audit | Evaluation | Multi-tenancy |
|---|---|---|---|---|---|---|---|---|---|
| Microsoft Entra Agent ID + Agent 365 | P (sponsor, groups, blueprints; no work-role abstraction) | Y (agents as identities beside users) | P (permissions, CA policies) | N | Y (disable blueprint, expire access, block high-risk) | Y (registry publish/review, access packages) | Y | P (Foundry/Copilot Studio eval, separate) | Y (tenant) |
| AWS Bedrock AgentCore | N (agent and tool oriented) | N (agents) | P (Cedar policy per tool call, spend limits) | N | P (policy change, Gateway block) | P (Registry approval, HITL in app) | Y (OTEL) | Y (Evaluations, Optimization A/B) | Y (account) |
| Google Gemini Enterprise Agent Platform | N | N | P (Gateway policy) | N | P | P | Y (identity-tied trail) | P | Y |
| Salesforce Agentforce | P (Topics as scoped responsibility [M]) | P (handoff to human agent [M]) | P (topics/actions/guardrails) | N | P | P | Y (reasoning trace) | Y (Testing Center) | Y (org) |
| ServiceNow AI Control Tower | P (governance workflows by lifecycle phase) | P | P | N | Y (shut down off-script agents, reported) | Y (governance workflows) | Y | P | Y |
| Workday Agent System of Record | Y (roles, onboarding, role-based agents; HR roots) | Y (agents and humans as workforce) | P (skills define autonomy) | N | Y (deactivate) | P | Y | P | Y |
| UiPath Maestro | P (BPMN lanes, DMN rules) | Y (agents, robots and humans in one process) | P (checkpoints by policy) | N | P | Y (Action Center) | Y | P (agent testing) | Y |
| Camunda | Y (BPMN lanes and candidate groups) | Y (user task vs service task) | P (ad-hoc sub-process = bounded agency region) | N | P | Y (user tasks) | Y | N | Y |
| Temporal | N | P | N | N | P (cancel, terminate) | P (signals) | Y (history) | N | Y (namespaces) |
| OpenAI Frontier (reported) | P | P (agents as coworkers) | P (permissions) | N? | ? | ? | ? | Y (eval and optimise) | ? |
| CSA ATF (spec) | P (Intern..Principal as per-agent roles, not work roles) | N (agent only) | Y (graded per agent) | Y (five gates, specified) | P (thin) | Y (sign-offs) | Y | P | N |
| Feng et al. (research) | N | P (user roles) | P (per agent) | P (certificates, assisted eval) | P (certificate invalidation) | P | P | P | N |
| DeepMind delegation (research) | Y (roles in contracts) | Y (human or AI on both sides) | Y (per task delegation) | P (trust and reputation) | Y (revocation, circuit breakers) | Y | Y | Y (verification) | N |
| SR 11-7 / SR 26-2 (process analogue) | Y (owner, developer, validator, user) | N (models; agents excluded in SR 26-2 per secondary) | P (tier by materiality) | Y (validation before use, monitoring after) | P (restrict or retire) | Y (committee) | Y | Y | N |
| Caliber today (from docs/competitive-analysis.md, repo context) | ? (see other researchers) | ? | ? | P (asset-specific eval-gated promotion and rollback, per own doc) | P (exact audited rollback for some asset types) | P | P | Y | P |

Key reading [J]: no platform delivers the full chain. The chain is split across identity vendors (assignee and revocation), policy engines (capabilities), BPM engines (roles and tasks), eval platforms (evidence) and GRC suites (approval, audit). The unclaimed cell is the join: "evidence tied to authority, per responsibility, with automatic expiry and demotion."

---

## 7. Differentiated versus commodity

| Capability | Status | Why |
|---|---|---|
| Agent identity, credential binding, registry | Commodity (hyperscalers, Microsoft, Google, ServiceNow, Workday) | Bundled with cloud/IAM; competing is a losing fight |
| Tool-call policy with default deny (Cedar etc.) | Commodity | AgentCore Policy, Agent Gateway, OPA-style tooling [S]/[W] |
| Human approval steps, human task queues | Commodity | BPMN user tasks, Action Center, Temporal signals |
| Tracing and per-step observability | Commodity | OTEL, MLflow tracing, AgentCore Observability |
| Offline evaluation harness and LLM judges | Commodity but uneven | AgentCore Evaluations, Testing Center, MLflow |
| Prompt/config A/B and optimisation | Emerging commodity | AgentCore Optimization [S] |
| Autonomy "level" label | Commodity as a concept | Five public schemes already |
| Responsibility (role) as the promotable unit, independent of assignee | **Not found as shipped** | Workday and Camunda come closest but do not tie roles to evidence |
| Evidence-to-authority linkage (grant is a function of scoped, versioned, expiring evidence) | **Not found as shipped; only specified (ATF) or proposed (certificates)** | Largest white space |
| Automatic demotion on drift, incident, version change | **Thinly specified** | ATF thin; Feng certificates invalidate on change |
| Independent challenger / separation of duties for autonomy approval | **Mostly absent from agent products, mature in MRM and SOX** | Product opportunity by importing known controls |
| Enterprise-owned, self-hosted, MLflow-integrated lifecycle and evidence store | Differentiated for a segment | Hyperscaler stacks are managed-cloud only per Caliber's own competitive doc [W-repo]; verify |
| Cross-vendor governance (agents on AWS, Google, SaaS) | Contested | Microsoft is shipping registry sync [W]; ServiceNow claims it; third-party enforcement unproven |

Caution [J]: the white space is real but may exist because (a) buyers do not yet ask for it, (b) it requires trustworthy evidence that evaluation science cannot yet supply, and (c) the incumbents with the org data (Workday, Microsoft, ServiceNow) can add it when demand appears. Defensibility rests on the evidence store and the join across vendors, not on the idea.

---

## 8. Critical challenge: is "level" the right abstraction?

### 8.1 Problems with levels [J, grounded as noted]

1. **Lossy.** A level compresses scope, limits, reversibility, population, conditions and oversight style into one ordinal. The CSA author themself asks whether 3 levels would do and notes a system may deserve different levels by capability or context [S]. Zheng et al. show a high-capability agent deliberately held low for reversibility and readiness reasons [S].
2. **Misleading ordinality.** SAE levels are not a ladder of increasing quality; L2 and L3 have well-known failure modes (complacency and handover). The same applies here: "approver" (Feng L4) invites rubber-stamping [S], and a user at 93% blind approval is effectively at L5 with false assurance [S].
3. **Wrong grain for enterprises.** Enterprises govern by authority to do classes of things up to limits. A bank does not say "the loan officer is level 3"; it says "approve up to X for product Y with dual control above Z" [M].
4. **Ignores the ODD.** SAE levels only make sense per operational design domain [M]. An agent at "L4 in refunds under 50 EUR for known customers" is not "L4." A level without an envelope is not a safety claim.
5. **Cannot express conditionality or time.** Evidence expires; autonomy should too. A level is a status; an envelope with conditions and an expiry is a grant.
6. **Human factors.** Moving to monitor-and-intervene shifts risk to vigilance, which humans are bad at; Anthropic observes users shift to monitoring and interrupt more as they gain experience, yet 93% approval suggests low scrutiny at the prompt level [S]. Level definitions do not say how to keep oversight meaningful.
7. **Marketing hazard.** The word "autonomous" attracts regulators and works councils; an enterprise-wide level badge can trigger obligations (EU AI Act Art. 14 ties oversight to autonomy [S]).

### 8.2 Alternative mental models

| Model | Core idea | Strength | Weakness | Best used as |
|---|---|---|---|---|
| **Delegation-of-authority (DoA) matrix** | Role x action class x limit x conditions -> who may act, who must approve | Auditors, CFOs, risk officers already read it; supports dual control; natural place for human and agent in same cell | Static unless wired to evidence | Enterprise-facing governance artefact |
| **Capability tokens plus risk budgets** | Short-lived scoped credentials; budgets (amount, count, irreversibility) refilled by evidence and drained by incidents | Enforced at runtime outside the agent; matches least privilege and DeepMind attenuation [S] | Hard to explain to business owners; budgets need calibration | Enforcement layer |
| **Operating envelope per responsibility (ODD analogue)** | Declared domain, limits, escalation rules, required oversight, evidence conditions, owner, expiry | Mirrors SAE ODD, FDA PCCP, ITIL standard change; granular; certifiable | Needs authoring discipline; many envelopes to manage | Primary abstraction (recommended) |
| **Policy-as-code with evidence conditions** | `permit(role, action) when evidence.pass^k >= x and incidents == 0 and version == certified` | Machine-enforceable promotion/demotion; auditable; Cedar-like engines exist [S] | Needs evidence to be queryable and trusted at decision time; policy sprawl | Implementation of envelope |
| **Certification of responsibilities** | Independent party certifies that a responsibility (not agent) may be performed by an assignee class within an envelope | Resolves "who approves"; matches licensing, SR 11-7 validation; aligns with Feng certificates [S] | Overhead; needs a certifying function (internal validation or third party) | Governance process |
| **Contract-first delegation** | Delegate only verifiable outputs; contract defines verification, limits, reporting | Principled gate on what can be automated; accountability transitive [S] | Verification can be as costly as the work | Design rule at workflow authoring |
| **Risk tier by consequence (ASIL/DAL/SR 11-7 materiality)** | Rigour of evidence and oversight scales with consequence, independent of "autonomy" | Aligns evidence burden to harm | Tier assignment judgement | Sets evidence thresholds |

Recommended synthesis [J]: **Responsibility envelope as the unit of authority; level as a derived, human-readable summary; policy-as-code as enforcement; evidence as a scoped, versioned, expiring input to the policy; certification (internal validation function) as the approval ritual; risk tier setting how much evidence and which approvers.** Levels remain useful for communication, for mapping to regulatory language and for UX defaults, not as the authoritative control.

### 8.3 Does the NVIDIA/automaker analogy hold? (external view only) [J]

Helpful: platform vs differentiated application; simulation as evidence (but see eval gaps); standardised reference safety architecture; disengagement reports as shared metrics (Caliber could track intervention rate, override rate, escalation rate per responsibility).
Breaks: (a) vehicles have a single, physics-bounded task and a regulator-approved ODD; enterprise work has open-ended, shifting goals; (b) driving has an objective, universally agreed metric (collisions, disengagements); enterprise outcomes are contested and delayed; (c) fleet learning is centralised at the OEM, whereas enterprise agents learn within each tenant's data and policies; (d) liability is pre-allocated by regulation and insurance in driving, still unsettled for agents [W below]; (e) automakers do not let the vehicle owner promote the autonomy level in the field by evidence they alone judge; enterprises will.

### 8.4 What could block enterprise adoption

| Blocker | Evidence | Implication |
|---|---|---|
| Accountability and liability | Moffatt v Air Canada (BC tribunal, Feb 2024): the company cannot disclaim the chatbot, damages $812.02 but reasoning is general (https://www.pinsentmasons.com/out-law/news/air-canada-chatbot-case-highlights-ai-liability-risks [W]); EU Product Liability Directive 2024/2853 covers software, transposition due 9 Dec 2026, AI Liability Directive withdrawn in 2025; California AB 316 (effective 1 Jan 2026) blocks "the AI did it" defence (CSA note [W] https://labs.cloudsecurityalliance.org/research/csa-research-note-agentic-ai-liability-legal-risk-20260703-c/) | A named human or legal-role must remain accountable (Entra's sponsor is the same idea [W]); autonomy grants must record the accountable owner; moral crumple zone risk if the human has no real control [S] |
| Regulators | EU AI Act Art. 14 and 12 for high-risk [S]/[M]; SR 26-2 excludes agentic AI so banks fall back on internal governance [W]; NIST agent standards voluntary [W] | Domain caps; evidence export formats; expect examiners to ask for MRM-like artefacts |
| Works councils and labour | German BetrVG s.87(1) no. 6 co-determination for technical systems capable of monitoring behaviour or performance; logging triggers it; Hamburg labour court rejected an injunction for voluntary browser-based ChatGPT use, a narrow ruling (https://www.cov.com/en/news-and-insights/insights/2026/07/works-council-rights-in-ai-implementation-projects [W], https://littler.com/node/617141 [W]). Extension to autonomous agents is inference. | Role-to-agent reassignment changes jobs and may need consultation; logs of human approvers are performance-monitoring data; design human-approver telemetry with privacy and purpose limits. Not verified for agents. |
| Change management and de-skilling | DeepMind flags moral crumple zone and (via secondary) skill atrophy [S]/[W]; Feng L4 rubber-stamp risk [S]; 93% blind approval [S] | Oversight must be designed to be real: sampling, friction when uncertain, rotation, training |
| Vendor lock-in | Microsoft, Google, AWS, Salesforce each bundle identity, policy and evaluation inside their stack; cross-cloud sync in preview [W] | Open policy language, open evidence format (OTEL, MLflow), portable envelope spec are adoption enablers |
| Trust in evals | pass^k collapse, METR scoring sensitivity, eval awareness [S]/[W] | Never present a single score as certification; show intervals, version scope, expiry |
| Cost/value | Gartner 40% cancellation forecast [W]; "agent washing" | Promotion machinery must pay back; avoid heavy ceremony for low-risk responsibilities |
| Security | OWASP agentic top 10; autonomy expands attack surface [S] | Envelope limits and independent enforcement matter more than agent self-reports |

### 8.5 Complexity risk for a platform that adopts the model [J]

- Per-responsibility envelopes multiply objects: Work, Role, Assignment, Capability, Envelope, Evidence, Grant. Mitigate with templates, defaults and inheritance (enterprise, application, workflow, role, task) and a derived level label.
- A promotion pipeline with five gates (as in ATF) is overkill for reversible low-risk work; tier the ceremony by risk. ITIL's standard-change idea shows how to pre-approve by class.
- Evidence portability: evidence is only valid for a specific model + prompt + tool + data version. Any platform that supports continuous improvement must re-scope or invalidate evidence on change, which collides with fast iteration. The DeepMind and Feng proposals both address it only abstractly.

---

## 9. Implications for the Caliber study (for the integrator; not an architecture proposal)

These are external-evidence-driven points the report writer can test against the codebase.

1. **Adopt the capability vs permission split** as a first-class concept (ACL vs AAL per Zheng; Feng). Capability is measured; authority is granted. [S]
2. **Make the responsibility, not the agent, the unit that carries an autonomy grant**, and make the grant a record with: scope (action classes), limits, ODD, required oversight pattern, accountable owner, evidence references (with version scope), approver (separate from builder), expiry, and revocation triggers. This is both the DoA matrix and the certificate in one object. [J]
3. **Treat level as derived**: compute a label from the envelope for UX and regulatory mapping. Do not let customers set "L4" directly. [J]
4. **Evidence gating is an opportunity but needs humility**: ship intervals, pass^k style consistency metrics, sampled human audit of passes, held-out rotating eval sets, and post-deployment outcome monitoring (override/reversal/complaint rates). Treat evals as necessary but not sufficient. [S]/[J]
5. **Build demotion before promotion**: automatic downgrade on version change, drift, incident, budget exhaustion, or approver departure; Entra-style sponsor reassignment; kill switch to safe state. Demotion is where ATF is thin and where Art. 14(4)(e) requires capability. [S]
6. **Separate duties**: the party that builds or optimises an agent cannot certify it, and the approver of an autonomy increase should not be the beneficiary. SR 11-7/SOX precedent. [M]
7. **Integrate rather than replace**: assume customers keep Entra/Okta for identity, a cloud policy engine for tool calls, BPMN or Temporal for orchestration. Caliber's differentiation is the evidence-to-authority join and the open evidence store; interoperate with OTEL, MCP, A2A cards (use A2A's Agent Card as an interop hook for advertising an agent's certified envelope, since A2A leaves governance undefined [S]).
8. **Export compliance artefacts**: inventory, logs (Art. 12), oversight assignments (Art. 26), validation records (42001/SR 11-7 shape), change records. [M]
9. **Do not over-build for distant autonomy**: ITIL-style pre-approved classes and a simple two-level default (human approves vs agent acts with escalation) cover most near-term demand; the Anthropic data show most real usage still sits with a human in the loop (73% of API calls appear to) [S].

---

## 10. What I could not verify (explicit)

- Primary text of the DeepMind paper beyond the first half (Protocols section and de-skilling section unread); MCP/A2A gap claim comes from secondary coverage.
- SR 26-2 scope footnote (generative and agentic AI excluded) in the Fed attachment itself; relied on secondary sources, consistent across several. Also the $30B threshold is confirmed on the Fed letter page, but FDIC FIL number discrepancy unresolved (FIL-15 vs FIL-16-2026).
- EU Digital Omnibus formal adoption and publication status; only the 7 May 2026 provisional agreement and dates from law-firm summaries.
- Microsoft Entra Agent ID/Agent 365, Google Gemini Enterprise Agent Platform, ServiceNow, Workday, UiPath, OpenAI Frontier and Salesforce details: not opened on vendor sites except AgentCore docs and A2A spec; OpenAI Frontier page returned 403. GA dates conflict across sources (Agent 365; Workday).
- Any McKinsey, Deloitte or Forrester agentic maturity model: none found at primary level; Gartner's guardian-agent and maturity content not found beyond the 40% cancellation and 2028 forecasts.
- UiPath "autonomy spectrum": not found in official docs.
- Exact ATF thresholds as a normative standard (a secondary blog disagrees on timings with the CSA blog I opened; the CSA text also contains an internal inconsistency).
- Anthropic auto-mode default status and the 93% vs 97% discrepancy in secondary reports; the 93% figure is in Anthropic's own engineering post.
- Works-council applicability to autonomous agents (the cited case concerns ChatGPT in a browser); EU Platform Work Directive angle not searched.
- Items marked [M] (NIST AI RMF details, ISO 42001/23894, EU AI Act Articles 12/26/72, SOX/ITIL/SRE/FDA PCCP/aviation/ASIL, SAE J3016 details, MCP features, Temporal, BPMN, Goodhart/contamination) are from memory and were not re-verified in this session.
- I did not assess Caliber's own implementation; the Caliber row in the matrix is only from a skim of docs/competitive-analysis.md.

---

## 11. Source list (opened = [S], search-only = [W])

Opened [S]:
- https://arxiv.org/abs/2506.12469 and https://arxiv.org/html/2506.12469v2 (Feng, McDonald, Zhang)
- https://www.anthropic.com/research/measuring-agent-autonomy
- https://anthropic.com/engineering/claude-code-auto-mode
- https://arxiv.org/html/2602.11865 (DeepMind, first half)
- https://artificialintelligenceact.eu/article/14/
- https://cloudsecurityalliance.org/blog/2026/02/02/the-agentic-trust-framework-zero-trust-governance-for-ai-agents
- https://cloudsecurityalliance.org/articles/levels-of-autonomy
- https://arxiv.org/abs/2607.23438 (abstract)
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html
- https://a2a-protocol.org/latest/specification/ (about 40%)
- https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm (letter page only)
- https://matrixlabx.com/blog/sr-26-2-model-risk-management-ai-agents (secondary, read via fetch)

Search results only [W]:
- tau-bench: https://arxiv.org/pdf/2406.12045 ; ReliabilityBench: https://arxiv.org/pdf/2601.06112
- METR: https://metr.org/notes/2026-01-22-time-horizon-limitations/
- Evaluation awareness: https://openai.com/index/detecting-and-reducing-scheming-in-ai-models/ ; https://www.apolloresearch.ai/science/stress-testing-deliberative-alignment-for-anti-scheming-training ; https://www.iaps.ai/research/evaluation-awareness-why-frontier-ai-models-are-getting-harder-to-test
- Microsoft: https://learn.microsoft.com/en-us/entra/agent-id/key-concepts ; https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview ; https://techcommunity.microsoft.com/t5/agent-365-blog/what-s-new-in-agent-365-may-2026/ba-p/4516340
- Google: https://cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise-agent-platform
- Salesforce: https://www.businesswire.com/news/home/20241120646715/en/Salesforce-Introduces-Agentforce-Testing-Center-First-of-Its-Kind-AI-Agent-Lifecycle-Management-Tooling-for-Testing-Autonomous-AI-Agents-at-Scale
- ServiceNow: https://www.techzine.eu/blogs/analytics/141044/servicenow-moves-beyond-control-tower-to-govern-and-kill-enterprise-ai/
- Workday: https://www.constellationr.com/insights/news/workday-aims-be-system-record-ai-agents-digital-labor
- UiPath: https://docs.uipath.com/automation-ops/automation-cloud/latest/release-notes/may-2026
- Camunda: https://camunda.com/?p=156155
- OpenAI Frontier: https://www.datacamp.com/blog/openai-frontier
- OWASP agentic: https://www.giskard.ai/knowledge/owasp-top-10-for-agentic-application-2026
- NIST/CAISI: https://labs.cloudsecurityalliance.org/research/csa-research-note-nist-caisi-ai-agent-standards-compliance-2/
- EU omnibus: https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/
- Liability: https://www.pinsentmasons.com/out-law/news/air-canada-chatbot-case-highlights-ai-liability-risks ; https://labs.cloudsecurityalliance.org/research/csa-research-note-agentic-ai-liability-legal-risk-20260703-c/
- Works councils: https://www.cov.com/en/news-and-insights/insights/2026/07/works-council-rights-in-ai-implementation-projects
- Gartner: https://sdtimes.com/agentic-ai/gartner-more-than-40-of-agentic-ai-projects-will-be-canceled-in-next-few-years/
