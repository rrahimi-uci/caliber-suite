/**
 * Shared Aria goal-plan rendering atoms.
 *
 * One source of truth for plan/step status colors, the step row, and the
 * mid-run interaction prompt, so the standalone Plans page and the inline
 * plan-in-chat card render identically. The orchestrator API and types live in
 * `@/api`; this module is presentation only.
 */

import { useEffect, useMemo, useState } from "react";

import type {
  AriaAutonomy,
  AriaInteraction,
  AriaInteractionAnswerPayload,
  AriaPlan,
  AriaPlanStatus,
  AriaPlanStep,
  AriaStepStatus,
} from "@/api/types";

const TERMINAL_PLAN_STATUSES: ReadonlySet<AriaPlanStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** A plan still worth showing in-thread (draft/approved/running/paused). */
export function isResumablePlanStatus(status: AriaPlanStatus): boolean {
  return !TERMINAL_PLAN_STATUSES.has(status);
}

/** The most recent still-open plan to restore inline (list arrives newest-first). */
export function pickResumablePlan(plans: AriaPlan[]): AriaPlan | null {
  return plans.find((p) => isResumablePlanStatus(p.status)) ?? null;
}

// Recommended default (review-then-run) first; raw enum values stay on the wire.
export const AUTONOMY_LEVELS: AriaAutonomy[] = ["approve_plan", "ask_each", "auto_guarded"];

/** Plain-language framing for the autonomy dial — "how often should Aria check in". */
export const AUTONOMY_LABELS: Record<AriaAutonomy, { label: string; hint: string }> = {
  approve_plan: {
    label: "Review the plan, then run",
    hint: "Approve the plan once; Aria runs it and only stops for risky (gated) steps.",
  },
  ask_each: {
    label: "Stop before each change",
    hint: "Aria pauses for your OK before every step that changes anything.",
  },
  auto_guarded: {
    label: "Run everything it safely can",
    hint: "Aria runs all non-gated steps without stopping; gated steps still pause.",
  },
};

export function PlanStatusBadge({ status }: { status: AriaPlanStatus }): JSX.Element {
  const cls: Record<AriaPlanStatus, string> = {
    draft: "bg-surface-200 text-gray-600",
    approved: "bg-mlflow-blue/10 text-mlflow-blue",
    running: "bg-amber-100 text-amber-700",
    paused: "bg-amber-100 text-amber-700",
    completed: "bg-emerald-100 text-emerald-700",
    failed: "bg-red-100 text-red-700",
    cancelled: "bg-gray-200 text-gray-500",
  };
  return (
    <span
      title={status}
      className={`text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls[status]}`}
    >
      {status}
    </span>
  );
}

