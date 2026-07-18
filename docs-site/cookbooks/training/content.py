# -*- coding: utf-8 -*-
"""Training content for the CALIBER Cookbooks HTML guide (data only).

Most steps are achievable through the CALIBER **UI** — no code or backend
changes. A few labs (04, 05, 11) still need a clearly-labelled out-of-band step.
Verified against the shipped build (see ../FEASIBILITY.md, ../ARIA-AUTONOMY.md,
../CRITIQUE-REPORT.md).
"""

TRACKS = ["Foundations", "Build", "Operate", "Govern", "Aria — Autonomous"]

INTRO = {
    "kicker": "UI Training Guide",
    "title": "CALIBER Cookbooks",
    "tagline": (
        "Sixteen practical, build-along recipes that teach the CALIBER platform "
        "end-to-end — most fully implementable in the product UI with no code or "
        "backend changes; a few labs flag a clearly-labelled out-of-band step."
    ),
    "meta": ["16 cookbooks", "5 tracks", "UI-first", "Training-ready", "Mermaid flows + UI mockups"],
    "footer": (
        "CALIBER Cookbooks · generated training guide · every recipe verified "
        "against the shipped platform (out-of-band steps are labelled). Pair each section with its "
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
        </ul>
      </div>
      <div class="callout">
        <strong>The UI-first promise.</strong> Almost everything is done in the product — no code, no backend; the few out-of-band steps (e.g. cookbook 11's <code>make allure-report</code>) are labelled where they occur.
        <ul>
          <li><strong>Datasets</strong> fill from real runs: <code>Observability → open a trace → Add to test set</code>.</li>
          <li><strong>Custom logic</strong> is a workflow <code>Python Code</code> node typed into the editor.</li>
          <li><strong>The Prompt Playground is a live chat</strong> (it calls the model); prompt regression is scored on the prompt's <code>Runs</code> stage, while <code>Evaluations</code> scores a Test Set with deterministic graders.</li>
          <li><strong>Judges</strong> are for LLM-graded criteria; deterministic checks are <strong>scorers</strong> or assertions.</li>
          <li><strong>Aria</strong> (12–15) plans from one sentence in <code>Plans</code>; you create each artifact in its own page.</li>
        </ul>
      </div>
    </div>
    """,
}


COOKBOOKS = [
    # ───────────────────────────── FOUNDATIONS ─────────────────────────────
    {
        "id": "CB-01", "num": "01", "slug": "intake-classifier", "track": "Foundations",
        "title": "Trustworthy Intake Classifier",
        "subtitle": "Prompt fundamentals: stable JSON output + a regression gate that catches weak edits.",
        "level": "Starter", "time": "20–30 min",
        "surfaces": ["Prompts", "Test Sets", "Observability"],
        "build": "A prompt that converts a free-text support message into a stable JSON record "
                 "(<code>intent, priority, confidence, needs_review, reason</code>), plus a baseline "
                 "and a regression check that fails a weaker prompt edit.",
        "learn": [
            "Author and version a prompt in the Prompts workspace",
            "How the Playground chats live and the Runs stage scores a regression",
            "Generate a golden Test Set on the Test Sets stage and save it",
            "Queue a background calibration pass on the prompt",
            "Pin a baseline and read a candidate-vs-baseline regression diff",
        ],
        "mermaid": """flowchart LR
  P[Prompt v1: intake-classifier] --> PG[Playground: live chat check]
  P --> TS[Test Sets: generate + save golden]
  TS --> R1[Runs: run tests]
  R1 --> BL[Set as baseline]
  P2[Weaker v2] --> R2[Runs: re-run]
  BL --> R2
  R2 --> DIFF[Vs. baseline diff → regression]
  P --> CAL[Calibration: queued job]""",
        "steps": [
            {"where": "Library › Prompts › New prompt", "do": "Create <code>intake-classifier</code> and paste the system template that demands JSON-only output with the five required keys.", "asset": "prompt: intake-classifier"},
            {"where": "Prompts › intake-classifier › Author", "do": "Click <code>Save &amp; promote</code> (v1) with a commit message to make it live, or <code>Save draft</code> to register it without rotating the alias. Versions resolve under the <code>prod</code> alias."},
            {"where": "Prompts › Playground", "do": "Render the prompt with a sample ticket and confirm the JSON layout. (This is a render check — there is no score here.)"},
            {"where": "Evaluate › Test Sets › New dataset", "do": "Create an empty dataset <code>intake-classifier-golden</code>."},
            {"where": "Prompts › Playground / Runs", "do": "Run the golden, edge, and negative tickets so each produces a trace."},
            {"where": "Observe › Observability", "do": "Open each run's trace and click <code>Add to test set → intake-classifier-golden</code>, setting the expected labels. This populates rows entirely in the UI.", "asset": "test set rows (from traces)"},
            {"where": "Evaluate › Judges › New judge", "do": "Create <code>InstructionCompliance</code> — instructions reference <code>{{ inputs }}</code>/<code>{{ outputs }}</code>/<code>{{ expectations }}</code>, return type <code>bool</code>.", "asset": "judge: InstructionCompliance"},
            {"where": "Evaluate › Evaluations › Run evaluation", "do": "Dataset = <code>intake-classifier-golden</code>; scorers = <code>contains_expected</code> + <code>exact_match</code> (deterministic graders only). Run it — this is your <strong>baseline</strong>.", "asset": "eval run: baseline"},
            {"where": "Prompts › Author", "do": "Save a deliberately weaker v2 (drop the “JSON only” rule) and re-run the same evaluation."},
            {"where": "Evaluations › run detail", "do": "Select the baseline run to compute per-example deltas; confirm the weaker variant visibly regresses. Pin the strong run as baseline.", "asset": "eval run: candidate"},
            {"where": "Prompts › Calibration", "do": "Queue a calibration pass and capture the job id (it runs in the background — don't wait for an inline score)."},
        ],
        "assets": [
            {"type": "Prompt", "name": "intake-classifier", "detail": "System template enforcing JSON-only output + allowed intent/priority values + abstain-when-unsure. <code>assets/prompts/</code>"},
            {"type": "Test Set", "name": "intake-classifier-golden", "detail": "Golden / edge / negative tickets, populated from traces via Observability."},
            {"type": "Judge", "name": "InstructionCompliance", "detail": "LLM judge: valid JSON + allowed labels + agrees with expectations + resists prompt-injection."},
            {"type": "Scorer", "name": "contains_expected", "detail": "Built-in deterministic scorer for the label match (selected in the Evaluations run panel)."},
        ],
        "gates": [
            {"gate": "Golden output is valid + schema-stable", "target": "overall ≥ 0.90"},
            {"gate": "Weaker variant is caught", "target": "visible regression in the Runs Vs. baseline diff"},
            {"gate": "Failures are explainable", "target": "root cause readable in the trace"},
        ],
        "mock": {"url": "/prompts", "nav": "Prompts", "title": "intake-classifier",
                 "tabs": ["Author", "Playground", "Test Sets", "Runs", "Calibration", "Bind"],
                 "active_tab": "Author",
                 "fields": [("Template", "You classify support input and must return JSON only…"),
                            ("Commit message", "v1 strict-JSON intake classifier")],
                 "buttons": ["Save draft", "Save & promote", "Open Playground"],
                 "caption": "Prompts workspace → Author stage"},
        "notes": [
            "The Playground is a live chat — it <strong>calls</strong> the model; scored regression runs are on the prompt's Runs stage (not Evaluations, which scores datasets).",
            "Author rows in the dataset editor at <code>/eval-datasets/:id → + Add example</code>, or capture them from <code>Observability → Add to test set</code>.",
            "Calibration enqueues a background optimizer job; show the job id rather than waiting.",
            "“valid_json” / allowed-labels are deterministic <strong>scorers</strong>, not a judge.",
        ],
    },
    {
        "id": "CB-02", "num": "02", "slug": "precision-skills", "track": "Foundations",
        "title": "Precision Skills",
        "subtitle": "Deterministic triggering + portable packaging — a skill that fires only when it should.",
        "level": "Starter", "time": "25–40 min",
        "surfaces": ["Skills", "Observability"],
        "build": "A reusable response skill (<code>support-tone-and-citation</code>) that activates on "
                 "support queries but stays silent on engineering ones, and exports cleanly as a portable "
                 "package (Download ZIP).",
        "learn": [
            "Author a skill with a tight trigger summary (the selector reads it)",
            "Render-preview variable substitution",
            "Run deterministic trigger tests (positive vs negative)",
            "Export a portable skill package (Download ZIP)",
            "Tighten the summary first when a negative query mis-fires",
        ],
        "mermaid": """flowchart TD
  A[Author skill: summary + content] --> R[Render Preview]
  A --> T{Trigger Tests}
  T -->|positive queries| Y[is_selected = true]
  T -->|engineering queries| N[is_selected = false]
  A --> X[Export ZIP: portable package]""",
        "steps": [
            {"where": "Library › Skills › New skill", "do": "Create <code>support-tone-and-citation</code>. Write a <strong>tight one-line summary</strong> scoped to customer-facing support replies and explicitly NOT engineering/code/API questions.", "asset": "skill: support-tone-and-citation"},
            {"where": "Skills › … › Author", "do": "Fill the full content (tone, citation hints, escalation) with variables <code>{{ user_message }}</code>, <code>{{ audience }}</code>, <code>{{ policy_context }}</code>."},
            {"where": "Skills › … › Render Preview", "do": "Provide the three variables; confirm there are no <code>unresolved_variables</code> and the layout is correct."},
            {"where": "Skills › … › Trigger Tests", "do": "Run positive queries (refund / how-to) → expect select; run engineering queries (“rotate a JWT signing key”) → expect NOT selected. Selection is deterministic.", "asset": "trigger cases"},
            {"where": "Skills › … › Author", "do": "If a negative query selects, sharpen the <strong>summary</strong> (it dominates selection) before touching the long-form content; re-test."},
            {"where": "Skill detail › Download ZIP", "do": "Export the portable package (SKILL.md + agent metadata + resources) as your portability artifact. (Re-import isn't available in the shipped UI — export is the portability proof.)", "asset": "skill package (Download ZIP)"},
            {"where": "Skills › … › Bind", "do": "Bind the skill to an agent or a future workflow node for reuse."},
        ],
        "assets": [
            {"type": "Skill", "name": "support-tone-and-citation", "detail": "Narrow trigger summary + tone/citation/escalation content. <code>assets/skills/</code>"},
            {"type": "Trigger set", "name": "trigger-cases", "detail": "≈6 positive + ≈6 negative queries with expected <code>is_selected</code>."},
            {"type": "Package", "name": "skill ZIP", "detail": "Exported portable package (Download ZIP); import is not available in the shipped UI."},
        ],
        "gates": [
            {"gate": "Trigger accuracy", "target": "≥ 0.95 (false-pos + false-neg counted)"},
            {"gate": "Portable export", "target": "Download ZIP yields a self-contained SKILL.md + agent manifest"},
        ],
        "mock": {"url": "/skills", "nav": "Skills", "title": "support-tone-and-citation",
                 "tabs": ["Author", "Render Preview", "Trigger Tests", "Scenario Sets", "Runs", "Bind"],
                 "active_tab": "Trigger Tests",
                 "fields": [("User message", "How do I get a refund for a double charge?"),
                            ("Result", "is_selected = true · score 0.82 · matched: support, refund")],
                 "buttons": ["Run trigger test", "Download ZIP"],
                 "caption": "Skills workspace → Trigger Tests (deterministic)"},
        "notes": [
            "Selection scoring is <strong>deterministic</strong> (no LLM) — this is the precision check; there is no LLM judge here.",
            "Skill names are kebab-case and must not start with <code>claude</code>/<code>anthropic</code>.",
            "Calibrate enqueues a background job; Scenario Sets is scaffolded — drive cases through Trigger Tests + Runs.",
        ],
    },
    {
        "id": "CB-03", "num": "03", "slug": "policy-safe-tool", "track": "Foundations",
        "title": "Policy-Safe Decision Tool",
        "subtitle": "Deterministic business logic first, then approval-gated side effects, then an explanation layer.",
        "level": "Core", "time": "40–60 min",
        "surfaces": ["Tools", "Workflows", "Judges", "Evaluations"],
        "build": "A refund decision flow: a deterministic eligibility node, a hard approval gate before "
                 "the (mocked) refund write, and an optional explanation that can never contradict the decision.",
        "learn": [
            "Register a shipped callable as a tool from the Spec form (no code)",
            "Author deterministic logic as a workflow Python Code node",
            "See write tools auto-mock in the sandbox + gate in a workflow",
            "Drive the human-approval gate in the Run Monitor",
            "Score an explanation with an LLM judge — after the deterministic lane is green",
        ],
        "mermaid": """flowchart LR
  RI[/run input JSON/] --> DR[Python Code: decide_refund · parses run input]
  DR --> HA{{Human Approval · gates every run when enabled}}
  HA -->|approve| IR[Tool: initiate_refund · mocked + gated]
  DR -.optional.-> EX[Explanation prompt] -.as a scorer.-> JF[Judge: ExplanationFaithfulness]
  LO[Tool: lookup_order] -.Tools sandbox demo.-> SBX[Fixtures + Hardening]""",
        "steps": [
            {"where": "Library › Tools › New tool (Spec)", "do": "Register <code>lookup_order</code> → module <code>caliber.workflows.demo_tools</code>, callable <code>lookup_order</code>, <code>side_effect_level: read</code>, <code>allow_in_preview: true</code>.", "asset": "tool: lookup_order"},
            {"where": "Library › Tools › New tool (Spec)", "do": "Register <code>initiate_refund</code> (same module), <code>side_effect_level: write</code> — it will be mocked in the sandbox and gated in the workflow.", "asset": "tool: initiate_refund"},
            {"where": "Tools › lookup_order › Sandbox", "do": "Test-run with an order id; confirm a live read result. Run <code>initiate_refund</code> and confirm the response is <code>mocked</code>."},
            {"where": "Tools › … › Fixtures + Hardening", "do": "Save deterministic assertion cases, run the suite inline, pin a baseline, and re-run after a change to see a regression delta."},
            {"where": "Compose › Workflows › New (template: hitl_review)", "do": "Start from the template that ships the <code>human_approval</code> node.", "asset": "workflow: refund-decision"},
            {"where": "Workflow editor › add Python Code node", "do": "Add <code>decide_refund</code>: it <strong>parses the JSON run input</strong> for <code>order_state, risk_flags, amount, days_since_order</code> → outputs <code>decision, reason_code, requires_approval</code>. Wire <code>decide_refund → human_approval → initiate_refund</code> (lookup_order is exercised separately in the Tools sandbox; the human-approval gate pauses every run when runtime approvals are enabled).", "asset": "python_code: decide_refund"},
            {"where": "Workflow editor › Run Monitor", "do": "With runtime approvals enabled (a deployment setting), execute a run → it pauses at <code>waiting_approval</code> on the human-approval node; approve, then <strong>resume</strong> → the mocked refund fires only after approval. The gate pauses every run regardless of <code>requires_approval</code> (which is informational)."},
            {"where": "Run Monitor", "do": "Reject the approval on another run to prove the external write cannot execute."},
            {"where": "Evaluate › Judges + Evaluations", "do": "(Optional) Add an explanation prompt/node, create <code>ExplanationFaithfulness</code>, and score it — only after the deterministic lane is green."},
        ],
        "assets": [
            {"type": "Tool", "name": "lookup_order / initiate_refund", "detail": "Shipped demo callables registered via Spec; <code>read</code> (preview-live) vs <code>write</code> (mocked + gated)."},
            {"type": "Python Code", "name": "decide_refund", "detail": "Deterministic eligibility node authored in the editor. <code>assets/tools/decide_refund.py</code>"},
            {"type": "Workflow", "name": "refund-decision", "detail": "<code>hitl_review</code> template + lookup → decide → approval → refund."},
            {"type": "Judge", "name": "ExplanationFaithfulness", "detail": "LLM judge: the explanation matches the deterministic decision and adds no new commitments."},
        ],
        "gates": [
            {"gate": "Deterministic fixture pass rate", "target": "≥ 0.97; decision mismatch = 0"},
            {"gate": "Approval enforced on high-risk", "target": "visible in the run timeline"},
            {"gate": "Explanation faithfulness", "target": "≥ 0.92"},
        ],
        "mock": {"url": "/workflows/refund-decision/editor", "nav": "Workflows", "title": "refund-decision · Run Monitor",
                 "tabs": ["Editor", "Run Monitor", "Checkpoints", "Recovery", "Debugger"],
                 "active_tab": "Run Monitor",
                 "fields": [("Status", "waiting_approval — Approve to unlock Resume"),
                            ("Node", "human_approval · required_role: caliber.approver")],
                 "buttons": ["Approve", "Reject", "Resume"],
                 "caption": "Workflow editor → Run Monitor (approval gate)"},
        "notes": [
            "A registered tool needs an importable <code>module_path</code>+<code>callable_name</code>; the shipped <code>demo_tools</code> callables let you do this from the UI with no code.",
            "Field is <code>side_effect_level</code> (read/write/external_action); read tools need <code>allow_in_preview: true</code> to run live in the sandbox.",
            "Custom decision logic belongs in a Python Code node (authored in the editor) — it versions with the workflow and needs no registration.",
        ],
    },
    {
        "id": "CB-05", "num": "05", "slug": "governed-mcp", "track": "Foundations",
        "title": "Governed Tool Connectivity (MCP)",
        "subtitle": "Connect an external system, discover its tools, and prove a blocked tool refuses to run.",
        "level": "Core", "time": "20–35 min",
        "surfaces": ["MCP Servers", "Observability"],
        "build": "A governed GitHub MCP connection with a working read tool and a write tool that is "
                 "policy-blocked — invoked from the Playground to show the refusal.",
        "learn": [
            "Quick-connect an MCP server from the catalog",
            "Test connection + tool discovery (schemas)",
            "Invoke a read-only tool from the Playground",
            "Apply a per-tool policy overlay and prove enforcement",
            "Save tool test cases and calibrate them",
        ],
        "mermaid": """flowchart LR
  C[Catalog: GitHub] --> S[Register server]
  S --> TC[Test connection] --> D[Discover tools]
  D --> RT[search_repositories · read] --> OK[Playground invoke ✓]
  D --> WT[create_issue · write]
  WT -->|policy allowed:false| BLK[Playground invoke ✗ refused]""",
        "steps": [
            {"where": "Library › MCP Servers › catalog", "do": "Click the <strong>GitHub</strong> tile, name the server, and supply the token env var; Register.", "asset": "mcp server: GitHub"},
            {"where": "MCP Servers › server row › Test", "do": "Run Test connection → expect “Connected · N tools”. Inspect discovered tools and their input/output schemas."},
            {"where": "MCP Servers › Playground", "do": "Select the server, pick <code>search_repositories</code> (read), invoke with a golden payload, confirm a result + duration."},
            {"where": "MCP Servers › server detail › tool policy", "do": "On the write tool <code>create_issue</code>, set <code>allowed: false</code> (block).", "asset": "policy: block create_issue"},
            {"where": "MCP Servers › Playground", "do": "Invoke the blocked tool → expect a structured <strong>refusal</strong> (<code>success:false</code>, policy error), not an execution. This is your enforcement record."},
            {"where": "MCP Servers › tool calibration", "do": "Save a couple of test cases + assertions for the read tool and run calibrate.", "asset": "mcp tool test cases"},
            {"where": "Observe › Observability", "do": "Confirm the successful invoke and the blocked attempt both appear in trace history."},
        ],
        "assets": [
            {"type": "MCP Server", "name": "GitHub (quick-connect)", "detail": "Catalog tile → token env var → Register. <code>assets/policy/</code> holds the PATCH bodies."},
            {"type": "Policy", "name": "block create_issue", "detail": "<code>allowed:false</code> — the control enforced on direct invoke."},
            {"type": "Test cases", "name": "search_repositories", "detail": "Calibration cases (<code>no_error</code> / <code>output_contains</code>)."},
        ],
        "gates": [
            {"gate": "Connection + discovery", "target": "100% connect; schemas visible"},
            {"gate": "Blocked tool enforcement", "target": "blocked tool cannot execute"},
            {"gate": "Read tool calibrated", "target": "≥ 1 saved calibration run"},
        ],
        "mock": {"url": "/mcp-servers", "nav": "MCP Servers", "title": "GitHub · Playground",
                 "tabs": ["Servers", "Playground"], "active_tab": "Playground",
                 "fields": [("Tool", "create_issue (write · blocked)"),
                            ("Result", "Failed — Tool 'create_issue' is blocked by policy")],
                 "buttons": ["Invoke Tool"],
                 "caption": "MCP Servers → Playground (policy refusal)"},
        "notes": [
            "On the <strong>direct invoke</strong> path only <code>allowed:false</code> blocks; <code>requires_approval</code> gates at <strong>workflow time</strong> (a human_approval node), not on direct invoke.",
            "Assertion types are <code>no_error</code>, <code>output_contains</code>, <code>equals</code>.",
            "Keep write-capable tools blocked by default until the read path is stable.",
        ],
    },
    # ───────────────────────────── BUILD ─────────────────────────────
    {
        "id": "CB-04", "num": "04", "slug": "doc-to-json", "track": "Build",
        "title": "Document-to-JSON Pipeline",
        "subtitle": "Turn Office documents into schema-valid JSON, with readable failures for bad inputs.",
        "level": "Core", "time": "35–55 min",
        "surfaces": ["Object Store", "Prompts", "Tools", "Workflows", "Observability"],
        "build": "A workflow that fetches a document, extracts its text/tables, structures it to JSON "
                 "with a prompt, and validates the result against a target schema — separating extraction "
                 "failures from validation failures.",
        "learn": [
            "Upload + preview + extract Office docs in Object Store",
            "Register the shipped extractor tool",
            "Author a structuring prompt that never invents values",
            "Validate JSON in a Python Code node",
            "Separate extraction vs normalization vs validation failures in traces",
        ],
        "mermaid": """flowchart LR
  OB[(Object Store: docs)] -.upload + preview/extract.-> LF[/local file path/]
  LF --> EX[Tool: extract_document · reads a local path]
  EX --> ST[Agent: doc-structurer prompt]
  ST --> VAL[Python Code: validate_document_json]
  VAL -->|pass / partial / fail| OUT[output]""",
        "steps": [
            {"where": "Knowledge › Object Store › New bucket", "do": "Create <code>doc-intake</code> and upload the sample .docx / .pptx / .xlsx.", "asset": "bucket: doc-intake"},
            {"where": "Object Store › file › Extract", "do": "Confirm text for DOCX/PPTX and sheet rows for XLSX; confirm a legacy <code>.doc</code> returns a readable “unsupported” error (your negative case)."},
            {"where": "Library › Tools › New tool (Spec)", "do": "Register <code>extract_document</code> → module <code>caliber.workflows.ingestion_tools</code>, <code>side_effect_level: read</code>, <code>allow_in_preview: true</code>.", "asset": "tool: extract_document"},
            {"where": "Library › Prompts › New prompt", "do": "Author <code>doc-structurer</code>: extract only verifiable facts, emit JSON matching the target schema, list <code>missing_fields</code>, never invent values.", "asset": "prompt: doc-structurer"},
            {"where": "Compose › Workflows › New (template: blank)", "do": "Add nodes: <code>extract_document (reads a local file path) → agent(doc-structurer) → Python Code(validate_document_json) → output</code>. (extract_document opens a local filesystem path, so land each document locally first — it does not read an object-store key.)", "asset": "workflow: doc-intake"},
            {"where": "Workflow editor › add Python Code node", "do": "Author <code>validate_document_json</code>: required keys + type checks → <code>validation_status</code> + <code>missing_fields</code>.", "asset": "python_code: validate_document_json"},
            {"where": "Run Monitor", "do": "Run a clean doc (pass), a partial doc (partial + missing_fields), and the unsupported file (clean error, not a crash)."},
            {"where": "Observe › Observability", "do": "Open each run's trace; separate extraction-node failures from validation-node failures. Capture a readable validation error."},
        ],
        "assets": [
            {"type": "Bucket", "name": "doc-intake", "detail": "S3/MinIO-backed; holds the sample documents + a JSON schema."},
            {"type": "Tool", "name": "extract_document", "detail": "Shipped ingestion callable (PDF/DOCX/PPTX/XLSX/MD)."},
            {"type": "Prompt", "name": "doc-structurer", "detail": "Structuring prompt bound to the target schema."},
            {"type": "Python Code", "name": "validate_document_json", "detail": "JSON-schema validator node. <code>assets/tools/validate_document_json.py</code>"},
        ],
        "gates": [
            {"gate": "Golden schema pass rate", "target": "≥ 0.95"},
            {"gate": "Unsupported format error readability", "target": "100% (identifies the stage)"},
        ],
        "mock": {"url": "/object-store/doc-intake", "nav": "Object Store", "title": "doc-intake",
                 "tabs": ["Objects", "Preview", "Extract"], "active_tab": "Extract",
                 "fields": [("invoice-clean.docx", "kind: document · 1,284 chars extracted"),
                            ("invoice.doc (legacy)", "kind: unsupported — convert to .docx")],
                 "buttons": ["Extract", "Upload"],
                 "caption": "Object Store → Extract (Office docs)"},
        "notes": [
            "Extract supports <code>.docx/.pptx/.xlsx</code> — not legacy <code>.doc/.ppt/.xls</code>. The readable “unsupported” error comes from the Object Store <strong>extract endpoint</strong>.",
            "Schema validation is deterministic (Python Code node) — not an LLM judge.",
            "Binary .docx/.xlsx can't be authored as text; <code>assets/dataset/sources/</code> ships stand-ins + conversion notes.",
        ],
    },
    {
        "id": "CB-06", "num": "06", "slug": "grounded-knowledge", "track": "Build",
        "title": "Grounded Knowledge Assistant",
        "subtitle": "Cited answers, honest abstention on missing/conflicting evidence, and a measured retrieval lift.",
        "level": "Core", "time": "40–60 min",
        "surfaces": ["Knowledge Base", "Prompts", "Workflows", "Evaluations", "Review Queues"],
        "build": "A retrieval assistant over a small policy corpus that cites its sources, abstains when "
                 "evidence is missing, surfaces conflicts, and routes low-confidence answers to human review.",
        "learn": [
            "Build a KB version from an Object Store corpus",
            "Compare dense / hybrid / graph_hybrid retrieval in Explore",
            "Run inline KB calibration (Recall@k, nDCG@k, Faithfulness)",
            "Wire a knowledge_query → answer → router workflow",
            "Route low-confidence runs to a Review Queue",
        ],
        "mermaid": """flowchart LR
  C[(Corpus: policy docs)] --> KB[KB build → version]
  KB --> EXP[Explore: dense/hybrid/graph]
  KB --> CAL[Calibrate: Recall@k…]
  KB --> KQ[knowledge_query] --> ANS[Agent: cite or abstain]
  ANS --> RT{router}
  RT -->|low confidence| RV[(Review Queue)]
  ANS --> JF[Judge: CitationFaithfulness]""",
        "steps": [
            {"where": "Knowledge › Object Store", "do": "Upload the policy corpus (include a deliberate contradiction, e.g. refund window 30 vs 14 days)."},
            {"where": "Knowledge › Knowledge Base › New knowledge base", "do": "Pick the source bucket + docs, choose an embedding model under Advanced configuration, Create; wait for the build run to reach <code>completed</code>.", "asset": "KB version"},
            {"where": "Knowledge Base › Explore › Chunks", "do": "Select the built version in the header switcher; confirm chunk quality + source lineage."},
            {"where": "Knowledge Base › Explore › Query", "do": "Run the same question across <code>dense</code>, <code>hybrid</code>, <code>graph_hybrid</code>; note which retrieves the right chunk."},
            {"where": "Knowledge Base › Calibrate", "do": "Supply question→expected pairs, run; read Recall@k / nDCG@k / Faithfulness / Answer-correctness. Tune, re-run, show recall improve; pin a baseline.", "asset": "calibration run"},
            {"where": "Library › Prompts › New prompt", "do": "Author <code>kb-answer</code>: answer only from retrieved chunks, cite each claim, abstain on missing evidence, surface conflicts.", "asset": "prompt: kb-answer"},
            {"where": "Compose › Workflows › New (template: knowledge_rag)", "do": "Wire <code>knowledge_query → Python Code(score_confidence) → agent(kb-answer) → router → output</code>, with a branch that enqueues a review item when confidence is low.", "asset": "workflow: kb-assistant"},
            {"where": "Run Monitor", "do": "Run an answerable question (cited), a missing-evidence question (abstain), and a conflicting question (clarify)."},
            {"where": "Evaluate › Evaluations", "do": "Turn the questions into a Test Set (author rows at <code>/eval-datasets/:id → + Add example</code> or via Observability) and run it on the deterministic graders plus <code>CitationFaithfulness</code> ticked under <strong>Custom LLM judges</strong>.", "asset": "judge: CitationFaithfulness"},
            {"where": "Observe › Review Queues › New queue", "do": "Create a citation/abstention queue; enqueue the ambiguous run's trace ids; reviewers answer — answers write back to the trace.", "asset": "review queue"},
        ],
        "assets": [
            {"type": "Knowledge Base", "name": "policy KB version", "detail": "Built from the corpus; Explore/Calibrate/Use stages."},
            {"type": "Prompt", "name": "kb-answer", "detail": "Cite-only + abstain-on-conflict answer prompt."},
            {"type": "Workflow", "name": "kb-assistant", "detail": "<code>knowledge_rag</code> template + confidence branch to review."},
            {"type": "Judge", "name": "CitationFaithfulness", "detail": "LLM judge: every claim supported by a cited chunk."},
        ],
        "gates": [
            {"gate": "Citation faithfulness", "target": "≥ 0.90"},
            {"gate": "Abstention policy compliance", "target": "100% on missing/conflicting"},
            {"gate": "Retrieval lift after calibration", "target": "Recall@k improves, faithfulness holds"},
        ],
        "mock": {"url": "/knowledge-bases", "nav": "Knowledge Base", "title": "policy-kb · v1 (active)",
                 "tabs": ["Build", "Explore", "Calibrate", "Use"], "active_tab": "Explore",
                 "fields": [("Query", "What is the refund window?"),
                            ("graph_hybrid", "Refund window 30 days [REFUND-POLICY] — conflicts with [REFUND-FAQ] 14 days → clarify")],
                 "buttons": ["Ask", "Sync to AGE"],
                 "caption": "Knowledge Base → Explore → Query (mode comparison)"},
        "notes": [
            "Explore sub-views are <code>Query</code> / <code>Chunks</code> / <code>Graph</code>. AGE retrieval needs a manual <strong>Sync to AGE</strong> first.",
            "KB Calibrate runs inline and returns the four metrics immediately.",
            "<code>CitationFaithfulness</code> is a real LLM judge; the abstain/clarify behavior is enforced by the prompt contract.",
        ],
    },
    {
        "id": "CB-07", "num": "07", "slug": "support-copilot", "track": "Build",
        "title": "Support Triage Copilot",
        "subtitle": "The full-stack recipe: classify → gather evidence → cited reply → approval-gated escalation.",
        "level": "Advanced", "time": "60–90 min",
        "surfaces": ["Prompts", "Skills", "Tools", "Knowledge Base", "MCP Servers", "Workflows", "Evaluations", "Review Queues"],
        "build": "An end-to-end support assistant that returns one of <code>reply / clarify / escalate_support "
                 "/ escalate_bug</code>, grounds replies in evidence + KB, and routes refunds and bug filings "
                 "through a human-approval gate.",
        "learn": [
            "Compose prompts + skills + tools + KB + MCP into one workflow",
            "Reuse assets from cookbooks 01/02/03/05/06",
            "Encode a four-outcome router",
            "Gate external writes (GitHub issue) behind approval",
            "Evaluate groundedness + route failures to review",
        ],
        "mermaid": """flowchart TD
  T[ticket_intake: classify] --> AL[account_lookup tool]
  AL --> IL[incident_lookup tool]
  IL --> KQ[kb_query]
  KQ --> RG[reply_generation: cited]
  RG --> RT{router: 4 outcomes}
  RT -->|escalate_bug| HA{{Human Approval}}
  HA -->|approve| CI[MCP: create_issue]
  RT -->|reply / clarify| OUT[output]""",
        "steps": [
            {"where": "Library (reuse)", "do": "Confirm reusable assets: tools <code>lookup_order</code>/<code>initiate_refund</code> (03), skill <code>support-tone-and-citation</code> (02), the support KB (06), the GitHub MCP server (05)."},
            {"where": "Library › Prompts", "do": "Author <code>ticket-intake</code> (emit intent + severity + decision) and <code>support-reply</code> (cited reply, never leak internal process).", "asset": "prompts: intake + reply"},
            {"where": "Compose › Workflows › New (template: hitl_review)", "do": "Wire <code>ticket_intake → account_lookup → incident_lookup → kb_query → reply_generation → router → human_approval → create_issue</code>.", "asset": "workflow: support-copilot"},
            {"where": "Workflow editor › router node", "do": "Branch on the reply's <code>decision</code>: reply/clarify finish at output; escalate_support opens an internal note; escalate_bug goes through approval → create_issue."},
            {"where": "MCP Servers › tool policy", "do": "Keep GitHub <code>create_issue</code> as a write tool, wired AFTER the human_approval node."},
            {"where": "Run Monitor", "do": "Run a how-to ticket (cited reply, no gate); a refund/bug ticket → <code>waiting_approval</code> → approve → issue created only after approval; reject another to prove it's blocked."},
            {"where": "Evaluate › Evaluations", "do": "Turn outcomes into a Test Set; run it on the deterministic graders plus <code>GroundedSupportReply</code> ticked under <strong>Custom LLM judges</strong>.", "asset": "judge: GroundedSupportReply"},
            {"where": "Observe › Review Queues", "do": "Route low-scoring runs to a review queue; reviewers answer; feed failures back into the dataset."},
        ],
        "assets": [
            {"type": "Prompts", "name": "ticket-intake, support-reply", "detail": "Classifier + cited-reply prompts. <code>assets/prompts/</code>"},
            {"type": "Skills", "name": "tone, escalation, citation", "detail": "Reused/extended from cookbook 02."},
            {"type": "Tools", "name": "lookup_account_state, lookup_recent_incidents", "detail": "Shipped <code>demo_tools</code> read callables."},
            {"type": "Workflow", "name": "support-copilot", "detail": "<code>hitl_review</code> template + four-outcome router + gated MCP write."},
            {"type": "Judge", "name": "GroundedSupportReply", "detail": "LLM judge: evidence-backed, customer-safe, correct decision."},
        ],
        "gates": [
            {"gate": "Grounded reply score", "target": "≥ 0.90"},
            {"gate": "Approval compliance", "target": "100% on refund / external write"},
            {"gate": "Escalation precision", "target": "≥ 0.85"},
        ],
        "mock": {"url": "/workflows/support-copilot/editor", "nav": "Workflows", "title": "support-copilot",
                 "tabs": ["Editor", "Run Monitor"], "active_tab": "Run Monitor",
                 "fields": [("Decision", "escalate_bug · requires_approval: true"),
                            ("Status", "waiting_approval → approve to file GitHub issue")],
                 "buttons": ["Approve", "Reject", "Resume"],
                 "caption": "Support copilot → Run Monitor (escalate_bug gate)"},
        "notes": [
            "This is the integration showcase — <strong>reuse</strong> 01/02/03/05/06 rather than rebuilding.",
            "The external write is gated by the <code>human_approval</code> node (workflow-time), not by the prompt.",
            "Citations are enforced by the reply prompt contract and verified by the judge.",
        ],
    },
    {
        "id": "CB-08", "num": "08", "slug": "incident-copilot", "track": "Build",
        "title": "Incident Response Copilot",
        "subtitle": "Separate facts from hypotheses, recommend the lowest-risk action, and gate rollbacks.",
        "level": "Advanced", "time": "60–90 min",
        "surfaces": ["Prompts", "Skills", "Tools", "Workflows", "Evaluations", "Review Queues"],
        "build": "An incident workflow that gathers deployment + health evidence (synthetic fixtures), "
                 "produces a fact/hypothesis/open-question summary, recommends an action with a risk flag, "
                 "and gates rollback / external writes behind approval.",
        "learn": [
            "Model evidence tools as Python Code fixture nodes (no shipped callable)",
            "Author a commander prompt that never claims resolution without evidence",
            "Route on risk + requires_approval",
            "Gate rollback / external write with human approval",
            "Score action correctness + review conflicting-evidence runs",
        ],
        "mermaid": """flowchart TD
  IN[ingest: alert/service/env] --> CD[Python Code: deployments]
  IN --> CH[Python Code: service_health]
  CD --> SUM[Agent: facts vs hypotheses]
  CH --> SUM
  SUM --> RT{router: risk}
  RT -->|rollback / write| HA{{Human Approval}}
  HA --> CI[MCP: incident issue]
  RT -->|monitor / investigate| OUT[output]""",
        "steps": [
            {"where": "Library › Prompts › New prompt", "do": "Author <code>incident-commander</code>: distinguish <code>known_facts</code> / <code>hypotheses</code> / <code>open_questions</code>, recommend lowest-risk action, set <code>requires_approval</code>.", "asset": "prompt: incident-commander"},
            {"where": "Compose › Workflows › New (template: hitl_review)", "do": "Add Python Code nodes <code>lookup_recent_deployments</code> and <code>query_service_health</code> returning fixtures keyed by service/env.", "asset": "python_code: evidence fixtures"},
            {"where": "Workflow editor", "do": "Wire <code>ingest → deployments + service_health → summarize(incident-commander) → router → human_approval → create_issue → output</code>.", "asset": "workflow: incident-copilot"},
            {"where": "Run Monitor", "do": "Run a clear post-deploy regression → rollback + <code>waiting_approval</code> → approve; run a low-severity blip → monitor (no gate); run ambiguous evidence → gather_more_evidence."},
            {"where": "Run Monitor", "do": "Confirm the summary keeps facts / hypotheses / open-questions distinct, and the action carries a risk + approval flag."},
            {"where": "Evaluate › Evaluations", "do": "Build a Test Set from the incident outputs; run it on the deterministic graders plus <code>IncidentActionCorrectness</code> ticked under <strong>Custom LLM judges</strong> (note Evaluations scores the model's direct answer, not the workflow).", "asset": "judge: IncidentActionCorrectness"},
            {"where": "Observe › Review Queues", "do": "Enqueue low-scoring / conflicting-evidence runs; reviewers answer; harden the checklist skills before editing the prompt."},
        ],
        "assets": [
            {"type": "Prompt", "name": "incident-commander", "detail": "Fact/hypothesis separation + risk-aware recommendation."},
            {"type": "Skills", "name": "severity matrix, rollback checklist, stakeholder update", "detail": "Reusable incident skills."},
            {"type": "Python Code", "name": "deployments, service_health", "detail": "Synthetic evidence fixtures (no shipped callable). <code>assets/tools/</code>"},
            {"type": "Judge", "name": "IncidentActionCorrectness", "detail": "LLM judge: action matches evidence + risk posture; approval set for rollback/write."},
        ],
        "gates": [
            {"gate": "Action correctness", "target": "≥ 0.88"},
            {"gate": "Approval compliance", "target": "100% on rollback / write"},
            {"gate": "Unsafe action rate", "target": "0"},
        ],
        "mock": {"url": "/workflows/incident-copilot/editor", "nav": "Workflows", "title": "incident-copilot · summarize",
                 "tabs": ["Editor", "Run Monitor", "Debugger"], "active_tab": "Run Monitor",
                 "fields": [("known_facts", "gateway/prod p99=4200ms, error_rate=0.18 after deploy a1b9f3c"),
                            ("recommended_action", "rollback a1b9f3c · requires_approval: true")],
                 "buttons": ["Approve", "Reject"],
                 "caption": "Incident copilot → Run Monitor (evidence + gated rollback)"},
        "notes": [
            "<code>lookup_recent_deployments</code> / <code>query_service_health</code> aren't shipped callables — author them as Python Code fixture nodes (keep service/env names consistent with the dataset).",
            "Rollback + external writes always require approval; read-only evidence gathering does not.",
            "The fact/hypothesis separation is enforced by the prompt's output contract.",
        ],
    },
    # ───────────────────────────── OPERATE ─────────────────────────────
    {
        "id": "CB-09", "num": "09", "slug": "self-healing-workflows", "track": "Operate",
        "title": "Self-Healing Workflows",
        "subtitle": "An operator playbook: reproduce a failure, localize it in the Run Monitor, patch, and validate.",
        "level": "Core", "time": "30–50 min",
        "surfaces": ["Workflows", "Plans", "Observability"],
        "build": "A repeatable recovery loop: drive a workflow to a failure, use the checkpoint / recovery / "
                 "debugger panels to find the root cause, apply a minimal manual patch (new version), and "
                 "prove the fix with a regression slice.",
        "learn": [
            "Reproduce a failure (reject path + a node fault)",
            "Read the checkpoint, recovery, and debugger panels",
            "Use retry lineage (Attempt N of M) + retry-from-checkpoint",
            "Apply a minimal patch by editing the manifest + saving a new version",
            "Validate with a regression slice",
        ],
        "mermaid": """flowchart LR
  R[Run failing input] --> F[failed / waiting]
  F --> DBG[Debugger + Checkpoint + Recovery]
  DBG --> RC[Root cause @ node]
  RC --> PATCH[Edit manifest → save new version]
  PATCH --> V[Preview + real run]
  V --> SLICE[Regression slice passes]""",
        "steps": [
            {"where": "Compose › Workflows › New (template: hitl_review)", "do": "Create a workflow; in the Run Monitor, execute to <code>waiting_approval</code>, then Reject → <code>failed</code> (the reproducible recovery case)."},
            {"where": "Run Monitor › Recovery / Checkpoint / Debugger", "do": "Open the panels: approval timeline, active checkpoint + state blob, and the per-step trace with event markers."},
            {"where": "Run Monitor", "do": "Retry → confirm “Attempt 2 of N” in the lineage panel; for the hitl case, approve → resume → completed."},
            {"where": "Workflow editor › add Python Code node", "do": "(Variant) Add a node that raises on a malformed field to manufacture a real node-level fault; localize the exception in the Debugger.", "asset": "python_code: failing_node"},
            {"where": "Workflow editor", "do": "Apply the smallest fix (node config / branch) and <strong>Save</strong> the draft (the editor has no “save as new version” — versions come from Publish); patching is manual, there is no auto-patch tool."},
            {"where": "Run Monitor", "do": "Run a Preview + real run of the failing input on the new version; confirm it now succeeds."},
            {"where": "Observe › Observability", "do": "Put the pre-fix and post-fix runs side by side; confirm the failing node is green and a small regression slice passes."},
        ],
        "assets": [
            {"type": "Workflow", "name": "recovery target", "detail": "<code>hitl_review</code> (reject→retry→approve→resume) + an optional faulting Python Code node."},
            {"type": "Python Code", "name": "failing_node", "detail": "Deliberately raises on a bad field. <code>assets/tools/failing_node.py</code>"},
            {"type": "Diagnosis note", "name": "root-cause", "detail": "Operator note tying the failure to a concrete node in the trace."},
        ],
        "gates": [
            {"gate": "Root cause is explicit in the trace", "target": "node + evidence identified"},
            {"gate": "Post-fix regression slice", "target": "≥ 0.95 pass; no new failures"},
        ],
        "mock": {"url": "/workflows/recovery/editor", "nav": "Workflows", "title": "recovery · Debugger",
                 "tabs": ["Editor", "Run Monitor", "Checkpoints", "Recovery", "Debugger"], "active_tab": "Debugger",
                 "fields": [("review (human_approval)", "approval.rejected → run failed"),
                            ("Lineage", "Attempt 2 of 2 · retry from checkpoint")],
                 "buttons": ["Retry", "Approve", "Resume"],
                 "caption": "Run Monitor → Debugger + retry lineage"},
        "notes": [
            "Patching is <strong>manual</strong> — edit the manifest in the editor and save a new version; there is no <code>propose_workflow_patch</code> tool.",
            "Aria can <em>narrate</em> a diagnosis but cannot debug/patch a workflow as a capability.",
            "The most reliable demo failure is the <code>hitl_review</code> reject → retry → approve → resume loop.",
        ],
    },
    {
        "id": "CB-16", "num": "16", "slug": "observability-triage", "track": "Operate",
        "title": "Production Observability & Triage",
        "subtitle": "Turn live traces into a triaged review queue + a regression test set — the production monitoring loop.",
        "level": "Core", "time": "30–45 min",
        "surfaces": ["Observability", "Review Queues", "Test Sets", "Evaluations"],
        "build": "An operations loop over a running workflow: monitor traces in Observability, filter to "
                 "errors, drill into a failing run to find the root cause, capture the failing examples into a "
                 "regression Test Set, and route them to a human triage Review Queue — so production signal "
                 "becomes durable evidence instead of being lost.",
        "learn": [
            "Filter live traces by status (OK / Error / In progress) and search by name",
            "Read a trace's node tree, inputs/outputs, latency, and feedback to find a root cause",
            "Capture a failing trace into a regression Test Set (Add to test set → Add example)",
            "Stand up a triage Review Queue and enqueue the flagged trace ids",
            "Answer review questions so labels write back onto the trace, and baseline the failure rate",
        ],
        "mermaid": """flowchart LR
  RUN[Live workflow runs] --> OBS[Observability: filter Status = Error]
  OBS --> TR[Open failing trace: node tree + root cause]
  TR -->|Add to test set| DS[(Test Set: prod-regression)]
  TR -->|trace ids| RQ[Review Queue: prod-triage]
  RQ -->|Submit review| WB[Labels written back to trace]
  DS --> EV[Evaluations: deterministic baseline]
  EV --> FIX[Re-run after fix: rate drops]""",
        "steps": [
            {"where": "Prereq · a running workflow", "do": "Have a workflow that has produced recent runs (ideally a few failing). Reuse cookbook 07 (support-copilot) or 09 (recovery) and run a handful of inputs so Observability has real traces, including at least one Error.", "asset": "existing runs/traces"},
            {"where": "Observe › Observability", "do": "Open Observability and set the <strong>Status</strong> filter to <strong>Error</strong> to isolate failing runs; use the search box to scope by workflow name.", "asset": "filtered error traces"},
            {"where": "Observe › Observability", "do": "Open a failing trace; the <code>trace_id</code> lands in the URL (<code>?trace=…</code>). Read the node tree — the failing node, its inputs/outputs, duration — to find the root cause.", "asset": "root cause (from trace)"},
            {"where": "Observe › Observability", "do": "Capture the failing example: click <strong>Add to test set</strong>, choose/create <code>prod-regression</code>, set the expected output, and <strong>Add example</strong>. Repeat for 2–3 representative traces.", "asset": "test set: prod-regression"},
            {"where": "Observe › Review Queues › + New Queue", "do": "Create <code>prod-triage</code> and add the question schema (root_cause_known / failure_mode / severity / expected_output / reviewer_notes), then <strong>Create queue</strong>.", "asset": "review queue: prod-triage"},
            {"where": "Observe › Review Queues", "do": "Click <strong>Add traces to review</strong>, paste the flagged trace ids, and <strong>Enqueue</strong>.", "asset": "enqueued trace ids"},
            {"where": "Observe › Review Queues", "do": "Open a queued item, answer the questions, and <strong>Submit review</strong> — answers write back onto the trace as assessments/expectations.", "asset": "triaged labels"},
            {"where": "Evaluate › Test Sets", "do": "Confirm <code>prod-regression</code> now holds the captured failing examples — the durable slice to re-run after a fix.", "asset": "regression slice"},
            {"where": "Evaluate › Evaluations", "do": "(Optional) <strong>Run evaluation</strong> on <code>prod-regression</code> with the deterministic graders to baseline the failure rate; re-run after a fix to confirm it drops.", "asset": "failure-rate baseline"},
        ],
        "assets": [
            {"type": "Test Set", "name": "prod-regression", "detail": "Failing examples captured from traces via Observability → Add to test set. <code>assets/dataset/</code>"},
            {"type": "Review Queue", "name": "prod-triage", "detail": "Triage question schema (root_cause_known / failure_mode / severity / expected_output / notes). <code>assets/review-queues/</code>"},
        ],
        "gates": [
            {"gate": "Error traces captured", "target": "every error trace in the window lands in the test set or the triage queue"},
            {"gate": "Root cause explainable", "target": "each flagged trace's failure is readable from its node tree"},
            {"gate": "Triage labeled + written back", "target": "queue items answered; assessments visible on the traces"},
            {"gate": "Regression baseline recorded", "target": "prod-regression scored in Evaluations (re-run after a fix)"},
        ],
        "mock": {"url": "/observability", "nav": "Observability", "title": "Traces · Status = Error",
                 "tabs": ["Traces", "Metrics", "Services"], "active_tab": "Traces",
                 "fields": [("Status", "Error"),
                            ("support-copilot · 14:32", "error · create_issue node raised · 1,240 ms")],
                 "buttons": ["Add to test set"],
                 "caption": "Observability → traces filtered to Error"},
        "notes": [
            "Observability <strong>Status</strong> options are All statuses / OK / Error / In progress; selecting a trace puts its id in the URL (<code>?trace=…</code>) — copy ids from there for the queue.",
            "Dataset rows can be authored in the editor at <code>/eval-datasets/:id → + Add example</code>, or captured via <strong>Add to test set → Add example</strong>.",
            "Review answers write back as MLflow <strong>assessments / expectations</strong> on the trace, so the trace becomes self-documenting.",
            "This is <strong>operations</strong>, not authoring — it converts live production signal into durable evidence (a regression test set + reviewed labels) you can act on and re-check after a fix.",
        ],
    },
    # ───────────────────────────── GOVERN ─────────────────────────────
    {
        "id": "CB-10", "num": "10", "slug": "trustworthy-evaluation", "track": "Govern",
        "title": "Trustworthy Evaluation",
        "subtitle": "Certify that an automated judge agrees with human reviewers before it can block a release.",
        "level": "Core", "time": "45–70 min",
        "surfaces": ["Test Sets", "Judges", "Evaluations", "Review Queues", "Observability"],
        "build": "A governance lane that scores candidate outputs with an LLM judge, has humans review a "
                 "sample of the same traces, and tallies judge↔human alignment — feeding disagreements back "
                 "into the dataset.",
        "learn": [
            "Build a dataset by harvesting traces (from-trace)",
            "Create a faithfulness judge with template variables",
            "Run baseline vs candidate evaluations + read deltas",
            "Enqueue sampled traces for human review",
            "Compute judge↔human alignment by hand and close the loop",
        ],
        "mermaid": """flowchart LR
  TR[Run traces 07/08] -->|Add to test set| DS[(Test Set)]
  DS --> EVb[Evaluations: baseline · deterministic graders]
  DS --> EVc[Evaluations: candidate · deterministic graders]
  J[Judge: FaithfulnessJudge] -.run as scorer.-> V[Judge verdicts]
  EVc --> FLAG[Flagged examples]
  FLAG --> RQ[(Review Queue: human labels)]
  V --> AL[Alignment tally: judge vs human]
  RQ --> AL""",
        "steps": [
            {"where": "Observe › Observability", "do": "From candidate runs (07/08), open traces and <code>Add to test set</code> to aggregate a representative set (auto-extracts input/expected).", "asset": "test set (from traces)"},
            {"where": "Evaluate › Judges › New judge", "do": "Create <code>FaithfulnessJudge</code> — instructions reference <code>{{ inputs }}/{{ outputs }}/{{ expectations }}</code>, return <code>bool</code>.", "asset": "judge: FaithfulnessJudge"},
            {"where": "Evaluate › Evaluations › Run evaluation", "do": "Run the same dataset on the baseline artifact, then the candidate. Capture both run ids."},
            {"where": "Evaluations › run detail", "do": "Open per-example results; in the candidate, select the baseline for deltas; flag the low/false examples."},
            {"where": "Observe › Review Queues › New queue", "do": "Create a queue with the same faithfulness question(s); enqueue the flagged traces; reviewers answer (answers write back to the trace).", "asset": "review queue"},
            {"where": "Manual tally", "do": "Lay judge verdicts next to reviewer answers for the sampled traces; compute alignment = agreements / total; add each disagreement back into the dataset and re-run."},
        ],
        "assets": [
            {"type": "Test Set", "name": "aggregated outputs", "detail": "Harvested from 07/08 traces; mix of pass/fail + planted disagreements."},
            {"type": "Judge", "name": "FaithfulnessJudge", "detail": "LLM judge: every claim supported by expectations."},
            {"type": "Review Queue", "name": "faithfulness review", "detail": "Question schema mirroring the judge criterion; trace-linked."},
            {"type": "Worksheet", "name": "alignment tally", "detail": "trace_id | judge | human | agree?  →  alignment ratio. <code>assets/alignment/</code>"},
        ],
        "gates": [
            {"gate": "Overall eval score", "target": "≥ 0.85"},
            {"gate": "Judge↔human alignment", "target": "≥ 0.80 over ≥ 3 reviewed traces"},
            {"gate": "Disagreements fed back", "target": "added to dataset / rubric"},
        ],
        "mock": {"url": "/evaluations", "nav": "Evaluations", "title": "FaithfulnessJudge · candidate vs baseline",
                 "tabs": ["Run", "Results", "Compare"], "active_tab": "Compare",
                 "fields": [("Example J03", "judge: FAIL · human: PASS → disagreement"),
                            ("Alignment", "5/8 agree = 0.625 (below 0.80 gate)")],
                 "buttons": ["Run evaluation", "Add flagged to queue"],
                 "caption": "Evaluations → Compare + review handoff"},
        "notes": [
            "Alignment / disagreement rate is <strong>not auto-computed</strong> — tally judge vs reviewer answers by hand (worksheet provided).",
            "Populate datasets either in the editor at <code>/eval-datasets/:id → + Add example</code> or via Observability <code>Add to test set</code>.",
            "LLM judges authored on the Judges page are selectable in Evaluations under <strong>Custom LLM judges</strong> (run as <code>Judge.&lt;id&gt;</code> scorers), so each row gets an automatic judge verdict; you still tally judge↔human <em>alignment</em> by hand.",
        ],
    },
    {
        "id": "CB-11", "num": "11", "slug": "release-signoff", "track": "Govern",
        "title": "Release Signoff Factory",
        "subtitle": "Aggregate cross-cookbook evidence into a defensible go / no-go decision.",
        "level": "Core", "time": "30–50 min",
        "surfaces": ["Evaluations", "Review Queues", "Observability", "Settings (Allure)"],
        "build": "A release control lane that gathers evaluation run ids, review-queue completion, and the "
                 "in-app Allure report, scores a weighted rubric by hand, and records a go / no-go decision "
                 "with blockers mapped to their owning cookbook.",
        "learn": [
            "Collect evidence from Evaluations + Review Queues",
            "Re-run critical slices and capture fresh run ids",
            "Open the in-app Allure report",
            "Score a weighted release rubric (operator)",
            "Record a decision with blockers + waivers",
        ],
        "mermaid": """flowchart TD
  E1[Eval run ids 07/08/10] --> SC[Score rubric]
  E2[Review queues complete] --> SC
  E3[Allure report loads] --> SC
  E4[Re-run critical slices] --> SC
  SC --> D{"blockers = 0 and score >= 0.90?"}
  D -->|yes| GO[go]
  D -->|no| NG[no_go + blockers]""",
        "steps": [
            {"where": "Evaluate › Evaluations", "do": "Gather the scorecard run ids for each required cookbook; confirm each required review queue is fully answered."},
            {"where": "Platform › Settings › Allure Report", "do": "Open the in-app Allure report and confirm it loads; capture the URL. (Regenerate it out-of-band with <code>make allure-report</code> beforehand.)", "asset": "Allure report"},
            {"where": "Run Monitor (re-run)", "do": "Re-execute the high-risk slices (07 approval branch, 08 rollback path); record the fresh run ids."},
            {"where": "Operator rubric", "do": "Apply the weights — component 0.30 / workflow 0.30 / review 0.20 / evidence 0.20 — mark each pass/partial/fail and compute the weighted score + blocker count.", "asset": "rubric"},
            {"where": "Decision record", "do": "Publish <strong>go</strong> only if <code>blocker_count = 0</code>, score ≥ 0.90, and Allure is visible; otherwise <strong>no_go</strong> with each blocker mapped to its owning cookbook.", "asset": "decision record"},
        ],
        "assets": [
            {"type": "Run-id manifest", "name": "required evidence", "detail": "Per-cookbook required run ids + review completion + Allure status. <code>assets/dataset/</code>"},
            {"type": "Rubric", "name": "release-rubric", "detail": "Weighted dimensions + gate thresholds + a worked example. <code>assets/rubric/</code>"},
            {"type": "Decision record", "name": "go / no-go", "detail": "Template with per-dimension scores, blockers, waiver log. <code>assets/decision/</code>"},
            {"type": "Prompt (optional)", "name": "release-risk-summarizer", "detail": "Drafts the rationale from your evidence notes."},
        ],
        "gates": [
            {"gate": "Overall release score", "target": "≥ 0.90"},
            {"gate": "Blocker count", "target": "= 0"},
            {"gate": "Allure visible", "target": "report loads in-app"},
        ],
        "mock": {"url": "/settings", "nav": "Settings", "title": "Settings · Allure Report",
                 "tabs": ["General", "Allure Report", "Agent Memory"], "active_tab": "Allure Report",
                 "fields": [("Report", "loaded · 312 tests · 0 failed"),
                            ("Release score", "0.93 · blockers: 0 → GO")],
                 "buttons": ["Open report", "Refresh"],
                 "caption": "Settings → Allure Report (release evidence)"},
        "notes": [
            "There is no built-in release-scoring engine — the rubric is computed by the operator (optionally narrated by a prompt).",
            "Allure is generated out-of-band (<code>make allure-report</code>) and only <strong>served</strong> in-app.",
            "Map every blocker back to its owning cookbook and reopen that gate.",
        ],
    },
    # ───────────────────────────── ARIA — AUTONOMOUS ─────────────────────────────
    {
        "id": "CB-12", "num": "12", "slug": "aria-eval-harness", "track": "Aria — Autonomous",
        "title": "Aria: Evaluation Harness from Intent",
        "subtitle": "One sentence → Aria plans a faithfulness judge + a test set; you create them in the UI.",
        "level": "Aria", "time": "15–25 min",
        "surfaces": ["Plans", "Judges", "Test Sets", "Evaluations"],
        "build": "Give Aria a one-line intent; it decomposes the plan (<code>judge.create</code> + "
                 "<code>eval_dataset.create</code>) in the Plans page. You confirm the plan and create the judge "
                 "and test set in their own UI pages.",
        "learn": [
            "Use the Plans page to turn an intent into a capability plan",
            "Read Aria's plan + autonomy dial",
            "Understand the planner needles (domain words)",
            "Create the planned artifacts in the Judges / Test Sets UI",
            "Where full hands-off autonomy stops today",
        ],
        "mermaid": """flowchart LR
  I[/"Intent: a judge for faithfulness + an eval dataset"/] --> PL[Aria Plans: decompose]
  PL --> S1[step: judge.create]
  PL --> S2[step: eval_dataset.create]
  S1 -.create in UI.-> JU[Judges › New judge]
  S2 -.create in UI.-> TS[Test Sets › New dataset]""",
        "steps": [
            {"where": "Plans › New goal", "do": "Paste the intent: <em>“Create a judge for answer faithfulness and an eval dataset to run it on.”</em> Set autonomy = <code>ask_each</code>.", "asset": "intent"},
            {"where": "Plans › plan detail", "do": "Confirm Aria decomposes it into two steps: <code>judge.create</code> and <code>eval_dataset.create</code>.", "asset": "expected plan"},
            {"where": "Plans › Approve", "do": "Approve the plan shape, then Execute to walk the steps (approve / deny each)."},
            {"where": "Evaluate › Judges › New judge", "do": "Create <code>AnswerFaithfulness</code> from the spec the plan calls for (instructions reference the template vars, <code>bool</code>).", "asset": "judge: AnswerFaithfulness"},
            {"where": "Evaluate › Test Sets › New dataset", "do": "Create <code>support-faithfulness-eval</code> (empty); add rows later from Observability.", "asset": "test set"},
            {"where": "Evaluate › Evaluations", "do": "(Follow-up) Run the dataset on the deterministic graders plus <code>AnswerFaithfulness</code> ticked under <strong>Custom LLM judges</strong> (cookbook 10 territory)."},
        ],
        "assets": [
            {"type": "Intent", "name": "one-liner", "detail": "Contains the planner needles “judge” + “eval dataset”. <code>assets/intent.md</code>"},
            {"type": "Judge", "name": "AnswerFaithfulness", "detail": "The judge.create payload — created via <code>Judges › New judge</code>. <code>assets/judges/</code>"},
            {"type": "Test Set", "name": "support-faithfulness-eval", "detail": "The eval_dataset.create payload — created via <code>Test Sets › New dataset</code>."},
        ],
        "gates": [
            {"gate": "Plan decomposed from intent", "target": "judge.create + eval_dataset.create present"},
            {"gate": "Artifacts exist", "target": "active judge + test set in the UI"},
        ],
        "mock": {"url": "/aria/plans", "nav": "Plans", "title": "Plan · evaluation harness",
                 "tabs": ["Draft", "Approved", "Running"], "active_tab": "Draft",
                 "fields": [("Goal", "Create a judge for answer faithfulness and an eval dataset to run it on"),
                            ("Steps", "1) judge.create   2) eval_dataset.create   ·   autonomy: ask_each")],
                 "buttons": ["Approve", "Execute"],
                 "note": "Aria plans the steps; create each artifact in its own UI page (Judges / Test Sets).",
                 "caption": "Plans → plan detail (decomposed from intent)"},
        "notes": [
            "Aria's <strong>planning</strong> from intent is real; <strong>execution</strong> is via the artifact UI pages because the shipped planner emits empty step inputs (see ARIA-AUTONOMY.md).",
            "The default planner keys off domain words — keep “judge” and “eval dataset” literal in the intent.",
            "<code>eval_dataset.create</code> makes an empty test set; add rows from Observability afterward.",
        ],
    },
    {
        "id": "CB-13", "num": "13", "slug": "aria-review-queue", "track": "Aria — Autonomous",
        "title": "Aria: Human-Review Queue from Intent",
        "subtitle": "One sentence → Aria plans a review queue with a safety / citation / tone label schema.",
        "level": "Aria", "time": "15–25 min",
        "surfaces": ["Plans", "Review Queues", "Observability"],
        "build": "Aria decomposes a review-governance intent into <code>review_queue.create</code> (+ "
                 "<code>add_items</code>); you create the queue with its question schema in the Review Queues UI.",
        "learn": [
            "Plan a governance queue from one intent",
            "Design a review question schema (pass_fail / categorical / numeric / text)",
            "See why “review queue” proposes two steps",
            "Create the queue in the Review Queues UI",
            "Skip the enqueue step when there are no traces yet",
        ],
        "mermaid": """flowchart LR
  I[/"Intent: review queue for safety, citation, tone"/] --> PL[Aria Plans]
  PL --> S1[step: review_queue.create]
  PL --> S2[step: review_queue.add_items]
  S1 -.create in UI.-> RQ[Review Queues › New queue]
  S2 -.deny if no traces.-> SK[skip]""",
        "steps": [
            {"where": "Plans › New goal", "do": "Paste: <em>“Set up a review queue for human labeling of agent replies for safety, citation, and tone.”</em> autonomy = <code>ask_each</code>.", "asset": "intent"},
            {"where": "Plans › plan detail", "do": "Confirm two steps appear (<code>review_queue.create</code> + <code>review_queue.add_items</code> — both from the “review queue” domain)."},
            {"where": "Observe › Review Queues › New queue", "do": "Create <code>agent-reply-review</code> with the question schema: <code>safety</code> (pass_fail, required), <code>severity</code> (categorical), <code>citation_ok</code> (pass_fail), <code>tone_ok</code> (pass_fail), <code>reviewer_notes</code> (text).", "asset": "review queue"},
            {"where": "Plans › Execute", "do": "<strong>Deny</strong> each step — the shipped planner emits empty inputs, so Approve would fail validation; the plan completes and you build the queue in the Review Queues UI."},
            {"where": "Observe › Review Queues", "do": "(Later) Enqueue real flagged traces and have reviewers answer; answers write back to each trace."},
        ],
        "assets": [
            {"type": "Intent", "name": "one-liner", "detail": "Contains the “review queue” needle. <code>assets/intent.md</code>"},
            {"type": "Review Queue", "name": "agent-reply-review", "detail": "The review_queue.create payload (question schema). <code>assets/review-queues/</code>"},
        ],
        "gates": [
            {"gate": "Plan decomposed", "target": "review_queue.create present"},
            {"gate": "Queue exists", "target": "active queue with the question schema"},
        ],
        "mock": {"url": "/review-queues", "nav": "Review Queues", "title": "agent-reply-review",
                 "tabs": ["Queues", "Detail"], "active_tab": "Detail",
                 "fields": [("Question · safety", "pass_fail · required · target: feedback"),
                            ("Question · severity", "categorical [low, medium, high]")],
                 "buttons": ["Create queue", "Enqueue traces"],
                 "note": "Aria plans review_queue.create; build the schema here in the UI.",
                 "caption": "Review Queues → new queue (label schema)"},
        "notes": [
            "“review queue” triggers both <code>review_queue.create</code> and <code>review_queue.add_items</code>; deny the second if you have no traces.",
            "Question item fields: <code>key, title, type, options, required, target (feedback|expectation)</code>.",
            "Aria plans; you create the queue in the Review Queues UI (planner emits empty inputs — see ARIA-AUTONOMY.md).",
        ],
    },
    {
        "id": "CB-14", "num": "14", "slug": "aria-starter-kit", "track": "Aria — Autonomous",
        "title": "Aria: Governance Starter Kit from Intent",
        "subtitle": "The flagship: one sentence → a plan for a judge + test set + review queue together.",
        "level": "Aria", "time": "20–30 min",
        "surfaces": ["Plans", "Judges", "Test Sets", "Review Queues", "Evaluations"],
        "build": "Aria decomposes a single intent into a whole evaluation+governance scaffold "
                 "(<code>judge.create</code> + <code>eval_dataset.create</code> + <code>review_queue.create</code>); "
                 "you create the three artifacts in their UI pages.",
        "learn": [
            "Compose three capabilities in one plan from one intent",
            "Recognize the three planner needles in the goal",
            "Create a judge, a test set, and a review queue in the UI",
            "Assemble a reusable governance starter kit",
            "Hand the kit to cookbook 10 for scoring",
        ],
        "mermaid": """flowchart TD
  I[/"Intent: judge + eval dataset + review queue"/] --> PL[Aria Plans]
  PL --> J[judge.create] -.UI.-> JU[Judges]
  PL --> D[eval_dataset.create] -.UI.-> TS[Test Sets]
  PL --> Q[review_queue.create] -.UI.-> RQ[Review Queues]
  PL --> A[review_queue.add_items] -.deny.-> SK[skip]""",
        "steps": [
            {"where": "Plans › New goal", "do": "Paste: <em>“Stand up our governance starter kit: a judge for answer faithfulness, an eval dataset to score against, and a review queue for human checks.”</em>", "asset": "intent"},
            {"where": "Plans › plan detail", "do": "Confirm the plan contains <code>judge.create</code>, <code>eval_dataset.create</code>, <code>review_queue.create</code> (+ an <code>add_items</code> step you'll deny)."},
            {"where": "Evaluate › Judges › New judge", "do": "Create <code>AnswerFaithfulness</code>.", "asset": "judge"},
            {"where": "Evaluate › Test Sets › New dataset", "do": "Create <code>release-candidates-eval</code>.", "asset": "test set"},
            {"where": "Observe › Review Queues › New queue", "do": "Create <code>governance-review</code> with faithful / citation / tone questions.", "asset": "review queue"},
            {"where": "Plans › Execute", "do": "Approve the three create steps; deny <code>review_queue.add_items</code>. The kit is complete."},
        ],
        "assets": [
            {"type": "Intent", "name": "one-liner", "detail": "Contains all three needles: judge / eval dataset / review queue."},
            {"type": "Judge", "name": "AnswerFaithfulness", "detail": "Created via Judges UI."},
            {"type": "Test Set", "name": "release-candidates-eval", "detail": "Created via Test Sets UI."},
            {"type": "Review Queue", "name": "governance-review", "detail": "Created via Review Queues UI."},
        ],
        "gates": [
            {"gate": "Plan has all three creates", "target": "judge + dataset + queue"},
            {"gate": "Kit exists", "target": "all three artifacts live in the UI"},
        ],
        "mock": {"url": "/aria/plans", "nav": "Plans", "title": "Plan · governance starter kit",
                 "tabs": ["Draft", "Approved"], "active_tab": "Draft",
                 "fields": [("Goal", "Stand up our governance starter kit: a judge, an eval dataset, and a review queue"),
                            ("Steps", "judge.create · eval_dataset.create · review_queue.create · (add_items → deny)")],
                 "buttons": ["Approve", "Execute"],
                 "note": "One intent → a whole eval+governance scaffold. Create each in its UI page.",
                 "caption": "Plans → flagship multi-capability plan"},
        "notes": [
            "The flagship “one sentence → whole governance setup” demo. Build the three artifacts in their UI pages.",
            "All three domain needles must appear in the intent for the default planner.",
            "Hand the judge + test set to cookbook 10 to actually score candidates.",
        ],
    },
    {
        "id": "CB-15", "num": "15", "slug": "aria-triage-loop", "track": "Aria — Autonomous",
        "title": "Aria: Triage & Recalibrate Loop",
        "subtitle": "One sentence → review the weak runs, then kick off an async workflow calibration.",
        "level": "Aria", "time": "20–35 min",
        "surfaces": ["Plans", "Review Queues", "Workflows", "Observability"],
        "build": "On an existing workflow with weak runs, Aria plans <code>review_queue.create</code> + "
                 "<code>add_items</code> + the async <code>workflow.calibrate</code>; you create the queue, enqueue "
                 "the flagged traces, trigger calibration, and watch the plan park + poll.",
        "learn": [
            "Plan a remediation loop from one intent",
            "Operate on existing ids (traces, workflow, agent)",
            "Read flagged trace ids from Observability",
            "Trigger a workflow calibration",
            "Understand async parking + polling in Aria plans",
        ],
        "mermaid": """flowchart LR
  I[/"Intent: review the flagged traces + calibrate the workflow"/] --> PL[Aria Plans]
  PL --> Q[review_queue.create] -.UI.-> RQ[Review Queues]
  PL --> E[review_queue.add_items] -.real trace ids.-> ENQ[enqueue]
  PL --> C[workflow.calibrate · async] --> POLL[park → poll → done]""",
        "steps": [
            {"where": "Prereq (cookbook 07)", "do": "Have an existing workflow with recent runs/traces; read the <strong>flagged trace ids</strong> from Observability and the <strong>workflow_id + agent_id</strong> from that workflow."},
            {"where": "Plans › New goal", "do": "Paste: <em>“Our workflow's recent runs look weak — set up a review queue for the flagged traces and kick off a workflow calibration.”</em>", "asset": "intent"},
            {"where": "Plans › plan detail", "do": "Confirm three steps: <code>review_queue.create</code>, <code>review_queue.add_items</code>, <code>workflow.calibrate</code>."},
            {"where": "Observe › Review Queues › New queue", "do": "Create <code>weak-runs-triage</code> (resolution_correct / failure_mode / severity / expected_answer).", "asset": "review queue"},
            {"where": "Review Queues › Enqueue traces", "do": "Enqueue the real flagged trace ids."},
            {"where": "Workflows › calibration", "do": "Trigger a workflow calibration on the real workflow + agent. In the Aria plan, this step parks (waiting_job); Poll until it resolves and the plan completes.", "asset": "calibration job"},
        ],
        "assets": [
            {"type": "Intent", "name": "one-liner", "detail": "Contains “review queue” + “workflow” needles. <code>assets/intent.md</code>"},
            {"type": "Review Queue", "name": "weak-runs-triage", "detail": "Triage question schema. <code>assets/review-queues/</code>"},
            {"type": "Enqueue", "name": "trace ids", "detail": "Real flagged trace ids (from Observability)."},
            {"type": "Calibration", "name": "workflow + agent", "detail": "Existing ids; the async job parks + polls."},
        ],
        "gates": [
            {"gate": "Queue + items exist", "target": "flagged traces enqueued"},
            {"gate": "Calibration completes", "target": "async job resolves; plan completes"},
        ],
        "mock": {"url": "/aria/plans", "nav": "Plans", "title": "Plan · triage & recalibrate",
                 "tabs": ["Draft", "Running"], "active_tab": "Running",
                 "fields": [("Steps", "review_queue.create ✓ · add_items ✓ · workflow.calibrate ⏳"),
                            ("workflow.calibrate", "waiting_job → poll until done")],
                 "buttons": ["Poll", "Refresh"],
                 "note": "The calibrate step is async: it parks (waiting_job); poll until the job resolves.",
                 "caption": "Plans → running plan with an async parked step"},
        "notes": [
            "This is a <strong>remediation</strong> loop — it operates on EXISTING ids (traces, workflow, agent). Build the subject workflow in cookbook 07 first.",
            "<code>workflow.calibrate</code> is Aria's async capability: the plan parks (<code>waiting_job</code>) and you poll until it resolves.",
            "As elsewhere in the Aria track, create the queue + enqueue + calibration via the UI (planner emits empty inputs).",
        ],
    },
]
