/**
 * MSW request handlers for CALIBER API endpoints.
 *
 * Used by tests to mock backend responses. Add handlers as pages are
 * tested — this file is the single source for all mock API shapes.
 */

import { http, HttpResponse } from "msw";

import { templateManifest } from "@/lib/workflowGraph";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const WORKFLOW_TEMPLATE_ID_MARKER = "__CALIBER_WORKFLOW_ID__";
const WORKFLOW_TEMPLATE_NAME_MARKER = "__CALIBER_WORKFLOW_NAME__";

function envelope<T>(data: T): { data: T } {
  return { data };
}

const WORKFLOW_TEMPLATE_FIXTURES = [
  {
    kind: "single_agent",
    label: "Single Agent",
    description: "One agent with tools and output.",
    icon: "🤖",
    gradient: "from-violet-500/10 to-caliber-500/10",
  },
  {
    kind: "multi_agent_handoff",
    label: "Multi-Agent Handoff",
    description: "Coordinator agent delegates specialist work via handoff.",
    icon: "🤝",
    gradient: "from-fuchsia-500/10 to-rose-500/10",
  },
  {
    kind: "guarded_pipeline",
    label: "Guarded Pipeline",
    description: "Agent → guardrail → output.",
    icon: "🛡️",
    gradient: "from-amber-500/10 to-orange-500/10",
  },
  {
    kind: "parallel_fanout",
    label: "Parallel Fan-Out",
    description: "Fork work across two agents, then join the results.",
    icon: "⚡",
    gradient: "from-sky-500/10 to-indigo-500/10",
  },
  {
    kind: "hitl_review",
    label: "Human Review",
    description: "Agent → PII redact → human approval → output.",
    icon: "✋",
    gradient: "from-emerald-500/10 to-teal-500/10",
  },
  {
    kind: "for_each_loop",
    label: "Batch Loop",
    description: "Process a list of items through one reusable worker agent.",
    icon: "🔁",
    gradient: "from-cyan-500/10 to-teal-500/10",
  },
  {
    kind: "refinement_loop",
    label: "Refinement Loop",
    description: "Iteratively improve one draft through the same worker agent.",
    icon: "🌀",
    gradient: "from-sky-500/10 to-emerald-500/10",
  },
  {
    kind: "knowledge_rag",
    label: "Knowledge Q&A",
    description: "Start → knowledge query → output.",
    icon: "📚",
    gradient: "from-sky-500/10 to-cyan-500/10",
  },
  {
    kind: "graph_hybrid_rag",
    label: "GraphRAG Hybrid",
    description: "Start → graph-hybrid knowledge query → output.",
    icon: "🧠",
    gradient: "from-cyan-500/10 to-emerald-500/10",
  },
  {
    kind: "knowledge_age",
    label: "AGE Graph Retrieval",
    description: "Start → AGE-backed knowledge query → output.",
    icon: "🕸️",
    gradient: "from-emerald-500/10 to-blue-500/10",
  },
  {
    kind: "knowledge_age_build",
    label: "AGE Knowledge Build",
    description: "Launch a graph-synced knowledge-base build for Apache AGE.",
    icon: "🏗️",
    gradient: "from-emerald-500/10 to-teal-500/10",
  },
  {
    kind: "event_resume",
    label: "Event Resume Gate",
    description: "Pause for an external event, then continue with an agent.",
    icon: "📨",
    gradient: "from-amber-500/10 to-sky-500/10",
  },
  {
    kind: "blank",
    label: "Blank Canvas",
    description: "Start from scratch.",
    icon: "📄",
    gradient: "from-slate-500/10 to-gray-500/10",
  },
] as const;

const WORKFLOW_BAKEOFF_SCENARIOS = [
  {
    id: "B1",
    title: "Single-agent answer with tools",
    starter_kind: "single_agent",
    capabilities: ["agent execution", "tool wiring", "output inspection"],
    evidence_to_capture: [
      "Time to first successful run",
      "Final output",
      "Step trace",
    ],
  },
  {
    id: "B2",
    title: "Multi-agent delegation",
    starter_kind: "multi_agent_handoff",
    capabilities: ["handoffs", "delegated terminal output", "run trace"],
    evidence_to_capture: [
      "Handoff configuration effort",
      "Final delegated answer",
      "Node trace",
    ],
  },
  {
    id: "B3",
    title: "Human review gate",
    starter_kind: "hitl_review",
    capabilities: ["guardrails", "human approval", "pause and resume"],
    evidence_to_capture: [
      "Approval UX",
      "Stored checkpoint",
      "Resume trace",
      "Post-approval output",
    ],
  },
  {
    id: "B4",
    title: "External event resume",
    starter_kind: "event_resume",
    capabilities: ["wait_for_event", "correlation", "long-running resume"],
    evidence_to_capture: [
      "Paused state UX",
      "Resume-by-event path",
      "Recovery trail",
    ],
  },
  {
    id: "B5",
    title: "Parallel synthesis",
    starter_kind: "parallel_fanout",
    capabilities: ["parallel branches", "join", "merged result"],
    evidence_to_capture: [
      "Branch visibility",
      "Join correctness",
      "Replay and debugger ergonomics",
    ],
  },
  {
    id: "B6",
    title: "Batch or iterative refinement",
    starter_kind: "for_each_loop",
    capabilities: ["loops", "repeated agent execution", "bounded iteration"],
    evidence_to_capture: [
      "Iteration visibility",
      "Partial failure handling",
      "Output consistency",
    ],
  },
  {
    id: "B7",
    title: "GraphRAG hybrid query",
    starter_kind: "graph_hybrid_rag",
    capabilities: ["knowledge query", "graph-hybrid retrieval", "citations"],
    evidence_to_capture: [
      "Retrieved chunks",
      "Graph context",
      "Citation traceability",
    ],
  },
  {
    id: "B8",
    title: "AGE-native graph build",
    starter_kind: "knowledge_age_build",
    capabilities: ["knowledge build", "graph extraction", "AGE sync config"],
    evidence_to_capture: [
      "Build configuration effort",
      "Run logs",
      "Version metadata",
      "Graph sync readiness",
    ],
  },
  {
    id: "B9",
    title: "AGE graph retrieval",
    starter_kind: "knowledge_age",
    capabilities: ["Apache AGE retrieval mode", "graph-aware answer path"],
    evidence_to_capture: [
      "Retrieval mode controls",
      "Graph evidence",
      "Fallback behavior",
    ],
  },
] as const;

const WORKFLOW_BAKEOFF_RUBRIC = [
  {
    title: "Authoring friction",
    checks: [
      "Time to create the workflow from a starter.",
      "Extra configuration needed before the first valid run.",
      "Whether missing setup appears as inline guidance or only as a runtime failure.",
    ],
  },
  {
    title: "First-pass execution",
    checks: [
      "Time to first successful run.",
      "Number of manual corrections before the workflow runs cleanly.",
      "Whether run inputs, outputs, and node-level state are inspectable without leaving the page.",
    ],
  },
  {
    title: "Recovery and degraded-path handling",
    checks: [
      "Whether paused runs expose a recoverable checkpoint trail.",
      "Whether retrieval or graph-sync fallbacks are visible to the operator.",
      "Whether retry, replay, or resume actions fail closed when state is incomplete or inconsistent.",
    ],
  },
  {
    title: "Observability and evidence",
    checks: [
      "Run history depth and searchability.",
      "Step trace clarity.",
      "Availability of final outputs, retrieved chunks, citations, and lineage metadata.",
    ],
  },
  {
    title: "Reusability and deployment",
    checks: [
      "Whether the workflow can be saved, versioned, exported, and rerun without re-authoring.",
      "Whether starter manifests can serve as reusable governed patterns instead of one-off demos.",
    ],
  },
] as const;