export function StepStatusBadge({ status }: { status: AriaStepStatus }): JSX.Element {
  const cls: Record<AriaStepStatus, string> = {
    pending: "bg-surface-100 text-gray-500",
    blocked: "bg-surface-100 text-gray-400",
    running: "bg-amber-100 text-amber-700",
    waiting_input: "bg-violet-100 text-caliber-purple",
    waiting_job: "bg-amber-100 text-amber-700",
    done: "bg-emerald-100 text-emerald-700",
    failed: "bg-red-100 text-red-700",
    skipped: "bg-gray-200 text-gray-500",
  };
  return (
    <span
      title={status}
      className={`shrink-0 text-[10px] font-semibold uppercase px-1.5 py-0.5 rounded ${cls[status]}`}
    >
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function PlanStepRow({ step }: { step: AriaPlanStep }): JSX.Element {
  const requiredInputs = Array.isArray(step.input_schema?.required)
    ? step.input_schema.required.filter((item): item is string => typeof item === "string")
    : [];
  return (
    <li className="px-4 py-3 flex items-start gap-3">
      <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-surface-100 text-xs font-mono text-gray-600">
        {step.seq + 1}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium text-gray-900">{step.title || step.capability_key}</span>
          <code className="text-[11px] text-caliber-purple bg-violet-50 px-1.5 py-0.5 rounded">
            {step.capability_key}
          </code>
        </div>
        {step.depends_on.length > 0 && (
          <div className="text-xs text-gray-400 mt-0.5">
            depends on {step.depends_on.length} prior step(s)
          </div>
        )}
        {requiredInputs.length > 0 && (
          <div className="text-xs text-gray-400 mt-0.5">
            inputs: {requiredInputs.join(", ")}
          </div>
        )}
        {step.error && <div className="text-xs text-red-600 mt-0.5">{step.error}</div>}
      </div>
      <StepStatusBadge status={step.status} />
    </li>
  );
}

type JsonSchemaProperty = {
  type?: string;
  title?: string;
  description?: string;
  enum?: unknown[];
};

type InputInteractionEvidence = {
  input_schema?: {
    properties?: Record<string, JsonSchemaProperty>;
    required?: string[];
  };
  current_inputs?: Record<string, unknown>;
  missing?: string[];
};

function isStepReference(value: unknown): boolean {
  return Boolean(
    value &&
      typeof value === "object" &&
      "$from_step" in (value as Record<string, unknown>),
  );
}

function initialInputValue(value: unknown, schema: JsonSchemaProperty): string | boolean {
  if (schema.type === "boolean") return value === true;
  if (value === undefined || value === null) return "";
  if (schema.type === "array" || schema.type === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function parseInputValue(
  name: string,
  value: string | boolean,
  schema: JsonSchemaProperty,
): unknown {
  if (schema.type === "boolean") return Boolean(value);
  const text = String(value).trim();
  if (schema.type === "integer") {
    const parsed = Number(text);
    if (!Number.isInteger(parsed)) throw new Error(`${name} must be an integer.`);
    return parsed;
  }
  if (schema.type === "number") {
    const parsed = Number(text);
    if (!Number.isFinite(parsed)) throw new Error(`${name} must be a number.`);
    return parsed;
  }
  if (schema.type === "array" || schema.type === "object") {
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`${name} must be valid JSON.`);
    }
  }
  return text;
}

/**
 * A mid-run pause rendered as an actionable prompt. Handles the kinds the
 * executor raises today — `permission`/`confirm` (approve/deny, with accept
 * /reject labels for a below-gate confirm) and `choice` (one button per option).
 */
export function PlanInteractionPrompt({
  interaction,
  pending,
  onAnswer,
}: {
  interaction: AriaInteraction;
  pending: boolean;
  onAnswer: (payload: AriaInteractionAnswerPayload) => void;
}): JSX.Element {
  const isConfirm = interaction.kind === "confirm";
  const isInput = interaction.kind === "input";
  const hasOptions = interaction.kind === "choice" && interaction.options.length > 0;
  const ev = interaction.evidence as { metric?: string; min?: number; value?: unknown };
  const hasGateEvidence = isConfirm && typeof ev.metric === "string" && ev.metric.length > 0;
  const inputEvidence = interaction.evidence as InputInteractionEvidence;
  const inputProperties = useMemo(
    () => inputEvidence.input_schema?.properties ?? {},
    [inputEvidence.input_schema?.properties],
  );
  const inputRequired = useMemo(
    () => new Set(inputEvidence.input_schema?.required ?? []),
    [inputEvidence.input_schema?.required],
  );
  const editableInputNames = useMemo(
    () =>
      Object.keys(inputProperties).filter(
        (name) => !isStepReference(inputEvidence.current_inputs?.[name]),
      ),
    [inputEvidence.current_inputs, inputProperties],
  );
  const [inputValues, setInputValues] = useState<Record<string, string | boolean>>({});
  const [inputError, setInputError] = useState<string | null>(null);

  useEffect(() => {
    const next: Record<string, string | boolean> = {};
    for (const name of editableInputNames) {
      next[name] = initialInputValue(
        inputEvidence.current_inputs?.[name],
        inputProperties[name] ?? {},
      );
    }
    setInputValues(next);
    setInputError(null);
  }, [editableInputNames, inputEvidence.current_inputs, inputProperties, interaction.interaction_id]);

  const submitInputs = (): void => {
    try {
      const inputs: Record<string, unknown> = {};
      for (const name of editableInputNames) {
        const schema = inputProperties[name] ?? {};
        const value = inputValues[name] ?? "";
        if (schema.type !== "boolean" && String(value).trim() === "") {
          if (inputRequired.has(name)) throw new Error(`${name} is required.`);
          continue;
        }
        inputs[name] = parseInputValue(name, value, schema);
      }
      setInputError(null);
      onAnswer({ inputs });
    } catch (error) {
      setInputError(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="rounded-lg border border-violet-200 bg-violet-50 px-4 py-3">
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-caliber-purple">
        {isInput
          ? "Aria needs information"
          : isConfirm
            ? "Aria needs you to confirm"
            : "Aria needs your approval"}
        {interaction.required_scope && (
          <span className="font-mono normal-case text-[10px] bg-white/70 px-1.5 py-0.5 rounded">
            requires {interaction.required_scope}
          </span>
        )}
      </div>
      <p className="mt-1 text-sm text-gray-800">{interaction.prompt}</p>

      {/* Why it's asking — make the stakes legible, not just "Approve/Deny". */}
      {hasGateEvidence && (
        <div className="mt-2 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-800">
          <span className="font-semibold">Below quality gate.</span>{" "}
          <span className="font-mono">
            {ev.metric}: {String(ev.value)}
          </span>{" "}
          (needs ≥ {String(ev.min)}). Accepting keeps the result; rejecting skips the step.
        </div>
      )}
      {!isConfirm && interaction.required_scope && (
        <p className="mt-1.5 text-xs text-gray-500">
          Separation of duties — a different person with{" "}
          <span className="font-mono">{interaction.required_scope}</span> authority must
          approve this; you can&rsquo;t approve your own request.
        </p>
      )}

      {isInput && (
        <div className="mt-3 space-y-3">
          {editableInputNames.map((name) => {
            const schema = inputProperties[name] ?? {};
            const value = inputValues[name] ?? "";
            return (
              <label key={name} className="block text-xs font-medium text-gray-700">
                <span>
                  {schema.title || name.replace(/_/g, " ")}
                  {inputRequired.has(name) && <span className="text-red-500"> *</span>}
                </span>
                {schema.description && (
                  <span className="ml-1 font-normal text-gray-400">{schema.description}</span>
                )}
                {Array.isArray(schema.enum) ? (
                  <select
                    className="mt-1 block w-full rounded-md border border-violet-200 bg-white px-2.5 py-1.5 text-sm"
                    value={String(value)}
                    onChange={(event) =>
                      setInputValues((current) => ({ ...current, [name]: event.target.value }))
                    }
                  >
                    <option value="">Select…</option>
                    {schema.enum.map((option) => (
                      <option key={String(option)} value={String(option)}>
                        {String(option)}
                      </option>
                    ))}
                  </select>
                ) : schema.type === "boolean" ? (
                  <input
                    className="ml-2 align-middle"
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(event) =>
                      setInputValues((current) => ({
                        ...current,
                        [name]: event.target.checked,
                      }))
                    }
                  />
                ) : schema.type === "array" || schema.type === "object" ? (
                  <textarea
                    className="mt-1 block min-h-20 w-full rounded-md border border-violet-200 bg-white px-2.5 py-1.5 font-mono text-xs"
                    value={String(value)}
                    placeholder={schema.type === "array" ? '["item"]' : '{"key":"value"}'}
                    onChange={(event) =>
                      setInputValues((current) => ({ ...current, [name]: event.target.value }))
                    }
                  />
                ) : (
                  <input
                    className="mt-1 block w-full rounded-md border border-violet-200 bg-white px-2.5 py-1.5 text-sm"
                    type={schema.type === "number" || schema.type === "integer" ? "number" : "text"}
                    value={String(value)}
                    onChange={(event) =>
                      setInputValues((current) => ({ ...current, [name]: event.target.value }))
                    }
                  />
                )}
              </label>
            );
          })}
          {inputError && <p className="text-xs text-red-600">{inputError}</p>}
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={pending || editableInputNames.length === 0}
              onClick={submitInputs}
              className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
            >
              Continue plan
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ approved: false })}
              className="text-sm font-medium text-gray-700 bg-white border border-surface-200 px-3 py-1.5 rounded-md hover:bg-surface-50 disabled:opacity-50"
            >
              Skip step
            </button>
          </div>
        </div>
      )}

      {!isInput && <div className="mt-3 flex flex-wrap gap-2">
        {hasOptions ? (
          interaction.options.map((opt, i) => (
            <button
              key={i}
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ value: opt.value, choice: opt.label })}
              className="text-sm font-medium text-white bg-caliber-purple px-3 py-1.5 rounded-md hover:bg-caliber-purple-dark disabled:opacity-50"
            >
              {opt.label}
            </button>
          ))
        ) : (
          <>
            <button
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ approved: true })}
              className="text-sm font-medium text-white bg-emerald-600 px-3 py-1.5 rounded-md hover:bg-emerald-700 disabled:opacity-50"
            >
              {isConfirm ? "Accept" : "Approve"}
            </button>
            <button
              type="button"
              disabled={pending}
              onClick={() => onAnswer({ approved: false })}
              className="text-sm font-medium text-gray-700 bg-white border border-surface-200 px-3 py-1.5 rounded-md hover:bg-surface-50 disabled:opacity-50"
            >
              {isConfirm ? "Reject" : "Deny"}
            </button>
          </>
        )}
      </div>}
    </div>
  );
}
