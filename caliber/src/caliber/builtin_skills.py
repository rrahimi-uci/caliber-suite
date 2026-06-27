"""Built-in CALIBER skill rows.

A small library of general-purpose agent skills — reusable prompt fragments
(tool grounding, safe refusal, structured output, …) that apply across many
agent applications, not just one workflow. They follow the progressive-
disclosure shape (``summary`` = level 1, always loaded; ``content`` = level 2,
loaded when relevant) and the standard category buckets the Skills page knows
about.

``register_builtin_skills`` is idempotent: it inserts only the skills whose
``name`` isn't already present, so it's safe to run on every seed / startup.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberSkill
from caliber.ids import new_skill_id

_OWNER = "@caliber"
_ANTHROPIC_SKILLS_REPO = "https://github.com/anthropics/skills"


def _anthropic_metadata(source_skill: str, license_name: str) -> dict[str, object]:
    """Metadata for Caliber-authored summaries of Anthropic public skills."""

    return {
        "source": "anthropics/skills",
        "source_skill": source_skill,
        "source_url": f"{_ANTHROPIC_SKILLS_REPO}/tree/main/skills/{source_skill}",
        "source_license": license_name,
        "adapted": True,
    }


def _anthropic_skill(
    *,
    name: str,
    category: str,
    tags: list[str],
    description: str,
    summary: str,
    content: str,
    license_name: str = "Apache-2.0",
    allowed_tools: str | None = None,
) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": name,
        "category": category,
        "tags": tags,
        "description": description,
        "summary": summary,
        "content": content,
        "skill_metadata": _anthropic_metadata(name, license_name),
        "visibility": "public",
    }
    if allowed_tools:
        spec["allowed_tools"] = allowed_tools
    return spec


# name -> skill definition. ``name`` is kebab-case (the create contract).
# ``category`` is one of: document_creation | workflow_automation |
# mcp_enhancement | custom.
BUILTIN_SKILLS: list[dict[str, object]] = [
    {
        "name": "tool-grounding",
        "category": "workflow_automation",
        "tags": ["tool-use", "grounding", "safety"],
        "description": (
            "Ground factual, policy, account, or pricing claims in a tool call before "
            "asserting them. Use for any agent that has retrieval/lookup tools and answers "
            "questions of record. Triggers when the response makes a checkable claim."
        ),
        "summary": (
            "Before stating a fact the user could hold you to — a policy, price, status, or "
            "record — call the tool that can confirm it. Never assert from memory when a "
            "grounding tool is available."
        ),
        "content": (
            "When a request asks about something verifiable (a policy, an order/account "
            "status, a price, an availability, a definition you have a tool for):\n"
            "1. Call the relevant tool FIRST and read its result.\n"
            "2. Base the answer on the tool output; quote the specific value.\n"
            "3. If the tool returns nothing or errors, say so explicitly — do not guess or "
            "fabricate a plausible-sounding value.\n"
            "4. If no tool can confirm the claim, hedge ('I'm not certain, but…') rather than "
            "stating it as fact.\n"
            "Grounding beats fluency: a correct 'let me check' is better than a confident "
            "wrong answer."
        ),
    },
    {
        "name": "structured-output",
        "category": "workflow_automation",
        "tags": ["formatting", "json", "schema"],
        "description": (
            "Return output that conforms exactly to a requested schema or format. Use whenever "
            "a downstream step parses the response (JSON APIs, tool args, extraction tasks). "
            "Triggers on 'return JSON', 'match this schema', 'extract these fields'."
        ),
        "summary": (
            "When a schema or format is specified, emit only that — valid, complete, and "
            "parseable. No prose around the JSON, no missing required fields, no invented keys."
        ),
        "content": (
            "When the task specifies an output shape:\n"
            "1. Output ONLY the requested structure (e.g. a single JSON object) with no "
            "surrounding commentary, markdown fences, or trailing text unless asked.\n"
            "2. Include every required field; use null/empty for unknowns rather than omitting "
            "them. Do not add keys that aren't in the schema.\n"
            "3. Use the correct types (numbers unquoted, booleans lowercase, arrays for lists).\n"
            "4. Ensure it actually parses — balanced braces/quotes, valid escaping.\n"
            "If you cannot fill a required field, return the structure with an explicit error "
            "field rather than free text."
        ),
    },
    {
        "name": "safe-refusal",
        "category": "custom",
        "tags": ["safety", "guardrail"],
        "description": (
            "Decline unsafe, disallowed, or clearly out-of-scope requests gracefully and "
            "briefly, then offer a safe alternative. Use in any user-facing agent. Triggers "
            "when a request is harmful, prohibited, or outside the agent's mandate."
        ),
        "summary": (
            "Refuse what you shouldn't do — kindly, in one or two sentences, without lecturing — "
            "and point the user toward something you can help with."
        ),
        "content": (
            "When a request is unsafe, disallowed, or out of scope:\n"
            "1. Decline plainly and briefly; don't moralize or over-explain.\n"
            "2. Give a one-line reason if it helps ('I can't help with that because…').\n"
            "3. Offer the nearest thing you CAN do.\n"
            "4. Never partially comply with the unsafe part to be helpful.\n"
            "Stay warm and non-judgmental — the user isn't the enemy."
        ),
    },
    {
        "name": "pii-protection",
        "category": "custom",
        "tags": ["safety", "privacy", "pii"],
        "description": (
            "Avoid echoing, logging, or storing personal data (emails, phone numbers, SSNs, "
            "card numbers) beyond what the task strictly needs. Use in any agent that handles "
            "user data. Triggers when input or output contains PII."
        ),
        "summary": (
            "Minimize PII. Don't repeat sensitive identifiers back unless required, never put "
            "them in logs or summaries, and prefer references ('the card on file') over values."
        ),
        "content": (
            "When handling personal data:\n"
            "1. Don't restate full PII (card numbers, SSNs, full emails/phones) in the response "
            "unless the task genuinely requires it — refer to it indirectly.\n"
            "2. Never write PII into logs, run summaries, titles, or analytics.\n"
            "3. When confirming identity, use the minimum (last 4 digits), not the whole value.\n"
            "4. If asked to expose someone else's PII, decline (see safe-refusal).\n"
            "Treat every identifier as something that could leak downstream."
        ),
    },
    {
        "name": "clarify-before-answering",
        "category": "workflow_automation",
        "tags": ["reasoning", "ux"],
        "description": (
            "Ask one focused clarifying question when a request is ambiguous enough that a "
            "wrong assumption would waste the user's time. Use in assistant/agent flows. "
            "Triggers when intent, scope, or a key parameter is unclear."
        ),
        "summary": (
            "If a missing detail would change your answer materially, ask one crisp question "
            "first. If a sensible default exists, state it and proceed instead of stalling."
        ),
        "content": (
            "Before answering an ambiguous request:\n"
            "1. Identify the single detail that most changes the outcome.\n"
            "2. If guessing it wrong would be costly or hard to undo, ask ONE specific question "
            "(not a list).\n"
            "3. If a reasonable default exists, proceed with it and say which assumption you "
            "made, so the user can correct you.\n"
            "Don't interrogate — one good question beats three, and acting on a stated "
            "assumption beats blocking."
        ),
    },
    {
        "name": "step-by-step-reasoning",
        "category": "workflow_automation",
        "tags": ["reasoning", "accuracy"],
        "description": (
            "Decompose multi-step or quantitative problems before producing the final answer, "
            "to reduce errors. Use for math, planning, multi-constraint, or logic tasks. "
            "Triggers on calculations, comparisons, or chained conditions."
        ),
        "summary": (
            "For anything with multiple steps or constraints, work through it in order before "
            "committing to an answer; then give the user the conclusion (and the key steps if "
            "useful), not a wall of scratch work."
        ),
        "content": (
            "On multi-step or quantitative tasks:\n"
            "1. Restate the goal and list the constraints/inputs.\n"
            "2. Work the steps in order; carry intermediate values explicitly.\n"
            "3. Sanity-check the result (units, sign, magnitude, edge cases).\n"
            "4. Present the answer clearly; include the reasoning only at the depth the user "
            "needs.\n"
            "Accuracy first: it's fine to slow down on the steps that are easy to get wrong."
        ),
    },
    {
        "name": "cite-sources",
        "category": "workflow_automation",
        "tags": ["research", "citations", "trust"],
        "description": (
            "Attach a source to each non-obvious factual claim so answers are auditable. Use "
            "in research, RAG, and knowledge-base agents. Triggers when answering from "
            "retrieved documents or external knowledge."
        ),
        "summary": (
            "Back factual claims with where they came from (a doc id, URL, or retrieved "
            "chunk). If you can't point to a source, say the claim is unverified."
        ),
        "content": (
            "When answering from sources:\n"
            "1. Attach a citation to each factual claim — a bracketed id [1], a title, or a "
            "URL the user can follow.\n"
            "2. Cite the source you actually used, not a plausible-looking one.\n"
            "3. If the sources disagree, surface that rather than picking silently.\n"
            "4. If a claim has no supporting source, label it as your own inference or as "
            "unverified.\n"
            "Citations turn a confident answer into a checkable one."
        ),
    },
    {
        "name": "concise-empathetic-tone",
        "category": "custom",
        "tags": ["tone", "communication"],
        "description": (
            "Write clearly, concisely, and with empathy — acknowledge the user's situation, "
            "then get to the point. Use in any customer- or user-facing agent. Triggers on "
            "support, onboarding, and conversational replies."
        ),
        "summary": (
            "Lead with a brief acknowledgement of the user's goal or frustration, then answer "
            "directly. Short sentences, no jargon, no filler."
        ),
        "content": (
            "In user-facing replies:\n"
            "1. Open by acknowledging what the user wants or feels — one short clause, not a "
            "scripted apology.\n"
            "2. Answer the actual question first; put caveats after.\n"
            "3. Prefer short sentences and plain words over jargon and hedging.\n"
            "4. Match the user's urgency — terse when they're frustrated, warmer when they're "
            "exploring.\n"
            "Respect their time: say the useful thing, then stop."
        ),
    },
    # ---- Document & integration skills (adapted from anthropics/skills) ----
    {
        "name": "docx-authoring",
        "category": "document_creation",
        "tags": ["docx", "word", "documents", "reports"],
        "description": (
            "Create, read, edit, or manipulate Word documents (.docx). Triggers on any mention "
            "of 'Word doc', '.docx', or requests for professional deliverables (reports, memos, "
            "letters, templates) with formatting like tables of contents, headings, page "
            "numbers, or letterheads, and on extracting or find/replacing content in Word "
            "files. Not for PDFs, spreadsheets, or Google Docs."
        ),
        "summary": (
            "For .docx work: read with pandoc, create with a document library (docx-js / "
            "python-docx), and edit existing files by unpacking the XML, changing it, and "
            "repacking — never regenerate a document you were asked to edit."
        ),
        "content": (
            "A .docx is a ZIP of XML parts. Pick the approach by task:\n"
            "- READ/analyze: extract text with pandoc (use --track-changes=all to keep tracked "
            "changes) or unpack the raw XML.\n"
            "- CREATE new: build with a document library (docx-js in JS, python-docx in Python) "
            "so headings, TOC, and page numbers are real Word constructs, not faked text.\n"
            "- EDIT existing: unpack -> edit the XML -> repack. Preserve the file's existing "
            "styles and structure; do not rebuild from scratch (that loses formatting, "
            "comments, and tracked changes).\n"
            "- Convert legacy .doc to .docx before editing.\n"
            "Match the document's established formatting, quote real values, and confirm the "
            "result opens cleanly before delivering."
        ),
    },
    {
        "name": "pdf-processing",
        "category": "document_creation",
        "tags": ["pdf", "documents", "ocr", "forms"],
        "description": (
            "Do anything with PDF files: read or extract text/tables, merge/split, rotate, "
            "watermark, fill forms, encrypt/decrypt, extract images, OCR scanned PDFs, or "
            "create new PDFs. Triggers whenever a .pdf is an input or the requested output."
        ),
        "summary": (
            "Use pypdf for structural ops (merge, split, rotate, encrypt) and a text/table "
            "extractor (pdfplumber) for content; OCR scanned PDFs before extracting. Preserve "
            "the source content faithfully."
        ),
        "content": (
            "Pick the tool by task:\n"
            "- EXTRACT text/tables: pdfplumber (tables) or pypdf extract_text; for scanned or "
            "image-only PDFs run OCR (ocrmypdf / tesseract) first, then extract.\n"
            "- MERGE/SPLIT/ROTATE/ENCRYPT: pypdf (PdfReader/PdfWriter) — add pages to a writer "
            "to merge, write one page each to split.\n"
            "- FILL FORMS: read the field names, set values via the form API, then flatten if a "
            "non-editable copy is wanted.\n"
            "- CREATE: generate with a PDF library (reportlab) or render from HTML.\n"
            "Never invent text that isn't in the source; if extraction returns nothing, say the "
            "PDF is image-only and needs OCR rather than guessing its contents."
        ),
    },
    {
        "name": "pptx-presentations",
        "category": "document_creation",
        "tags": ["pptx", "slides", "presentations", "decks"],
        "description": (
            "Create, read, edit, or combine PowerPoint presentations (.pptx) — slide decks, "
            "pitch decks, templates, speaker notes. Triggers on 'deck', 'slides', "
            "'presentation', or any .pptx file as input or output."
        ),
        "summary": (
            "Read decks with markitdown; edit from a template by unpacking and manipulating "
            "slides; create from scratch with a slide library (pptxgenjs). Design real slides — "
            "not plain bullets on a white background."
        ),
        "content": (
            "Approach by task:\n"
            "- READ/analyze: markitdown for text, a thumbnail render for visual layout, or "
            "unpack the XML.\n"
            "- EDIT from a template: study the template visually, then unpack -> duplicate/edit "
            "slides -> repack; match the template's master layouts, fonts, and colors exactly.\n"
            "- CREATE from scratch: build with a slide library (pptxgenjs) when no template is "
            "available.\n"
            "Design matters: vary layouts, use visual hierarchy and imagery, and keep one idea "
            "per slide — avoid walls of bullet text. Preserve speaker notes when present."
        ),
    },
    {
        "name": "xlsx-spreadsheets",
        "category": "document_creation",
        "tags": ["xlsx", "excel", "spreadsheets", "csv", "financial-models"],
        "description": (
            "Open, read, edit, fix, or create spreadsheets (.xlsx, .xlsm, .csv, .tsv) — add "
            "columns, compute formulas, format, chart, or clean messy tabular data. Triggers "
            "whenever a spreadsheet file is the primary input or output. Not when the "
            "deliverable is a Word doc, HTML report, or database pipeline."
        ),
        "summary": (
            "Deliver spreadsheets with ZERO formula errors and a consistent professional font. "
            "When editing a template, match its existing conventions exactly. Put assumptions "
            "in their own cells and reference them in formulas."
        ),
        "content": (
            "Output rules:\n"
            "- Zero formula errors: no #REF!, #DIV/0!, #VALUE!, #N/A, or #NAME? in the delivered "
            "file. Verify ranges and edge cases (zeros, negatives).\n"
            "- Preserve templates: when updating an existing file, exactly match its format, "
            "style, and conventions — they override these defaults.\n"
            "- Assumptions: place growth rates, margins, and multiples in separate cells and "
            "reference them (=B5*(1+$B$6), never =B5*1.05).\n"
            "Financial-model color conventions (unless the template says otherwise): blue text "
            "= hardcoded inputs, black = formulas, green = links within the workbook, red = "
            "links to other files. Number formats: currency $#,##0 with units in the header, "
            "percentages 0.0%, multiples 0.0x, negatives in parentheses, zeros shown as '-'."
        ),
    },
    {
        "name": "mcp-server-builder",
        "category": "mcp_enhancement",
        "tags": ["mcp", "tools", "integration", "api"],
        "description": (
            "Build high-quality MCP (Model Context Protocol) servers that expose external "
            "services to LLMs as well-designed tools. Use when creating an MCP server to "
            "integrate an API or service, in Python (FastMCP) or TypeScript (MCP SDK)."
        ),
        "summary": (
            "Design MCP tools for how agents actually work: clear action-oriented names with a "
            "consistent prefix, concise descriptions, paginated/filtered results, and "
            "actionable error messages. Favor comprehensive API coverage when unsure."
        ),
        "content": (
            "Server quality is measured by how well an agent accomplishes real tasks with it:\n"
            "- Tool design: action-oriented names with a consistent service prefix "
            "(github_create_issue, github_list_repos). Balance broad API coverage with a few "
            "high-level workflow tools; when unsure, favor coverage.\n"
            "- Context: keep tool descriptions concise and return focused data; support "
            "filtering and pagination so results don't flood the context window.\n"
            "- Errors: make messages actionable — name the problem and the next step, not just "
            "a stack trace.\n"
            "- Stack: TypeScript (MCP SDK) or Python (FastMCP); use streamable HTTP (stateless "
            "JSON) for remote servers and stdio for local ones.\n"
            "Read the spec at modelcontextprotocol.io (fetch pages with a .md suffix) before "
            "finalizing the tool surface."
        ),
    },
]


# The public anthropics/skills repo mixes Apache-2.0 examples with
# source-available/proprietary document skills. Keep these rows as compact
# Caliber-native summaries and preserve source/license metadata on each row.
_ANTHROPIC_SKILLS: list[dict[str, object]] = [
    _anthropic_skill(
        name="algorithmic-art",
        category="code_generation",
        tags=["art", "p5js", "generative-design", "creative-coding"],
        description=(
            "Create original generative artwork with code. Use when a request mentions "
            "algorithmic art, p5.js, seeded randomness, flow fields, particles, or "
            "interactive visual systems."
        ),
        summary=(
            "Turn the brief into an original generative-art direction, then express it "
            "as reproducible code with seeded randomness and adjustable parameters."
        ),
        content=(
            "For generative-art work:\n"
            "1. Start by naming the visual system and its computational idea: motion, "
            "noise, particles, fields, recursion, rhythm, or another algorithmic rule.\n"
            "2. Build with seeded randomness so outputs are reproducible and variants are "
            "intentional, not accidental.\n"
            "3. Prefer p5.js or another lightweight browser canvas stack when an interactive "
            "viewer helps the user explore parameters.\n"
            "4. Expose a small set of meaningful controls such as seed, density, palette, "
            "noise scale, speed, or attraction.\n"
            "5. Create original work. Use references only for broad direction; do not copy "
            "a living artist's distinctive style."
        ),
    ),
    _anthropic_skill(
        name="brand-guidelines",
        category="document_creation",
        tags=["brand", "visual-design", "style-guide", "anthropic"],
        description=(
            "Apply Anthropic-style brand guidance to artifacts. Use when the user asks for "
            "Anthropic visual identity, brand colors, typography, or company-style polish."
        ),
        summary=(
            "Use brand styling deliberately: choose the approved palette and type direction, "
            "keep hierarchy clear, and avoid applying Anthropic branding to unrelated assets."
        ),
        content=(
            "When brand guidance is requested:\n"
            "1. Confirm the artifact should intentionally use Anthropic styling rather than a "
            "generic or customer-specific brand.\n"
            "2. Apply a restrained palette, strong contrast, and a clear type hierarchy.\n"
            "3. Keep brand elements consistent across slides, docs, web artifacts, and images.\n"
            "4. Treat brand as a system, not decoration: spacing, tone, color, typography, and "
            "component choices should reinforce one visual language.\n"
            "5. If the artifact is not about Anthropic or an Anthropic-branded workflow, ask "
            "before using another organization's identity."
        ),
    ),
    _anthropic_skill(
        name="canvas-design",
        category="document_creation",
        tags=["visual-design", "poster", "pdf", "png", "art"],
        description=(
            "Create polished static visual artifacts such as posters, printable PDFs, or PNG "
            "designs. Use when the output is primarily visual rather than prose."
        ),
        summary=(
            "Define a visual philosophy first, then render it with composition, color, scale, "
            "and minimal text. Originality and craft matter more than filling space."
        ),
        content=(
            "For static visual design:\n"
            "1. Translate the request into a concise visual direction: mood, composition, "
            "material, color, and visual hierarchy.\n"
            "2. Use words sparingly. Let shape, scale, spacing, image, and rhythm carry most "
            "of the message.\n"
            "3. Produce an actual deliverable such as PNG or PDF when requested, with correct "
            "dimensions and export quality.\n"
            "4. Avoid generic centered layouts unless the concept demands it. Make one or two "
            "bold choices and carry them through.\n"
            "5. Create original visuals; do not reproduce a specific artist's protected style."
        ),
    ),
    _anthropic_skill(
        name="claude-api",
        category="tool_integration",
        tags=["anthropic", "claude", "api", "sdk", "llm"],
        description=(
            "Reference guidance for Claude API and Anthropic SDK work. Use when a task names "
            "Claude, Anthropic, Sonnet, Opus, Haiku, Anthropic SDKs, Claude tool use, MCP, "
            "streaming, token counting, caching, or model migration."
        ),
        summary=(
            "When implementing Claude API behavior, verify the current Anthropic docs or SDK "
            "surface first, avoid provider mixups, and prefer official SDKs over ad-hoc shims."
        ),
        content=(
            "For Claude API work:\n"
            "1. First confirm the project is meant to use Anthropic. If the code is clearly "
            "OpenAI, Gemini, local-model, or provider-neutral, pause before changing it.\n"
            "2. Use the official Anthropic SDK for the project language when available; use raw "
            "HTTP only when requested or when no SDK exists.\n"
            "3. Check current documentation for model IDs, request shapes, streaming helpers, "
            "tool-use schemas, caching, and token-counting behavior before writing code.\n"
            "4. Keep provider configuration explicit through environment variables or secret "
            "sources; never hard-code API keys.\n"
            "5. Add a small test, smoke script, or mocked contract check for new integration "
            "logic."
        ),
        allowed_tools="WebFetch",
    ),
    _anthropic_skill(
        name="doc-coauthoring",
        category="content_writing",
        tags=["documentation", "writing", "specs", "collaboration"],
        description=(
            "Guide a collaborative documentation workflow. Use when writing proposals, PRDs, "
            "technical specs, RFCs, decision docs, or other substantial team documents."
        ),
        summary=(
            "Use a three-stage writing loop: gather context, shape the document with the user, "
            "then test whether a fresh reader would understand it."
        ),
        content=(
            "For co-authoring docs:\n"
            "1. Gather context first: document type, audience, desired decision or outcome, "
            "constraints, and any template.\n"
            "2. Invite a context dump. Organize it into a clear outline before polishing prose.\n"
            "3. Build section by section, keeping open questions visible instead of hiding gaps.\n"
            "4. Check the draft from the reader's perspective: what is missing, confusing, or "
            "unsupported?\n"
            "5. End with actionable next steps, owners, risks, and decisions when the doc is "
            "meant to drive work."
        ),
        license_name="not specified in source folder",
    ),
    _anthropic_skill(
        name="docx",
        category="document_creation",
        tags=["docx", "word", "documents", "reports"],
        description=(
            "Create, read, edit, or manipulate Word documents. Use for .docx inputs or "
            "outputs, reports, memos, letters, templates, tracked changes, comments, tables "
            "of contents, page numbers, or find-and-replace in Word files."
        ),
        summary=(
            "Treat .docx files as structured documents. Preserve existing formatting when "
            "editing, use real document constructs, and verify the output opens cleanly."
        ),
        content=(
            "For Word document tasks:\n"
            "1. If reading, extract text and structure without losing comments, tracked changes, "
            "headings, or tables that matter to the task.\n"
            "2. If editing an existing file, preserve its styles, numbering, media, comments, "
            "and document relationships. Do not rebuild from scratch unless asked.\n"
            "3. If creating a new file, use real document features for headings, tables, lists, "
            "page numbers, and table of contents rather than visual approximations.\n"
            "4. Match the user's template or brand conventions when present.\n"
            "5. Validate the file by opening or parsing it after writing."
        ),
        license_name="source-available/proprietary",
    ),
    _anthropic_skill(
        name="frontend-design",
        category="code_generation",
        tags=["frontend", "ui", "visual-design", "css"],
        description=(
            "Guide distinctive frontend design choices. Use when building or reshaping a UI "
            "and the task needs intentional visual direction, typography, layout, color, or "
            "interaction design."
        ),
        summary=(
            "Choose a clear visual concept, avoid generic AI-looking defaults, and make the UI "
            "feel designed for this product rather than a template."
        ),
        content=(
            "For frontend design:\n"
            "1. Identify the product mood and pick a visual language before arranging controls.\n"
            "2. Use typography, spacing, color, and hierarchy as deliberate product decisions.\n"
            "3. Avoid interchangeable centered cards, default font stacks, and decorative "
            "gradients that do not support the concept.\n"
            "4. Make mobile and desktop layouts both intentional.\n"
            "5. Add motion only where it clarifies state, rhythm, or progression."
        ),
    ),
    _anthropic_skill(
        name="internal-comms",
        category="communication",
        tags=["communications", "status-report", "leadership", "writing"],
        description=(
            "Write internal communications such as status reports, leadership updates, "
            "incident reports, project updates, FAQs, newsletters, or team announcements."
        ),
        summary=(
            "Write for the audience and decision at hand: clear context, crisp status, direct "
            "asks, risks, owners, and next steps."
        ),
        content=(
            "For internal communications:\n"
            "1. Start by identifying audience, purpose, and desired action.\n"
            "2. Lead with the headline: what changed, why it matters, and what happens next.\n"
            "3. Separate facts, interpretation, risks, and asks so readers can scan quickly.\n"
            "4. Use the organization's preferred format when provided; otherwise pick a simple "
            "structure such as TL;DR, status, risks, decisions, next steps.\n"
            "5. Keep tone direct, calm, and useful. Do not bury escalations."
        ),
    ),
    _anthropic_skill(
        name="mcp-builder",
        category="mcp_enhancement",
        tags=["mcp", "tools", "integration", "api"],
        description=(
            "Create high-quality MCP servers for external APIs or services. Use when building "
            "MCP integrations in Python, TypeScript, or another supported stack."
        ),
        summary=(
            "Design MCP tools around agent workflows: clear names, scoped inputs, useful "
            "errors, pagination, and enough coverage for real tasks."
        ),
        content=(
            "For MCP server work:\n"
            "1. Model the user workflows first, then define tools that map to useful actions.\n"
            "2. Use consistent, action-oriented tool names and descriptions that tell the agent "
            "when to call each tool.\n"
            "3. Return focused data. Support pagination, filtering, and IDs so results do not "
            "flood context.\n"
            "4. Make errors actionable: what failed, why, and how to recover.\n"
            "5. Include authentication, rate limits, transport choice, and a small test client "
            "before considering the server usable."
        ),
    ),
    _anthropic_skill(
        name="pdf",
        category="document_creation",
        tags=["pdf", "documents", "ocr", "forms"],
        description=(
            "Work with PDF files: read, extract, merge, split, rotate, watermark, fill forms, "
            "encrypt or decrypt, OCR scans, extract images, or create PDFs."
        ),
        summary=(
            "Pick the right PDF path: text extraction for digital PDFs, OCR for scans, form "
            "APIs for fields, and structural libraries for merge/split/rotate tasks."
        ),
        content=(
            "For PDF work:\n"
            "1. Determine whether the PDF is digital text, scanned images, forms, or mixed.\n"
            "2. Use extraction tools for digital text and tables; run OCR before extracting "
            "image-only pages.\n"
            "3. Use structural PDF operations for merging, splitting, rotating, encrypting, "
            "and watermarking rather than rasterizing pages unnecessarily.\n"
            "4. Preserve page order, form fields, metadata, and visual fidelity when editing.\n"
            "5. If extraction is uncertain, say what was recoverable and what needs OCR or "
            "manual review."
        ),
        license_name="source-available/proprietary",
    ),
    _anthropic_skill(
        name="pptx",
        category="document_creation",
        tags=["pptx", "slides", "presentations", "decks"],
        description=(
            "Create, read, edit, combine, or analyze PowerPoint presentations. Use whenever "
            "a .pptx file, slide deck, pitch deck, template, speaker notes, or presentation "
            "deliverable is involved."
        ),
        summary=(
            "Treat decks as visual narratives. Preserve templates when editing, use real slide "
            "structures, and design slides around one clear idea each."
        ),
        content=(
            "For presentation work:\n"
            "1. Read the existing deck visually and structurally before editing.\n"
            "2. Preserve master layouts, theme colors, fonts, speaker notes, and media unless "
            "the user asks for a redesign.\n"
            "3. When creating new slides, build a narrative arc and make each slide carry one "
            "main point.\n"
            "4. Use charts, diagrams, imagery, and layout hierarchy instead of dense bullet "
            "walls.\n"
            "5. Verify the deck opens and the generated slides render correctly."
        ),
        license_name="source-available/proprietary",
    ),
    _anthropic_skill(
        name="skill-creator",
        category="workflow_automation",
        tags=["skills", "prompting", "evaluation", "agents"],
        description=(
            "Create, improve, or evaluate agent skills. Use when drafting a new skill, editing "
            "an existing skill, optimizing trigger descriptions, or designing skill evals."
        ),
        summary=(
            "Clarify what the skill should do, when it should trigger, how success is tested, "
            "then iterate on concise instructions and examples."
        ),
        content=(
            "For creating skills:\n"
            "1. Capture intent: capability, trigger conditions, expected outputs, constraints, "
            "and tools/resources.\n"
            "2. Keep the description trigger-rich and the instructions operational.\n"
            "3. Include examples or test prompts for objective skills; use qualitative review "
            "for subjective skills.\n"
            "4. Run or simulate evaluations, inspect failures, then rewrite the skill.\n"
            "5. Prefer compact progressive disclosure over long catch-all instructions."
        ),
    ),
    _anthropic_skill(
        name="slack-gif-creator",
        category="document_creation",
        tags=["gif", "slack", "animation", "image"],
        description=(
            "Create animated GIFs optimized for Slack. Use when users ask for Slack emoji GIFs, "
            "message GIFs, lightweight animations, or GIF concepts."
        ),
        summary=(
            "Design short loopable animations, keep dimensions and palette tight, and optimize "
            "for Slack's display and file-size constraints."
        ),
        content=(
            "For Slack GIFs:\n"
            "1. Decide whether the GIF is an emoji-scale loop or a larger message animation.\n"
            "2. Keep the motion simple, readable, and loopable, usually under a few seconds.\n"
            "3. Use limited colors and frame counts to control file size.\n"
            "4. If using an uploaded image, decide whether to animate it directly or use it only "
            "as reference.\n"
            "5. Preview the final dimensions and timing before delivery."
        ),
    ),
    _anthropic_skill(
        name="theme-factory",
        category="document_creation",
        tags=["themes", "branding", "color", "typography"],
        description=(
            "Apply or create visual themes for slides, documents, reports, HTML pages, and "
            "other artifacts. Use when the user asks for a cohesive look or theme."
        ),
        summary=(
            "Pick or create a theme with a coherent palette and font pairing, then apply it "
            "consistently across the artifact."
        ),
        content=(
            "For theming artifacts:\n"
            "1. Establish the theme's audience, mood, and constraints.\n"
            "2. Choose a small palette with clear roles: background, text, secondary surfaces, "
            "and accent.\n"
            "3. Pair fonts intentionally and keep hierarchy consistent.\n"
            "4. Apply the theme across every page, slide, chart, component, and callout.\n"
            "5. Check contrast, readability, and whether the theme supports the content."
        ),
    ),
    _anthropic_skill(
        name="web-artifacts-builder",
        category="code_generation",
        tags=["html", "react", "artifacts", "frontend"],
        description=(
            "Build complex HTML/web artifacts with modern frontend tooling. Use when a request "
            "needs stateful React-style UI, routing, components, or bundled single-file output."
        ),
        summary=(
            "Use a real frontend stack for complex artifacts, bundle the result into a portable "
            "HTML file, and test interaction before handing it off."
        ),
        content=(
            "For web artifacts:\n"
            "1. Choose a simple static HTML file only for simple artifacts; use React or a "
            "component stack when state, routing, or rich interactions are required.\n"
            "2. Keep the implementation portable and self-contained for handoff.\n"
            "3. Use a distinctive visual direction rather than default component-library looks.\n"
            "4. Bundle assets and dependencies into a single deliverable when requested.\n"
            "5. Smoke-test the artifact in a browser and fix console/runtime errors."
        ),
        allowed_tools="Bash(npm:*) Bash(node:*)",
    ),
    _anthropic_skill(
        name="webapp-testing",
        category="workflow_automation",
        tags=["testing", "playwright", "browser", "frontend"],
        description=(
            "Test local web applications with browser automation. Use for verifying frontend "
            "behavior, debugging UI state, inspecting rendered DOM, screenshots, and console "
            "errors."
        ),
        summary=(
            "Use browser automation against the running app: navigate, wait for JS, inspect the "
            "rendered state, perform the user action, and verify the outcome."
        ),
        content=(
            "For web app testing:\n"
            "1. Determine whether the app is static or needs a dev server.\n"
            "2. Navigate with a real browser automation tool and wait for the rendered app to "
            "settle before selecting elements.\n"
            "3. Prefer robust selectors from labels, roles, text, or test IDs.\n"
            "4. Capture screenshots, console logs, and network clues when behavior is unclear.\n"
            "5. Turn the repro into a focused test or smoke script when possible."
        ),
        allowed_tools="Bash(python:*)",
    ),
    _anthropic_skill(
        name="xlsx",
        category="document_creation",
        tags=["xlsx", "excel", "spreadsheets", "csv", "data"],
        description=(
            "Create, read, edit, clean, format, or convert spreadsheet files such as .xlsx, "
            ".xlsm, .csv, and .tsv. Use when the deliverable is a spreadsheet or tabular file."
        ),
        summary=(
            "Deliver clean spreadsheets: preserve templates, use correct formulas and formats, "
            "separate assumptions, and verify there are no formula errors."
        ),
        content=(
            "For spreadsheet work:\n"
            "1. Identify whether the task is data cleaning, analysis, formatting, charting, "
            "modeling, or file conversion.\n"
            "2. Preserve existing workbook structure, formulas, named ranges, styling, and "
            "template conventions when editing.\n"
            "3. Put assumptions in explicit cells and reference them in formulas.\n"
            "4. Use appropriate number formats, headers, units, and validation.\n"
            "5. Recalculate or inspect formulas and confirm there are no obvious errors such "
            "as broken references or division by zero."
        ),
        license_name="source-available/proprietary",
    ),
]

BUILTIN_SKILLS.extend(_ANTHROPIC_SKILLS)


def register_builtin_skills(session: Session) -> int:
    """Ensure the built-in general-purpose skills exist. Returns rows created.

    Idempotent — inserts only skills whose ``name`` isn't already present, so it
    is safe to run on every seed or startup.
    """
    names = [str(s["name"]) for s in BUILTIN_SKILLS]
    existing = set(
        session.execute(select(CaliberSkill.name).where(CaliberSkill.name.in_(names)))
        .scalars()
        .all()
    )
    created = 0
    for spec in BUILTIN_SKILLS:
        name = str(spec["name"])
        if name in existing:
            continue
        session.add(
            CaliberSkill(
                skill_id=new_skill_id(),
                name=name,
                description=str(spec["description"]),
                summary=str(spec["summary"]),
                content=str(spec["content"]),
                owner=_OWNER,
                category=str(spec["category"]),
                tags=[str(t) for t in cast("list[str]", spec["tags"])],
                skill_metadata=dict(cast("dict[str, object]", spec.get("skill_metadata", {}))),
                allowed_tools=(str(spec["allowed_tools"]) if spec.get("allowed_tools") else None),
                depends_on=[str(t) for t in cast("list[str]", spec.get("depends_on", []))],
                status="active",
                visibility=str(spec.get("visibility", "public")),
                version=1,
            )
        )
        created += 1
    session.flush()
    return created


__all__ = ["BUILTIN_SKILLS", "register_builtin_skills"]