const PROMPT_TEMPLATE_LIBRARY = {
  catalog_version: "2.0.0",
  base_templates: [
    {
      id: "zs-instruct",
      kind: "base",
      source_kind: "library",
      title: "zs-instruct",
      summary: "Answer concisely.",
      domain: "question-answering",
      technique: "zero-shot",
      recommended_modifiers: ["format-enforce"],
      recommended_scorers: ["Correctness", "RelevanceToQuery", "Guidelines"],
      variables: [],
      runtime_variables: [
        {
          name: "instruction",
          label: "instruction",
          description: "the task to perform",
        },
        {
          name: "input",
          label: "input",
          description: "the data to act on",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction: "{{instruction}}",
        context: null,
        examples: null,
        input: "Input:\n{{input}}",
        output_indicator: "Answer concisely.",
      },
      composable_with: ["add-fewshot", "add-cot", "format-enforce"],
      execution_note: null,
    },
    {
      id: "zs-cot-trigger",
      kind: "base",
      source_kind: "library",
      title: "zs-cot-trigger",
      summary: "Let's think step by step.",
      domain: "reasoning",
      technique: "zero-shot-cot",
      recommended_modifiers: ["format-enforce"],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [],
      runtime_variables: [
        {
          name: "question",
          label: "question",
          description: "the question requiring reasoning",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction: "{{question}}",
        context: null,
        examples: null,
        input: null,
        output_indicator: "Let's think step by step.",
      },
      composable_with: ["self-consistency", "format-enforce"],
      execution_note: null,
    },
    {
      id: "rag-grounded-qa",
      kind: "base",
      source_kind: "library",
      title: "rag-grounded-qa",
      summary:
        "Answer the question using ONLY the retrieved context below. If the answer is not in the context, say 'Not found in provided sources.'",
      domain: "question-answering",
      technique: "rag",
      recommended_modifiers: ["self-critique", "format-enforce"],
      recommended_scorers: ["Correctness", "RelevanceToQuery", "Guidelines"],
      variables: [],
      runtime_variables: [
        {
          name: "retrieved_docs",
          label: "retrieved_docs",
          description: "concatenated retrieved chunks with ids",
        },
        {
          name: "question",
          label: "question",
          description: "user question",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction:
          "Answer the question using ONLY the retrieved context below. If the answer is not in the context, say 'Not found in provided sources.'",
        context: "Retrieved context:\n{{retrieved_docs}}",
        examples: null,
        input: "Question:\n{{question}}",
        output_indicator:
          "Answer in prose. Cite the source id(s) in brackets after each claim, e.g. [doc_2].",
      },
      composable_with: ["self-critique", "format-enforce"],
      execution_note: null,
    },
    {
      id: "react-tool-loop",
      kind: "base",
      source_kind: "library",
      title: "react-tool-loop",
      summary:
        "Answer the question by interleaving reasoning and tool use. Available tools: {tools}.",
      domain: "reasoning",
      technique: "react",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [],
      runtime_variables: [
        {
          name: "question",
          label: "question",
          description: "the question/task",
        },
        {
          name: "tools",
          label: "tools",
          description: "list of available tools and signatures",
        },
        {
          name: "scratchpad",
          label: "scratchpad",
          description: "accumulated thought/action/observation history",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction:
          "Answer the question by interleaving reasoning and tool use. Available tools: {{tools}}.",
        context: "Scratchpad so far:\n{{scratchpad}}",
        examples: null,
        input: "Question:\n{{question}}",
        output_indicator:
          "Respond in repeating blocks: 'Thought:' (reasoning), 'Action:' (tool[input]), 'Observation:' (result). When done, output 'Final Answer:'.",
      },
      composable_with: ["reflexion-retry"],
      execution_note: null,
    },
    {
      id: "check-hallucination",
      kind: "base",
      source_kind: "library",
      title: "check-hallucination",
      summary:
        "Determine whether the answer is supported by the provided context. Flag any claim not grounded in it.",
      domain: "truthfulness",
      technique: "zero-shot",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [],
      runtime_variables: [
        {
          name: "context_docs",
          label: "context_docs",
          description: "ground-truth context",
        },
        {
          name: "answer",
          label: "answer",
          description: "answer to verify",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction:
          "Determine whether the answer is supported by the provided context. Flag any claim not grounded in it.",
        context: "Context:\n{{context_docs}}",
        examples: null,
        input: "Answer to check:\n{{answer}}",
        output_indicator:
          'Return JSON: {"supported": bool, "unsupported_claims": [..]}. No prose.',
      },
      composable_with: ["rag-grounded-qa"],
      execution_note: null,
    },
    {
      id: "grounded-answer",
      kind: "base",
      source_kind: "core",
      title: "Grounded Answer",
      summary:
        "Answer directly while staying constrained by available evidence.",
      domain: "question-answering",
      technique: "zero-shot",
      recommended_modifiers: ["rag-context", "markdown-output"],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [
        {
          name: "task_description",
          label: "Answering goal",
          description: "What a good answer should optimize for.",
          required: true,
        },
        {
          name: "missing_answer_policy",
          label: "Missing-answer policy",
          description: "What to do when context is insufficient.",
          required: false,
          default: "Say what evidence is missing instead of guessing.",
        },
        {
          name: "answer_style",
          label: "Answer style",
          description: "Preferred response shape.",
          required: false,
          default: "concise prose with clear rationale",
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction:
          "You answer questions using the available instructions and evidence.",
        context:
          "Answering goal:\n{{task_description}}\n\nMissing-answer policy:\n{{missing_answer_policy}}",
        examples: null,
        input:
          "Answer the user's question directly and stay grounded in the supplied evidence.",
        output_indicator: "Use {{answer_style}}.",
      },
      composable_with: [],
      execution_note: null,
    },
    {
      id: "extract-structured-data",
      kind: "base",
      source_kind: "core",
      title: "Extract Structured Data",
      summary:
        "Pull named fields or schema-aligned values from source content.",
      domain: "information-extraction",
      technique: "zero-shot",
      recommended_modifiers: ["json-output"],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [
        {
          name: "task_description",
          label: "Extraction goal",
          description: "What to extract and why.",
          required: true,
          default:
            "Extract the key customer, invoice, and payment fields so a downstream workflow can ingest the record without manual cleanup.",
        },
        {
          name: "schema",
          label: "Target schema",
          description: "Field list or JSON schema.",
          required: true,
          default:
            '{\n  "customer_name": "string | null",\n  "invoice_number": "string | null",\n  "due_date": "string | null",\n  "amount_due": "number | null",\n  "payment_status": "string | null"\n}',
        },
        {
          name: "missing_data_policy",
          label: "Missing-data policy",
          description: "How to handle absent fields.",
          required: false,
          default: "Use null for missing fields and never guess.",
        },
        {
          name: "extraction_output_format",
          label: "Output format",
          description: "How the extracted fields should be returned.",
          required: false,
          default: "only valid JSON matching the target schema",
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction: "You extract structured information from incoming content.",
        context:
          "Extraction goal:\n{{task_description}}\n\nTarget schema:\n{{schema}}\n\nMissing-data policy:\n{{missing_data_policy}}",
        examples: null,
        input: "Populate every field you can justify from the source content.",
        output_indicator: "Return {{extraction_output_format}}.",
      },
      composable_with: [],
      execution_note: null,
    },
    {
      id: "custom-prompt",
      kind: "base",
      source_kind: "system",
      title: "Custom Prompt",
      summary:
        "Start from a freeform prompt and layer optional retrieval, output, or safety modifiers.",
      domain: "freeform",
      technique: "manual",
      recommended_modifiers: ["rag-context", "markdown-output", "json-output"],
      recommended_scorers: ["Correctness", "RelevanceToQuery", "Guidelines"],
      variables: [
        {
          name: "custom_prompt",
          label: "Custom prompt",
          description:
            "Write the full prompt text. You can include runtime placeholders such as {{user_input}} or {{retrieved_docs}}.",
          required: true,
          default:
            "You are a careful assistant.\n\nUse any supplied context when it is available.\nIf the answer is not supported, say what is missing instead of guessing.\n\nUser request:\n{{user_input}}",
        },
      ],
      runtime_variables: [
        {
          name: "user_input",
          label: "User input",
          description: "Primary user request supplied at runtime.",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      sections: {
        instruction: "{{custom_prompt}}",
        context: null,
        examples: null,
        input: null,
        output_indicator: null,
      },
      composable_with: [],
      execution_note: null,
    },
  ],
  modifiers: [
    {
      id: "few-shot-examples",
      kind: "modifier",
      title: "Few-Shot Examples",
      summary:
        "Append concrete examples that demonstrate the desired behavior.",
      domain: "evaluation",
      technique: "few-shot",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness"],
      variables: [
        {
          name: "few_shot_examples",
          label: "Few-shot examples",
          description: "Examples in a readable input/output format.",
          required: true,
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      operations: [
        {
          element: "examples",
          mode: "append",
          content: "Examples:\n{{few_shot_examples}}",
        },
      ],
    },
    {
      id: "rag-context",
      kind: "modifier",
      title: "RAG Context",
      summary: "Ground the answer in retrieved source material.",
      domain: "question-answering",
      technique: "rag",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness"],
      variables: [],
      runtime_variables: [
        {
          name: "retrieved_docs",
          label: "Retrieved docs",
          description: "Runtime-provided source material.",
        },
      ],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      operations: [
        {
          element: "context",
          mode: "append",
          content:
            "When retrieved source material is supplied, treat it as the primary evidence.\n\nRetrieved context:\n{{retrieved_docs}}",
        },
      ],
    },
    {
      id: "format-enforce",
      kind: "modifier",
      title: "Format Enforce",
      summary:
        "Apply an arbitrary output contract beyond the built-in JSON or markdown helpers.",
      domain: "evaluation",
      technique: "zero-shot",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [
        {
          name: "required_format",
          label: "Required format",
          description: "The exact output contract the response must satisfy.",
          required: true,
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: ["json-output", "markdown-output"],
      operations: [
        {
          element: "output_indicator",
          mode: "append",
          content:
            "Respond ONLY in the required format. No preamble, no explanation, and no markdown fences unless the required format explicitly asks for them.\nRequired format:\n{{required_format}}",
        },
      ],
    },
    {
      id: "self-critique",
      kind: "modifier",
      title: "Self-Critique",
      summary: "Add an inline self-review pass before the final answer.",
      domain: "evaluation",
      technique: "reflexion",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness", "Guidelines"],
      variables: [
        {
          name: "critique_focus",
          label: "Critique focus",
          description: "What to check before finalizing.",
          required: false,
          default:
            "errors, unsupported claims, and gaps against the user's task",
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: [],
      operations: [
        {
          element: "output_indicator",
          mode: "append",
          content:
            "Before finalizing, review your draft for {{critique_focus}}. Revise silently, then return only the corrected final answer.",
        },
      ],
    },
    {
      id: "markdown-output",
      kind: "modifier",
      title: "Markdown Output",
      summary: "Force the response into named markdown sections.",
      domain: "evaluation",
      technique: "zero-shot",
      recommended_modifiers: [],
      recommended_scorers: ["Guidelines"],
      variables: [
        {
          name: "markdown_sections",
          label: "Markdown sections",
          description: "Named sections or headings to emit.",
          required: false,
          default: "Summary, Evidence, Final Answer",
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: ["json-output"],
      operations: [
        {
          element: "output_indicator",
          mode: "append",
          content: "Use markdown with these sections:\n{{markdown_sections}}",
        },
      ],
    },
    {
      id: "json-output",
      kind: "modifier",
      title: "JSON Output",
      summary: "Force the response into a strict JSON contract.",
      domain: "evaluation",
      technique: "zero-shot",
      recommended_modifiers: [],
      recommended_scorers: ["Correctness"],
      variables: [
        {
          name: "json_schema",
          label: "JSON schema or shape",
          description: "The structure the output must satisfy.",
          required: true,
        },
      ],
      runtime_variables: [],
      compatible_base_ids: [],
      incompatible_modifier_ids: ["markdown-output"],
      operations: [
        {
          element: "output_indicator",
          mode: "append",
          content:
            "Return only valid JSON matching this schema:\n{{json_schema}}\nNo markdown fences.",
        },
      ],
    },
  ],
  starter_recipes: [
    {
      id: "zs-instruct",
      title: "zs-instruct",
      summary: "Answer concisely.",
      domain: "question-answering",
      technique: "zero-shot",
      support_level: "builder",
      support_reason: "Imported directly from template_library.json.",
      base_template_id: "zs-instruct",
      modifier_ids: [],
      builder_values: {},
      runtime_variables: ["instruction", "input"],
      preview_variables: {
        instruction:
          "Explain the deployment difference between staging and production.",
        input:
          "Staging is where we validate and calibrate a prompt before promoting it to prod.",
      },
      template_override: null,
      suggested_modifier_ids: ["format-enforce"],
      source_label: "Prompting Guide",
      source_url: "https://www.promptingguide.ai/techniques/zeroshot",
      composable_with: ["add-fewshot", "add-cot", "format-enforce"],
      execution_note: null,
    },
    {
      id: "zs-cot-trigger",
      title: "zs-cot-trigger",
      summary: "Let's think step by step.",
      domain: "reasoning",
      technique: "zero-shot-cot",
      support_level: "builder",
      support_reason: "Imported directly from template_library.json.",
      base_template_id: "zs-cot-trigger",
      modifier_ids: [],
      builder_values: {},
      runtime_variables: ["question"],
      preview_variables: {
        question:
          "If a team ships 3 releases per week, how many releases will they ship in 8 weeks?",
      },
      template_override: null,
      suggested_modifier_ids: ["format-enforce"],
      source_label: "Prompting Guide",
      source_url: "https://www.promptingguide.ai/techniques/cot",
      composable_with: ["self-consistency", "format-enforce"],
      execution_note: null,
    },
    {
      id: "rag-grounded-qa",
      title: "rag-grounded-qa",
      summary:
        "Answer the question using ONLY the retrieved context below. If the answer is not in the context, say 'Not found in provided sources.'",
      domain: "question-answering",
      technique: "rag",
      support_level: "builder",
      support_reason: "Imported directly from template_library.json.",
      base_template_id: "rag-grounded-qa",
      modifier_ids: [],
      builder_values: {},
      runtime_variables: ["retrieved_docs", "question"],
      preview_variables: {
        retrieved_docs:
          "[doc_1] Refunds take 5-7 business days after approval.\n[doc_2] Only approved refunds can be expedited.",
        question: "How long do refunds take after approval?",
      },
      template_override: null,
      suggested_modifier_ids: ["self-critique", "format-enforce"],
      source_label: "Prompting Guide",
      source_url: "https://www.promptingguide.ai/techniques/rag",
      composable_with: ["self-critique", "format-enforce"],
      execution_note: null,
    },
    {
      id: "react-tool-loop",
      title: "react-tool-loop",
      summary:
        "Answer the question by interleaving reasoning and tool use. Available tools: {tools}.",
      domain: "reasoning",
      technique: "react",
      support_level: "builder",
      support_reason: "Imported directly from template_library.json.",
      base_template_id: "react-tool-loop",
      modifier_ids: [],
      builder_values: {},
      runtime_variables: ["question", "tools", "scratchpad"],
      preview_variables: {
        question: "Should I refund this order immediately?",
        tools: "lookup_policy(order_id), lookup_customer(account_id)",
        scratchpad: "Thought: I should inspect the refund policy first.",
      },
      template_override: null,
      suggested_modifier_ids: [],
      source_label: "Prompting Guide",
      source_url: "https://www.promptingguide.ai/techniques/react",
      composable_with: ["reflexion-retry"],
      execution_note: null,
    },
    {
      id: "check-hallucination",
      title: "check-hallucination",
      summary:
        "Determine whether the answer is supported by the provided context. Flag any claim not grounded in it.",
      domain: "truthfulness",
      technique: "zero-shot",
      support_level: "builder",
      support_reason: "Imported directly from template_library.json.",
      base_template_id: "check-hallucination",
      modifier_ids: [],
      builder_values: {},
      runtime_variables: ["context_docs", "answer"],
      preview_variables: {
        context_docs:
          "Refund approvals take 5-7 business days. Expedited refunds require approval first.",
        answer: "Refunds always arrive the same day they are requested.",
      },
      template_override: null,
      suggested_modifier_ids: [],
      source_label: "Prompting Guide",
      source_url: "https://www.promptingguide.ai/prompts/truthfulness",
      composable_with: ["rag-grounded-qa"],
      execution_note: null,
    },
  ],
} as const;

function buildPromptTemplatePreview(body: Record<string, unknown>) {
  const baseTemplateId =
    typeof body.base_template_id === "string"
      ? body.base_template_id
      : "zs-instruct";
  const baseTemplate =
    PROMPT_TEMPLATE_LIBRARY.base_templates.find(
      (template) => template.id === baseTemplateId,
    ) ?? PROMPT_TEMPLATE_LIBRARY.base_templates[0];
  const builderValues = asStringRecord(body.builder_values);
  const previewVariables = asStringRecord(body.preview_variables);
  const modifierIds = Array.isArray(body.modifier_ids)
    ? body.modifier_ids.map((value) => String(value))
    : [];
  const customRuntimeVariables = Array.isArray(body.runtime_variables)
    ? body.runtime_variables.map((value) => String(value))
    : [];
  const templateOverride =
    typeof body.template_override === "string"
      ? body.template_override.trim()
      : "";
  const ELEMENT_NAMES = [
    "instruction",
    "context",
    "examples",
    "input",
    "output_indicator",
  ] as const;
  const rawSectionOverrides = asStringRecord(body.section_overrides);
  const sectionOverrides: Partial<Record<(typeof ELEMENT_NAMES)[number], string>> =
    {};
  for (const element of ELEMENT_NAMES) {
    if (Object.prototype.hasOwnProperty.call(rawSectionOverrides, element)) {
      sectionOverrides[element] = rawSectionOverrides[element]!;
    }
  }
  const modifierById = new Map(
    PROMPT_TEMPLATE_LIBRARY.modifiers.map((modifier) => [
      modifier.id,
      modifier,
    ]),
  );

  const selectedModifiers = modifierIds
    .map((modifierId) => modifierById.get(modifierId))
    .filter((modifier): modifier is NonNullable<typeof modifier> =>
      Boolean(modifier),
    );
  const sections = { ...(baseTemplate.sections ?? {}) };
  const builderSpecs = [...baseTemplate.variables];
  const runtimeSpecs = [...baseTemplate.runtime_variables];

  for (const modifier of selectedModifiers) {
    for (const variable of modifier.variables) {
      if (!builderSpecs.some((current) => current.name === variable.name)) {
        builderSpecs.push(variable);
      }
    }
    for (const variable of modifier.runtime_variables) {
      if (!runtimeSpecs.some((current) => current.name === variable.name)) {
        runtimeSpecs.push(variable);
      }
    }
    for (const operation of modifier.operations ?? []) {
      const element = operation.element as
        | "instruction"
        | "context"
        | "examples"
        | "input"
        | "output_indicator";
      const current = (sections[element] ?? "").trim();
      const content = String(operation.content).trim();
      if (!current) {
        sections[element] = content;
      } else if (operation.mode === "prepend") {
        sections[element] = `${content}\n\n${current}`;
      } else if (operation.mode === "replace") {
        sections[element] = content;
      } else {
        sections[element] = `${current}\n\n${content}`;
      }
    }
  }

  // Snapshot composed (post-behavior, pre-override) elements, then apply any
  // per-element overrides wholesale — mirrors the backend.
  const composedSections: Record<string, string> = {};
  for (const element of ELEMENT_NAMES) {
    composedSections[element] = sections[element] ?? "";
  }
  const overriddenSections: string[] = [];
  for (const element of ELEMENT_NAMES) {
    if (Object.prototype.hasOwnProperty.call(sectionOverrides, element)) {
      sections[element] = sectionOverrides[element]!;
      overriddenSections.push(element);
    }
  }

  const defaults = Object.fromEntries(
    builderSpecs
      .filter((spec) => typeof spec.default === "string")
      .map((spec) => [spec.name, String(spec.default)]),
  );
  const compiledValues = { ...defaults, ...builderValues };
  const generated = [
    sections.instruction,
    sections.context,
    sections.examples,
    sections.input,
    sections.output_indicator,
  ]
    .filter((value) => typeof value === "string" && value.trim().length > 0)
    .map((value) =>
      substituteKnownPlaceholders(String(value), compiledValues),
    )
    .join("\n\n");

  const builderVariables = builderSpecs.map((variable) => ({
    ...variable,
    value: builderValues[variable.name] || variable.default || "",
  }));
  const runtimeVariables = [
    ...runtimeSpecs,
    ...customRuntimeVariables
      .filter(
        (name) => !runtimeSpecs.some((variable) => variable.name === name),
      )
      .map((name) => ({
        name,
        label: name,
        description: "Custom runtime placeholder declared in the builder.",
      })),
  ];
  const errors: string[] = [];
  const warnings: string[] = [];
  const missingRequiredBuilderNames = new Set<string>();
  for (const variable of builderSpecs) {
    const currentValue = builderValues[variable.name] || variable.default || "";
    if (variable.required && !String(currentValue).trim()) {
      missingRequiredBuilderNames.add(variable.name);
      errors.push(`Builder field '${variable.name}' is required.`);
    }
  }

  const compiledTemplate = templateOverride || generated;
  const renderedPreview = renderPreviewTemplate(compiledTemplate, previewVariables);
  const detectedVariables = findTemplatePlaceholders(compiledTemplate);
  const unresolvedVariables = findTemplatePlaceholders(renderedPreview.rendered);
  const runtimeVariableNames = new Set(
    runtimeVariables.map((variable) => variable.name),
  );
  const builderVariableNames = new Set(
    builderSpecs.map((variable) => variable.name),
  );
  for (const name of detectedVariables) {
    if (
      builderVariableNames.has(name) &&
      !runtimeVariableNames.has(name) &&
      !missingRequiredBuilderNames.has(name)
    ) {
      errors.push(
        `Builder field '${name}' is still unresolved in the prompt template.`,
      );
    }
  }

  return {
    catalog_version: PROMPT_TEMPLATE_LIBRARY.catalog_version,
    base_template: baseTemplate,
    modifiers: selectedModifiers,
    generated_template: generated,
    compiled_template: compiledTemplate,
    rendered_preview: renderedPreview.rendered,
    composed_sections: composedSections,
    overridden_sections: overriddenSections,
    builder_variables: builderVariables,
    runtime_variables: runtimeVariables,
    detected_variables: detectedVariables,
    unresolved_variables: unresolvedVariables,
    preview_variables_applied: renderedPreview.applied,
    validation_report: {
      valid: errors.length === 0,
      errors,
      warnings,
    },
    word_count: renderedPreview.rendered.split(/\s+/).filter(Boolean).length,
    char_count: renderedPreview.rendered.length,
    recommended_scorers: baseTemplate.recommended_scorers,
  };
}

function substituteKnownPlaceholders(
  template: string,
  values: Record<string, string>,
): string {
  return template.replace(
    /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g,
    (match, name) => values[name] ?? match,
  );
}

function renderPreviewTemplate(
  template: string,
  values: Record<string, string>,
): { rendered: string; applied: Record<string, string> } {
  const applied: Record<string, string> = {};
  const rendered = template.replace(
    /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g,
    (match, name) => {
      if (values[name] == null) {
        return match;
      }
      applied[name] = values[name]!;
      return values[name]!;
    },
  );
  return { rendered, applied };
}

function findTemplatePlaceholders(template: string): string[] {
  return Array.from(
    new Set(
      Array.from(
        template.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g),
      ).map((match) => match[1]!),
    ),
  );
}

function asStringRecord(value: unknown): Record<string, string> {
  if (!value || Array.isArray(value) || typeof value !== "object") {
    return {};
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      String(item),
    ]),
  );
}

const WORKFLOW_BENCHMARK_REPORT_FIXTURES: Array<Record<string, unknown>> = [];

export const handlers = [
  http.get(`${API_BASE}/health`, () => {
    return HttpResponse.json(envelope({ status: "ok", version: "test" }));
  }),

  // The sidebar polls plans for its "needs you" badge on every app render;
  // default to none so full-app tests don't trip onUnhandledRequest.
  http.get(`${API_BASE}/aria/plans`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.get(`${API_BASE}/csrf`, () => {
    return HttpResponse.json(
      envelope({ enabled: false, token: null, ttl_seconds: 0 }),
    );
  }),

  http.get(`${API_BASE}/dashboard/summary`, () => {
    return HttpResponse.json(
      envelope({
        agents_total: 3,
        agents_enabled: 2,
        verification_pending: 5,
        verification_pending_critical: 1,
        jobs_queued: 2,
        jobs_running: 1,
        jobs_awaiting_approval: 3,
        jobs_completed: 10,
        jobs_failed: 0,
        jobs_rejected: 0,
        approvals_pending: 3,
        assistant_slo: {
          intent_confidence_avg: 0.92,
          plans_total: 12,
          plans_ready: 11,
          plan_readiness_rate: 0.92,
          clarification_rate: 0.08,
          executions_total: 12,
          executions_completed: 11,
          executions_failed: 1,
          executions_blocked: 0,
          execution_success_rate: 0.92,
          adapter_error_classes: {},
          publish_total: 4,
          publish_success: 4,
          publish_failed: 0,
          publish_success_rate: 1,
        },
        generated_at: new Date().toISOString(),
      }),
    );
  }),

  http.get(`${API_BASE}/me`, () => {
    return HttpResponse.json(
      envelope({
        user_id: "@local-admin",
        scopes: [
          "caliber.admin",
          "caliber.approver",
          "caliber.operator",
          "caliber.viewer",
        ],
        is_admin: true,
      }),
    );
  }),

  http.get(`${API_BASE}/settings/llm`, () => {
    return HttpResponse.json(
      envelope({
        llm_provider: "openai",
        gateway_url: "http://127.0.0.1:5000/gateway/mlflow/v1",
        openai_key_env: "OPENAI_API_KEY",
        openai_key_present: true,
        anthropic_key_present: false,
        assistant_engine: "openai",
        // Presence + masked tail only; the API never returns resolved keys.
        openai_key_fingerprint: "••••7f3a",
        anthropic_key_fingerprint: "",
      }),
    );
  }),

  http.patch(`${API_BASE}/settings/llm`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(
      envelope({
        llm_provider: "openai",
        gateway_url:
          typeof body.gateway_url === "string"
            ? body.gateway_url
            : "http://127.0.0.1:5000/gateway/mlflow/v1",
        openai_key_env: "OPENAI_API_KEY",
        openai_key_present: typeof body.openai_api_key === "string" || true,
        anthropic_key_present: typeof body.anthropic_api_key === "string",
        assistant_engine: "openai",
        openai_key_fingerprint: "••••7f3a",
        anthropic_key_fingerprint:
          typeof body.anthropic_api_key === "string" ? "••••test" : "",
      }),
    );
  }),

  http.get(`${API_BASE}/settings/runtime`, () => {
    return HttpResponse.json(
      envelope({
        summary: {
          total: 8,
          live_editable: 2,
          environment_managed: 6,
          configured: 3,
          defaults: 5,
          secret_sources: 1,
        },
        groups: [
          {
            id: "assistant",
            title: "Assistant Runtime",
            description: "Assistant availability, model routing, and limits.",
            configured_count: 1,
            live_editable_count: 2,
            settings: [
              {
                key: "assistant_model",
                env_var: "CALIBER_ASSISTANT_MODEL",
                label: "Assistant model",
                description:
                  "Initial model for provider-backed assistant engines.",
                display_value: "gpt-4o-mini",
                value_type: "string",
                source: "default",
                control: "live",
                restart_required: false,
                sensitive: false,
              },
              {
                key: "assistant_reasoning",
                env_var: "CALIBER_ASSISTANT_REASONING",
                label: "Reasoning effort",
                description:
                  "Optional reasoning setting for OpenAI assistant engines.",
                display_value: "medium",
                value_type: "string",
                source: "configured",
                control: "live",
                restart_required: false,
                sensitive: false,
              },
            ],
          },
          {
            id: "storage",
            title: "Files & Storage",
            description: "Local and S3-compatible storage controls.",
            configured_count: 2,
            live_editable_count: 0,
            settings: [
              {
                key: "workflow_storage.backend",
                env_var: "CALIBER_WORKFLOW_STORAGE_BACKEND",
                label: "Default backend",
                description: "Default backend for file directories.",
                display_value: "local",
                value_type: "string",
                source: "default",
                control: "environment",
                restart_required: true,
                sensitive: false,
              },
              {
                key: "workflow_storage.access_key_source",
                env_var: "CALIBER_WORKFLOW_STORAGE_ACCESS_KEY_SOURCE",
                label: "Access key source",
                description: "Secret source for S3 access key material.",
                display_value: "MINIO_ROOT_USER",
                value_type: "string",
                source: "configured",
                control: "environment",
                restart_required: true,
                sensitive: true,
              },
            ],
          },
        ],
      }),
    );
  }),

  http.get(`${API_BASE}/agents`, () => {
    return HttpResponse.json(
      envelope([
        {
          agent_id: "support-agent",
          experiment_id: "exp-support",
          name: "Support Agent",
          owner: "@sarah",
          artifact_types: ["prompt"],
          eval_thresholds: {},
          optimizer_config: {},
          approval_policy: {},
          optimize_for: "quality",
          collaboration_mode: null,
          enabled: true,
          required_approvals: 1,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/projects/storage`, () => {
    return HttpResponse.json(
      envelope({
        backend: "local",
        backend_label: "Local file system",
        available_backends: [
          {
            id: "local",
            label: "Local file system",
            active: true,
            configured: true,
            reason: null,
          },
          {
            id: "s3",
            label: "MinIO / S3-compatible object storage",
            active: false,
            configured: true,
            reason: null,
          },
        ],
        base_uri: "file://./caliber-workspaces",
        bucket: "caliber-workspaces",
        prefix: "",
        public_endpoint_url: "http://localhost:9000",
      }),
    );
  }),

  http.get(`${API_BASE}/projects`, () => {
    return HttpResponse.json(
      envelope([
        {
          project_id: "PRJ-001",
          name: "Default Workspace",
          description: "Shared workspace for default UI smoke tests.",
          owner: "@sarah",
          status: "active",
          storage_backend: "local",
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
          file_count: 0,
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/readiness`, () => {
    return HttpResponse.json(
      envelope({
        providers: {
          llm: "openai",
          eval: "openai",
          promoter: "openai",
          artifact_store: "mlflow",
        },
        simulated: [],
        all_real: true,
        tracing_enabled: true,
        tracing_autolog_enabled: true,
        workflow_llm_judge_enabled: true,
      }),
    );
  }),

  http.get(`${API_BASE}/capabilities`, () => {
    return HttpResponse.json(
      envelope({
        workflow_runs: {
          queue_enabled: true,
          supports_async_submit: true,
          supports_cancel: true,
          supports_retry: true,
          supports_resume: true,
          runtime_approvals_enabled: true,
          checkpointing_enabled: true,
          event_backend: "memory",
        },
        sync_workflow_version_run: true,
      }),
    );
  }),

  http.get(`${API_BASE}/workflow-components`, () => {
    return HttpResponse.json(
      envelope({
        schema_version: 1,
        components: [
          {
            type: "start",
            label: "Start",
            category: "Inputs & Outputs",
            description: "Entry point of the flow.",
            docs: [
              "Use event or cron triggers when runs should start automatically.",
            ],
            default_inputs: {},
            default_outputs: {
              output: {
                type: "string",
                description: "Initial workflow input.",
              },
            },
            fields: [
              {
                key: "trigger",
                label: "Trigger",
                type: "Start Trigger Config | null",
                required: false,
                default: null,
                description:
                  "Choose whether this workflow starts manually, from an event, or on a cron schedule.",
                constraints: { nullable: true },
                examples: [],
              },
            ],
          },
          {
            type: "agent",
            label: "Agent",
            category: "Agents",
            description:
              "LLM-powered reasoning step with tools, skills, and handoffs.",
            docs: [
              "Agents can use inline instructions or prompt refs and may call tools.",
            ],
            default_inputs: {},
            default_outputs: {
              text: { type: "string" },
              result: { type: "structured" },
            },
            fields: [
              {
                key: "model",
                label: "Model",
                type: "string",
                required: true,
                default: "gpt-4.1-mini",
                description: "LLM model reference used by this agent.",
                constraints: {},
                examples: ["gpt-4.1-mini"],
              },
              {
                key: "instructions",
                label: "Instructions",
                type: "Inline Instructions | Prompt Ref Instructions",
                required: true,
                default: {
                  type: "inline",
                  text: "You are a helpful assistant.",
                },
                description:
                  "Inline instructions or a registered prompt reference for this agent.",
                constraints: {},
                examples: [],
              },
            ],
          },
          {
            type: "knowledge_query",
            label: "Knowledge Query",
            category: "Integrations",
            description:
              "Query a knowledge base with dense, hybrid, or AGE-backed retrieval.",
            docs: [
              "Supports GraphRAG hybrid and Apache AGE graph retrieval with query-time overrides.",
            ],
            default_inputs: {
              question: { type: "string" },
              history: { type: "structured" },
              retrieval_modes: { type: "structured" },
              version_ids: { type: "structured" },
              graph_overrides: { type: "structured" },
            },
            default_outputs: {
              text: { type: "string" },
              answer: { type: "string" },
              result: { type: "structured" },
              citations: { type: "structured" },
              chunks: { type: "structured" },
              graph_context: { type: "structured" },
            },
            fields: [
              {
                key: "knowledge_base_id",
                label: "Knowledge Base ID",
                type: "string",
                required: false,
                default: "",
                description: "Knowledge base queried by this node.",
                constraints: {},
                examples: ["KB-ops"],
              },
              {
                key: "retrieval_modes",
                label: "Retrieval Modes",
                type: "list<enum>",
                required: true,
                default: ["dense"],
                description:
                  "Retrieval strategies applied when querying the knowledge base.",
                constraints: {},
                examples: [],
              },
              {
                key: "graph_overrides",
                label: "Graph Overrides",
                type: "Knowledge Query Graph Overrides | null",
                required: false,
                default: null,
                description:
                  "Optional query-time GraphRAG and AGE retrieval settings.",
                constraints: { nullable: true },
                examples: [],
              },
            ],
          },
          {
            type: "loop",
            label: "Loop",
            category: "Orchestration",
            description:
              "Repeat one executable target until a stop condition matches or the bound is reached.",
            docs: [
              "Stop conditions can reference iteration, state, output, result, and outputs.",
            ],
            default_inputs: {
              input: { type: "string" },
              state: { type: "structured" },
            },
            default_outputs: {
              output: { type: "string" },
              result: { type: "structured" },
              iterations: { type: "structured" },
              metadata: { type: "structured" },
            },
            fields: [
              {
                key: "target_node_id",
                label: "Target Node",
                type: "string | null",
                required: false,
                default: null,
                description: "Executable node repeated by this loop.",
                constraints: { nullable: true },
                examples: ["python", "agent"],
              },
              {
                key: "max_iterations",
                label: "Max Iterations",
                type: "integer",
                required: true,
                default: 10,
                description:
                  "Upper bound on loop repetitions to keep runs bounded.",
                constraints: { minimum: 1, maximum: 10000 },
                examples: [5],
              },
              {
                key: "stop_condition",
                label: "Stop Condition",
                type: "string",
                required: false,
                default: "",
                description: "Safe expression evaluated after each iteration.",
                constraints: {},
                examples: ["state.done or iteration >= 3"],
              },
            ],
            setup_checks: [
              {
                label: "Select a loop target",
                help: "Choose the executable node this loop should repeat.",
                kind: "non_empty_string",
                field: "target_node_id",
              },
              {
                label: "Choose an executable loop target",
                help: "The selected loop target should point to an executable node in this workflow.",
                kind: "target_node_executable_if_set",
                field: "target_node_id",
              },
            ],
          },
          {
            type: "tool",
            label: "Tool",
            category: "Integrations",
            description:
              "Invoke a registered workflow tool binding directly from the runtime.",
            docs: [
              "Use this when the flow should invoke a capability deterministically without an LLM deciding whether to call it.",
            ],
            default_inputs: {
              input: { type: "string" },
              arguments: { type: "structured" },
            },
            default_outputs: {
              text: { type: "string" },
              result: { type: "structured" },
              metadata: { type: "structured" },
            },
            fields: [
              {
                key: "tool_name",
                label: "Tool Name",
                type: "string",
                required: true,
                default: "",
                description:
                  "Local manifest tool binding invoked by this node.",
                constraints: {},
                examples: ["lookup_policy", "mcp:Docs/search_docs"],
              },
            ],
            setup_checks: [
              {
                label: "Select a tool binding",
                help: "Choose the manifest tool binding this node should invoke directly.",
                kind: "non_empty_string",
                field: "tool_name",
              },
            ],
          },
          {
            type: "template",
            label: "Template",
            category: "Utilities",
            description:
              "Render a no-code prompt or JSON payload from workflow inputs.",
            docs: [
              "Use placeholders like {{input}} and {{variables.customer.name}} without dropping into Python.",
            ],
            default_inputs: {
              input: { type: "string" },
              variables: { type: "structured" },
            },
            default_outputs: {
              text: { type: "string" },
              result: { type: "structured" },
              metadata: { type: "structured" },
            },
            fields: [
              {
                key: "template",
                label: "Template",
                type: "string",
                required: true,
                default: "{{input}}",
                description:
                  "Text or JSON template rendered from the current workflow inputs.",
                constraints: {},
                examples: ["Hello {{input}}"],
              },
              {
                key: "output_format",
                label: "Output Format",
                type: "enum",
                required: false,
                default: "text",
                description:
                  "Whether the rendered template stays text or is parsed as JSON.",
                constraints: {},
                examples: ["text", "json"],
              },
              {
                key: "missing_variable_mode",
                label: "Missing Variable Mode",
                type: "enum",
                required: false,
                default: "preserve",
                description:
                  "Controls what happens when the template references a missing variable.",
                constraints: {},
                examples: ["preserve", "empty", "error"],
              },
            ],
          },
        ],
      }),
    );
  }),

  http.get(`${API_BASE}/workflow-templates`, () => {
    return HttpResponse.json(
      envelope({
        schema_version: 1,
        templates: WORKFLOW_TEMPLATE_FIXTURES.map((item) => ({
          ...item,
          manifest_template: templateManifest(
            item.kind,
            WORKFLOW_TEMPLATE_ID_MARKER,
            WORKFLOW_TEMPLATE_NAME_MARKER,
          ),
        })),
        bakeoff_scenarios: [...WORKFLOW_BAKEOFF_SCENARIOS],
        operator_rubric: [...WORKFLOW_BAKEOFF_RUBRIC],
      }),
    );
  }),

  http.get(`${API_BASE}/workflow-benchmark-reports`, () => {
    return HttpResponse.json(envelope([...WORKFLOW_BENCHMARK_REPORT_FIXTURES]));
  }),

  http.get(`${API_BASE}/knowledge-bases/options`, () => {
    return HttpResponse.json(
      envelope({
        chunking_strategies: [],
        embedding_models: [],
        retrieval_modes: [
          {
            id: "dense",
            name: "Dense retrieval",
            description: "Vector search",
            defaults: {},
            tags: [],
          },
          {
            id: "graph_hybrid",
            name: "Graph hybrid",
            description: "Graph + vector",
            defaults: {},
            tags: [],
          },
          {
            id: "age_graph",
            name: "Apache AGE",
            description: "AGE traversal",
            defaults: {},
            tags: [],
          },
        ],
        graph_extractors: [],
        graph_output_targets: [],
        graph_retrieval_strengths: [],
        graph_age_seed_modes: [],
        graph_entity_types: [],
        graph_query_presets: [
          {
            id: "hybrid_balanced",
            label: "Balanced GraphRAG",
            eyebrow: "Portable",
            description:
              "Blend dense recall with graph-aware evidence expansion.",
            badges: ["Local graph", "1-hop", "Balanced"],
            retrieval_mode: "graph_hybrid",
            patch: {
              retrieval_strength: "balanced",
              minimum_relationship_weight: 1,
              age_traversal_hops: 1,
            },
            recommended: false,
            age_required: false,
          },
          {
            id: "age_native",
            label: "AGE-native retrieval",
            eyebrow: "Graph-first",
            description: "Use Apache AGE as the primary retrieval path.",
            badges: ["AGE primary", "2-hop", "Graph-first"],
            retrieval_mode: "age_graph",
            patch: {
              retrieval_strength: "aggressive",
              minimum_relationship_weight: 1,
              age_seed_mode: "query_entities_and_text",
              age_traversal_hops: 2,
              age_candidate_pool_size: 40,
              age_dense_rerank_weight: 0.2,
            },
            recommended: true,
            age_required: true,
          },
        ],
        default_graph_config: {
          extractor_backend: "heuristic",
          spacy_model: null,
          max_entities_per_chunk: 12,
          entity_types: [],
          minimum_entity_mentions: 1,
          minimum_relationship_weight: 1,
          default_retrieval_mode: "graph_hybrid",
          retrieval_strength: "balanced",
          output_target: "object_store",
          age_seed_mode: "entity_then_text",
          age_traversal_hops: 1,
          age_candidate_pool_size: 24,
          age_dense_rerank_weight: 0.35,
        },
        age_enabled: true,
        age_graph_name: "knowledge_graph",
        age_unavailable_reason: null,
        reserved_output_prefix: "knowledge/",
      }),
    );
  }),

  http.get(`${API_BASE}/knowledge-bases`, () => {
    return HttpResponse.json(
      envelope([
        {
          knowledge_base_id: "KB-1",
          project_id: null,
          visibility: "user",
          name: "Contracts KB",
          description: "",
          owner: "@test",
          status: "active",
          source_bucket: "docs",
          source_manifest: [],
          source_fingerprint: "fp-1",
          active_version_id: "KBV-1",
          last_run_id: null,
          last_run_status: null,
          last_run_completed_at: null,
          active_version_summary: null,
          created_at: "2026-06-10T00:00:00Z",
          updated_at: "2026-06-10T00:00:00Z",
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/skills`, () => {
    return HttpResponse.json(
      envelope([
        {
          skill_id: "sk-001",
          name: "reasoning-v1",
          description: "Chain-of-thought reasoning rubric",
          summary: "Chain-of-thought reasoning rubric",
          content: "Think step by step.",
          owner: "@sarah",
          category: "custom",
          tags: ["reasoning"],
          skill_metadata: {},
          allowed_tools: null,
          depends_on: [],
          status: "active",
          version: 1,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
        },
      ]),
    );
  }),

  // ── Durable skill-test runs + per-skill Workspace ("pytest for skills"). The
  // ``/skills/test-runs`` literals are registered before ``/skills/:skillId`` so
  // they aren't captured as a skill whose id is "test-runs".
  http.get(`${API_BASE}/skills/test-runs`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.post(`${API_BASE}/skills/test-runs`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const results = Array.isArray(body.results) ? body.results : [];
    return HttpResponse.json(
      envelope({
        test_run_id: "STR-default",
        skill_id: typeof body.skill_id === "string" ? body.skill_id : "",
        skill_version: (body.skill_version as number | null) ?? null,
        kind: typeof body.kind === "string" ? body.kind : "scenario",
        test_set_size: results.length,
        passed_count: 0,
        failed_count: 0,
        partial_count: 0,
        overall_score: null,
        host_agent_id: null,
        trace_id: null,
        mlflow_run_id: null,
        created_by: "@test",
        status: "completed",
        created_at: "2025-01-01T00:00:00Z",
        completed_at: "2025-01-01T00:00:00Z",
      }),
      { status: 201 },
    );
  }),

  http.get(`${API_BASE}/skills/test-runs/:testRunId`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        test_run_id: String(params.testRunId),
        skill_id: "sk-001",
        skill_version: 1,
        kind: "selection",
        test_set_size: 0,
        passed_count: 0,
        failed_count: 0,
        partial_count: 0,
        overall_score: null,
        host_agent_id: null,
        trace_id: null,
        mlflow_run_id: null,
        created_by: "@test",
        status: "completed",
        created_at: "2025-01-01T00:00:00Z",
        completed_at: "2025-01-01T00:00:00Z",
        results: [],
      }),
    );
  }),

  // Per-skill Workspace header summary (registered before ``/skills/:skillId``).
  http.get(`${API_BASE}/skills/:skillId/workspace`, () => {
    return HttpResponse.json(
      envelope({
        version: 1,
        category: "custom",
        status: "active",
        lifecycle: "Tested",
        last_run: null,
        baseline_run_id: null,
        baseline_run: null,
        bound_to: null,
      }),
    );
  }),

  // Single-skill fetch backing the Workspace detail (registered after the
  // ``/skills/test-runs`` and ``/skills/:skillId/workspace`` literals so they
  // win, but before any catch-all).
  http.get(`${API_BASE}/skills/:skillId`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        skill_id: String(params.skillId),
        name: "reasoning-v1",
        description: "Chain-of-thought reasoning rubric",
        summary: "Chain-of-thought reasoning rubric",
        content: "Think step by step.",
        owner: "@sarah",
        category: "custom",
        tags: ["reasoning"],
        skill_metadata: {},
        allowed_tools: null,
        depends_on: [],
        status: "active",
        version: 1,
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-02T00:00:00Z",
      }),
    );
  }),

  http.post(`${API_BASE}/skills/:skillId/test-selection`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    const message = typeof body.user_message === "string" ? body.user_message : "";
    return HttpResponse.json(
      envelope({
        skill_id: "sk-001",
        skill_name: "reasoning-v1",
        is_selected: true,
        selection_score: 0.8,
        selection_reason: `Matched signals for: ${message}`,
      }),
    );
  }),

  http.post(`${API_BASE}/skills/:skillId/baseline`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    return HttpResponse.json(
      envelope({ baseline_run_id: String(body.test_run_id ?? "STR-default") }),
    );
  }),

  http.post(`${API_BASE}/skills/:skillId/bind`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<string, unknown>;
    return HttpResponse.json(envelope({ bound_to: body, status: "Bound" }));
  }),

  http.post(`${API_BASE}/skills/:skillId/calibrate`, () => {
    return HttpResponse.json(
      envelope({
        item: { item_id: "VI-skill" },
        job: { job_id: "JOB-skill" },
      }),
      { status: 201 },
    );
  }),

  http.get(`${API_BASE}/tools`, () => {
    return HttpResponse.json(
      envelope([
        {
          tool_id: "tool-001",
          name: "search_docs",
          version: "1.0.0",
          description: "Search internal docs",
          module_path: "tools.docs",
          callable_name: "search_docs",
          input_schema: { type: "object", properties: {} },
          output_schema: { type: "object", properties: {} },
          side_effect_level: "read",
          requires_approval: false,
          allow_in_preview: true,
          secret_refs: [],
          test_cases: [],
          last_calibration: null,
          owner: "@sarah",
          status: "active",
          deprecated_at: null,
          successor_tool_id: null,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/tools/:toolId/source`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        module_path: "tools.docs",
        callable_name: String(params.toolId ?? "search_docs"),
        available: true,
        signature: "search_docs(query: str) -> dict",
        doc: "Searches indexed documents.",
        source:
          "def search_docs(query: str) -> dict:\n    return {'query': query, 'results': []}\n",
        error: null,
      }),
    );
  }),

  // Durable tool-test runs + per-tool Workspace. The ``/tools/test-runs``
  // literals are registered before ``/tools/:toolId`` so they aren't captured
  // as a tool whose id is "test-runs".
  http.get(`${API_BASE}/tools/test-runs`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.post(`${API_BASE}/tools/test-runs`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    const results = Array.isArray(body.results) ? body.results : [];
    return HttpResponse.json(
      envelope({
        test_run_id: "TTR-default",
        tool_id: typeof body.tool_id === "string" ? body.tool_id : "",
        tool_version: (body.tool_version as string | null) ?? null,
        kind: typeof body.kind === "string" ? body.kind : "suite",
        test_set_size: results.length,
        passed_count: 0,
        failed_count: 0,
        partial_count: 0,
        overall_score: null,
        trace_id: null,
        mlflow_run_id: null,
        created_by: "@test",
        status: "completed",
        created_at: "2025-01-01T00:00:00Z",
        completed_at: "2025-01-01T00:00:00Z",
      }),
      { status: 201 },
    );
  }),

  http.get(`${API_BASE}/tools/test-runs/:testRunId`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        test_run_id: String(params.testRunId),
        tool_id: "tool-001",
        tool_version: "1.0.0",
        kind: "sandbox",
        test_set_size: 0,
        passed_count: 0,
        failed_count: 0,
        partial_count: 0,
        overall_score: null,
        trace_id: null,
        mlflow_run_id: null,
        created_by: "@test",
        status: "completed",
        created_at: "2025-01-01T00:00:00Z",
        completed_at: "2025-01-01T00:00:00Z",
        results: [],
      }),
    );
  }),

  // Per-tool Workspace header summary (registered before ``/tools/:toolId``).
  http.get(`${API_BASE}/tools/:toolId/workspace`, () => {
    return HttpResponse.json(
      envelope({
        version: "1.0.0",
        side_effect_level: "read",
        status: "active",
        lifecycle: "Tested",
        last_run: null,
        baseline_run_id: null,
        baseline_run: null,
        has_fixtures: false,
        last_calibration_score: null,
      }),
    );
  }),

  http.post(`${API_BASE}/tools/:toolId/baseline`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({
        baseline_run_id:
          typeof body.test_run_id === "string" ? body.test_run_id : "",
      }),
    );
  }),

  http.post(`${API_BASE}/tools/:toolId/test-run`, async ({ params, request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        output: { input: body.input ?? {} },
        mocked: false,
        duration_ms: 3,
        error: null,
      }),
    );
  }),

  http.put(`${API_BASE}/tools/:toolId/test-cases`, async ({ params, request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        test_cases: Array.isArray(body.test_cases) ? body.test_cases : [],
      }),
    );
  }),

  http.post(`${API_BASE}/tools/:toolId/calibrate`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        pass_rate: 1,
        total: 0,
        passed: 0,
        cases: [],
        ran_at: "2025-01-01T00:00:00Z",
      }),
    );
  }),

  http.get(`${API_BASE}/tools/:toolId/usage`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        name: "search_docs",
        usage: [],
      }),
    );
  }),

  http.patch(`${API_BASE}/tools/:toolId`, async ({ params, request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        name: "search_docs",
        version: "1.0.0",
        description: "Search internal docs",
        module_path: "tools.docs",
        callable_name: "search_docs",
        input_schema: { type: "object", properties: {} },
        output_schema: { type: "object", properties: {} },
        side_effect_level: "read",
        requires_approval: false,
        allow_in_preview: true,
        secret_refs: [],
        test_cases: [],
        last_calibration: null,
        owner: "@sarah",
        status: typeof body.status === "string" ? body.status : "active",
        deprecated_at: null,
        successor_tool_id: null,
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-02T00:00:00Z",
      }),
    );
  }),

  http.post(`${API_BASE}/tools/:toolId/archive`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        name: "search_docs",
        version: "1.0.0",
        description: "Search internal docs",
        module_path: "tools.docs",
        callable_name: "search_docs",
        input_schema: { type: "object", properties: {} },
        output_schema: { type: "object", properties: {} },
        side_effect_level: "read",
        requires_approval: false,
        allow_in_preview: true,
        secret_refs: [],
        test_cases: [],
        last_calibration: null,
        owner: "@sarah",
        status: "archived",
        deprecated_at: null,
        successor_tool_id: null,
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-02T00:00:00Z",
      }),
    );
  }),

  http.get(`${API_BASE}/tools/:toolId`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        tool_id: String(params.toolId ?? "tool-001"),
        name: "search_docs",
        version: "1.0.0",
        description: "Search internal docs",
        module_path: "tools.docs",
        callable_name: "search_docs",
        input_schema: { type: "object", properties: {} },
        output_schema: { type: "object", properties: {} },
        side_effect_level: "read",
        requires_approval: false,
        allow_in_preview: true,
        secret_refs: [],
        test_cases: [],
        last_calibration: null,
        owner: "@sarah",
        status: "active",
        deprecated_at: null,
        successor_tool_id: null,
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-02T00:00:00Z",
      }),
    );
  }),

  http.get(`${API_BASE}/mcp-servers`, () => {
    return HttpResponse.json(
      envelope([
        {
          server_id: "mcp-001",
          name: "GitHub",
          description: "GitHub MCP server",
          transport: "stdio",
          uri: "",
          command: "npx",
          args: ["-y", "@modelcontextprotocol/server-github"],
          env: {},
          headers: {},
          auth_type: "none",
          auth_config: {},
          tool_policies: {},
          tool_test_cases: {},
          tool_calibrations: {},
          icon: "github",
          owner: "@sarah",
          status: "active",
          connection_error: null,
          discovered_tools: [],
          last_connected_at: null,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/mcp-servers/:serverId/tools`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        server_id: String(params.serverId ?? "mcp-001"),
        tools: [],
      }),
    );
  }),

  http.get(`${API_BASE}/object-store/status`, () => {
    return HttpResponse.json(
      envelope({
        connected: true,
        endpoint: "http://localhost:9000",
        bucket_count: 1,
      }),
    );
  }),

  http.get(`${API_BASE}/object-store/buckets`, () => {
    return HttpResponse.json(
      envelope([
        {
          name: "documents",
          creation_date: "2025-01-01T00:00:00Z",
        },
      ]),
    );
  }),

  http.get(
    `${API_BASE}/object-store/buckets/:bucket/objects`,
    ({ params, request }) => {
      const url = new URL(request.url);
      return HttpResponse.json(
        envelope({
          bucket: String(params.bucket ?? "documents"),
          prefix: url.searchParams.get("prefix") ?? "",
          prefixes: [],
          objects: [],
          next_token: null,
          is_truncated: false,
        }),
      );
    },
  ),

  http.get(
    `${API_BASE}/object-store/buckets/:bucket/object/preview`,
    ({ params, request }) => {
      const url = new URL(request.url);
      const key = url.searchParams.get("key") ?? "sample.txt";
      return HttpResponse.json(
        envelope({
          bucket: String(params.bucket ?? "documents"),
          key,
          size: 0,
          created_at: "2025-01-01T00:00:00Z",
          last_modified: "2025-01-01T00:00:00Z",
          etag: "etag-sample",
          content_type: "text/plain",
          preview_bytes: 0,
          truncated: false,
          is_text: true,
          text: "",
        }),
      );
    },
  ),

  http.post(
    `${API_BASE}/object-store/buckets/:bucket/objects/delete`,
    async ({ request }) => {
      const body = (await request.json().catch(() => ({}))) as {
        keys?: unknown[];
        prefixes?: unknown[];
      };
      const keys = Array.isArray(body.keys) ? body.keys.length : 0;
      const prefixes = Array.isArray(body.prefixes) ? body.prefixes.length : 0;
      return HttpResponse.json(
        envelope({
          deleted: keys + prefixes,
          errors: [],
        }),
      );
    },
  ),

  http.get(`${API_BASE}/prompts`, () => {
    return HttpResponse.json(
      envelope([
        {
          agent_id: "support-agent",
          agent_name: "Support Agent",
          agent_enabled: true,
          prompt_name: "support-agent",
          version: 3,
          alias: "prod",
          available_aliases: ["prod", "staging"],
          template_preview: "You are a helpful support assistant.",
          template_length: 36,
          approval_id: "apr-abc12345",
          artifact_ref: "prompts:/support-agent@prod",
          has_prompt: true,
          needs_prompt: false,
          source: "both",
        },
        {
          // A pure promptless-agent placeholder: a registered Caliber agent that
          // has no prompt authored yet. The backend gives these a null
          // ``prompt_name`` so the FE keeps them out of the testable set
          // (playground / calibration) while still listing them in the backlog.
          agent_id: "billing-agent",
          agent_name: "Billing Agent",
          agent_enabled: true,
          prompt_name: null,
          version: null,
          alias: "prod",
          available_aliases: [],
          template_preview: "",
          template_length: 0,
          approval_id: null,
          artifact_ref: null,
          has_prompt: false,
          needs_prompt: true,
          source: "caliber",
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/prompts/template-library`, () => {
    return HttpResponse.json(envelope(PROMPT_TEMPLATE_LIBRARY));
  }),

  http.post(
    `${API_BASE}/prompts/template-library/preview`,
    async ({ request }) => {
      const body = (await request.json().catch(() => ({}))) as Record<
        string,
        unknown
      >;
      return HttpResponse.json(envelope(buildPromptTemplatePreview(body)));
    },
  ),

  http.post(`${API_BASE}/prompts`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    const name = typeof body.name === "string" ? body.name : "new-prompt";
    const template = typeof body.template === "string" ? body.template : "";
    const targetAlias =
      typeof body.target_alias === "string" ? body.target_alias : "staging";
    return HttpResponse.json(
      envelope({
        name,
        version: 1,
        uri: `prompts:/${name}/1`,
        template_preview: template.slice(0, 200),
        template_length: template.length,
        alias_changed: true,
        active_alias: targetAlias,
      }),
      { status: 201 },
    );
  }),

  // Durable ad-hoc prompt-test runs. Registered before ``/prompts/:name`` so
  // ``/prompts/test-runs`` isn't captured as a prompt named "test-runs".
  http.get(`${API_BASE}/prompts/test-runs`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.post(`${API_BASE}/prompts/test-runs`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    const results = Array.isArray(body.results) ? body.results : [];
    return HttpResponse.json(
      envelope({
        test_run_id: "PTR-default",
        agent_id: typeof body.agent_id === "string" ? body.agent_id : "",
        prompt_name: typeof body.prompt_name === "string" ? body.prompt_name : "",
        prompt_alias: (body.prompt_alias as string | null) ?? null,
        prompt_version: (body.prompt_version as number | null) ?? null,
        model: (body.model as string | null) ?? null,
        eval_dataset_id: (body.eval_dataset_id as string | null) ?? null,
        test_set_size: results.length,
        passed_count: 0,
        failed_count: 0,
        partial_count: 0,
        overall_score: null,
        trace_id: null,
        mlflow_run_id: null,
        created_by: "@test",
        status: "completed",
        created_at: "2025-01-01T00:00:00Z",
        completed_at: "2025-01-01T00:00:00Z",
      }),
      { status: 201 },
    );
  }),

  http.get(`${API_BASE}/prompts/test-runs/:testRunId`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        test_run_id: String(params.testRunId),
        agent_id: "support-agent",
        prompt_name: "support-agent",
        prompt_alias: "prod",
        prompt_version: 1,
        model: "gpt-4o-mini",
        eval_dataset_id: null,
        test_set_size: 0,
        passed_count: 0,
        failed_count: 0,
        partial_count: 0,
        overall_score: null,
        trace_id: null,
        mlflow_run_id: null,
        created_by: "@test",
        status: "completed",
        created_at: "2025-01-01T00:00:00Z",
        completed_at: "2025-01-01T00:00:00Z",
        results: [],
      }),
    );
  }),

  // Per-prompt Workspace header summary. Registered before ``/prompts/:name``
  // so ``/prompts/{name}/workspace`` resolves here rather than being read as a
  // prompt named "{name}" with a stray path segment.
  http.get(`${API_BASE}/prompts/:name/workspace`, ({ params }) => {
    const name = String(params.name);
    return HttpResponse.json(
      envelope({
        model: "gpt-4o-mini",
        version: 3,
        status: "Tested",
        bound_to: null,
        dataset_id: null,
        last_run: {
          test_run_id: `PTR-${name}`,
          overall_score: 0.82,
          test_set_size: 5,
          passed_count: 4,
          failed_count: 1,
          partial_count: 0,
          created_at: "2025-01-01T00:00:00Z",
        },
        baseline_run_id: null,
        baseline_run: null,
      }),
    );
  }),

  // Pin a run as the comparison baseline (Phase 4 Runs tab).
  http.post(`${API_BASE}/prompts/:name/baseline`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({
        baseline_run_id:
          typeof body.test_run_id === "string" ? body.test_run_id : "",
      }),
    );
  }),

  // Bind the prompt target to an agent / workflow node / standalone (Phase 4).
  http.post(`${API_BASE}/prompts/:name/bind`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({ bound_to: body, status: "Bound" }),
    );
  }),

  http.get(`${API_BASE}/prompts/:name`, ({ params, request }) => {
    const name = String(params.name);
    const alias = new URL(request.url).searchParams.get("alias") ?? "prod";
    const template =
      alias === "staging"
        ? `FULL STAGING TEMPLATE for ${name}. Ask clarifying questions before policy decisions.`
        : `FULL PROD TEMPLATE for ${name}. Ask clarifying questions before policy decisions.`;
    return HttpResponse.json(
      envelope({
        name,
        version: alias === "staging" ? 4 : 3,
        alias,
        template,
        template_length: template.length,
        artifact_ref: `prompts:/${name}@${alias}`,
      }),
    );
  }),

  http.get(`${API_BASE}/prompts/:name/versions`, ({ params }) => {
    const name = String(params.name);
    return HttpResponse.json(
      envelope([
        {
          name,
          version: 4,
          aliases: ["staging"],
          creation_timestamp: "2025-01-04T00:00:00Z",
          updated_timestamp: "2025-01-04T00:00:00Z",
          run_id: "run-4",
          source: "caliber",
          commit_message: "Improve grounding examples",
          current: false,
        },
        {
          name,
          version: 3,
          aliases: ["prod"],
          creation_timestamp: "2025-01-03T00:00:00Z",
          updated_timestamp: "2025-01-03T00:00:00Z",
          run_id: "run-3",
          source: "caliber",
          commit_message: "Current production prompt",
          current: true,
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/prompts/:name/versions/:version`, ({ params }) => {
    const name = String(params.name);
    const version = Number(params.version);
    return HttpResponse.json(
      envelope({
        name,
        version,
        template:
          version >= 4
            ? "You are a helpful support assistant. Ask clarifying questions before policy decisions."
            : "You are a helpful support assistant.",
        template_length: version >= 4 ? 86 : 36,
        artifact_ref: `prompts:/${name}@${version}`,
      }),
    );
  }),

  http.post(
    `${API_BASE}/prompts/:name/versions`,
    async ({ params, request }) => {
      const body = (await request.json().catch(() => ({}))) as Record<
        string,
        unknown
      >;
      const name = String(params.name);
      const template = typeof body.template === "string" ? body.template : "";
      const targetAlias =
        typeof body.target_alias === "string" ? body.target_alias : "prod";
      return HttpResponse.json(
        envelope({
          name,
          version: 4,
          uri: `prompts:/${name}/4`,
          template_preview: template.slice(0, 200),
          template_length: template.length,
          alias_changed: true,
          active_alias: targetAlias,
        }),
        { status: 201 },
      );
    },
  ),

  http.get(`${API_BASE}/assistant/config`, () => {
    return HttpResponse.json(
      envelope({
        engine: "fake",
        model: "gpt-4o-mini",
        provider: "openai",
        reasoning: "medium",
        enabled: true,
        disabled_intents: [],
        disabled_domains: [],
        available_models: [
          { id: "gpt-4o-mini", name: "GPT-4o Mini", provider: "openai" },
          { id: "qwen2.5:7b", name: "qwen2.5:7b", provider: "ollama" },
        ],
      }),
    );
  }),

  http.get(`${API_BASE}/assistant/sessions`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.post(`${API_BASE}/assistant/sessions`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as Record<
      string,
      unknown
    >;
    return HttpResponse.json(
      envelope({
        session_id: "ASST-msw0001",
        title: typeof body.title === "string" ? body.title : "New session",
        owner: "@test",
        status: "active",
        goal: typeof body.goal === "string" ? body.goal : "",
        metadata_: {
          assistant_skill_runtime: {
            mode:
              body.skill_mode === "manual" || body.skill_mode === "off"
                ? body.skill_mode
                : "auto",
            pinned_skill_names: [],
            disabled_skill_names: [],
            last_selected_skills: [],
          },
        },
        active_draft_id: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      }),
      { status: 201 },
    );
  }),

  http.patch(
    `${API_BASE}/assistant/sessions/:sessionId`,
    async ({ params, request }) => {
      const body = (await request.json().catch(() => ({}))) as Record<
        string,
        unknown
      >;
      return HttpResponse.json(
        envelope({
          session_id: String(params.sessionId),
          title: "New session",
          owner: "@test",
          status: "active",
          goal: "",
          metadata_: {
            assistant_skill_runtime: {
              mode:
                body.skill_mode === "manual" || body.skill_mode === "off"
                  ? body.skill_mode
                  : "auto",
              pinned_skill_names: Array.isArray(body.pinned_skill_names)
                ? body.pinned_skill_names
                : [],
              disabled_skill_names: Array.isArray(body.disabled_skill_names)
                ? body.disabled_skill_names
                : [],
              last_selected_skills: [],
            },
          },
          active_draft_id: null,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }),
      );
    },
  ),

  http.get(`${API_BASE}/assistant/sessions/:sessionId/messages`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.post(
    `${API_BASE}/assistant/sessions/:sessionId/messages`,
    async ({ params, request }) => {
      await request.json().catch(() => ({}));
      return HttpResponse.json(
        envelope({
          assistant_message: {
            message_id: "AMSG-msw0001",
            session_id: String(params.sessionId),
            role: "assistant",
            content: "I'll help you create a tool.",
            metadata_: {},
            sequence_number: 1,
            created_at: new Date().toISOString(),
          },
          questions: [
            {
              question: "What should the tool be named?",
              field: "name",
              options: [],
            },
          ],
          draft_updates: [],
          run: null,
        }),
        { status: 201 },
      );
    },
  ),

  http.get(`${API_BASE}/assistant/sessions/:sessionId/drafts`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.get(`${API_BASE}/assistant/sessions/:sessionId/attachments`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.get(`${API_BASE}/assistant/sessions/:sessionId/queue`, () => {
    return HttpResponse.json(envelope([]));
  }),

  // ── Calibration defaults ───────────────────────────────────────────────
  // The merged Calibration tab always mounts both halves (test-set + optimizer),
  // so these endpoints fire on every calibration render. Tests that exercise a
  // specific run still override these via server.use().
  http.get(`${API_BASE}/prompts/calibration/options`, () => {
    return HttpResponse.json(
      envelope({
        optimizers: ["MetaPrompt", "MIPROv2"],
        default_optimizer: "MetaPrompt",
        scorers: [
          {
            name: "helpfulness",
            label: "Helpfulness",
            description: "Rates whether the response is helpful.",
            requires_config: false,
            provider: "mlflow",
            category: "core",
            available: true,
            unavailable_reason: null,
            install_command: null,
            config_template: null,
          },
        ],
        default_scorers: ["helpfulness"],
        default_gate: { min_aggregate_score: 0.85, max_regression_delta: 0.02 },
      }),
    );
  }),

  http.get(`${API_BASE}/eval-datasets`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.get(`${API_BASE}/verification-queue`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.get(`${API_BASE}/workflows`, () => {
    return HttpResponse.json(
      envelope([
        {
          workflow_id: "WF-001",
          name: "Support Workflow",
          description: "Default workflow for test navigation.",
          owner: "@sarah",
          status: "active",
          default_experiment_id: null,
          created_at: "2025-01-01T00:00:00Z",
          updated_at: "2025-01-02T00:00:00Z",
        },
      ]),
    );
  }),

  http.get(`${API_BASE}/workflow-runs/:runId/files`, () => {
    return HttpResponse.json(
      envelope({
        items: [],
        next_cursor: null,
      }),
    );
  }),

  http.get(`${API_BASE}/workflow-runs/:runId/trace`, () => {
    return HttpResponse.json(envelope({ trace_id: null, spans: [] }));
  }),

  http.get(`${API_BASE}/workflow-runs/:runId/lineage`, ({ params }) => {
    const runId = String(params.runId);
    return HttpResponse.json(
      envelope({
        workflow_run_id: runId,
        root_run_id: runId,
        total_attempts: 1,
        parent_count: 0,
        child_count: 0,
        missing_parent_id: null,
        truncated: false,
        runs: [
          {
            workflow_run_id: runId,
            workflow_id: "WF-1",
            project_id: null,
            tenant_id: null,
            workflow_version_id: "WFV-1",
            deployment_alias: "manual",
            mlflow_run_id: null,
            trace_id: null,
            session_id: null,
            status: "completed",
            source: "manual",
            priority: 0,
            queued_at: "2026-01-01T00:00:00Z",
            started_at: "2026-01-01T00:00:10Z",
            completed_at: "2026-01-01T00:00:20Z",
            claimed_by: null,
            claimed_at: null,
            lease_expires_at: null,
            last_heartbeat_at: null,
            attempt_number: 1,
            parent_run_id: null,
            cancel_requested_at: null,
            cancel_requested_by: null,
            cancel_reason: null,
            current_node_id: null,
            idempotency_key: null,
            input_file_ref: null,
            error_code: null,
            error_summary: null,
            summary: {},
          },
        ],
      }),
    );
  }),

  http.get(`${API_BASE}/workflow-runs/:runId/manifest`, ({ params }) => {
    return HttpResponse.json(
      envelope({
        workflow_run_id: String(params.runId),
        workflow_id: "WF-1",
        workflow_version_id: "WFV-1",
        manifest_mode: "saved_version",
        manifest_hash: "hash-default-run-manifest",
        manifest: {
          schema_version: 1,
          workflow_id: "WF-1",
          name: "Default Run Manifest",
          nodes: {
            start: {
              id: "start",
              type: "start",
              outputs: { user_message: { type: "string" } },
            },
            support_agent: {
              id: "support_agent",
              type: "agent",
              name: "support-agent",
              model: "inherit",
              instructions: { type: "inline", text: "hi" },
              tools: [],
              inputs: { input: { type: "string" } },
              outputs: { final_output: { type: "string" } },
            },
            final: {
              id: "final",
              type: "output",
              inputs: { response: { type: "string" } },
            },
          },
          edges: [
            {
              id: "e1",
              from: "start",
              to: "support_agent",
              map: { user_message: "input" },
            },
            {
              id: "e2",
              from: "support_agent",
              to: "final",
              map: { final_output: "response" },
            },
          ],
        },
      }),
    );
  }),

  http.get(`${API_BASE}/jobs`, () => {
    return HttpResponse.json(envelope([]));
  }),

  http.post(`${API_BASE}/jobs/:jobId/apply`, ({ params }) => {
    return HttpResponse.json(
      envelope({ job_id: String(params.jobId), status: "applied", promotion: {} }),
    );
  }),
];
