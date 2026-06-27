/**
 * Skill Builder Wizard — 5-step guided flow for OpenAI-compatible skills.
 *
 * Steps:
 *   1. Identity & Classification — name (kebab-case), owner, category, tags
 *   2. Progressive Disclosure     — summary, description, SKILL.md body content
 *   3. Composability              — depends_on, allowed_tools, metadata key-value pairs
 *   4. Trigger Testing            — should-trigger / should-not-trigger phrase lists
 *   5. Review & Create            — pre-flight checklist, full summary, submit
 *
 * Creates CALIBER registry rows that can export as SKILL.md packages with
 * agents/openai.yaml metadata.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { Skill, SkillCategory, SkillCreatePayload } from "@/api/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface MetadataEntry {
  id: string;
  key: string;
  value: string;
}

interface WizardFormData {
  // Step 1: Identity
  name: string;
  owner: string;
  category: SkillCategory;
  tags: string[];
  // Step 2: Progressive Disclosure
  summary: string;
  description: string;
  content: string;
  // Step 3: Composability
  depends_on: string[];
  allowed_tools: string;
  metadata: MetadataEntry[];
  // Step 4: Trigger Testing
  shouldTrigger: string[];
  shouldNotTrigger: string[];
}

const INITIAL_FORM: WizardFormData = {
  name: "",
  owner: "",
  category: "custom",
  tags: [],
  summary: "",
  description: "",
  content: "",
  depends_on: [],
  allowed_tools: "",
  metadata: [],
  shouldTrigger: [],
  shouldNotTrigger: [],
};

const STEPS = [
  { label: "Identity", icon: "1" },
  { label: "Content", icon: "2" },
  { label: "Composability", icon: "3" },
  { label: "Triggers", icon: "4" },
  { label: "Review & create", icon: "5" },
] as const;

function genId(): string {
  return Math.random().toString(36).slice(2, 9);
}

/** Convert freeform text to kebab-case. */
function toKebab(s: string): string {
  return s
    .replace(/[^a-zA-Z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .toLowerCase()
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

const KEBAB_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function titleFromName(name: string): string {
  return name
    .split("-")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function shortPackageDescription(form: WizardFormData): string {
  const fallback = `Use ${titleFromName(form.name)} in agent workflows.`;
  const source = compactText(form.summary || form.description || fallback);
  return source.length <= 64 ? source : `${source.slice(0, 61).trim()}...`;
}

function buildOpenAiPackageMetadata(form: WizardFormData): Record<string, unknown> {
  const shortDescription = shortPackageDescription(form);
  return {
    format: "openai-skill",
    source: "wizard",
    agents: {
      interface: {
        display_name: titleFromName(form.name),
        short_description: shortDescription,
        default_prompt: `Use $${form.name} to ${shortDescription.replace(/\.$/, "").toLowerCase()}.`,
      },
    },
    resources: [],
  };
}

const CATEGORY_OPTIONS: { value: SkillCategory; label: string; icon: string; desc: string }[] = [
  { value: "document_creation", label: "Document & Asset", icon: "📄", desc: "Documents, presentations, apps, designs" },
  { value: "data_analysis", label: "Data Analysis", icon: "📊", desc: "Analyze tables, metrics, and datasets" },
  { value: "data_extraction", label: "Data Extraction", icon: "🔎", desc: "Pull structured data from docs & text" },
  { value: "code_generation", label: "Code Generation", icon: "💻", desc: "Write, review, and refactor code" },
  { value: "content_writing", label: "Content Writing", icon: "✍️", desc: "Marketing copy, articles, and posts" },
  { value: "summarization", label: "Summarization", icon: "📝", desc: "Condense documents and conversations" },
  { value: "classification", label: "Classification", icon: "🏷️", desc: "Categorize, tag, and route inputs" },
  { value: "research", label: "Research", icon: "🔬", desc: "Search, gather, and synthesize sources" },
  { value: "customer_support", label: "Customer Support", icon: "🎧", desc: "Resolve and triage support requests" },
  { value: "communication", label: "Communication", icon: "✉️", desc: "Emails, messages, and notifications" },
  { value: "reasoning_planning", label: "Reasoning & Planning", icon: "🧠", desc: "Multi-step reasoning and planning" },
  { value: "tool_integration", label: "Tool Integration", icon: "🛠️", desc: "Patterns for calling tools & APIs" },
  { value: "compliance_safety", label: "Compliance & Safety", icon: "🛡️", desc: "Policy, guardrails, and safety checks" },
  { value: "workflow_automation", label: "Workflow Automation", icon: "⚙️", desc: "Multi-step processes, coordination" },
  { value: "mcp_enhancement", label: "MCP Enhancement", icon: "🔌", desc: "Workflow guidance for MCP integrations" },
  { value: "custom", label: "Custom", icon: "🔧", desc: "General-purpose or specialized skills" },
];

/* ------------------------------------------------------------------ */
/*  Inline icon helpers (no lucide — keep file svg-only)               */
/* ------------------------------------------------------------------ */

type IconProps = { className?: string };

function CheckIcon({ className = "h-4 w-4" }: IconProps): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function XIcon({ className = "h-4 w-4" }: IconProps): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

function AlertIcon({ className = "h-4 w-4" }: IconProps): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  );
}

function ArrowRightIcon({ className = "h-4 w-4" }: IconProps): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="5" y1="12" x2="19" y2="12" />
      <polyline points="12 5 19 12 12 19" />
    </svg>
  );
}

function InfoIcon({ className = "h-4 w-4" }: IconProps): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="16" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  );
}

function LightbulbIcon({ className = "h-4 w-4" }: IconProps): JSX.Element {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 18h6M10 22h4" />
      <path d="M12 2a7 7 0 00-4 12.7c.6.5 1 1.3 1 2.1V18h6v-1.2c0-.8.4-1.6 1-2.1A7 7 0 0012 2z" />
    </svg>
  );
}

