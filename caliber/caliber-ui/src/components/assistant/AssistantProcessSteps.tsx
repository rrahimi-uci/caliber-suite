/**
 * AssistantProcessSteps — compact "show your work" chips rendered under Aria
 * replies so the user can see how a turn resolved (thinking, actions, review,
 * publish, etc.).
 */

import type { AssistantProcessStep } from "@/api/assistantTypes";
import { cn } from "@/lib/utils";

import { toolCallsFromMetadata } from "./ToolCallList";

interface AssistantProcessStepsProps {
  steps: AssistantProcessStep[];
}

function isTone(value: unknown): value is AssistantProcessStep["tone"] {
  return value === "neutral" || value === "success" || value === "warning" || value === "error";
}

function isProcessStep(value: unknown): value is AssistantProcessStep {
  return (
    !!value &&
    typeof value === "object" &&
    typeof (value as { key?: unknown }).key === "string" &&
    typeof (value as { label?: unknown }).label === "string" &&
    ((value as { tone?: unknown }).tone === undefined || isTone((value as { tone?: unknown }).tone))
  );
}

export function processStepsFromMetadata(
  metadata: Record<string, unknown> | undefined,
): AssistantProcessStep[] {
  const raw = metadata?.process_steps;
  if (Array.isArray(raw)) {
    const parsed = raw.filter(isProcessStep).map((step) => ({
      key: step.key,
      label: step.label,
      tone: step.tone ?? "neutral",
    }));
    if (parsed.length > 0) return parsed;
  }

  const steps: AssistantProcessStep[] = [{ key: "thinking", label: "Thinking", tone: "neutral" }];
  const toolCalls = toolCallsFromMetadata(metadata);
  if (toolCalls.length > 0) {
    steps.push({
      key: "actions",
      label: toolCalls.length === 1 ? "1 action" : `${toolCalls.length} actions`,
      tone: toolCalls.every((call) => call.ok) ? "success" : "warning",
    });
  }
  const questions = Array.isArray(metadata?.questions) ? metadata.questions.length : 0;
  if (questions > 0) {
    steps.push({ key: "needs_input", label: "Needs input", tone: "warning" });
  }
  if (metadata?.error === true) {
    steps.push({ key: "error", label: "Error", tone: "error" });
  }
  return steps;
}

export function AssistantProcessSteps({
  steps,
}: AssistantProcessStepsProps): JSX.Element | null {
  if (steps.length === 0) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-1.5" data-testid="assistant-process-steps">
      {steps.map((step) => (
        <span
          key={step.key}
          className={cn(
            "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-medium",
            step.tone === "success" &&
              "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-400/30 dark:bg-emerald-500/15 dark:text-emerald-200",
            step.tone === "warning" &&
              "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-400/30 dark:bg-amber-500/15 dark:text-amber-200",
            step.tone === "error" &&
              "border-red-200 bg-red-50 text-red-700 dark:border-red-400/30 dark:bg-red-500/15 dark:text-red-200",
            (!step.tone || step.tone === "neutral") &&
              "border-slate-200 bg-slate-50 text-slate-600 dark:border-slate-700 dark:bg-slate-950/60 dark:text-slate-300",
          )}
        >
          {step.label}
        </span>
      ))}
    </div>
  );
}
