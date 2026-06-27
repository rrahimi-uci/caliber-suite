/**
 * AssistantSettings — modal over the existing `/assistant/config` API exposing
 * Aria's runtime knobs (model, reasoning effort, disabled intents/domains).
 * Saving requires the operator scope; the backend rebuilds the engine.
 */

import { useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { AssistantConfig } from "@/api/assistantTypes";
import { useApiQuery, useApiMutation, useInvalidate } from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import {
  ASSISTANT_REASONING_OPTIONS,
  assistantProviderLabel,
  normalizeAssistantReasoningValue,
} from "./assistantConfigUi";

interface AssistantSettingsProps {
  onClose: () => void;
}

export function AssistantSettings({ onClose }: AssistantSettingsProps): JSX.Element {
  const invalidate = useInvalidate();
  const { data: config, isLoading } = useApiQuery<AssistantConfig>(
    ["assistant", "config"],
    () => caliberApi.getAssistantConfig(),
  );

  const [model, setModel] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [disabledIntents, setDisabledIntents] = useState("");
  const [disabledDomains, setDisabledDomains] = useState("");

  useEffect(() => {
    if (!config) return;
    setModel(config.model);
    setReasoning(normalizeAssistantReasoningValue(config.reasoning));
    setDisabledIntents((config.disabled_intents ?? []).join(", "));
    setDisabledDomains((config.disabled_domains ?? []).join(", "));
  }, [config]);

  const save = useApiMutation(
    () =>
      caliberApi.updateAssistantConfig({
        model,
        reasoning,
        disabled_intents: splitCsv(disabledIntents),
        disabled_domains: splitCsv(disabledDomains),
      }),
    {
      onSuccess() {
        invalidate(["assistant", "config"]);
        showToast.success("Aria settings saved");
        onClose();
      },
      onError(err) {
        showToast.error(err.message || "Failed to save settings");
      },
    },
  );

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/30 p-4"
      data-testid="assistant-settings"
    >
      <div className="w-full max-w-md rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-xl">
        <div className="flex items-center justify-between border-b border-slate-200 dark:border-slate-700 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-100">Aria settings</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close settings"
            className="p-1 rounded-md text-slate-400 hover:text-slate-600 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800"
          >
            <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        {isLoading || !config ? (
          <p className="p-4 text-sm text-slate-500 dark:text-slate-300">Loading…</p>
        ) : (
          <div className="space-y-4 p-4">
            <label className="block">
              <span className="text-xs font-medium text-slate-500 dark:text-slate-300">Model</span>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
              >
                {config.available_models.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.name} ({assistantProviderLabel(m.provider)})
                  </option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="text-xs font-medium text-slate-500 dark:text-slate-300">
                Reasoning effort
              </span>
              <select
                value={reasoning}
                onChange={(e) => setReasoning(e.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
              >
                {ASSISTANT_REASONING_OPTIONS.map((option) => (
                  <option key={option.value || "default"} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="block">
              <span className="text-xs font-medium text-slate-500 dark:text-slate-300">
                Disabled intents (comma-separated)
              </span>
              <input
                value={disabledIntents}
                onChange={(e) => setDisabledIntents(e.target.value)}
                placeholder="e.g. propose_promotion"
                className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
              />
            </label>

            <label className="block">
              <span className="text-xs font-medium text-slate-500 dark:text-slate-300">
                Disabled domains (comma-separated)
              </span>
              <input
                value={disabledDomains}
                onChange={(e) => setDisabledDomains(e.target.value)}
                placeholder="e.g. mcp_server"
                className="mt-1 w-full rounded-lg border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-800 dark:text-slate-100"
              />
            </label>

            <p className="text-[11px] text-slate-400 dark:text-slate-400">
              Engine: {config.engine} · Provider: {config.provider}
            </p>

            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg border border-slate-200 dark:border-slate-700 px-3 py-1.5 text-sm text-slate-600 dark:text-slate-200 hover:bg-slate-50 dark:hover:bg-slate-800"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => save.mutate(undefined)}
                disabled={save.isPending || !model}
                className="rounded-lg bg-caliber-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-caliber-700 disabled:opacity-50"
              >
                {save.isPending ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
