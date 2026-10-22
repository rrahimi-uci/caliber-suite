# -*- coding: utf-8 -*-
"""Training content for the CALIBER Cookbooks HTML guide (data only).

Every recipe is installable through the CALIBER **Cookbooks** UI as a paused,
review-required workflow draft. Live providers and connectors remain explicit
deployment prerequisites.
Verified against the shipped build (see ../FEASIBILITY.md, ../ARIA-AUTONOMY.md,
../CRITIQUE-REPORT.md).

Only the keys the generator renders live here (track, level, time, title, subtitle,
build, surfaces, learn, mermaid, gates, notes). The numbered steps and the copy-paste
assets of each guide come from the lab's own ``training-steps.json`` (and the files
it links under ``assets/``), so they cannot drift from the lab README. Each block
below is copied from its lab's README, scenario.yaml and verification.yaml.
"""

TRACKS = ["Foundations", "Build", "Operate", "Govern", "Aria — Autonomous"]

INTRO = {
    "kicker": "UI Training Guide",
    "title": "CALIBER Cookbooks",
    "tagline": (
        "Seventeen practical, build-along recipes that teach the CALIBER platform "
        "end-to-end — each backed by a versioned, installable system example with "
        "readiness checks and an explicit review-before-activation boundary."
    ),
    "meta": [
        "17 cookbooks",
        "5 tracks",
        "UI-first",
        "Training-ready",
        "Mermaid flows + UI mockups",
    ],
    "footer": (
        "CALIBER Cookbooks · generated training guide · every recipe verified "
        "against the shipped platform (environment gates are labelled). Pair each section with its "
        "folder under <code>cookbooks/&lt;nn&gt;-…/</code> (scenario.yaml, build.yaml, "
        "test-data.yaml, verification.yaml, and copy-paste <code>assets/</code>)."
    ),
    "body": """
    <div class="ck-cols">
      <div class="callout">
        <strong>How to read each cookbook.</strong>
        <ul>
          <li>Sign in at <code>/caliber/login</code> as <code>admin</code> / <code>admin</code> on a running stack.</li>
          <li>Each cookbook has a <strong>flow diagram</strong>, a numbered <strong>UI walkthrough</strong>
            (the dark chip is the exact navigation path; “Fill in” lists every field; “Click” is the button to press),
            a <strong>UI mockup</strong>, an <strong>assets table</strong>, and <strong>quality gates</strong>.</li>
          <li>Every <strong>green link</strong> opens the copy-paste asset file in that cookbook's
            <code>assets/</code> folder — paste its contents into the matching field.</li>
          <li><strong>Preview areas are off by default.</strong> Each cookbook's notes name the feature flags it needs
            (<code>CALIBER_FEATURE_COOKBOOKS</code> for the gallery; <code>CALIBER_FEATURE_ARIA</code> for labs 12–15,
            <code>CALIBER_FEATURE_INTEGRATIONS</code> for lab 05 or <code>CALIBER_FEATURE_RELEASE_CANDIDATES</code> for lab 11).
            Set them on the server and restart. <code>scripts/cookbook-env.sh up NAME --flags …</code> starts a disposable stack with them.</li>
        </ul>
      </div>
      <div class="callout">
        <strong>The UI-first promise.</strong> With <code>CALIBER_FEATURE_COOKBOOKS=true</code> set on the server (off by default), open <strong>Cookbooks</strong> to install any of the 17 recipes as a paused workflow and editable draft. Review readiness before activation.
        <ul>
          <li><strong>Test sets</strong> come from a file (<code>Upload JSON/JSONL</code>, labs 01 and 06) or from real runs (<code>Observability → open a trace → Add to test set</code>, lab 16).</li>
          <li><strong>The default model and evaluator are simulated.</strong> <code>Evaluations</code> refuses to score without a real model (<code>400 evaluation requires a real LLM provider</code>), and calibration evidence on the default stack is canned and marked <em>Simulated evaluation — not evidence</em>. Each lab says which of its steps are real and which are simulated.</li>
          <li><strong>Deterministic logic</strong> uses Data Transform for decisions, JSON Schema, mapping, confidence, and fixtures; Python Code remains a bounded extension.</li>
          <li><strong>Judges</strong> are for LLM-graded criteria; deterministic checks are <strong>scorers</strong> or assertions.</li>
          <li><strong>Aria</strong> (12–15) plans from one sentence in <code>Plans</code> (needs <code>CALIBER_FEATURE_ARIA=true</code>, off by default). The planner is rules, not a model; nothing runs until you approve the plan, each step asks for the fields it cannot know, and Aria creates the artifact once you approve.</li>
        </ul>
      </div>
    </div>
    """,
}