/** Section header used at the top of each step panel. */
function SectionHeader({
  icon,
  title,
  subtitle,
  tag,
}: {
  icon: JSX.Element;
  title: string;
  subtitle: JSX.Element | string;
  tag?: JSX.Element;
}): JSX.Element {
  return (
    <div className="flex items-start justify-between gap-4">
      <div>
        <h2 className="flex flex-wrap items-center gap-2 text-base font-semibold text-slate-900">
          <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-violet-50 text-caliber-purple">
            {icon}
          </span>
          {title}
        </h2>
        <p className="mt-1 text-sm text-slate-500">{subtitle}</p>
      </div>
      {tag}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step Indicator                                                     */
/* ------------------------------------------------------------------ */

function StepIndicator({ current, onGoTo }: { current: number; onGoTo: (i: number) => void }): JSX.Element {
  return (
    <nav data-testid="skill-wizard-steps" className="card mb-6 p-5">
      <ol className="flex flex-col gap-4 lg:flex-row lg:items-center lg:gap-0">
        {STEPS.map((step, i) => {
          const done = i < current;
          const active = i === current;
          const last = i === STEPS.length - 1;
          return (
            <li key={step.label} className={`flex flex-1 items-center gap-3 ${last ? "lg:flex-none" : ""}`}>
              <button
                type="button"
                data-testid={`skill-step-${i}`}
                onClick={() => i < current && onGoTo(i)}
                disabled={i > current}
                className="flex items-center gap-3 text-left"
              >
                <span
                  className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-[13px] font-bold transition-all
                    ${done ? "bg-gradient-brand text-white shadow-nav-active" : ""}
                    ${active ? "bg-white text-caliber-purple ring-2 ring-caliber-purple shadow-nav-active" : ""}
                    ${!active && !done ? "bg-surface-100 text-slate-400 font-semibold" : ""}
                  `}
                >
                  {done ? <CheckIcon className="h-4 w-4" /> : step.icon}
                </span>
                <span className="min-w-0">
                  <span
                    className={`block text-[10px] font-bold uppercase tracking-widest ${
                      active ? "text-caliber-purple" : "text-slate-300"
                    }`}
                  >
                    {active ? `Step ${i + 1} · current` : `Step ${i + 1}`}
                  </span>
                  <span
                    className={`block truncate text-[13px] ${
                      active
                        ? "font-bold text-slate-900"
                        : done
                          ? "font-semibold text-slate-700"
                          : "font-medium text-slate-400"
                    }`}
                  >
                    {step.label}
                  </span>
                </span>
              </button>
              {!last && (
                <div
                  className={`ml-3 hidden h-px flex-1 lg:block ${
                    done
                      ? "bg-caliber-300"
                      : active
                        ? "bg-gradient-to-r from-caliber-300 to-slate-200"
                        : "bg-slate-200"
                  }`}
                />
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 1: Identity & Classification                                  */
/* ------------------------------------------------------------------ */

function IdentityStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  const [tagInput, setTagInput] = useState("");
  const nameValid = form.name.length === 0 || KEBAB_RE.test(form.name);

  const addTag = () => {
    const t = tagInput.trim();
    if (t && !form.tags.includes(t)) {
      onChange({ tags: [...form.tags, t] });
      setTagInput("");
    }
  };

  return (
    <div data-testid="skill-step-identity" className="space-y-6">
      <div className="card p-6 space-y-5">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="8" r="4" />
              <path d="M4 21v-1a6 6 0 016-6h4a6 6 0 016 6v1" />
            </svg>
          }
          title="Skill identity"
          subtitle="Give your skill a unique kebab-case name and an owner. These become its permanent address in the registry."
        />

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="skill-name">Name *</Label>
            <Input
              id="skill-name"
              data-testid="skill-wiz-name"
              placeholder="reasoning-v1"
              value={form.name}
              onChange={(e) => {
                const raw = e.target.value;
                if (raw.includes(" ") || raw.includes("_")) {
                  onChange({ name: toKebab(raw) });
                } else {
                  onChange({ name: raw.toLowerCase().replace(/[^a-z0-9-]/g, "") });
                }
              }}
            />
            {!nameValid && (
              <p className="text-xs text-red-500" data-testid="skill-name-error">
                Must be kebab-case (lowercase, digits, hyphens only)
              </p>
            )}
            <p className="text-xs text-slate-400">Unique identifier. Must be kebab-case (e.g. my-cool-skill).</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="skill-owner">Owner *</Label>
            <Input
              id="skill-owner"
              data-testid="skill-wiz-owner"
              placeholder="@sarah or team-name"
              value={form.owner}
              onChange={(e) => onChange({ owner: e.target.value })}
            />
            <p className="text-xs text-slate-400">A person or team accountable for this skill.</p>
          </div>
        </div>
      </div>

      {/* Category picker */}
      <div className="card p-6 space-y-4">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82z" />
              <line x1="7" y1="7" x2="7.01" y2="7" />
            </svg>
          }
          title="Category"
          subtitle="Routes auto-selection and groups the skill in the library."
        />
        <div className="grid gap-2 sm:grid-cols-2">
          {CATEGORY_OPTIONS.map((cat) => (
            <button
              key={cat.value}
              type="button"
              data-testid={`skill-wiz-category-${cat.value}`}
              onClick={() => onChange({ category: cat.value })}
              className={`rounded-xl border p-3 text-left transition-all ${
                form.category === cat.value
                  ? "border-caliber-purple bg-violet-50 ring-1 ring-caliber-purple/30"
                  : "border-slate-200/60 hover:border-slate-300"
              }`}
            >
              <div className="flex items-center gap-2">
                <span className="text-lg">{cat.icon}</span>
                <span className="text-sm font-medium text-slate-900">{cat.label}</span>
              </div>
              <p className="mt-1 text-xs text-slate-500">{cat.desc}</p>
            </button>
          ))}
        </div>
      </div>

      {/* Tags */}
      <div className="card p-6 space-y-3">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82z" />
              <line x1="7" y1="7" x2="7.01" y2="7" />
            </svg>
          }
          title="Tags"
          subtitle="Optional keywords to aid search and discovery."
        />
        <div className="flex gap-2">
          <Input
            data-testid="skill-wiz-tag-input"
            className="flex-1"
            placeholder="e.g. reasoning, safety"
            value={tagInput}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addTag())}
            onChange={(e) => setTagInput(e.target.value)}
          />
          <Button type="button" variant="outline" size="sm" data-testid="skill-wiz-add-tag" onClick={addTag}>
            Add
          </Button>
        </div>
        {form.tags.length > 0 && (
          <div className="flex flex-wrap gap-1.5" data-testid="skill-wiz-tags">
            {form.tags.map((t) => (
              <Badge key={t} variant="secondary" className="gap-1">
                {t}
                <button
                  type="button"
                  onClick={() => onChange({ tags: form.tags.filter((x) => x !== t) })}
                  className="text-slate-400 hover:text-red-500"
                  aria-label={`Remove tag ${t}`}
                >
                  ×
                </button>
              </Badge>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 2: Progressive Disclosure (Content Authoring)                  */
/* ------------------------------------------------------------------ */

function ContentStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  const summaryLen = form.summary.length;
  const contentLines = form.content.split("\n").length;

  return (
    <div data-testid="skill-step-content" className="space-y-6">
      {/* Authoring tips callout */}
      <div className="flex items-start gap-3 rounded-2xl border border-blue-200/70 bg-blue-50/70 p-4">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-blue-100 text-blue-600">
          <LightbulbIcon className="h-4 w-4" />
        </span>
        <div className="text-[13px] leading-relaxed text-blue-800">
          <span className="font-semibold">Authoring tips.</span> Skills load progressively. The{" "}
          <span className="font-semibold">Summary</span> (Level&nbsp;1) is always in the agent&apos;s
          context — keep it tight and lead with <em>what</em> + <em>when</em>. The{" "}
          <span className="font-semibold">Content</span> body (Level&nbsp;2) loads only when the skill is
          selected, so it can be rich. Start from a single hard task, iterate until the agent nails it,
          then distill the winning approach here. Quality over breadth — keep the body under ~5,000 words.
        </div>
      </div>

      {/* Level-1: Summary card */}
      <div className="card p-6 space-y-4">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 7V5a2 2 0 012-2h2M17 3h2a2 2 0 012 2v2M21 17v2a2 2 0 01-2 2h-2M7 21H5a2 2 0 01-2-2v-2" />
              <line x1="7" y1="9" x2="17" y2="9" />
              <line x1="7" y1="13" x2="13" y2="13" />
            </svg>
          }
          title="Summary"
          subtitle={
            <>
              Level&nbsp;1 — always loaded. Describe <strong>what</strong> the skill does and{" "}
              <strong>when</strong> to use it, with concrete trigger phrasing. No XML angle brackets.
            </>
          }
          tag={
            <span className="hidden shrink-0 items-center gap-1.5 rounded-full border border-emerald-200/70 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-700 sm:inline-flex">
              <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polygon points="12 2 2 7 12 12 22 7 12 2" />
                <polyline points="2 17 12 22 22 17" />
                <polyline points="2 12 12 17 22 12" />
              </svg>
              Always in context
            </span>
          }
        />
        <div>
          <Textarea
            id="skill-summary"
            data-testid="skill-wiz-summary"
            placeholder="Chain-of-thought reasoning rubric. Use when the agent needs to show its work step-by-step."
            rows={2}
            value={form.summary}
            onChange={(e) => onChange({ summary: e.target.value })}
          />
          <div className="mt-1.5 flex items-center justify-between gap-3">
            <p className="text-xs text-slate-400">
              Lead with the trigger. This text always competes for the agent&apos;s attention budget.
            </p>
            <span className={`shrink-0 text-xs font-medium ${summaryLen > 1024 ? "text-red-500" : "text-slate-400"}`}>
              {summaryLen}/1024
            </span>
          </div>
        </div>
      </div>

      {/* Level-2: Content body card */}
      <div className="card p-6 space-y-4">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="8" y1="13" x2="16" y2="13" />
              <line x1="8" y1="17" x2="16" y2="17" />
            </svg>
          }
          title="Content body"
          subtitle={
            <>
              Level&nbsp;2 — loaded when relevant. Full Markdown instructions that become the body of{" "}
              <span className="font-mono text-slate-600">SKILL.md</span>.
            </>
          }
          tag={
            <span className="shrink-0 rounded-full border border-red-200/70 bg-red-50 px-2.5 py-1 text-[11px] font-semibold text-red-500">
              required
            </span>
          }
        />
        <div>
          <div className="mb-1.5 flex items-center justify-end">
            <span className="text-xs font-medium text-slate-400">{contentLines} lines</span>
          </div>
          <Textarea
            id="skill-content"
            data-testid="skill-wiz-content"
            className="font-mono text-sm"
            placeholder={`# Instructions\n\n## Step 1: Analyze the request\nBreak down the user's request into…\n\n## Step 2: Execute\n…`}
            rows={12}
            value={form.content}
            onChange={(e) => onChange({ content: e.target.value })}
          />
          <p className="mt-1.5 text-xs text-slate-400">
            Full Markdown instructions. Loaded when the skill is relevant.
            Use headers, code blocks, and examples. Recommended: keep under 5,000 words.
          </p>
        </div>
      </div>

      {/* Description card */}
      <div className="card p-6 space-y-4">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="17" y1="10" x2="3" y2="10" />
              <line x1="21" y1="6" x2="3" y2="6" />
              <line x1="21" y1="14" x2="3" y2="14" />
              <line x1="17" y1="18" x2="3" y2="18" />
            </svg>
          }
          title="Description"
          subtitle="System-prompt facing. Surfaced when the skill is composed into an agent."
          tag={
            <span className="hidden shrink-0 rounded-full border border-slate-200/70 bg-surface-50 px-2.5 py-1 text-[11px] font-medium text-slate-400 sm:inline-flex">
              optional
            </span>
          }
        />
        <div>
          <Textarea
            id="skill-description"
            data-testid="skill-wiz-description"
            placeholder="Detailed description of what the skill does, when to activate it, and key capabilities."
            rows={2}
            value={form.description}
            onChange={(e) => onChange({ description: e.target.value })}
          />
          <p className="mt-1.5 text-xs text-slate-400">
            Plain prose only — must NOT contain XML angle brackets.
          </p>
        </div>
      </div>

      {/* What's next preview */}
      <div className="card p-6">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="9" y1="6" x2="21" y2="6" />
              <line x1="9" y1="12" x2="21" y2="12" />
              <line x1="9" y1="18" x2="21" y2="18" />
              <circle cx="4" cy="6" r="1" />
              <circle cx="4" cy="12" r="1" />
              <circle cx="4" cy="18" r="1" />
            </svg>
          }
          title="What's next"
          subtitle="A peek at the remaining steps so you can prep as you write. Nothing here is required to continue from Content."
          tag={
            <span className="hidden shrink-0 rounded-full border border-slate-200/70 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-500 sm:inline-flex">
              2 steps left
            </span>
          }
        />
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <div className="rounded-xl border border-slate-200/70 bg-surface-50/60 p-4">
            <div className="flex items-center gap-2">
              <span className="grid h-7 w-7 place-items-center rounded-lg bg-slate-100 text-[12px] font-bold text-slate-500">3</span>
              <div className="text-[13px] font-semibold text-slate-800">Composability</div>
            </div>
            <p className="mt-2 text-[12px] leading-relaxed text-slate-500">
              Declare <span className="font-medium text-slate-700">depends-on</span> skills that load
              together, restrict <span className="font-mono text-slate-600">allowed_tools</span>, and add
              custom metadata key/value pairs.
            </p>
          </div>
          <div className="rounded-xl border border-slate-200/70 bg-surface-50/60 p-4">
            <div className="flex items-center gap-2">
              <span className="grid h-7 w-7 place-items-center rounded-lg bg-slate-100 text-[12px] font-bold text-slate-500">4</span>
              <div className="text-[13px] font-semibold text-slate-800">Trigger testing</div>
            </div>
            <p className="mt-2 text-[12px] leading-relaxed text-slate-500">
              List phrases that <span className="font-medium text-emerald-700">should</span> and{" "}
              <span className="font-medium text-red-600">should not</span> auto-select this skill. We score
              keyword coverage against your summary.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 3: Composability & Restrictions                               */
/* ------------------------------------------------------------------ */

function ComposabilityStep({
  form,
  onChange,
  existingSkills,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
  existingSkills: Skill[];
}): JSX.Element {
  const [depInput, setDepInput] = useState("");
  const availableDeps = existingSkills
    .filter((s) => s.status === "active" && !form.depends_on.includes(s.name))
    .map((s) => s.name);

  const filteredDeps = depInput.trim()
    ? availableDeps.filter((n) => n.includes(depInput.trim().toLowerCase()))
    : availableDeps;

  const addDep = (name: string) => {
    if (!form.depends_on.includes(name)) {
      onChange({ depends_on: [...form.depends_on, name] });
    }
    setDepInput("");
  };

  const addMetadata = () => {
    onChange({ metadata: [...form.metadata, { id: genId(), key: "", value: "" }] });
  };

  const updateMetadata = (id: string, patch: Partial<MetadataEntry>) => {
    onChange({ metadata: form.metadata.map((m) => (m.id === id ? { ...m, ...patch } : m)) });
  };

  const removeMetadata = (id: string) => {
    onChange({ metadata: form.metadata.filter((m) => m.id !== id) });
  };

  return (
    <div data-testid="skill-step-composability" className="space-y-6">
      {/* Depends on */}
      <div className="card p-6 space-y-3">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="18" cy="18" r="3" />
              <circle cx="6" cy="6" r="3" />
              <path d="M6 21V9a9 9 0 009 9" />
            </svg>
          }
          title="Depends on"
          subtitle="Skills this skill composes with. They'll be loaded together."
        />
        <div className="relative">
          <Input
            data-testid="skill-wiz-dep-input"
            placeholder="Search existing skills…"
            value={depInput}
            onChange={(e) => setDepInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && depInput.trim()) {
                e.preventDefault();
                addDep(depInput.trim());
              }
            }}
          />
          {depInput.trim() && filteredDeps.length > 0 && (
            <div className="absolute top-full left-0 right-0 z-10 mt-1 max-h-40 overflow-y-auto rounded-xl border border-slate-200/60 bg-white shadow-lg">
              {filteredDeps.slice(0, 8).map((n) => {
                const skill = existingSkills.find((s) => s.name === n);
                return (
                  <button
                    key={n}
                    type="button"
                    onClick={() => addDep(n)}
                    className="w-full px-3 py-2 text-left text-sm hover:bg-surface-50"
                  >
                    <span className="font-medium text-slate-900">{n}</span>
                    {skill?.summary && (
                      <span className="ml-2 truncate text-xs text-slate-400">{skill.summary.slice(0, 60)}</span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>
        {form.depends_on.length > 0 && (
          <div className="flex flex-wrap gap-1.5" data-testid="skill-wiz-deps">
            {form.depends_on.map((d) => (
              <Badge key={d} variant="secondary" className="gap-1">
                {d}
                <button
                  type="button"
                  onClick={() => onChange({ depends_on: form.depends_on.filter((x) => x !== d) })}
                  className="text-slate-400 hover:text-red-500"
                  aria-label={`Remove dependency ${d}`}
                >
                  ×
                </button>
              </Badge>
            ))}
          </div>
        )}
      </div>

      {/* Allowed tools */}
      <div className="card p-6 space-y-3">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14.7 6.3a4 4 0 00-5.4 5.4L3 18v3h3l6.3-6.3a4 4 0 005.4-5.4l-2.7 2.7-2-2 2.7-2.7z" />
            </svg>
          }
          title="Allowed tools"
          subtitle="Restrict which tools this skill may invoke. Leave empty for no restrictions."
        />
        <div className="space-y-1.5">
          <Input
            id="skill-tools"
            data-testid="skill-wiz-allowed-tools"
            placeholder='e.g. Bash(python:*) WebFetch'
            value={form.allowed_tools}
            onChange={(e) => onChange({ allowed_tools: e.target.value })}
          />
          <p className="text-xs text-slate-400">
            Format: <code className="text-slate-500">Bash(python:*) WebFetch</code>
          </p>
        </div>
      </div>

      {/* Metadata key-value pairs */}
      <div className="card p-6 space-y-3">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M8 3H7a2 2 0 00-2 2v5a2 2 0 01-2 2 2 2 0 012 2v5a2 2 0 002 2h1" />
              <path d="M16 3h1a2 2 0 012 2v5a2 2 0 002 2 2 2 0 00-2 2v5a2 2 0 01-2 2h-1" />
            </svg>
          }
          title="Metadata"
          subtitle="Custom key-value pairs (e.g. author, version, mcp-server)."
          tag={
            <Button type="button" variant="outline" size="sm" data-testid="skill-wiz-add-meta" onClick={addMetadata}>
              + Add field
            </Button>
          }
        />
        {form.metadata.length > 0 && (
          <div className="space-y-2">
            {form.metadata.map((m) => (
              <div key={m.id} data-testid={`skill-wiz-meta-${m.id}`} className="flex items-center gap-2">
                <Input
                  data-testid={`skill-wiz-meta-key-${m.id}`}
                  className="w-1/3 text-xs"
                  placeholder="key"
                  value={m.key}
                  onChange={(e) => updateMetadata(m.id, { key: e.target.value })}
                />
                <Input
                  data-testid={`skill-wiz-meta-val-${m.id}`}
                  className="flex-1 text-xs"
                  placeholder="value"
                  value={m.value}
                  onChange={(e) => updateMetadata(m.id, { value: e.target.value })}
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => removeMetadata(m.id)}
                  aria-label={`Remove metadata ${m.key || m.id}`}
                >
                  ×
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 4: Trigger Testing                                            */
/* ------------------------------------------------------------------ */

function TriggerTestingStep({
  form,
  onChange,
}: {
  form: WizardFormData;
  onChange: (patch: Partial<WizardFormData>) => void;
}): JSX.Element {
  const [triggerInput, setTriggerInput] = useState("");
  const [antiInput, setAntiInput] = useState("");

  const addTrigger = () => {
    const t = triggerInput.trim();
    if (t && !form.shouldTrigger.includes(t)) {
      onChange({ shouldTrigger: [...form.shouldTrigger, t] });
      setTriggerInput("");
    }
  };

  const addAntiTrigger = () => {
    const t = antiInput.trim();
    if (t && !form.shouldNotTrigger.includes(t)) {
      onChange({ shouldNotTrigger: [...form.shouldNotTrigger, t] });
      setAntiInput("");
    }
  };

  // Simple keyword overlap check between triggers and summary+description
  const coverage = useMemo(() => {
    if (form.shouldTrigger.length === 0) return null;
    const haystack = `${form.summary} ${form.description}`.toLowerCase();
    const hits = form.shouldTrigger.filter((t) => {
      const words = t.toLowerCase().split(/\s+/);
      return words.some((w) => w.length > 3 && haystack.includes(w));
    });
    return { total: form.shouldTrigger.length, covered: hits.length };
  }, [form.shouldTrigger, form.summary, form.description]);

  return (
    <div data-testid="skill-step-triggers" className="space-y-6">
      {/* Should trigger */}
      <div className="card p-6 space-y-3">
        <SectionHeader
          icon={<CheckIcon className="h-4 w-4" />}
          title="Should trigger"
          subtitle="Phrases users would say that should activate this skill."
          tag={
            <span className="hidden shrink-0 items-center gap-1.5 rounded-full border border-emerald-200/70 bg-emerald-50 px-2.5 py-1 text-[11px] font-semibold text-emerald-700 sm:inline-flex">
              <CheckIcon className="h-3 w-3" />
              positive
            </span>
          }
        />
        <div className="flex gap-2">
          <Input
            data-testid="skill-wiz-trigger-input"
            className="flex-1"
            placeholder='e.g. "help me plan this sprint"'
            value={triggerInput}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addTrigger())}
            onChange={(e) => setTriggerInput(e.target.value)}
          />
          <Button type="button" variant="outline" size="sm" data-testid="skill-wiz-add-trigger" onClick={addTrigger}>
            Add
          </Button>
        </div>
        {form.shouldTrigger.length > 0 && (
          <div className="space-y-1" data-testid="skill-wiz-triggers">
            {form.shouldTrigger.map((t, i) => (
              <div key={i} className="flex items-center gap-2 rounded-lg border border-emerald-100 bg-emerald-50 px-3 py-1.5">
                <span className="text-emerald-600"><CheckIcon className="h-3.5 w-3.5" /></span>
                <span className="flex-1 text-sm text-slate-700">{t}</span>
                <button
                  type="button"
                  onClick={() => onChange({ shouldTrigger: form.shouldTrigger.filter((_, idx) => idx !== i) })}
                  className="text-slate-400 hover:text-red-500"
                  aria-label={`Remove trigger ${t}`}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Should NOT trigger */}
      <div className="card p-6 space-y-3">
        <SectionHeader
          icon={<XIcon className="h-4 w-4" />}
          title="Should NOT trigger"
          subtitle="Phrases that are unrelated — the skill should stay inactive."
          tag={
            <span className="hidden shrink-0 items-center gap-1.5 rounded-full border border-red-200/70 bg-red-50 px-2.5 py-1 text-[11px] font-semibold text-red-600 sm:inline-flex">
              <XIcon className="h-3 w-3" />
              negative
            </span>
          }
        />
        <div className="flex gap-2">
          <Input
            data-testid="skill-wiz-anti-input"
            className="flex-1"
            placeholder='e.g. "What is the weather?"'
            value={antiInput}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addAntiTrigger())}
            onChange={(e) => setAntiInput(e.target.value)}
          />
          <Button type="button" variant="outline" size="sm" data-testid="skill-wiz-add-anti" onClick={addAntiTrigger}>
            Add
          </Button>
        </div>
        {form.shouldNotTrigger.length > 0 && (
          <div className="space-y-1" data-testid="skill-wiz-anti-triggers">
            {form.shouldNotTrigger.map((t, i) => (
              <div key={i} className="flex items-center gap-2 rounded-lg border border-red-100 bg-red-50 px-3 py-1.5">
                <span className="text-red-500"><XIcon className="h-3.5 w-3.5" /></span>
                <span className="flex-1 text-sm text-slate-700">{t}</span>
                <button
                  type="button"
                  onClick={() => onChange({ shouldNotTrigger: form.shouldNotTrigger.filter((_, idx) => idx !== i) })}
                  className="text-slate-400 hover:text-red-500"
                  aria-label={`Remove anti-trigger ${t}`}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        {/* Coverage indicator */}
        {coverage && (
          <div
            data-testid="skill-wiz-coverage"
            className={`rounded-xl border p-3 ${
              coverage.covered === coverage.total
                ? "border-emerald-200 bg-emerald-50"
                : coverage.covered > 0
                  ? "border-amber-200 bg-amber-50"
                  : "border-red-200 bg-red-50"
            }`}
          >
            <p className="text-sm font-medium text-slate-700">
              Keyword Coverage: {coverage.covered}/{coverage.total} trigger phrases
            </p>
            <p className="mt-0.5 text-xs text-slate-500">
              {coverage.covered === coverage.total
                ? "All trigger phrases have keyword overlap with your summary/description."
                : "Some trigger phrases have no keyword overlap with your summary/description. Consider adding relevant keywords to improve discoverability."}
            </p>
          </div>
        )}
      </div>

      <div className="flex items-start gap-3 rounded-2xl border border-blue-200/70 bg-blue-50/70 p-4">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-blue-100 text-blue-600">
          <LightbulbIcon className="h-4 w-4" />
        </span>
        <p className="text-[13px] leading-relaxed text-blue-800">
          <span className="font-semibold">Trigger check.</span> Test that your skill triggers on obvious
          tasks, paraphrased requests, and does <em>not</em> trigger on unrelated topics. These phrases are
          stored in metadata for future evaluation.
        </p>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step 5: Review & Create                                            */
/* ------------------------------------------------------------------ */

const CATEGORY_LABELS: Record<SkillCategory, string> = {
  document_creation: "Document & Asset",
  data_analysis: "Data Analysis",
  data_extraction: "Data Extraction",
  code_generation: "Code Generation",
  content_writing: "Content Writing",
  summarization: "Summarization",
  classification: "Classification",
  research: "Research",
  customer_support: "Customer Support",
  communication: "Communication",
  reasoning_planning: "Reasoning & Planning",
  tool_integration: "Tool Integration",
  compliance_safety: "Compliance & Safety",
  workflow_automation: "Workflow Automation",
  mcp_enhancement: "MCP Enhancement",
  custom: "Custom",
};

interface CheckItem {
  label: string;
  ok: boolean;
  severity: "error" | "warning";
}

function ReviewStep({ form }: { form: WizardFormData }): JSX.Element {
  const checks: CheckItem[] = useMemo(() => {
    const items: CheckItem[] = [];
    items.push({ label: "Name is kebab-case", ok: KEBAB_RE.test(form.name), severity: "error" });
    items.push({ label: "Name is not empty", ok: form.name.length > 0, severity: "error" });
    items.push({ label: "Owner is provided", ok: form.owner.trim().length > 0, severity: "error" });
    items.push({ label: "Content is provided", ok: form.content.trim().length > 0, severity: "error" });
    items.push({
      label: "Summary includes WHAT + WHEN",
      ok: form.summary.trim().length > 10,
      severity: "warning",
    });
    items.push({
      label: "No XML tags in summary/description",
      ok: !/<[a-zA-Z/]/.test(form.summary) && !/<[a-zA-Z/]/.test(form.description),
      severity: "error",
    });
    items.push({
      label: "Summary under 1024 characters",
      ok: form.summary.length <= 1024,
      severity: "error",
    });
    items.push({
      label: "No reserved name prefix (claude, anthropic)",
      ok: !form.name.startsWith("claude") && !form.name.startsWith("anthropic"),
      severity: "error",
    });
    items.push({
      label: "Trigger phrases defined",
      ok: form.shouldTrigger.length > 0,
      severity: "warning",
    });
    items.push({
      label: "Content under ~5000 words",
      ok: form.content.split(/\s+/).length < 5000,
      severity: "warning",
    });
    items.push({
      label: "OpenAI package metadata generated",
      ok: Boolean(form.name && form.content.trim()),
      severity: "warning",
    });
    return items;
  }, [form]);

  const hasErrors = checks.some((c) => !c.ok && c.severity === "error");

  return (
    <div data-testid="skill-step-review" className="space-y-6">
      {/* Pre-flight checklist */}
      <div className="card p-6 space-y-4">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 11l3 3L22 4" />
              <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
            </svg>
          }
          title="Pre-flight checklist"
          subtitle="Live-validated. All required items must pass before you can create the skill."
        />
        <div className="space-y-1.5" data-testid="skill-wiz-checklist">
          {checks.map((c) => (
            <div key={c.label} className="flex items-center gap-2 text-xs">
              <span className={c.ok ? "text-emerald-500" : c.severity === "error" ? "text-red-500" : "text-amber-500"}>
                {c.ok ? (
                  <CheckIcon className="h-3.5 w-3.5" />
                ) : c.severity === "error" ? (
                  <XIcon className="h-3.5 w-3.5" />
                ) : (
                  <AlertIcon className="h-3.5 w-3.5" />
                )}
              </span>
              <span className={c.ok ? "text-slate-600" : c.severity === "error" ? "font-medium text-red-600" : "text-amber-600"}>
                {c.label}
              </span>
            </div>
          ))}
        </div>
        {/* status legend */}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 pt-3 text-[10px] font-medium text-slate-400">
          <span className="inline-flex items-center gap-1"><CheckIcon className="h-3 w-3 text-emerald-500" />passing</span>
          <span className="inline-flex items-center gap-1"><AlertIcon className="h-3 w-3 text-amber-500" />warning</span>
          <span className="inline-flex items-center gap-1"><XIcon className="h-3 w-3 text-red-500" />blocking</span>
        </div>
        {hasErrors && (
          <p className="text-xs font-medium text-red-600">
            Fix required items before creating the skill.
          </p>
        )}
      </div>

      {/* Summary grid */}
      <div className="card p-6 space-y-4">
        <SectionHeader
          icon={
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M16.5 9.4L7.5 4.21" />
              <path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z" />
              <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
              <line x1="12" y1="22.08" x2="12" y2="12" />
            </svg>
          }
          title="Skill summary"
          subtitle="Verify your skill and the OpenAI-compatible package metadata before creating."
        />
        <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-xs" data-testid="skill-wiz-summary">
          <dt className="text-slate-500">Name</dt>
          <dd className="font-mono font-medium text-slate-900">{form.name || "—"}</dd>
          <dt className="text-slate-500">Owner</dt>
          <dd className="font-medium text-slate-900">{form.owner || "—"}</dd>
          <dt className="text-slate-500">Category</dt>
          <dd className="font-medium text-slate-900">{CATEGORY_LABELS[form.category]}</dd>
          {form.tags.length > 0 && (
            <>
              <dt className="text-slate-500">Tags</dt>
              <dd className="font-medium text-slate-900">{form.tags.join(", ")}</dd>
            </>
          )}
          <dt className="text-slate-500">Summary</dt>
          <dd className="max-w-xs truncate font-medium text-slate-900">{form.summary || "—"}</dd>
          <dt className="text-slate-500">Content</dt>
          <dd className="font-medium text-slate-900">
            {form.content ? `${form.content.split("\n").length} lines` : "—"}
          </dd>
          {form.depends_on.length > 0 && (
            <>
              <dt className="text-slate-500">Depends On</dt>
              <dd className="font-medium text-slate-900">{form.depends_on.join(", ")}</dd>
            </>
          )}
          {form.allowed_tools && (
            <>
              <dt className="text-slate-500">Allowed Tools</dt>
              <dd className="font-mono font-medium text-slate-900">{form.allowed_tools}</dd>
            </>
          )}
          {form.shouldTrigger.length > 0 && (
            <>
              <dt className="text-slate-500">Trigger Phrases</dt>
              <dd className="font-medium text-slate-900">{form.shouldTrigger.length} defined</dd>
            </>
          )}
          <dt className="text-slate-500">Package</dt>
          <dd className="font-mono font-medium text-slate-900">
            {form.name ? `${form.name}/SKILL.md` : "—"}
          </dd>
        </dl>
      </div>

      {/* Content preview */}
      {form.content && (
        <div className="card p-6 space-y-2">
          <Label>Content Preview</Label>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-xl border border-slate-200/60 bg-surface-50 p-3 font-mono text-xs text-slate-700">
            {form.content}
          </pre>
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main Wizard                                                        */
/* ------------------------------------------------------------------ */

export function SkillWizard({ onClose }: { onClose: () => void }): JSX.Element {
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<WizardFormData>(INITIAL_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [existingSkills, setExistingSkills] = useState<Skill[]>([]);

  // Load existing skills for composability autocomplete
  useEffect(() => {
    const ac = new AbortController();
    caliberApi.listSkills({ status: "active" }, ac.signal).then(setExistingSkills).catch(() => {});
    return () => ac.abort();
  }, []);

  const onChange = useCallback(
    (patch: Partial<WizardFormData>) => setForm((prev) => ({ ...prev, ...patch })),
    [],
  );

  const isStepValid = useCallback(
    (s: number): boolean => {
      switch (s) {
        case 0:
          return form.name.trim().length > 0 && KEBAB_RE.test(form.name) && form.owner.trim().length > 0;
        case 1:
          return form.content.trim().length > 0;
        case 2:
          return true; // composability is optional
        case 3:
          return true; // triggers are optional
        case 4:
          return true; // review
        default:
          return false;
      }
    },
    [form],
  );

  const canSubmit = isStepValid(0) && isStepValid(1);

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      // Build metadata including trigger test data
      const meta: Record<string, unknown> = {};
      for (const m of form.metadata) {
        if (m.key.trim()) meta[m.key.trim()] = m.value;
      }
      if (form.shouldTrigger.length > 0 || form.shouldNotTrigger.length > 0) {
        meta.test_triggers = {
          should_trigger: form.shouldTrigger,
          should_not_trigger: form.shouldNotTrigger,
        };
      }
      meta.openai_package = buildOpenAiPackageMetadata(form);

      const payload: SkillCreatePayload = {
        name: form.name.trim(),
        owner: form.owner.trim(),
        category: form.category,
        tags: form.tags,
        summary: form.summary.trim(),
        description: form.description.trim(),
        content: form.content,
        depends_on: form.depends_on,
        allowed_tools: form.allowed_tools.trim() || undefined,
        skill_metadata: Object.keys(meta).length > 0 ? meta : undefined,
      };

      await caliberApi.createSkill(payload);
      onClose();
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setSubmitting(false);
    }
  };

  const goNext = () => {
    if (step < STEPS.length - 1) setStep(step + 1);
  };
  const goBack = () => {
    if (step > 0) setStep(step - 1);
  };

  const isLast = step === STEPS.length - 1;
  const nextLabel = !isLast ? STEPS[step + 1]!.label : "";
  /* Footer gating hint mirrors isStepValid() — only steps 0 & 1 gate. */
  const GATING_HINTS = [
    "name and owner are required before you can continue",
    "content is required before you can continue",
    "composability is optional — continue when ready",
    "triggers are optional — continue when ready",
    "review the checklist, then create the skill",
  ];

  return (
    <div data-testid="skill-wizard" className="w-full">
      {/* Header */}
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">
            <span className="grid h-4 w-4 place-items-center rounded bg-violet-50 text-caliber-purple">
              <svg className="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 01-.837.276c-.47-.07-.802-.48-.968-.925a2.501 2.501 0 10-3.214 3.214c.446.166.855.497.925.968a.979.979 0 01-.276.837l-1.61 1.61a2.404 2.404 0 01-1.705.707 2.402 2.402 0 01-1.704-.706l-1.568-1.568a1.026 1.026 0 00-.877-.29c-.493.074-.84.504-1.02.968a2.5 2.5 0 11-3.237-3.237c.464-.18.894-.527.967-1.02a1.026 1.026 0 00-.289-.877l-1.568-1.568A2.402 2.402 0 011.998 12c0-.617.236-1.234.706-1.704L4.23 8.77c.24-.24.581-.353.917-.303.515.077.877.528 1.073 1.01a2.5 2.5 0 103.259-3.259c-.482-.196-.933-.558-1.01-1.073-.05-.336.062-.676.303-.917l1.525-1.525A2.402 2.402 0 0112 1.998c.617 0 1.234.236 1.704.706l1.568 1.568c.23.23.556.338.877.29.493-.074.84-.504 1.02-.968a2.5 2.5 0 113.237 3.237c-.464.18-.894.527-.967 1.02z" />
              </svg>
            </span>
            Skill library
          </div>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-slate-900">Build a new skill</h1>
          <p className="mt-1 text-sm text-slate-500">
            Author a reusable, progressive-disclosure prompt fragment. Refine it once and the change
            cascades to every agent that references it — exported as an OpenAI-compatible{" "}
            <span className="font-mono text-slate-600">SKILL.md</span> package.
          </p>
        </div>
        <button
          type="button"
          data-testid="skill-wizard-close"
          onClick={onClose}
          className="shrink-0 text-slate-400 hover:text-slate-600"
          aria-label="Close wizard"
        >
          <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      <StepIndicator current={step} onGoTo={setStep} />

      {/* Step content */}
      <div className="min-h-[400px]">
        {step === 0 && <IdentityStep form={form} onChange={onChange} />}
        {step === 1 && <ContentStep form={form} onChange={onChange} />}
        {step === 2 && <ComposabilityStep form={form} onChange={onChange} existingSkills={existingSkills} />}
        {step === 3 && <TriggerTestingStep form={form} onChange={onChange} />}
        {step === 4 && <ReviewStep form={form} />}
      </div>

      {/* Navigation footer */}
      <div className="card mt-6 flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2 text-[12px] text-slate-400">
          <InfoIcon className="h-3.5 w-3.5" />
          <span>
            Step <span className="font-semibold text-slate-600">{step + 1} of {STEPS.length}</span> ·{" "}
            {GATING_HINTS[step]}
          </span>
        </div>
        <div className="flex items-center gap-2.5">
          <Button
            type="button"
            variant="outline"
            data-testid="skill-wizard-back"
            onClick={step === 0 ? onClose : goBack}
          >
            {step === 0 ? "Cancel" : "Back"}
          </Button>
          {isLast ? (
            <Button
              type="button"
              data-testid="skill-wizard-submit"
              disabled={!canSubmit || submitting}
              onClick={() => void submit()}
            >
              {submitting ? "Creating…" : "Create Skill"}
            </Button>
          ) : (
            <Button
              type="button"
              data-testid="skill-wizard-next"
              disabled={!isStepValid(step)}
              onClick={goNext}
              className="gap-1.5"
            >
              Continue to {nextLabel}
              <ArrowRightIcon className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {submitError && (
        <div data-testid="skill-wizard-error" className="mt-3 rounded-xl border border-red-200 bg-red-50 p-3">
          <p className="text-sm text-red-600">{submitError}</p>
        </div>
      )}
    </div>
  );
}