COOKBOOKS = [
    {
        "id": "CB-01",
        "num": "01",
        "slug": "intake-classifier",
        "track": "Foundations",
        "title": "Trustworthy Intake Classifier",
        "subtitle": "Release a prompt change you can trust: a 44-case test set, a caught regression, then calibrate, review, apply and roll back.",
        "level": "Starter",
        "time": "30–60 min",
        "surfaces": [
            "Prompts",
            "Test Sets",
            "Evaluations",
            "Releases",
            "Audit Log",
        ],
        "build": "A support-desk prompt, <code>cookbook-01-intake-classifier</code>, that turns every inbound message into one strict JSON record (<code>intent, priority, confidence, needs_review, reason</code>). You import a <strong>44-case labelled test set</strong> from <code>assets/dataset/intake-classifier.jsonl</code>, register a weaker v2 that drops the JSON-only rules, then calibrate the live prompt, review the candidate, <strong>Apply</strong> it explicitly, verify the release in the MLflow registry, on <strong>Releases</strong> and in the <strong>Audit Log</strong>, and roll back. Runs on the default simulated model and evaluator; only <code>CALIBER_FEATURE_COOKBOOKS=true</code> is needed.",
        "learn": [
            "Author v1 (strict, live) and save a weaker v2 as a draft: registering a version does not make it live",
            "Import a 44-example test set from a file on the prompt's <strong>Calibration</strong> stage (dataset version <code>45</code>, because every example bumps the version)",
            "Evaluate a prompt version on <strong>Evaluations</strong>: refused with <code>400 evaluation requires a real LLM provider</code> on the simulated model, run for real against a stand-in model",
            "Calibrate, read <strong>Review candidate before applying</strong> (diff, <strong>Simulated evaluation — not evidence</strong>, gate), then <strong>Apply candidate</strong>: nothing is applied until you click it",
            "Verify the release in the MLflow registry (<code>prod</code> to v3), <strong>Releases</strong> (live board and timeline) and <strong>Audit Log</strong>, then <strong>Roll back</strong> to v1",
            "The failure paths: a viewer's Apply is <code>403</code>, a candidate below the gate is <code>rejected</code> and its Apply is <code>409</code>",
        ],
        "mermaid": """flowchart LR
  A["Prompts: author v1, live"] --> B["Author: save weaker v2 draft"]
  A --> C["Calibration: import 44-case file"]
  C --> D["Test Sets: check v45"]
  D --> E["Evaluations: score a prompt version"]
  E --> F["Compare to baseline: v2 regression"]
  C --> G["Calibration: MetaPrompt run"]
  G --> H["Review and apply: candidate"]
  H --> I["Apply candidate: prod to v3"]
  I --> J["Releases and Audit Log: verify"]
  J --> K["Roll back: prod to v1"]""",
        "gates": [
            {"gate": "Test set imported from the file", "target": "44 examples, dataset version 45"},
            {"gate": "Evaluation outcome", "target": "simulated model: 400 refusal (missing test set: 404); with a model that answers: v1 44/44 strict JSON, v2 0/44, v2 fails the regression gate (allowed pass-rate drop 0.05)"},
            {"gate": "Calibration evidence", "target": "candidate_ready; simulated: true, 44 examples, dataset v45; gate min_aggregate_score 0.85 / max_regression_delta 0.02"},
            {"gate": "Apply is gated", "target": "viewer Apply 403; candidate below the gate rejected, Apply 409"},
            {"gate": "Release evidence after Apply", "target": "registry prod = v3; live board v3 by you; timeline v1 to v3; audit promote_prompt and apply_candidate by you"},
            {"gate": "Rollback", "target": "prod is v1 again in the registry and on the live board (26/26 SDK checks pass)"},
        ],
        "notes": [
            "<strong>Feature flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> and nothing else: no Aria, no integrations. Prompt, test-set, calibration and release routes work without it; only the Cookbooks gallery needs it.",
            "<strong>Simulated by default.</strong> With <code>CALIBER_LLM_PROVIDER=fake</code> / <code>CALIBER_EVAL_PROVIDER=fake</code> there is no model: Evaluations is refused, and the calibration candidate and scores (canned <code>0.94</code> vs <code>0.88</code>) are marked <code>simulated: true</code>. The example count (44), dataset version, Apply, registry alias, live board, timeline, audit and rollback are real. Never read <code>0.94</code> as quality.",
            "<strong>Real release path required.</strong> <code>CALIBER_PROMOTER_PROVIDER=mlflow</code> and <code>CALIBER_ARTIFACT_STORE_PROVIDER=mlflow</code>; if <code>/readiness</code> lists <code>promoter</code> or <code>artifact_store</code> as simulated, Apply will not move the MLflow alias.",
            "<strong>Stand-in model.</strong> <code>assets/stand-in-model/server.py</code> is a keyword server behind an OpenAI-compatible endpoint, not a model. It lets you watch v1 and v2 scored and the regression caught (<code>token_f1</code> 0.535 to 0.267); 37 of 44 labels match on v1. Needs a second stack (<code>--real-llm</code>); calibration needs an optimizer model, so run the script with <code>--skip-release</code>.",
            "<strong>Dataset keys.</strong> The importers read <code>input</code> and <code>expected</code> (not <code>inputs</code>/<code>expectations</code>); built-in scorers are text-level, so the JSON-contract checks live in the SDK script, not the scorecard.",
            "<strong>Not covered.</strong> Playground, Generate Test Cases, Run Tests &amp; Judge, the Runs stage baseline diff (these need <code>CALIBER_FEATURE_ARIA</code> and a real model), the <code>InstructionCompliance</code> judge and the Observability trace walkthrough.",
            "<strong>Run it.</strong> <code>scripts/cookbook-env.sh up lab01 --flags cookbooks</code>, then <code>sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py</code> (exits 0 only if all checks hold); browser: <code>e2e/cookbooks/labs/01-prompt-release-loop.spec.ts</code> and <code>01b-evaluation-regression.spec.ts</code>. A calibration run needs background workers on.",
        ],
    },
    {
        "id": "CB-02",
        "num": "02",
        "slug": "precision-skills",
        "track": "Foundations",
        "title": "Precision Skills",
        "subtitle": "A support-tone skill measured on 40 trigger cases (30 right, then 40), calibrated, applied, rolled back, packaged and archived.",
        "level": "Starter",
        "time": "45–75 min",
        "surfaces": [
            "Cookbooks",
            "Skills",
            "Administration",
            "Audit Log",
        ],
        "build": "A narrow skill, <code>support-tone-and-citation</code>, that gives customer-facing support replies one voice (calm, no over-promising, cited knowledge-base ids, an escalation hint) and must not fire on engineering work. You run a <strong>40-case trigger set</strong> against the real, deterministic skill selector, tighten the trigger phrases, pin the baseline, then calibrate, review, Apply and roll back, export and re-import it as a ZIP package, try the failure paths and archive it. Needs only <code>CALIBER_FEATURE_COOKBOOKS=true</code>; calibration is simulated.",
        "learn": [
            "Author a skill in <strong>Build Skill</strong>: the selector reads name, tags, summary and trigger phrases, never the long-form content",
            "<strong>Render Preview</strong>: variable substitution, and what stays unresolved (a partial set shows <strong>2 unresolved</strong>)",
            "Run the 40 trigger cases: the six starter phrases get <code>30/40</code> right (8 false positives, 2 false negatives); tightened phrases (3 should-trigger, 14 should-not-trigger) get <code>40/40</code>",
            "Pin that run as the baseline, <strong>Calibrate</strong>, read the review (<strong>Simulated evaluation — not evidence</strong>, <code>0 evaluation examples</code>), <strong>Apply to skill</strong> (v2), then <strong>Roll back</strong> (v3 with the text of v1)",
            "<strong>Download ZIP</strong>, then <strong>Import ZIP</strong> with reject (409), rename or merge: the package holds the instructions, not the tags, category or trigger phrases",
            "Failure paths: invalid content (400, duplicate name 409), a calibration with no baseline (still works), a viewer's Apply, Calibrate, Roll back and Create (403)",
        ],
        "mermaid": """flowchart TD
  A["Skills: Build Skill, v1 live"] --> B["Render Preview: full and partial variables"]
  A --> C["Trigger Tests and Scenario Sets: round 1"]
  C -->|"starter phrases: 30 of 40"| D["Details and package: tighten trigger phrases"]
  D --> E["Round 2: 40 of 40"]
  E --> F["Runs: Set as baseline"]
  F --> G["Calibrate: simulated candidate"]
  G --> H["Review and apply: v2"]
  H --> I["Roll back: v3 has the text of v1"]
  A --> J["Download ZIP: SKILL.md and openai.yaml"]
  J --> K["Import ZIP: reject, rename, merge"]
  I --> L["Archive"]""",
        "gates": [
            {"gate": "Trigger accuracy (round 1, starter phrases)", "target": "30/40 right: false positives T27-T32, T34, T35; false negatives T14, T23"},
            {"gate": "Trigger accuracy (round 2, tightened phrases)", "target": "40/40, above trigger_accuracy_min 0.95; that run is the pinned baseline"},
            {"gate": "Apply and rollback", "target": "candidate_ready with simulated: true and n_examples 0; history 3, 2, 1 and version 3 has the text of version 1"},
            {"gate": "Package round trip", "target": "ZIP holds exactly SKILL.md and agents/openai.yaml; same-name re-import 409; decisions match after restoring metadata (gate 1.0)"},
            {"gate": "Failure paths", "target": "viewer Apply, Calibrate, Roll back, Create each 403; invalid content 400; every skill the lab created is archived"},
        ],
        "notes": [
            "<strong>Feature flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> only opens the Cookbooks gallery; Skills work without it. No Aria, integrations or release-candidate flag is needed.",
            "<strong>What is simulated.</strong> Trigger selection (<code>test-selection</code>) is deterministic code and real on every stack; there is no LLM judge. Calibration is simulated by default (canned <code>0.94</code> vs <code>0.88</code>, <code>provider: fake</code>); <code>n_examples</code> is <code>0</code> because a skill has no test set, so the 40 cases score selection only. It proves wiring, not that the candidate is better.",
            "<strong>Selector gotchas.</strong> One summary word is enough to select; a positive sharing no word with name, tags or summary is not selected (<code>T14</code>, <code>T23</code>). A matching should-not-trigger phrase ends the score at 0. Fix misses with phrases, never content edits; scores depend on the skill name, so assert decisions, not scores.",
            "<strong>Package limits.</strong> A package carries instructions only: the copy gets category <code>custom</code>, no tags, no trigger phrases and a 64-character summary, so its decisions differ (13 cases in the run) until you restore them. A merge import (admin only) overwrites the summary with the shortened one.",
            "<strong>Roles and names.</strong> Create needs operator; patching metadata and rolling back need admin. Names are kebab-case, must not start with <code>claude</code> or <code>anthropic</code>, are unique across the deployment, and an archived skill keeps its name.",
            "<strong>Background worker.</strong> Calibration stays <code>queued</code> unless <code>CALIBER_BACKGROUND_TASKS_ENABLED=true</code> (set by <code>scripts/cookbook-env.sh</code>). Scenario Sets live in the page and clear on leaving it.",
            "<strong>Not covered.</strong> The Bind stage, a calibration that improves the skill, project roles other than Viewer, and Aria choosing the skill. Run it with <code>scripts/cookbook-env.sh up lab02 --flags cookbooks</code>, <code>sdk/caliber-sdk/examples/cookbooks/cookbook_02_precision_skills.py</code> and <code>e2e/cookbooks/labs/02-skill-trigger-packaging.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-03",
        "num": "03",
        "slug": "policy-safe-tool",
        "track": "Foundations",
        "title": "Policy-Safe Decision Tool",
        "subtitle": "A deterministic refund policy hardened against 49 fixtures, then a refund that only a second person can release.",
        "level": "Core",
        "time": "15–30 min",
        "surfaces": [
            "Tools",
            "Workflows",
            "Administration",
            "Audit Log",
            "Cookbooks",
        ],
        "build": "A refund policy as a registered <strong>read</strong> tool (<code>decide_refund</code>: approve, deny or manual review, with a stable reason code) and a <strong>write</strong> tool (<code>initiate_refund</code>, registered with <code>requires_approval</code>). You run them in the sandbox, save a <strong>49-case fixture suite</strong>, pin a baseline and catch one deliberate regression, then import the <code>refund-decision</code> workflow that decides, routes, and issues the refund only after a <strong>different person</strong> approves and the run is resumed. No model is involved: nothing here is simulated.",
        "learn": [
            "Register a read tool and a write tool: <strong>Write</strong> locks <strong>Requires Approval</strong> on, and a write tool registered without it is refused with <code>400</code>",
            "<strong>Sandbox</strong>: the read tool runs live in a subprocess, the write tool is mocked (<strong>Success (mocked)</strong>)",
            "Save 49 fixtures, <strong>Run calibration</strong> (<strong>Pass rate 100% — 49/49 passed</strong>), pin the run as the baseline, then break R09 and read <strong>1 regression</strong> (48/49)",
            "Import the workflow and <strong>Run</strong> it: the run stops at <strong>Awaiting approval</strong> on the <code>refund</code> node; the initiator is refused with <code>403</code>, <code>approver1</code> approves, and the refund fires only after <strong>Resume</strong>",
            "Reject (<code>approval_rejected</code>, no refund), deny (completes without approval), and read who approved what in the <strong>Audit Log</strong>",
            "Where the sandbox stops being a boundary: a stalled tool is killed, a large write is refused, the environment is empty, and the network is still reachable",
        ],
        "mermaid": """flowchart LR
  A["Tools: register decide_refund and initiate_refund"] --> B["Sandbox: live read, mocked write"]
  B --> C["Fixtures: 49 cases"]
  C --> D["Hardening: run calibration, 49/49"]
  D --> E["Test Runs: Set as baseline"]
  E --> F["Break R09: 1 regression"]
  F --> G["Workflows: import refund-decision"]
  G --> H{"Router on decision"}
  H -->|deny| I["Output: denied, no approval"]
  H -->|other| J["initiate_refund: waiting_approval"]
  J -->|"approver1 approves, admin Resume"| K["Refund issued once"]
  K --> L["Audit Log: approver and initiator"]""",
        "gates": [
            {"gate": "Fixture pass rate", "target": "1.0 over 49 cases (tool_fixture_pass_rate_min 1.0); recorded as a suite run and pinned as the baseline"},
            {"gate": "Regression is visible", "target": "one wrong fixture (R09) scores 48/49 with exactly 1 regression against the baseline"},
            {"gate": "Approval enforced", "target": "run waits at waiting_approval on refund; initiator 403; approver1 approves; refund fires only after Resume"},
            {"gate": "Reject and deny paths", "target": "rejected run fails with approval_rejected and issues no refund; a denied decision finishes with no approval"},
            {"gate": "Audit trail and roles", "target": "approver1 and the initiator named in the audit log; a viewer cannot run, calibrate or approve (403)"},
        ],
        "notes": [
            "<strong>Feature flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> only opens the Cookbooks gallery starter (the last step); the rest of the lab needs no flag. No Aria, integrations or release-candidate flag.",
            "<strong>Nothing is simulated.</strong> No model and no judge are involved, so the fake LLM provider is irrelevant here.",
            "<strong>Runtime settings.</strong> Needs runtime approvals, the workflow run queue, checkpointing and a worker (<code>scripts/cookbook-env.sh</code> turns them on). Without <code>CALIBER_WORKFLOW_RUN_RUNTIME_APPROVALS_ENABLED=true</code> a run does not pause. <code>CALIBER_APPROVER_USERS</code> must list <code>approver1</code>; leave <code>CALIBER_APPROVAL_ALLOW_SELF_APPROVAL</code> unset or the second-person rule does not bite.",
            "<strong>Split of duties.</strong> Run, calibrate and Resume need <code>caliber.operator</code>; deciding needs <code>caliber.approver</code>; only an admin registers a tool. <code>approver1</code> deliberately cannot Resume.",
            "<strong>Tools are importable Python.</strong> A registered tool needs a <code>module_path</code> and <code>callable_name</code> in the server's environment (the shipped <code>caliber.workflows.demo_tools</code> provides both); there is no inline tool editor. Set <code>CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST</code> beyond a trial.",
            "<strong>Limits.</strong> The sandbox is a process boundary, not isolation (the child keeps network and filesystem access), and the memory limit is not enforced on macOS. The effect ledger covers webhook and API-request nodes, not registered tools, so make a payments tool idempotent itself.",
            "<strong>Calibration is slow.</strong> 49 cases take about 35 s synchronously, longer than the SDK default 30 s timeout. Run it with <code>sdk/caliber-sdk/examples/cookbooks/cookbook_03_policy_safe_decision_tool.py</code> (<code>python -m examples.cookbooks.cookbook_03_policy_safe_decision_tool</code>) or the Playwright spec <code>e2e/cookbooks/labs/03-tool-hardening.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-05",
        "num": "05",
        "slug": "governed-mcp",
        "track": "Foundations",
        "title": "Governed Tool Connectivity (MCP)",
        "subtitle": "Connect an MCP server, classify every tool before anything calls it, and prove from the server's own ledger that a refused call never ran.",
        "level": "Core",
        "time": "20–40 min",
        "surfaces": [
            "MCP Servers",
            "Workflows",
            "Observability",
            "Audit Log",
        ],
        "build": "A governed connection to the lab's own MCP server (<code>assets/server/repo_tracker_mcp.py</code>, run over <code>stdio</code>) with six tools: four read tools, the write tool <code>issue_write</code> and the external action <code>create_release</code>. You discover the tools, classify each one, call an allowed read tool, calibrate the three read tools on 28 saved cases, and bind <code>issue_write</code> in a workflow that stops at <code>waiting_approval</code> until a second person approves. No model is involved, so nothing is simulated.",
        "learn": [
            "Register a stdio MCP server in <strong>MCP Servers</strong>, run <strong>Test</strong> and read the 6 discovered tools and their schemas",
            "Classify every tool (<strong>Allow tool</strong>, <strong>Requires approval</strong>, <strong>Side effect level</strong>, <strong>Rate limit / minute</strong>): discovery is not authorization",
            "Invoke an allowed read tool, then meet each refusal (blocked, unknown, rate-limited) and each failed call (tool error, missing argument, timeout)",
            "Calibrate the read tools with saved cases, and see one wrong expectation fail exactly one case",
            "Hold <code>issue_write</code> in a workflow for a second person, and read the audit log and <code>mcp.&lt;server&gt;.&lt;tool&gt;</code> traces",
        ],
        "mermaid": """flowchart LR
  R["Register stdio server"] --> T["Test: discover 6 tools"]
  T --> U["All unclassified, so denied"]
  U --> C["Classify each tool"]
  C --> RD["Read tool: invoke and calibrate"]
  C --> BL["Blocked, unknown or rate-limited call: refused"]
  BL --> LG[("Server ledger: no line written")]
  C --> WR["issue_write in a workflow"]
  WR --> AP{{"waiting_approval: second person"}}
  AP -->|approve and Resume| OK["Write runs"]
  RD --> TR["Audit log and mcp traces"]""",
        "gates": [
            {"gate": "Discovery", "target": "6 tools, every one unclassified and denied until classified"},
            {"gate": "Search call", "target": "search_repositories returns the 2 repositories tagged mcp; the ledger shows exactly one execution"},
            {"gate": "Calibration of the read tools", "target": "16/16, 8/8 and 4/4; a wrong expectation gives 15/16 (pass rate 0.9375)"},
            {"gate": "Refusals never run", "target": "0 ledger lines and no trace for create_release, drop_database and a rate-limited call"},
            {"gate": "Write tool in a workflow", "target": "waiting_approval, starter refused (403), runs only after a second person approves and the run is resumed"},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_INTEGRATIONS=true</code> opens the <strong>MCP Servers</strong> page and <code>/mcp-servers</code> (<code>404 feature_disabled</code> otherwise); <code>CALIBER_FEATURE_COOKBOOKS=true</code> is needed only for the last gallery step. Both are preview areas, off by default. Start with <code>scripts/cookbook-env.sh up lab05 --flags integrations,cookbooks</code>.",
            "<strong>Nothing is simulated.</strong> No model is involved; enforcement is deterministic and proved from the server's ledger (<code>--ledger</code>), not only from CALIBER's answer.",
            "<strong>stdio, not HTTP.</strong> Set <code>CALIBER_MCP_STDIO_PYTHON_SCRIPT_ALLOWLIST</code> to the server script's absolute path. A loopback HTTP endpoint is registered and then refused by the egress policy, and <code>/bin/sh</code> or <code>python -c</code> are refused with the reason. A stdio server is a child process, not a sandbox.",
            "<strong>Accounts and scopes.</strong> <code>admin</code> registers, classifies and invokes; <code>lab05-operator</code> saves cases and calibrates; <code>lab05-approver</code> approves. Set <code>CALIBER_OPERATOR_USERS</code>/<code>CALIBER_APPROVER_USERS</code>, leave <code>CALIBER_APPROVAL_ALLOW_SELF_APPROVAL</code> unset, and enable the run queue and runtime approvals or the workflow will not pause.",
            "<strong>The admin Playground is break-glass.</strong> <code>requires_approval</code> does not gate it (<code>issue_write</code> runs there); it holds the tool only inside a workflow. A write or external-action tool must carry approval or be blocked (<code>400</code> otherwise).",
            "<strong>Limits.</strong> Classification is yours (a tool marked read that writes is invisible); policy lives on the server record; the rate limit is per tool, per minute, in one process. Calibration assertions are <code>no_error</code>, <code>output_contains</code> and <code>equals</code>, and calibrating an approval-class tool returns <code>409</code>.",
            "<strong>Not run.</strong> The GitHub remote MCP variant (<code>assets/real-world/github-mcp.server.json</code>) is documented but was not executed. Runnable: <code>sdk/caliber-sdk/examples/cookbooks/cookbook_05_governed_tool_connectivity.py</code> (about a minute) and <code>caliber/caliber-ui/e2e/cookbooks/labs/05-mcp-governance.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-04",
        "num": "04",
        "slug": "doc-to-json",
        "track": "Build",
        "title": "Document-to-JSON Pipeline",
        "subtitle": "Turn invoice documents into schema-checked JSON, and hold every invalid one for a second person to approve or reject.",
        "level": "Core",
        "time": "20–30 min",
        "surfaces": [
            "Object Store",
            "Cookbooks",
            "Workflows",
            "Administration",
        ],
        "build": "A workflow that reads one invoice from the <code>doc-intake</code> bucket, extracts JSON with a deterministic <code>python_code</code> node, and checks it against a JSON Schema with a <code>data_transform</code> <code>json_schema</code> node. A valid record is written to <code>doc-extracted</code> under <code>invoices/&lt;invoice number&gt;.json</code>; an invalid one stops at <code>review_gate</code>, where a second person approves it (written under <code>reviewed/</code>) or rejects it (nothing is written). No model is called, so simulated and real providers give the same result.",
        "learn": [
            "Create the <code>doc-intake</code> and <code>doc-extracted</code> buckets, upload the 30 sample documents under <code>incoming/</code> and preview one in <strong>Object Store</strong>",
            "Install the <strong>Document-to-JSON Pipeline</strong> cookbook as a paused draft from <strong>Cookbooks</strong>, then <strong>Resume</strong> it",
            "Run one document per queued run, with the object key as the run input, and read each step in the Execution Debugger",
            "See a duplicate invoice refused by the output bucket, and hold a document that fails the schema at an approval gate",
            "Approve and reject as the <code>reviewer</code> account; the account that started a run is refused (<code>403</code>)",
            "Check what reached the output bucket: exactly 17 objects, and nothing for the rejected document",
        ],
        "mermaid": """flowchart LR
  IN[("Bucket doc-intake: incoming/")] --> RD["read_document"]
  RD --> EX["extract_fields: python_code"]
  EX --> VS["validate_schema: JSON Schema"]
  VS --> PK["package_result"]
  PK --> RT{"route"}
  RT -->|valid| EE["emit_extracted"]
  EE --> WE[("write_extracted: doc-extracted/invoices/")]
  RT -->|invalid| RG{{"review_gate: second person"}}
  RG -->|approve and resume| WR[("write_reviewed: doc-extracted/reviewed/")]
  RG -->|reject| FAIL["run fails, nothing written"]""",
        "gates": [
            {"gate": "Valid invoices extracted and matching the expected JSON", "target": "16 of 16"},
            {"gate": "Invalid documents held at review_gate with exactly the listed schema errors", "target": "13 of 13"},
            {"gate": "Duplicate invoice refused by the output bucket", "target": "1 of 1"},
            {"gate": "Account that started a run cannot approve it", "target": "403"},
            {"gate": "Approved document written under reviewed/, rejected document writes nothing", "target": "doc-extracted holds exactly 17 objects"},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> opens the gallery (a preview area, off by default); no other flag is needed. The lab also needs an S3-compatible store: <code>scripts/cookbook-env.sh up lab04 --flags cookbooks --s3</code> starts a private in-memory one, so objects do not survive <code>restart</code>.",
            "<strong>Nothing in the pipeline is simulated, and no model is called.</strong> The simulated default LLM and evaluator providers are never used; the result is deterministic. The object store is real S3 protocol (moto), not a production service.",
            "<strong>Second person required.</strong> Start with <code>CALIBER_APPROVER_USERS=admin,reviewer CALIBER_OPERATOR_USERS=admin,reviewer</code> (approver to approve or reject, operator to resume). Leave <code>CALIBER_APPROVAL_ALLOW_SELF_APPROVAL</code> unset, and keep runtime approvals and the run queue on.",
            "<strong>Text only.</strong> <code>input_bucket</code> decodes UTF-8 with replacement characters and keeps 100,000 bytes per object, so binary <code>.docx</code>/<code>.xlsx</code> cannot flow through it, and the 120 KB oversized file is truncated.",
            "<strong>Preview is blocked.</strong> The editor's Preview refuses this workflow (it cannot isolate <code>python_code</code>, <code>input_bucket</code> or <code>output_bucket</code>); run it for real, queued, through <code>POST /workflow-runs</code>.",
            "<strong>Extractor is rules, not a model.</strong> It understands only the layouts in the corpus (aligned text, Markdown table, CSV) and writes <code>null</code> for anything a document does not state. A new layout is a new dataset case and a new rule. One run handles one document.",
            "<strong>Keep the gate's shape.</strong> After an approval only what flows through the gate reaches later nodes; the installed workflow puts the record and its schema errors in one text (<code>package_result</code>) for that reason.",
            "<strong>Runnable.</strong> <code>sdk/caliber-sdk/examples/cookbooks/cookbook_04_document_to_json_pipeline.py</code> (about 75 s, 30 runs); UI: <code>caliber/caliber-ui/e2e/cookbooks/labs/04-document-extraction.spec.ts</code>; environment: <code>scripts/cookbook-env.sh up lab04</code>.",
        ],
    },
    {
        "id": "CB-06",
        "num": "06",
        "slug": "grounded-knowledge",
        "track": "Build",
        "title": "Grounded Knowledge Assistant",
        "subtitle": "A policy assistant you can measure: cited chunks, retrieval modes compared, calibration that lifts Recall@1, and low-confidence runs sent to review.",
        "level": "Core",
        "time": "120–180 min",
        "surfaces": [
            "Object Store",
            "Knowledge Bases",
            "Prompts",
            "Workflows",
            "Cookbooks",
            "Observability",
            "Review Queues",
            "Evaluations",
        ],
        "build": "A retrieval assistant over 11 company policies (5,100 words): a <code>policy-corpus</code> bucket, a knowledge base <code>policy-qa-kb</code> built as two versions (1,400- and 500-character chunks), retrieval calibrated on 41 questions, and an installed <code>knowledge_rag</code> workflow bound to it. A top-1 similarity gate marks outside-the-corpus questions <code>needs_review</code> and a review queue receives them. Storage, chunking, embeddings and ranking are real; answer text and model-judged scores are simulated by default.",
        "learn": [
            "Upload 11 policies to the object store and build knowledge base version 1 (a queued job a worker runs): 11 sources, 27 chunks, 384-dimension vectors",
            "Ask in <strong>Dense</strong>, <strong>Hybrid</strong> and <strong>GraphRAG</strong> and check that the cited chunk holds the clause (<code>365 days</code>)",
            "Import 41 calibration questions and run <strong>Calibrate</strong> (Recall@k, nDCG@k), set a baseline and compare modes",
            "Build the tuned version 2 (69 chunks), <strong>Set active</strong> to switch versions and roll back",
            "Meet real build failures: no object store, no embedding libraries, an unreadable file (build completes with <code>failed_sources: 1</code>), nothing readable (build fails)",
            "Install the cookbook workflow, run it, and enqueue the low-confidence run in a <strong>Review Queue</strong>",
        ],
        "mermaid": """flowchart LR
  C[("Bucket policy-corpus: 11 policies")] --> B1["Build KB v1: 27 chunks"]
  B1 --> EX["Explore: dense, hybrid, GraphRAG"]
  B1 --> CAL["Calibrate: 41 questions"]
  CAL --> B2["Build v2: 69 chunks, auto-active"]
  B2 --> ACT["Set active and rollback"]
  B1 --> WF["Workflow: knowledge_query to Confidence"]
  WF --> G{"Top-1 score gate 0.42"}
  G -->|confident| OUT["Output with citations"]
  G -->|needs_review| RQ[("Review Queue")]
  RQ --> TR["Reviewer answer written back to trace"]""",
        "gates": [
            {"gate": "Version 1 build", "target": "completed, 11 sources, 27 chunks, active"},
            {"gate": "Clause's chunk in the top 3 (hybrid)", "target": "41/41 (dense 40/41, graph_hybrid 35/41)"},
            {"gate": "Score gate 0.42", "target": "refuses all 5 questions outside the corpus; cannot refuse the 2 trap questions"},
            {"gate": "Calibration at top_k = 1, dense to hybrid", "target": "Recall@1 0.878 to 0.9268, nDCG@1 0.9024 to 0.9512"},
            {"gate": "Version 2 and workflow review", "target": "69 chunks, rollback works; only the 0.25-confidence run is enqueued for review"},
        ],
        "notes": [
            "<strong>Flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> and nothing else (no Aria, no integrations). Knowledge needs more than the base install: an S3-compatible store, <code>pip install \"caliber-suite[knowledge,knowledge-local]\"</code> and the <code>all-MiniLM-L6-v2</code> model (about 90 MB). Start with <code>HF_HUB_OFFLINE=1 scripts/cookbook-env.sh up lab06 --flags cookbooks --s3</code>.",
            "<strong>Real:</strong> object store, queue and worker, chunking, embeddings, ranking, Recall@k, nDCG@k, the score gate, activation and rollback. <strong>Simulated</strong> (default <code>fake</code> LLM/eval providers): answer text is an extractive digest, Faithfulness and Answer-correctness are blank (<code>—</code>), and <strong>Evaluations</strong> refuses scoring with <code>400 evaluation requires a real LLM provider</code>.",
            "<strong>No decision is produced on a simulated stack.</strong> The <code>kb-answer</code> prompt and the <code>CitationFaithfulness</code> judge are stored for real, but answer, abstain or clarify and the 0.90 faithfulness target need a real model. The lab verifies the evidence (both conflicting refund documents are retrieved together) and what retrieval alone can refuse.",
            "<strong>The gate has a limit.</strong> 0.42 is tuned on this question set. It cannot refuse the 2 trap questions (Enterprise terms), whose policy exists but whose clause does not (scores 0.56 and 0.66); that needs a model and the review queue. Re-derive the gate from your own scores.",
            "<strong>Retrieval always returns chunks.</strong> The dress-code question still returns nearest chunks; refusing is a decision on top. <code>graph_hybrid</code> is worse here (35/41), and hybrid helps the 1,400-character chunks but hurts the 500-character ones, which is why you calibrate.",
            "<strong>Build behavior.</strong> A finished build activates itself, even with an unreadable file, so read <code>failed_sources</code>. AGE retrieval is not used (pgvector and Apache AGE are off). The first build can sit <code>queued</code> for minutes without <code>HF_HUB_OFFLINE=1</code>.",
            "<strong>Runnable.</strong> <code>sdk/caliber-sdk/examples/cookbooks/cookbook_06_grounded_knowledge_assistant.py</code> (about a minute, 51 ok lines) and <code>caliber/caliber-ui/e2e/cookbooks/labs/06-knowledge-retrieval.spec.ts</code>; environment: <code>scripts/cookbook-env.sh up lab06</code>.",
        ],
    },
    {
        "id": "CB-07",
        "num": "07",
        "slug": "support-copilot",
        "track": "Build",
        "title": "Support Triage Copilot",
        "subtitle": "Classify a ticket, route it to reply, clarify or escalate, pause refunds and bug filings at a second person's approval, and roll back a bad version.",
        "level": "Advanced",
        "time": "45–70 min",
        "surfaces": [
            "Administration",
            "Workflows",
            "Test Sets",
            "Cookbooks",
            "Audit Log",
        ],
        "build": "A <code>support-triage</code> workflow (9 nodes) that reads a ticket and returns one of <code>reply</code>, <code>clarify</code>, <code>escalate_support</code> or <code>escalate_bug</code>, with a severity and an SLA, and pauses every refund and bug filing at a <code>human_approval</code> gate. You run it through the queue over <strong>44 tickets</strong>, answer the gate as a second person (<code>lena</code>), break it with a bad version that the dataset catches, and roll the <code>staging</code> alias back. <strong>The classifier is deterministic rules in one Python Code node, not a model</strong>; the one agent node (<code>draft_reply</code>) runs on the simulated provider.",
        "learn": [
            "Encode a four-outcome router (<code>classify</code> Python Code node, <code>triage_router</code>) and check it exactly against a 44-ticket test set: decision, severity, SLA minutes, approval flag and flags",
            "Pause customer-impacting actions at a <code>human_approval</code> node: the account that started a run gets <code>403</code> when it approves, an approver needs <code>caliber.approver</code> plus the project role <code>reviewer</code> or <code>owner</code>, and a rejection never reaches <code>action_request</code>",
            "Run a workflow through the queue and read the verdict from <code>summary.steps[]</code> and <code>summary.node_path</code>; Preview refuses a Python Code node and a synchronous run of an approval workflow is refused (400)",
            "Gate a promotion to the <code>staging</code> alias behind a second person, catch a bad version with the dataset (<code>3 of 11 bug tickets wrong</code>) and roll back",
            "Read the audit log for who enqueued, who approved and who resumed a run",
        ],
        "mermaid": """flowchart TD
  S[start: ticket as JSON text] --> C[classify: Python Code rules]
  C --> R{triage_router}
  R -->|reply| DR[draft_reply: agent, simulated]
  R -->|clarify| CQ[clarify_question: template]
  R -->|escalate_support| SN[support_note: template]
  R -->|approval required| HA{{review: human_approval}}
  HA -->|second person approves| AR[action_request: template]
  HA -->|reject| FAIL[run failed: nothing released]
  DR --> F[final]
  CQ --> F
  SN --> F
  AR --> F""",
        "gates": [
            {"gate": "Routing accuracy (decision, severity, SLA, approval, flags)", "target": "1.0 (44/44 tickets)"},
            {"gate": "Approval compliance", "target": "1.0: exactly the 15 approval-required tickets pause at the gate"},
            {"gate": "Unsafe release count", "target": "0 (a rejected run never reaches action_request)"},
            {"gate": "Bad version caught by the dataset, then rolled back", "target": "3 of 11 bug tickets wrong; all pass again after rollback"},
        ],
        "notes": [
            "<strong>Prerequisite flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> (the Cookbooks gallery is off by default); nothing else in this lab is behind a flag. Start the stack with <code>scripts/cookbook-env.sh up lab07 --flags cookbooks</code> and export <code>CALIBER_OPERATOR_USERS=admin,lena,sam</code>, <code>CALIBER_APPROVER_USERS=admin,lena</code> and <code>CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=staging</code> first (<code>sam</code> is deliberately not an approver).",
            "<strong>What is simulated.</strong> The classifier is rules, not a model; <code>draft_reply</code> runs on the fake provider and returns canned text (<code>[support-reply] processed: ...</code>), so the lab asserts the branch it ran on, never what it wrote. No judge or <code>Evaluations</code> run is used; <code>Evaluations</code> with a <code>Judge.&lt;id&gt;</code> scorer needs a real model.",
            "<strong>Not covered.</strong> Knowledge-base retrieval, evidence tools, the GitHub MCP write and <code>GroundedSupportReply</code> judge scoring are not run and not claimed; the files in <code>assets/prompts</code>, <code>skills</code>, <code>tools</code> and <code>judges</code> are reference only. The <code>action_request</code> template stands in for the external write.",
            "<strong>API-only parts.</strong> The alpha UI has no Deployments or Promotions tab, so the <code>staging</code> alias, gated promotion and rollback are API only. A <code>prod</code> alias needs a deploy gate that a Python Code workflow cannot have (lab 08 shows a gated <code>prod</code>).",
            "<strong>Preview and runs.</strong> Preview refuses the <code>classify</code> Python Code node; run through the queue (<code>POST /workflow-runs</code>). The single worker takes about 5 seconds per ticket, so 44 tickets take about 4 minutes. Set <code>CALIBER_APPROVAL_ALLOW_SELF_APPROVAL=true</code> only for a single-operator install.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_07_support_triage_copilot</code> (<code>sdk/caliber-sdk/examples/cookbooks/cookbook_07_support_triage_copilot.py</code>) prints <code>All documented outcomes verified.</code>; the UI path is <code>caliber/caliber-ui/e2e/cookbooks/labs/07-support-triage.spec.ts</code>. Add any ticket the rules get wrong to <code>assets/dataset/ticket-cases.jsonl</code> before changing a rule.",
        ],
    },
    {
        "id": "CB-08",
        "num": "08",
        "slug": "incident-copilot",
        "track": "Build",
        "title": "Incident Response Copilot",
        "subtitle": "Turn an incident record into a severity and the lowest-risk action, gate a rollback behind a second person, and release to prod only through a deploy gate.",
        "level": "Advanced",
        "time": "40–65 min",
        "surfaces": [
            "Administration",
            "Workflows",
            "Test Sets",
            "Cookbooks",
            "Audit Log",
        ],
        "build": "An <code>incident-commander</code> workflow (14 nodes) that turns an incident record (alert, deployments, health numbers, a noisy log excerpt, paging and customer-report counts) into a severity and an action using declarative decision tables (Data Transform, Join, Template, Router), and pauses a <code>rollback</code> at a human approval. Because nothing in the decision needs isolating, <strong>Preview can run it</strong>, and the <code>prod</code> alias carries a deploy gate that replays the <strong>41-incident</strong> test set <code>incident-cases</code> before it moves. The only agent node, <code>draft_update</code>, runs on the simulated provider and its text is never asserted.",
        "learn": [
            "Express a severity and action policy as two decision tables (first match wins) and check all 41 incidents: 8 <code>sev1</code>, 10 <code>sev2</code>, 12 <code>sev3</code>, 11 <code>sev4</code>, including noisy logs, conflicting evidence, missing metrics and the boundary cases <code>IN40</code> and <code>IN41</code>",
            "Import a workflow that names a test set: it is refused with <code>unresolved_import_dependency</code> until the test set <code>incident-cases</code> exists in the workspace",
            "Promote to <code>prod</code> through a deploy gate that replays the test set (pass rate 1.0, executor reported as <code>fake</code>), wait for a second person's approval, and see a bad version refused with <code>400 deploy gate failed</code> (pass rate 0.9512)",
            "Gate only the 5 rollback incidents (<code>IN01</code>, <code>IN11</code>, <code>IN12</code>, <code>IN15</code>, <code>IN23</code>) at the <code>review</code> node: self-approval is <code>403</code>, an approval releases <code>ROLLBACK_APPROVED</code>, a rejection releases nothing",
            "Run the failure drills: Error Boundary fallback, a missing tool refused at import, a blocked API Request host, and Wait For Event pause and resume",
        ],
        "mermaid": """flowchart TD
  S[start: incident JSON] --> FO[fan_out: parallel]
  FO --> IN[ingest]
  FO --> SV[severity_table]
  FO --> AC[action_table]
  FO --> DU[draft_update: agent, simulated]
  IN --> J[join]
  SV --> J
  AC --> J
  DU --> J
  J --> SU[summarize: Verdict]
  SU --> RT{triage_router}
  RT -->|rollback| HA{{review: human_approval}}
  HA -->|approve| RR[rollback_request]
  RT -->|escalate_human| HN[handoff_note]
  RT -->|other actions| RC[recommendation]
  RR --> F[final]
  HN --> F
  RC --> F""",
        "gates": [
            {"gate": "Verdict matches dataset (severity and action, Preview replay and queue)", "target": "41/41 incidents"},
            {"gate": "Deploy gate pass rate for the good version", "target": "1.0 over 41 replays; the bad version scores 0.9512 and is refused"},
            {"gate": "Approval compliance", "target": "1.0: exactly the 5 rollback incidents pause at the gate"},
            {"gate": "Unsafe release count", "target": "0 (a rejected rollback releases nothing)"},
        ],
        "notes": [
            "<strong>Prerequisite flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> (<code>scripts/cookbook-env.sh up lab08 --flags cookbooks</code>). The optional MCP evidence nodes of the original recipe would also need <code>CALIBER_FEATURE_INTEGRATIONS=true</code>; they are not part of this lab.",
            "<strong>Release policy settings.</strong> Export <code>CALIBER_OPERATOR_USERS=admin,lena,sam</code>, <code>CALIBER_APPROVER_USERS=admin,lena</code>, <code>CALIBER_RELEASE_REQUIRE_HUMAN_APPROVAL_FOR_ENVIRONMENT_CLASSES=production</code> and set <code>CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES</code> to empty. Left at its default <code>production</code>, the <code>prod</code> promotion is refused until a real model is configured.",
            "<strong>What is simulated.</strong> The decision is deterministic tables (no model). The deploy gate replays with the fake executor and says so (<code>provider: fake</code>, <code>deterministic: true</code>): it proves the graph and the data, not a model. Evidence is supplied inside each incident record. <code>Evaluations</code> on a workflow version is refused (400) without a real model.",
            "<strong>Workflow calibration finds no candidate.</strong> On this rule-table workflow it runs, reports a baseline and finishes <code>rejected</code> (<code>no candidate met calibration gates</code>), so the Simulated evaluation marker was not observed here.",
            "<strong>API-only parts.</strong> Aliases, the <code>prod</code> gate, promotions and rollback have no UI in this alpha build (single-environment mode); the UI spec covers import, Test Sets, Preview, publish, run and approve. Preview refuses a Python Code or API Request node.",
            "<strong>Not covered.</strong> Live evidence (deployment and service-health APIs, a status-page snapshot, an incident issue) needs egress, MCP servers and credentials; the GitHub, Playwright and PostgreSQL MCP steps were removed, not run. <code>assets/prompts</code>, <code>skills</code> and <code>judges</code> are reference only.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_08_incident_response_copilot</code> (<code>sdk/caliber-sdk/examples/cookbooks/cookbook_08_incident_response_copilot.py</code>) or the spec <code>caliber/caliber-ui/e2e/cookbooks/labs/08-incident-response.spec.ts</code>. Add every incident the rules get wrong to <code>assets/dataset/incident-cases.jsonl</code> so the gate protects the fix.",
        ],
    },
    {
        "id": "CB-09",
        "num": "09",
        "slug": "self-healing-workflows",
        "track": "Operate",
        "title": "Self-Healing Workflows",
        "subtitle": "Reproduce production failures, read the run debugger and trace, write the patch as a new version, prove it on the same 35 tickets, and roll an alias back.",
        "level": "Core",
        "time": "20–30 min",
        "surfaces": [
            "Workflows",
            "Run Monitor",
            "Versions",
            "Observability",
        ],
        "build": "A <code>refund-intake</code> workflow (<code>start</code>, <code>validate</code>, <code>check_reference</code>, <code>lookup</code>, <code>decide</code>, <code>final</code>) that is fine on the tickets you tested and fails on the ones production sends. Version 1 completes <strong>11 of 35</strong> tickets and fails 24, in three nodes for three reasons: a schema violation in <code>validate</code>, a poison record that makes <code>check_reference</code> time out, and an account-service error that fails <code>decide</code> two steps after <code>lookup</code>. You write the patch yourself (three node bodies, saved as version 2) and rerun all 35. <strong>No model is involved</strong> (no agent node) and the account service is a stand-in, <code>assets/upstream/account_service.py</code>.",
        "learn": [
            "Reproduce a schema failure (<code>F12</code>), a poison record (<code>F29</code>) and an upstream failure (<code>F32</code>), which all show the same <strong>failed</strong> badge, and find the node in <strong>Recovery Diagnostics</strong> (<strong>Current node</strong>), <strong>Execution Debugger</strong> and the error trace",
            "See that a node failure leaves no checkpoint (only approvals and waits do) and that <strong>Retry</strong> (<strong>Attempt 2 of 2</strong>) fails at the same node: a retry is not a fix for a code fault",
            "Call <code>propose-patch</code> and see its limits: a generic prompt suggestion for the timed-out node and a <code>400</code> for a guardrail patch aimed at a code node; the <strong>CALIBER Patches</strong> tab stays empty",
            "Write version 2 by hand (<code>assets/workflow/patch/validate.py</code>, <code>check_reference.py</code>, <code>decide.py</code>), prove it on all 35 tickets, and compare versions in <strong>Versions</strong>",
            "Move the <code>staging</code> alias to version 2, run through it, roll back, and see <code>prod</code> refuse a version with no deploy gate",
        ],
        "mermaid": """flowchart LR
  R[Run 35 tickets on v1] --> F[24 failed, 11 completed]
  F --> D[Recovery Diagnostics, Execution Debugger, trace]
  D --> RC[Root cause node: validate, check_reference or decide]
  RC --> RT[Retry: Attempt 2 of 2 fails at same node]
  RT --> PP[propose-patch: no code fix]
  PP --> P[Edit 3 node bodies, save and publish v2]
  P --> V[Run 35 tickets on v2: 0 failed]
  V --> A[staging alias to v2, rollback restores v1]
  A --> PR[prod refuses: no deploy gate]""",
        "gates": [
            {"gate": "Version 1 against the dataset", "target": "11 completed, 24 failed: 17 validate, 3 check_reference, 4 decide"},
            {"gate": "Failing node is findable", "target": "ERROR trace span names the node; no checkpoint; retry fails at the same node"},
            {"gate": "Version 2 against the dataset", "target": "35 completed, 0 failed (10 approve, 13 manual_review, 12 rejected_input)"},
            {"gate": "Regression slice", "target": "the 11 tickets v1 handled give identical output"},
            {"gate": "Alias and prod guard", "target": "rollback restores v1; prod promote of an ungated version is refused (400)"},
        ],
        "notes": [
            "<strong>Prerequisite flags.</strong> The lab itself needs none: workflows, the Run Monitor, versions and aliases are part of the supported alpha. <code>CALIBER_FEATURE_COOKBOOKS=true</code> only opens the gallery, whose starter this lab does not use. Aria is not used.",
            "<strong>Egress and the stand-in service.</strong> Start <code>assets/upstream/account_service.py 8780</code> (not a real system; fixed 503, 500 and 404 ids) and the stack with <code>CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1 scripts/cookbook-env.sh up lab09 --flags cookbooks</code>; without the allowed host <code>lookup</code> fails with <code>outbound request to '127.0.0.1' is blocked</code>. Needs a workflow worker.",
            "<strong>Nothing here needs a model.</strong> There is no agent node, so <code>CALIBER_LLM_PROVIDER=fake</code> does not matter and every number is measured. The poison records time out because Python <code>re</code> really backtracks.",
            "<strong>Preview and deploy gates cannot run this workflow.</strong> Python Code and API Request nodes are refused (<code>preview blocked before execution ...</code>), so the proof is real runs of the dataset on version 2, and a <code>prod</code> promotion is refused for lack of a deploy gate.",
            "<strong>Patching is manual.</strong> <code>propose-patch</code> is API only, agent-shaped (it cannot repair a code node) and no page calls it. Aria is not used and there is no UI for aliases: the alpha UI has one live alias (<code>prod</code>), so <code>staging</code> and its rollback are API only.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk python sdk/caliber-sdk/examples/cookbooks/cookbook_09_self_healing_workflows.py</code> (about three minutes, about 75 runs) or the spec <code>caliber/caliber-ui/e2e/cookbooks/labs/09-workflow-debugger.spec.ts</code> (<code>npx playwright test --config=playwright.labs.config.ts</code>); offline check <code>caliber/tests/test_cookbook_09_self_healing.py</code>.",
            "<strong>Retry from a checkpoint</strong> exists for approvals and waits (lab 03), not for node failures; role failures are covered by lab 18.",
        ],
    },
    {
        "id": "CB-16",
        "num": "16",
        "slug": "observability-triage",
        "track": "Operate",
        "title": "Production Observability & Triage",
        "subtitle": "Turn real production traces into a regression test set and a triage review queue, with the reviewers' answers written back onto the traces.",
        "level": "Core",
        "time": "30–45 min",
        "surfaces": [
            "Observability",
            "Test Sets",
            "Review Queues",
            "Workflows",
        ],
        "build": "You replay 40 production-like refund tickets through a real workflow (the buggy version 1 of cookbook 09), so CALIBER records 40 MLflow traces: 12 <code>ERROR</code> and 28 <code>OK</code>, four of which are silently wrong. You find failures with the status filter, read a trace's node tree for the failing node, capture the 16 traces that matter into a test set with their gold outcome, and route them to a <code>prod-triage</code> review queue whose answers appear as assessments on the traces. Nothing here needs a model.",
        "learn": [
            "Filter traces by status (<strong>All statuses</strong> / <strong>OK</strong> / <strong>Error</strong> / <strong>In progress</strong>) and search by text",
            "Read the <strong>Spans</strong> tree: <code>node.validate</code>, <code>node.check_reference</code> or <code>node.decide</code> is <code>ERROR</code>; for an upstream failure <code>node.lookup</code> is <code>OK</code> and <code>decide</code> fails",
            "Capture traces into a test set with <strong>Add to test set</strong> or <strong>Capture from trace</strong>, then replace the captured error text with the gold outcome",
            "Stand up a review queue with five triage questions, enqueue trace ids and submit reviews so answers write back as trace assessments",
            "See how Observability behaves on an unknown trace (404), no match (empty list) and MLflow down (503)",
            "Find the 4 silent failures (<code>P25</code> to <code>P28</code>) that the Error filter cannot see",
        ],
        "mermaid": """flowchart LR
  SEED["seed_traces.py: 40 tickets through workflow v1"] --> OBS["Operate: Observability, filter Status = Error"]
  OBS --> TR["Open trace: Spans node tree"]
  TR -->|"Add to test set"| DS[("Test Set: prod-regression")]
  DS --> GOLD["Edit Expected: gold outcome"]
  TR -->|"trace ids, Enqueue"| RQ[("Review Queue: prod-triage")]
  RQ -->|"Submit review"| AS["Assessments on the trace"]
  OBS --> SIL["Find the OK but wrong traces by hand"]
  SIL --> RQ""",
        "gates": [
            {"gate": "Traffic recorded", "target": "40 new traces, 12 ERROR and 28 OK, and the Error filter finds every failing one"},
            {"gate": "Root cause readable", "target": "the failing node (validate, check_reference or decide) is ERROR in the node tree"},
            {"gate": "Failure paths", "target": "unknown trace id 404, empty window empty list, MLflow down 503"},
            {"gate": "Test set", "target": "16 traces (12 errors + 4 wrong OK ones), each with its gold outcome"},
            {"gate": "Triage written back", "target": "prod-triage holds the same 16, two answered, and their five answers appear as assessments"},
        ],
        "notes": [
            "<strong>Feature flag.</strong> The lab needs none (Observability, Test Sets and Review Queues are supported alpha); <code>CALIBER_FEATURE_COOKBOOKS=true</code> only opens the gallery. Aria is not used.",
            "<strong>Setup.</strong> The traces come from cookbook 09's workflow: run the stand-in account service (<code>account_service.py 8780</code>, not a real system) and start the server with <code>CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1</code>, otherwise all 40 seed runs fail. A worker is required.",
            "<strong>Nothing is simulated, no model needed.</strong> The workflow has no agent node, so the fake default providers change nothing. The optional Evaluations baseline on the test set is not part of this lab (Evaluations refuse on a simulated stack, see lab 10).",
            "A capture stores the trace's <strong>response</strong> as the expected answer, which for a failed run is the error text; edit the example (or pass <code>expected</code> in the API) to put the gold outcome on it.",
            "MLflow records traces asynchronously, so the last trace of a run can read <code>IN_PROGRESS</code> for a moment. With <code>CALIBER_TRACING_ENABLED=false</code> runs complete but <code>trace_id</code> is null.",
            "Alpha limit: traces are not scoped to a project, so any signed-in user can read every trace and its request text; do not put sensitive data in them on a shared deployment.",
            "Not covered: trace-to-refinement automation, the Evaluations baseline, and the UI rendering of the MLflow-down 503 (checked through the API only).",
            "Run it with <code>sdk/caliber-sdk/examples/cookbooks/cookbook_16_observability_triage.py</code> or the Playwright spec <code>caliber/caliber-ui/e2e/cookbooks/labs/16-observability-triage.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-10",
        "num": "10",
        "slug": "trustworthy-evaluation",
        "track": "Govern",
        "title": "Trustworthy Evaluation",
        "subtitle": "Certify that an automated judge agrees with human reviewers before it is trusted to block a release.",
        "level": "Core",
        "time": "60–120 min",
        "surfaces": [
            "Prompts",
            "Test Sets",
            "Judges",
            "Evaluations",
            "Review Queues",
        ],
        "build": "A governance lane that loads a 50-row test set of support replies and incident summaries, defines a custom <code>FaithfulnessJudge</code>, scores a baseline and a candidate with deterministic graders plus the judge, then has people review the same outputs and imports their labels so CALIBER computes agreement, Cohen's kappa and false positives and negatives. The judge is <strong>certified</strong> only if it clears the thresholds on enough rows, otherwise <strong>rejected</strong>, and every disagreement goes back into the test set as a hard case.",
        "learn": [
            "Load a 50-row test set (<strong>Prompts &gt; Calibration &gt; Upload JSON/JSONL</strong>); each appended example is a version, so 50 rows is version 51",
            "Create a judge whose instructions use only <code>{{ inputs }}</code>, <code>{{ outputs }}</code>, <code>{{ expectations }}</code>, <code>{{ conversation }}</code> or <code>{{ trace }}</code>; <code>{{ evidence }}</code> is refused with a 400",
            "Run baseline and candidate Evaluations with <strong>Contains expected</strong>, <strong>Non-empty</strong> and the judge, and compare per-row results",
            "Review the same traces in a queue, then <strong>Import labels</strong> and <strong>Check alignment</strong> in <strong>Judges &gt; Try it &gt; Human alignment</strong>",
            "Certify or reject on agreement, kappa and a minimum row count, using a deliberately weak <code>LenientJudge</code> as the negative control",
            "Feed the nine disagreements back into the test set (tag <code>judge-disagreement</code>)",
        ],
        "mermaid": """flowchart LR
  DS[("Test Set: 50 rows, v51")] --> BASE["Evaluations: baseline"]
  DS --> CAND["Evaluations: candidate grounded-reply@1"]
  J["Judges: FaithfulnessJudge + LenientJudge"] -.-> CAND
  TR["Seeded traces, one per row"] --> RQ[("Review Queue: faithfulness-certification")]
  RQ -->|Import labels| AL["Judges: Check alignment"]
  J --> AL
  AL --> DEC{"agreement 0.80, kappa 0.60, 30 rows"}
  DEC -->|meets| OK["CERTIFIED"]
  DEC -->|misses| NO["REJECTED"]
  AL --> FB["Disagreements added to Test Set"]
  FB --> DS""",
        "gates": [
            {"gate": "Judge agreement with human labels", "target": "≥ 0.80"},
            {"gate": "Cohen's kappa", "target": "≥ 0.60"},
            {"gate": "Labelled rows", "target": "≥ 30, every row scored (scored = n)"},
            {"gate": "Weak judge control", "target": "LenientJudge is rejected (0.58 agreement, kappa 0.0933 with the stand-in model)"},
            {"gate": "Disagreements fed back", "target": "9 added to the test set (version 60)"},
        ],
        "notes": [
            "<strong>Feature flag.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> opens the gallery card and the SDK script's recipe check; the lab itself uses only test sets, judges, evaluations and review queues. Start a stack with <code>scripts/cookbook-env.sh up lab10 --flags cookbooks</code>.",
            "<strong>Simulated default cannot run the judge.</strong> With <code>CALIBER_LLM_PROVIDER=fake</code> and <code>CALIBER_EVAL_PROVIDER=fake</code>, Evaluations answer 400 and alignment reports every row as an error (<code>scored</code> 0 of 50). Nothing is faked; use a real provider or the lab's stand-in model (<code>CALIBER_EVAL_PROVIDER=mlflow</code> with a real LLM provider, or the server refuses to start).",
            "<strong>The stand-in model proves wiring and arithmetic, not judge quality.</strong> It is a scripted OpenAI-compatible server (<code>assets/stand-in-model/stand_in_model_server.py</code>); its numbers (FaithfulnessJudge 0.82 and kappa 0.6365 over 50 rows) say nothing about how a real model would judge this data.",
            "<strong>Calibration gate on a canned score.</strong> On the simulated stack the calibration job on the same test set passes its gate on a canned 0.94 (<code>simulated: true</code>); that verdict means nothing without a judge you have already checked against people.",
            "Only five rows can give agreement 0.8 and kappa 0.6154: the product returns numbers for any sample of 1 to 100 rows, so the lab's rule of 30 scored rows is what makes certification meaningful. The UI path over 10 rows is a readout, not a certification.",
            "<strong>Keep reviewer notes out of the queue.</strong> <strong>Import labels</strong> passes every non-pass/fail answer to the judge as <code>{{ expectations }}</code>, so a reviewer's quote of the unsupported claim lowers agreement (0.82 to 0.66); keep the evidence box to evidence.",
            "Run it through the API with <code>sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py</code> (add <code>--expect-stand-in</code> to pin the numbers), or the Playwright spec <code>caliber/caliber-ui/e2e/cookbooks/labs/10-judge-certification.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-11",
        "num": "11",
        "slug": "release-signoff",
        "track": "Govern",
        "title": "Release Signoff Factory",
        "subtitle": "Record an accountable go / no-go: server-scored criteria, owned waivers, an immutable signoff. A record, not a deployment.",
        "level": "Core",
        "time": "20–40 min",
        "surfaces": [
            "Releases",
            "Audit Log",
        ],
        "build": "You create 20 release candidates from a dataset and check the server's weighted score, status and blockers against figures worked out independently (14 ready, 6 blocked). On a few of them you record admin waivers, generate an Allure-compatible report snapshot and sign off GO and NO-GO, then see that a signed candidate is final. <strong>Releases</strong> is a preview, record-only page: signing off writes a decision and deploys or rolls back nothing. No model is involved, so nothing is simulated.",
        "learn": [
            "Create candidates with weighted criteria and read the server-computed <code>weighted_score</code>, status and blockers; ready means score at or above <code>required_score</code> and no unwaived blocker",
            "Record an admin waiver (existing criterion key, reason of 8 or more characters, future expiry): it clears a blocker but never a low score",
            "<strong>Generate Allure</strong> after the last change, because a report made earlier is a stale snapshot with a different SHA-256",
            "Sign off GO or NO-GO with a rationale; a signed candidate cannot be evaluated, waived or signed again (409)",
            "See that a signed GO moves no alias and adds nothing to the timeline; <strong>Promote</strong> on the prompt is the real release",
            "Read who created, waived, reported and signed in the audit log",
        ],
        "mermaid": """flowchart TD
  A["New candidate: criteria, scores, evidence"] --> B{"Server evaluates"}
  B -->|"blocker unmet"| W["Admin waiver: reason, expiry"]
  W --> B
  B -->|"score under the bar"| N["Sign off NO-GO"]
  B -->|"ready"| R["Generate Allure snapshot"]
  R --> G["Sign off GO: needs planned action and rollback target"]
  G --> F["signed_go: final, 409 on any change"]
  N --> F2["signed_no_go: final"]
  F --> X["Deploys nothing: Promote is a separate act"]
  F --> AU["Audit Log: who created, waived, signed"]""",
        "gates": [
            {"gate": "Candidates created", "target": "20, server score, status and blockers equal the dataset (14 ready, 6 blocked)"},
            {"gate": "Waiver", "target": "RC16 becomes ready at the same 0.721; RC17 stays blocked (0.6575) after both blockers are waived"},
            {"gate": "GO", "target": "refused (409) on a blocked candidate and on one with no planned action or rollback target"},
            {"gate": "After signoff", "target": "evaluate, waive and a second signoff are refused (409)"},
            {"gate": "Signoff deploys nothing", "target": "prompt aliases, live board and timeline unchanged; only Promote moves the alias"},
        ],
        "notes": [
            "<strong>Feature flags.</strong> <code>CALIBER_FEATURE_RELEASE_CANDIDATES=true</code> shows the <strong>Release signoff factory</strong> section on <strong>Releases</strong> and enables <code>/releases/candidates</code> (404 <code>feature_disabled</code> without it); <code>CALIBER_FEATURE_COOKBOOKS=true</code> is only for the gallery. Both are preview areas, off by default. Start with <code>scripts/cookbook-env.sh up lab11 --flags release_candidates,cookbooks</code>.",
            "<strong>Scopes.</strong> Creating, re-evaluating and reporting need the operator scope (<code>CALIBER_OPERATOR_USERS</code>); waivers and signoffs are admin-only (403 otherwise).",
            "<strong>The scores are typed in.</strong> CALIBER does not read them from Evaluations, Review Queues or traces, and does not open evidence references or check that the artifact exists; a candidate for a missing artifact is accepted.",
            "<strong>The report is a snapshot.</strong> Allure-compatible JSON with a SHA-256 of the candidate at that moment, not a CI test report; generate the one you keep after the last waiver.",
            "A waiver does not raise a score, and there is no route that edits a candidate: create a new candidate for new evidence. The UI form has no expiry field, so write the expiry into the reason (the API takes <code>expires_at</code>).",
            "Candidates cannot be deleted. Those created outside a workspace are private to their creator, so a viewer does not see the admin's list.",
            "Run it with <code>sdk/caliber-sdk/examples/cookbooks/cookbook_11_release_signoff_factory.py</code> or the Playwright spec <code>caliber/caliber-ui/e2e/cookbooks/labs/11-release-signoff.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-18",
        "num": "18",
        "slug": "projects-roles-tokens-audit",
        "track": "Govern",
        "title": "Projects, Roles, Tokens & Audit",
        "subtitle": "Set up a nine-person team in a workspace with owner, editor, reviewer and viewer roles, bind an access token to it, and read the audit trail.",
        "level": "Core",
        "time": "45–60 min",
        "surfaces": [
            "Administration",
            "Workspaces",
            "Settings",
            "Audit Log",
            "Releases",
        ],
        "build": "A nine-person support team in a workspace (the UI says <em>workspace</em>, the API says <em>project</em>): accounts from <code>team-roster.csv</code>, project roles, and a role matrix proved with 72 real requests (9 people x 8 actions) from <code>role-matrix.csv</code>. An editor issues an access token bound to the workspace, uses it from <code>caliberctl</code> and the Python SDK to release one prompt, and sees it refused (403) in another project and (401) once revoked or expired. The audit log, the release timeline and live board, and <code>/health</code> and <code>/readiness</code> record and explain all of it.",
        "learn": [
            "Create the nine accounts under <strong>Admin &gt; Administration &gt; Accounts</strong>, create a workspace from the top bar (its creator is the primary owner) and add members under <strong>Project access</strong> as <strong>Owner (Admin)</strong>, <strong>Editor</strong>, <strong>Reviewer</strong> or <strong>Viewer</strong>",
            "Read the role matrix: a non-member gets 404, a viewer 403, and the primary owner cannot be removed or demoted (409); both the platform scope (<code>CALIBER_OPERATOR_USERS</code>, <code>CALIBER_APPROVER_USERS</code>) and the project role must allow a write",
            "Issue a token in <strong>Admin &gt; Settings &gt; API tokens</strong> bound to one workspace (secret shown once), then see it refused with 403 in another project and with 401 once revoked or expired",
            "Find each action in <strong>Operate &gt; Audit Log</strong> by actor, action and entity ID (admin only; no token secret in any row; refused requests are not audited)",
            "Read the prompt release on <strong>Prompt lifecycle &gt; Releases</strong> (one <strong>Promote</strong> event, <strong>What's live now</strong>), and <code>/readiness</code> plus the <strong>Simulated providers</strong> banner",
        ],
        "mermaid": """flowchart LR
  AD["Admin: create 9 accounts"] --> WS["maya creates workspace: primary owner"]
  WS --> MB["Project access: add 7 members with roles"]
  MB --> RM["Role matrix: 72 requests"]
  MB --> TK["priya issues token bound to workspace"]
  TK --> UV["SDK and caliberctl: release a prompt to prod"]
  TK --> RF["403 other project, 401 revoked or expired"]
  UV --> RL["Releases: Promote event + live board"]
  RM --> AU["Audit Log: actor, action, entity"]
  RF --> AU
  RL --> RD["Readiness: eval and llm simulated"]""",
        "gates": [
            {"gate": "Role matrix", "target": "all 72 requests (9 people x 8 actions) return the status role-matrix.csv expects, 0 mismatches"},
            {"gate": "Bound token", "target": "acts in its own workspace; 403 in another project; 401 once revoked or expired"},
            {"gate": "Primary owner", "target": "cannot be removed (409) or demoted (409); no role can be escalated by the person asking"},
            {"gate": "Failure scenarios", "target": "F01-F15 refused with the documented status and text (F16 CSRF only when the server enables it)"},
            {"gate": "Audit", "target": "one row for each action, filterable by actor, action and entity; 0 rows carry a token secret"},
            {"gate": "Release and readiness", "target": "one Promote event for the prompt by the editor, prompt live at v1 on @prod; /readiness ready and names eval and llm as simulated"},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_COOKBOOKS=true</code> only opens the gallery (the lab is listed and installable); projects, roles, tokens, audit and readiness need no flag. Start with <code>scripts/cookbook-env.sh up lab18 --flags cookbooks</code>.",
            "<strong>Server platform scopes are required.</strong> Export <code>CALIBER_ADMIN_USERS=admin</code>, <code>CALIBER_OPERATOR_USERS=admin,maya,omar,priya,sam,quinn,rosa,victor,nora</code> and <code>CALIBER_APPROVER_USERS=admin,maya,omar,quinn</code> before the stack starts: an account holds a password only. <code>wen</code> is on neither list on purpose; <code>nora</code> is an operator in no project.",
            "<strong>Two different 403s.</strong> A viewer who is an operator (<code>victor</code>) is refused by the <em>project role</em>; one who is not (<code>wen</code>) is refused by the <em>platform scope</em> first. A non-member gets 404, not 403. Only someone on both operator and approver lists may be an <code>owner</code> (409).",
            "<strong>Simulated vs real.</strong> Nothing calls a model (default <code>fake</code> LLM/eval providers). The only visible effect is the <code>evaluation.run</code> row: an allowed member gets 400 <code>evaluation requires a real LLM provider</code> (201 with a real provider; the matrix accepts <code>400|201</code>), a viewer gets 403.",
            "<strong>Limitations the lab shows.</strong> Traces and prompt releases are not project-scoped (the timeline lists promotions from every project; the prompt inventory is scoped); refused requests are not audited; apply has no second approver. <code>GET /me</code> answers <code>anonymous</code> for a revoked token, so check a route that needs a user.",
            "<strong>Demo password.</strong> Every roster account uses the public password <code>lab18-demo-passphrase</code>: create them only on a stack nobody else can reach. Leave <code>CALIBER_PROJECT</code> unset so the bound token visibly acts in its own project.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_18_projects_roles_tokens_audit</code> (about ten seconds; <code>--cleanup</code> archives the <code>lab18-*</code> projects and disables the nine accounts). UI spec: <code>18-projects-roles-tokens-audit.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-19",
        "num": "19",
        "slug": "openapi-governance",
        "track": "Build",
        "title": "Governed OpenAPI Tool",
        "subtitle": "Import an OpenAPI spec, curate a read-only tool pack and a governed write draft, publish both, and gate the write behind a second person.",
        "level": "Core",
        "time": "20–40 min",
        "surfaces": [
            "OpenAPI Integrations",
            "Tools",
            "Workflows",
            "Audit Log",
        ],
        "build": "A three-operation Ticket API (<code>assets/server/ticket_api.py</code>), imported by URL over CALIBER's guarded egress path. CALIBER normalizes and classifies the operations, auto-wires one dependency from an explicit OpenAPI <code>link</code>, and leaves a same-tag dependency for a human to confirm. A read-only tool pack binds two operations into one callable; a write draft defaults to requiring approval and cannot be previewed until an operator opts in. Both drafts publish into the ordinary tool registry, and the write tool's approval gate holds in a workflow until a second person decides. Reimporting the upstream's changed spec afterward shows the diff catching one breaking change and missing another, real one.",
        "learn": [
            "Create an integration and import a spec by URL (<strong>Integrations &gt; OpenAPI Integrations &gt; Import</strong>); see it normalize and classify every operation as read, write or external action automatically",
            "Review <strong>Dependencies</strong>: an explicit OpenAPI link auto-wires at high confidence and cannot be reviewed (409); a weaker, same-tag match can be confirmed or rejected",
            "Generate a tool pack from two selected operations (<strong>Operations &gt; bind as one tool pack</strong>) and a separate write draft; meet the write draft's preview refusal (409) until <strong>allow in preview</strong> is set",
            "Publish both drafts (<strong>Tool Drafts &gt; Publish</strong>) into the standard tool registry, then bind the write tool in a workflow and watch a second person's approval hold it at <code>waiting_approval</code>",
            "Change the upstream's spec and <strong>Reimport &amp; diff</strong>: one breaking change (an operation marked deprecated) is caught; a new required request-body field is not, because the diff tracks a fixed set of fields",
        ],
        "mermaid": """flowchart LR
  IM["Import spec by URL"] --> CL["Classify: 2 read, 1 write"]
  CL --> DEP["Auto-wire the explicit link"]
  CL --> PACK["Generate a read-only tool pack"]
  CL --> WRITE["Generate the write draft"]
  WRITE --> GATE["Preview refused: 409"]
  GATE --> OPTIN["allow_in_preview opt-in (break-glass)"]
  PACK --> PUB["Publish into the tool registry"]
  OPTIN --> PUB
  PUB --> WF["Workflow: waiting_approval"]
  WF --> APR["Second person approves + Resume"]
  APR --> RI["Reimport after a spec change"]
  RI --> DIFF["Diff: 1 breaking, 1 change undetected"]""",
        "gates": [
            {"gate": "Classification", "target": "3 operations normalized: GET /tickets and GET /tickets/{ticket_id} read, POST /tickets write"},
            {"gate": "Dependency detection", "target": "exactly 1 auto-wired (high-confidence) dependency, from the explicit OpenAPI link; it cannot be reviewed (409)"},
            {"gate": "Preview gate", "target": "the write draft is refused at preview (409) until allow_in_preview is set; the upstream's own request log shows no call for a refused preview"},
            {"gate": "Role floors", "target": "a reviewer can preview (403 on publish); an editor can publish (403 on approve); only the reviewer role plus the platform approver scope can approve"},
            {"gate": "Approval gate", "target": "the published write tool's workflow run pauses at waiting_approval and completes only after a second person approves and Resume is called"},
            {"gate": "Reimport diff", "target": "changed_count 2, breaking_count 1 after the upstream's spec changes; the operation whose request body gained a required field is absent from \"changed\""},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_INTEGRATIONS=true</code> opens the OpenAPI Integrations page and API, shared with cookbook 05's MCP governance. Start with <code>scripts/cookbook-env.sh up lab19 --flags integrations</code>.",
            "<strong>Egress and the secret.</strong> The upstream runs on loopback, so <code>CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1</code> is required for import, preview and reimport to reach it. <code>OPENAPI_TICKET_TOKEN</code> must be the same literal value in both the upstream's environment and CALIBER's (the auth binding reads it as <code>env://OPENAPI_TICKET_TOKEN</code>).",
            "<strong>Preview is not simulated.</strong> <code>result</code> is <code>{status_code, text, json, headers}</code> from a real outbound call; a non-2xx upstream response comes back as data, not as a thrown error.",
            "<strong>Four different gates, easy to conflate.</strong> <code>resource.execute</code> gates preview; the strictly narrower <code>resource.publish</code> gates publish; <code>resource.approve</code> plus the platform <strong>approver</strong> scope (independent of either) gates approving a workflow run.",
            "<strong>The trust boundary, honestly.</strong> The diff compares a fixed field set (summary, side-effect level, deprecated, auth, whether a body is required, content types, response statuses, tags) -- not which fields inside a body are required. A real breaking change can reimport clean; re-run a published tool's calibration after every reimport, not just the diff.",
            "<strong>Run it.</strong> Start the upstream first: <code>python3 docs-site/cookbooks/19-openapi-integration-governance-lab/assets/server/ticket_api.py 8781 &amp;</code>, then <code>cd sdk/caliber-sdk &amp;&amp; ../../caliber/.venv/bin/python -m examples.cookbooks.cookbook_19_openapi_integration_governance</code> (about a minute; <code>--cleanup</code> archives what it created).",
        ],
    },
    {
        "id": "CB-12",
        "num": "12",
        "slug": "aria-eval-harness",
        "track": "Aria — Autonomous",
        "title": "Aria: Evaluation Harness from Intent",
        "subtitle": "One sentence → a rules-based Aria plan creates a judge and a Test Set; you fill their fields, approve each change, load 30 cases and see what Aria refuses.",
        "level": "Aria",
        "time": "15–30 min",
        "surfaces": [
            "Plans",
            "Judges",
            "Test Sets",
            "Evaluations",
        ],
        "build": "You give Aria one sentence, <em>Create a judge for answer faithfulness and an eval dataset to run it on.</em>, and it draws a two-step plan, <code>judge.create</code> then <code>eval_dataset.create</code>. Nothing runs until you approve; each step then stops on an <strong>Aria needs information</strong> form and, with <strong>Stop before each change</strong>, an <strong>Approve</strong> / <strong>Deny</strong> prompt. At the end a real active judge (<code>AnswerFaithfulness</code>, <code>bool</code>) and a real, empty Test Set exist; you add 30 support cases from <code>faithfulness-cases.jsonl</code> and exercise the refusals.",
        "learn": [
            "State an intent on <strong>Operate &gt; Plans</strong> (<strong>Goal</strong>, <strong>Autonomy: Stop before each change</strong>, <strong>Decompose goal</strong>) and read the draft before you approve it",
            "Understand that the planner is deterministic rules, not a model: it adds a step for every domain word (<code>judge</code>, <code>eval dataset</code>, <code>test set</code>) and was checked against 28 goals (28/28)",
            "Answer a step's <strong>Aria needs information</strong> form (name, instructions, feedback value type) and its approval prompt; each step asks for its own <code>name</code>, nothing is wired between them",
            "Verify the artifacts in <strong>Agent resources &gt; Judges</strong> and <strong>Prompt lifecycle &gt; Test Sets</strong>, then add rows with <strong>+ Add example</strong> (30 cases, one version per row)",
            "See the refusals: a sentence with no steps cannot be approved (409), a bad form answer keeps the question open, a judge with no evaluation variable fails its step and the plan, and an account without the operator scope creates nothing",
        ],
        "mermaid": """flowchart LR
  I[/"Intent sentence"/] --> D["Plans: Decompose goal (rules planner)"]
  D --> A["Approve plan"]
  A --> E["Execute"]
  E --> F1["Form + Approve: judge.create"]
  F1 --> F2["Form + Approve: eval_dataset.create"]
  F1 --> J["Judges: AnswerFaithfulness (bool)"]
  F2 --> T["Test Sets: own name, v1, empty"]
  T --> R["+ Add example x30 (v31)"]
  R --> V["Evaluations: 400 on simulated stack"]
  D --> N["No steps: 409 at approval"]
  F1 --> X["Bad variable or taken name: step and plan failed"]""",
        "gates": [
            {"gate": "Planner agreement", "target": "28/28 goals in assets/dataset/goals.jsonl draw the documented plan; none of the drafts run"},
            {"gate": "Intent draws the plan", "target": "judge.create then eval_dataset.create; four questions answered (judge form, approval, test set form, approval); plan reads completed"},
            {"gate": "Artifacts are real", "target": "judge active as bool; test set has its own name (not the judge's), v1 and no rows"},
            {"gate": "Test set rows", "target": "30 cases loaded, one version per row (version 31); each stored row is the file's row"},
            {"gate": "Refusals hold", "target": "no-step goal 409; bad form answers 409 and stay open; denied step skipped; bad judge fails the plan; no operator scope creates nothing; non-owner 403 / 404"},
            {"gate": "Script and UI spec", "target": "script prints \"All documented outcomes verified.\" (exit 0); Playwright 12-aria-evaluation-harness.spec.ts passes"},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_ARIA=true</code> opens <code>/aria/*</code> and <strong>Plans</strong> (404 <code>feature_disabled</code> without it); <code>CALIBER_FEATURE_COOKBOOKS=true</code> is only needed for the gallery card. Start the stack with <code>scripts/cookbook-env.sh up lab12 --flags cookbooks,aria</code>.",
            "<strong>Operator accounts.</strong> Export <code>CALIBER_OPERATOR_USERS=admin,olga</code> before starting the stack: <code>judge.create</code> and <code>eval_dataset.create</code> need <code>caliber.operator</code>, and <code>vera</code> (created by the script) must not be an operator.",
            "<strong>Simulated vs real.</strong> The planner is rules (no model, same on a real deployment); <code>judge.create</code>, <code>eval_dataset.create</code> and the 30 rows are real. With the default <code>fake</code> LLM/eval providers <strong>Evaluations</strong> is refused (<code>400 evaluation requires a real LLM provider</code>), so the lab verifies the refusal, not a score; <strong>Ask Aria</strong> is not used.",
            "<strong>Planner limits.</strong> It does not read negation, synonyms beyond <code>testset</code> / <code>test set</code> / <code>test-set</code> / <code>eval dataset</code> / <code>eval_dataset</code>, or questions, and matches <code>judge</code> inside <code>judgement</code>. \"Faithfulness scoring\" and \"evaluation dataset\" do not match, so read the plan before approving.",
            "<strong>Empty Test Set.</strong> <code>eval_dataset.create</code> makes an empty set; adding the 30 rows is not an Aria capability (API <code>POST /eval-datasets/{id}/examples</code>, UI <strong>+ Add example</strong>; the UI recipe adds three by hand).",
            "<strong>Failed plans stay failed.</strong> A failed plan cannot be executed again (409): state the goal again with a new name or fixed instructions. Plans cannot be deleted; the script cancels its drafts and <code>--cleanup</code> archives the <code>cookbook-12-*</code> judges and test sets.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_12_aria_evaluation_harness</code> (example: <code>sdk/caliber-sdk/examples/cookbooks/cookbook_12_aria_evaluation_harness.py</code>) with an admin <code>CALIBER_TOKEN</code>.",
        ],
    },
    {
        "id": "CB-13",
        "num": "13",
        "slug": "aria-review-queue",
        "track": "Aria — Autonomous",
        "title": "Aria: Human-Review Queue from Intent",
        "subtitle": "One sentence → a rules-based Aria plan creates a review queue and enqueues 30 flagged traces; people review 12, and the answers land on the traces as labelled examples.",
        "level": "Aria",
        "time": "15–30 min",
        "surfaces": [
            "Plans",
            "Review Queues",
            "Observability",
        ],
        "build": "You give Aria one sentence, <em>Set up a review queue for human labeling of agent replies for safety, citation, and tone.</em>, and it draws two steps: <code>review_queue.create</code> then <code>review_queue.add_items</code>. You enter the queue's name and five-question schema (<code>safety</code>, <code>severity</code>, <code>citation_ok</code>, <code>tone_ok</code>, <code>reviewer_notes</code>) in the plan's form; the enqueue form asks only for <code>trace_ids</code> because the queue's id is wired from step 1. The result is an active queue with 30 pending traces; 12 are reviewed, each answer becomes an assessment on its trace and a labelled example for judge alignment (lab 10).",
        "learn": [
            "Plan a governance queue from one intent on <strong>Operate &gt; Plans</strong>, and see why a review queue <em>always</em> brings its <code>review_queue.add_items</code> step (skip it if you have no traces)",
            "Enter a queue's question schema (<code>pass_fail</code>, <code>categorical</code>, <code>numeric</code>, <code>text</code>; fields <code>key, title, type, options, required, target</code>) in the plan's <strong>Aria needs information</strong> form",
            "See dependency wiring: the enqueue step's only wired input is <code>queue_id</code> from the create step, so its form asks for <code>trace_ids</code> and <code>assigned_to</code> only",
            "Review items in <strong>Operate &gt; Review Queues</strong> (<strong>Submit review</strong>); answers are validated against the questions (400 on a mismatch, item stays pending) and written to the trace in <strong>Observability</strong>",
            "Read the refusals: a skipped queue skips the enqueue, a denied enqueue keeps an empty queue, a categorical question with no options fails the plan, a taken name fails the plan, an account without the operator scope creates nothing",
        ],
        "mermaid": """flowchart LR
  I[/"Intent sentence"/] --> D["Plans: Decompose goal (rules planner)"]
  D --> A["Approve plan, Execute"]
  A --> S1["Form + Approve: review_queue.create"]
  S1 --> Q["Review Queues: 5 questions"]
  S1 -- "queue_id wired" --> S2["Form + Approve: review_queue.add_items"]
  S2 --> P["30 traces pending"]
  P --> R["Submit review x12"]
  R --> AS["Assessments on traces (Observability)"]
  R --> L["alignment-examples: 12 safety labels"]
  S1 -. "Skip step" .-> SK["Enqueue skipped too"]""",
        "gates": [
            {"gate": "Planner agreement", "target": "26/26 goals in assets/dataset/goals.jsonl draw the documented plan; none run"},
            {"gate": "Plan and wiring", "target": "review_queue.create then review_queue.add_items; only queue_id is wired; enqueue form asks trace_ids only; plan reads completed"},
            {"gate": "Queue is real", "target": "active, 5 questions, 30 traces pending and assigned"},
            {"gate": "Reviews", "target": "12 completed, 18 pending; every answer on its trace as an assessment; 12 safety labels (4 unsafe)"},
            {"gate": "Refusals hold", "target": "mismatched review 400; re-review 409; no operator scope 403; non-owner reviewer 404; missing trace 502 on review; bad form 409; categorical without options and taken name fail the plan"},
            {"gate": "Script and UI spec", "target": "script prints \"All documented outcomes verified.\" (exit 0); Playwright 13-aria-review-queue.spec.ts passes"},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_ARIA=true</code> opens <code>/aria/*</code> and <strong>Plans</strong> (404 <code>feature_disabled</code> without it); <code>CALIBER_FEATURE_COOKBOOKS=true</code> is only for the gallery card. <code>scripts/cookbook-env.sh up lab13 --flags cookbooks,aria</code> with <code>CALIBER_OPERATOR_USERS=admin,olga</code> exported first (<code>vera</code> must not be an operator).",
            "<strong>No model anywhere.</strong> The planner is rules (same on a real deployment) and review is people's work; the 12 reviews are the labels in <code>assets/dataset/agent-replies.jsonl</code>, submitted by the script. Judge alignment against them needs a real model and is lab 10.",
            "<strong>Traces are seeded.</strong> A fresh checkout has no agent, so <code>assets/seed_traces.py</code> writes 30 MLflow traces (needs <code>mlflow</code> from the dev virtualenv and <code>MLFLOW_TRACKING_URI</code> from <code>env.sh</code>); with your own agent you copy the flagged trace ids.",
            "<strong>The enqueue does not check ids.</strong> A trace id that does not exist is queued (201, pending) and fails when reviewed (502 <code>RESOURCE_DOES_NOT_EXIST</code>); it stays pending. The enqueue does not discover traces either.",
            "<strong>Queues are personal.</strong> A queue created outside a workspace belongs to its owner: an operator listed in <code>reviewers</code> still gets 404, because <code>reviewers</code> is a label, not access. Team review needs a workspace (lab 18).",
            "<strong>Limits.</strong> Aria only creates a queue and adds traces (no edit or delete; change a schema with <code>PATCH /review-queues/{id}</code> or <strong>Review Queues</strong>). A failed plan cannot be run again (409); plans cannot be deleted.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_13_aria_review_queue</code> (example: <code>sdk/caliber-sdk/examples/cookbooks/cookbook_13_aria_review_queue.py</code>); <code>--cleanup</code> archives the <code>cookbook-13-*</code> queues.",
        ],
    },
    {
        "id": "CB-14",
        "num": "14",
        "slug": "aria-starter-kit",
        "track": "Aria — Autonomous",
        "title": "Aria: Governance Starter Kit from Intent",
        "subtitle": "One sentence → a four-step Aria plan creates a judge, a review queue and a Test Set under three names, at two autonomy levels, with the failure behaviour shown.",
        "level": "Aria",
        "time": "15–30 min",
        "surfaces": [
            "Plans",
            "Judges",
            "Test Sets",
            "Review Queues",
            "Evaluations",
        ],
        "build": "You give Aria one sentence (<em>Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks.</em>) and it draws four steps in registry order: <code>judge.create</code>, <code>review_queue.create</code>, <code>eval_dataset.create</code>, <code>review_queue.add_items</code>. Each step asks for its own fields and, under <strong>Stop before each change</strong>, an approval; you skip the enqueue step (no traces yet). The result is a real judge (<code>AnswerFaithfulness</code>, <code>bool</code>), a queue (<code>governance-review</code>, four questions, no items) and an empty Test Set (<code>release-candidates-eval</code>), which you fill with 30 policy cases.",
        "learn": [
            "Compose several capabilities in one plan from one intent; the plan follows registry order, not the order of the sentence, and a review queue always brings its enqueue step",
            "Fill the judge, queue and Test Set forms and approve each step: seven questions in all (three forms with an approval each, then the enqueue form, skipped); only the enqueue step has a wired input (<code>queue_id</code>)",
            "Change a draft's <strong>Autonomy</strong> to <strong>Run everything it safely can</strong> before approving (409 after): the same kit is built with only the four forms, no approval prompts",
            "See what <strong>Deny</strong> does (denying the queue skips the enqueue with it) and what a half-failed plan leaves behind (a taken queue name fails the plan after the judge was made; Aria does not undo earlier steps)",
            "Verify the kit in <strong>Judges</strong>, <strong>Review Queues</strong> and <strong>Test Sets</strong>, load the 30 cases with <strong>+ Add example</strong>, and see <strong>Evaluations</strong> refused on the simulated stack",
        ],
        "mermaid": """flowchart TD
  I[/"Intent sentence"/] --> D["Plans: Decompose goal (rules planner, 4 steps)"]
  D --> A["Approve plan, Execute"]
  A --> S1["judge.create: form + Approve"]
  S1 --> S2["review_queue.create: form + Approve"]
  S2 --> S3["eval_dataset.create: form + Approve"]
  S3 --> S4["review_queue.add_items: Skip step"]
  S1 --> J["Judges: AnswerFaithfulness (bool)"]
  S2 --> Q["Review Queues: governance-review, no items"]
  S3 --> T["Test Sets: v1, empty, then 30 cases (v31)"]
  S2 -. "Deny" .-> X["Enqueue skipped with it"]""",
        "gates": [
            {"gate": "Planner agreement", "target": "28/28 goals in assets/dataset/goals.jsonl draw the documented plan; none run"},
            {"gate": "Plan completes", "target": "four steps in registry order; seven questions answered; plan completed with 3 steps done and the enqueue step skipped"},
            {"gate": "Kit is real", "target": "active bool judge, queue with four questions and no items, test set v1 with no rows, under three different names"},
            {"gate": "Test set rows", "target": "30 cases loaded, one version per row (version 31)"},
            {"gate": "Autonomy", "target": "Run everything it safely can asks 4 forms and no approvals and builds the same kit; autonomy editable on a draft, 409 after approval"},
            {"gate": "Refusals hold", "target": "denied queue skips enqueue; taken queue name fails the plan and the judge stays; bad judge fails before anything else is made; no-step goal 409; no operator scope creates nothing; non-owner 403 / 404"},
        ],
        "notes": [
            "<strong>Flags.</strong> <code>CALIBER_FEATURE_ARIA=true</code> opens <code>/aria/*</code> and <strong>Plans</strong> (404 <code>feature_disabled</code> without it); <code>CALIBER_FEATURE_COOKBOOKS=true</code> is only for the gallery card. <code>scripts/cookbook-env.sh up lab14 --flags cookbooks,aria</code> with <code>CALIBER_OPERATOR_USERS=admin,olga</code> exported first (<code>vera</code> must not be an operator).",
            "<strong>Simulated vs real.</strong> The planner is rules (no model, same on a real deployment); the judge, queue, test set and the 30 rows are real. With the default <code>fake</code> LLM/eval providers <strong>Evaluations</strong> is refused (<code>400 evaluation requires a real LLM provider</code>); a real provider would let the judge score the 30 rows.",
            "<strong>Three names.</strong> Each step asks for its own <code>name</code>; nothing is shared between the judge, queue and test set (an older build linked names). Names are unique across the deployment, archived ones included.",
            "<strong>Planner limits.</strong> Domain words only: <code>judge</code>, <code>eval dataset</code>, <code>test set</code>, <code>review queue</code>, <code>workflow</code>. \"Governance\", \"starter kit\" and \"spot-check\" plan nothing; <code>test-set</code> works but <code>review-queue</code> does not; negation and \"delete\" are not read; a <code>workflow</code> plans a calibration of an existing one (lab 15), Aria cannot build one.",
            "<strong>Partial failure.</strong> A failed plan cannot be run again (409): state the goal again. What earlier steps made stays; archive it under Judges, Review Queues or Test Sets.",
            "<strong>Scope.</strong> Enqueuing and reviewing traces is lab 13 (this lab skips it); adding the 30 rows is not an Aria capability (UI recipe adds three by hand, the script adds all 30). Plans cannot be deleted.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_14_aria_governance_starter_kit</code> (example: <code>sdk/caliber-sdk/examples/cookbooks/cookbook_14_aria_governance_starter_kit.py</code>); UI spec <code>14-aria-governance-starter-kit.spec.ts</code>.",
        ],
    },
    {
        "id": "CB-15",
        "num": "15",
        "slug": "aria-triage-loop",
        "track": "Aria — Autonomous",
        "title": "Aria: Triage & Recalibrate Loop",
        "subtitle": "One sentence about weak runs → a three-step plan creates a review queue, enqueues 12 flagged traces and parks on a real calibration job that settles on its own.",
        "level": "Aria",
        "time": "20–35 min",
        "surfaces": [
            "Plans",
            "Review Queues",
            "Workflows",
            "Observability",
        ],
        "build": "On a workflow, agent and flagged traces that already exist (created first by <code>assets/setup_subject.py</code> and <code>assets/seed_traces.py</code>), Aria draws <code>review_queue.create</code>, <code>review_queue.add_items</code> and <code>workflow.calibrate</code> from one sentence. You answer six questions (a form and an approval per step); the last approval queues a real calibration job and the plan <strong>parks</strong> (<code>running</code>, step <code>waiting job</code>). The plan worker resumes it with no click and the plan ends where the job ended, while a reviewer triages the 12 flagged traces in <strong>Review Queues</strong>.",
        "learn": [
            "Plan a remediation loop from one intent: it operates on existing ids (trace ids, workflow id, registered agent id), Aria creates neither the traces nor the workflow",
            "Wire and answer the forms: the enqueue step's only wired input is the queue id; enter the 12 trace ids from <strong>Observability</strong> or <code>seed_traces.py</code>, and the workflow and agent ids from <code>setup_subject.py</code>",
            "Watch an async step park and resume: approving <code>workflow.calibrate</code> leaves the plan <code>running</code> with the step <code>waiting job</code>; the page re-reads every five seconds and a parked plan cannot be cancelled (409)",
            "Triage flagged runs in <strong>Operate &gt; Review Queues</strong> (<code>resolution_correct</code>, <code>failure_mode</code>, <code>severity</code>, <code>expected_answer</code>); answers land on the trace as assessments, the expected answer as its expectation",
            "See the refusals: an unknown workflow, unregistered agent or workflow with no deploy-gate test set fails the step at once without queueing a job, and a denied calibration queues nothing",
        ],
        "mermaid": """flowchart LR
  SU["setup_subject.py + seed_traces.py"] --> I[/"Intent: weak runs, queue + calibrate"/]
  I --> D["Plans: Decompose goal (rules planner)"]
  D --> Q["review_queue.create: form + Approve"]
  Q --> E["review_queue.add_items: 12 trace ids"]
  E --> C["workflow.calibrate: workflow id + agent id + Approve"]
  C --> P["Plan parks: running, waiting job"]
  P --> W["Plan worker polls the job"]
  W --> R["Plan mirrors the job: job rejected, plan failed here"]
  Q --> RQ["Review Queues: triage 3 of 12"]""",
        "gates": [
            {"gate": "Planner agreement", "target": "28/28 goals in assets/dataset/goals.jsonl draw the documented plan; none run"},
            {"gate": "Plan and wiring", "target": "review_queue.create, review_queue.add_items, workflow.calibrate; only the queue id is wired; six questions answered"},
            {"gate": "Parks and resumes", "target": "plan running with step waiting_job and a job id RFN-...; parked plan cannot be cancelled (409); worker resumes it unasked; plan ends where the job ended"},
            {"gate": "Queue and triage", "target": "queue with four questions and 12 flagged traces pending and assigned; three triaged runs carry verdict, failure mode and expected answer on their traces"},
            {"gate": "Refusals hold", "target": "unknown workflow, unregistered agent, no test set each fail the step at once with no job; denied calibration queues nothing; no-step goal 409; no operator scope fails at execute; non-owner 403 / 404"},
            {"gate": "Script and UI spec", "target": "script prints \"All documented outcomes verified.\" (exit 0); Playwright 15-aria-triage-recalibrate-loop.spec.ts passes"},
        ],
        "notes": [
            "<strong>Flags and workers.</strong> <code>CALIBER_FEATURE_ARIA=true</code> opens <code>/aria/*</code> and <strong>Plans</strong>; <code>CALIBER_FEATURE_COOKBOOKS=true</code> is only for the gallery card. The plan worker (resumes parked plans about every 10 s) and the refinement worker (runs the job) must be on: <code>scripts/cookbook-env.sh up lab15 --flags cookbooks,aria</code> starts both, <code>--no-workers</code> does not. Export <code>CALIBER_OPERATOR_USERS=admin,olga</code> first.",
            "<strong>Simulated calibration.</strong> The planner is rules, parking, resume and the job are real, but with the default <code>fake</code> providers the fake executor gives the candidate the baseline's outputs: quality delta <code>+0.000</code> &lt; epsilon <code>0.02</code>, the job ends <code>rejected</code> and the plan <code>failed</code> (<code>rejected: workflow calibration rejected: no candidate met calibration gates ...</code>). This is expected, not a bug.",
            "<strong>Completing path not shown.</strong> A job reaching <code>candidate_ready</code> (plan completes) needs a real model behind the executor and is covered by unit tests (<code>caliber/tests/test_aria_async.py</code>), not demonstrated live. The plan never applies the candidate; that is <code>POST /jobs/{id}/apply</code>, a person's decision.",
            "<strong>Existing objects only.</strong> The traces are 12 recorded runs written by <code>seed_traces.py</code>; the subject is a published support workflow with a 30-case test set as its deploy gate, two tools and an agent. The enqueue step does not check that trace ids exist; calibration needs a visible workflow, a registered agent and a deploy-gate test set with rows.",
            "<strong>Planner limits.</strong> <code>workflow</code> plans a calibration whatever the sentence means (\"create a new workflow\", \"roll back\", \"outage\"); \"pipeline\" and \"tune\" plan nothing; negation is not read; there is no add-to-existing-queue step, so a queue is created first.",
            "<strong>Queue visibility.</strong> A queue created outside a workspace is visible to its owner only; <code>reviewers</code> is a label (lab 13). The workflow's own runs do not write traces on this stack.",
            "<strong>Run it.</strong> <code>PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_15_aria_triage_recalibrate_loop</code> (example: <code>sdk/caliber-sdk/examples/cookbooks/cookbook_15_aria_triage_recalibrate_loop.py</code>); the first UI test takes about a minute and a half.",
        ],
    },
]
