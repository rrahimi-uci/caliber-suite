import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { BarChart3, ExternalLink, KeyRound, RefreshCw, Rocket, Server } from "lucide-react";

import { caliberApi } from "@/api/caliberApi";
import type { AssistantConfig, SkillRuntimeMode } from "@/api/assistantTypes";
import type { LlmSetupStatus, LlmSetupUpdate, SystemService } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { PageTabs, type PageTab } from "@/components/PageTabs";
import {
  ASSISTANT_REASONING_OPTIONS,
  normalizeAssistantReasoningValue,
} from "@/components/assistant/assistantConfigUi";
import { useApiMutation, useApiQuery, useInvalidate } from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import {
  readDefaultAssistantSkillMode,
  writeDefaultAssistantSkillMode,
} from "@/lib/assistantPreferences";

const TABS: PageTab[] = [
  { key: "assistant", label: "Aria" },
  { key: "providers", label: "Providers", icon: <KeyRound className="h-4 w-4" /> },
  { key: "services", label: "Services", icon: <Server className="h-4 w-4" /> },
  { key: "versioning", label: "Versioning", icon: <Rocket className="h-4 w-4" /> },
  { key: "allure", label: "Allure Report", icon: <BarChart3 className="h-4 w-4" /> },
];

// Shared content width so every tab lines up and matches as you switch tabs.
// Full page width + fluid (cards/inputs flex with the viewport).
const TAB_WIDTH = "w-full";

// Where the rendered Allure report is served (per-browser preference). The
// report itself is produced by the test tooling (`make allure`); this just
// remembers the URL so you can jump to it from Settings.
const ALLURE_URL_KEY = "caliber.allure.reportUrl";
// Default to the report CALIBER serves in-app (no separate Allure server needed
// once `make allure-report` has generated it). Override to point elsewhere
// (e.g. an allure-docker-service) is still honoured + persisted.
const defaultAllureUrl = (): string => caliberApi.allureReportUrl();

function readAllureUrl(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(ALLURE_URL_KEY) ?? "";
  } catch {
    return "";
  }
}

function writeAllureUrl(url: string): void {
  try {
    if (url) window.localStorage.setItem(ALLURE_URL_KEY, url);
    else window.localStorage.removeItem(ALLURE_URL_KEY);
  } catch {
    // Ignore storage failures; the link still works for this page load.
  }
}

function normalizeAllureUrl(rawUrl: string): string {
  const value = rawUrl.trim();
  if (!value) return "";

  const candidates: string[] = [value];
  if (value.startsWith("//")) {
    candidates.push(`${window.location.protocol}${value}`);
  } else if (value.startsWith("/")) {
    candidates.push(`${window.location.origin}${value}`);
  } else if (!/^[a-z][a-z\d+\-.]*:\/\//i.test(value)) {
    candidates.push(`http://${value}`);
  }

  for (const candidate of candidates) {
    try {
      const parsed = new URL(candidate);
      if (parsed.protocol === "http:" || parsed.protocol === "https:") {
        return parsed.toString();
      }
    } catch {
      // Try the next candidate.
    }
  }
  return "";
}

const QK = {
  assistantConfig: ["assistant", "config"] as const,
  llmSetup: ["settings", "llm"] as const,
};

const SKILL_MODE_OPTIONS: Array<{ value: SkillRuntimeMode; label: string; help: string }> = [
  { value: "auto", label: "Auto", help: "Aria selects skills from the job and prompt." },
  { value: "manual", label: "Manual", help: "Aria only uses skills you pin into a session." },
  { value: "off", label: "Off", help: "Aria runs without CALIBER skill injection." },
];

export function Settings(): JSX.Element {
  const [activeTab, setActiveTab] = useState("assistant");
  const { data: assistantConfig, isLoading: assistantLoading } = useApiQuery<AssistantConfig>(
    QK.assistantConfig,
    () => caliberApi.getAssistantConfig(),
  );
  const { data: llmSetup, isLoading: llmLoading } = useApiQuery<LlmSetupStatus>(
    QK.llmSetup,
    (signal) => caliberApi.getLlmSetup(signal),
  );
  const invalidate = useInvalidate();

  const assistantMutation = useApiMutation(
    async (payload: { model?: string; reasoning?: string }) =>
      caliberApi.updateAssistantConfig(payload),
    {
      onSuccess() {
        void invalidate(QK.assistantConfig);
        showToast.success("Assistant settings saved");
      },
      onError(error) {
        showToast.error(error.message || "Failed to save assistant settings");
      },
    },
  );

  const providerMutation = useApiMutation(
    async (payload: LlmSetupUpdate) => caliberApi.updateLlmSetup(payload),
    {
      onSuccess() {
        void invalidate(QK.llmSetup);
        showToast.success("Provider settings saved");
      },
      onError(error) {
        showToast.error(error.message || "Failed to save provider settings");
      },
    },
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Settings"
        subtitle="Manage assistant defaults and live provider credentials without leaving the workspace."
      />
      <PageTabs tabs={TABS} active={activeTab} onChange={setActiveTab} />
      {activeTab === "assistant" && (
        <AssistantSettingsTab
          config={assistantConfig ?? null}
          isLoading={assistantLoading}
          isSaving={assistantMutation.isPending}
          onSave={(payload) => assistantMutation.mutate(payload)}
        />
      )}
      {activeTab === "providers" && (
        <ProviderSettingsTab
          setup={llmSetup ?? null}
          isLoading={llmLoading}
          isSaving={providerMutation.isPending}
          // Async so the tab can clear the entered key only after the write
          // actually lands — a failed save must not silently discard it.
          onSave={(payload) => providerMutation.mutateAsync(payload)}
        />
      )}
      {activeTab === "services" && <ServicesTab />}
      {activeTab === "versioning" && <VersioningSettingsTab />}
      {activeTab === "allure" && <AllureReportTab />}
    </div>
  );
}

function HealthDot({ healthy }: { healthy: boolean | null }): JSX.Element {
  const cls =
    healthy === true
      ? "bg-emerald-500"
      : healthy === false
        ? "bg-red-500"
        : "bg-slate-300";
  const label = healthy === true ? "Healthy" : healthy === false ? "Down" : "Unknown";
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`h-2.5 w-2.5 rounded-full ${cls}`} aria-hidden="true" />
      <span
        className={
          healthy === true
            ? "text-emerald-700"
            : healthy === false
              ? "text-red-700"
              : "text-slate-500"
        }
      >
        {label}
      </span>
    </span>
  );
}

function VersioningSettingsTab(): JSX.Element {
  const query = useApiQuery(["runtime-settings"], (s) => caliberApi.getRuntimeConfiguration(s));
  const group = query.data?.groups.find((g) => g.id === "versioning");

  return (
    <div className={`${TAB_WIDTH} space-y-4`} data-testid="versioning-settings-tab">
      <div className="rounded-lg border border-surface-200 bg-white p-4">
        <h2 className="text-base font-semibold text-gray-900">Versioning &amp; Releases</h2>
        <p className="mt-1 text-sm text-gray-600">
          Policy that governs promotion across artifacts. See the{" "}
          <Link to="/releases" className="text-caliber-purple hover:underline">
            Releases page
          </Link>{" "}
          for what&apos;s live and the promotion/rollback timeline.
        </p>
      </div>

      {query.isLoading && <div className="text-sm text-gray-400">Loading…</div>}
      {group && (
        <div className="rounded-lg border border-surface-200 bg-white p-4">
          <p className="mb-3 text-sm text-gray-500">{group.description}</p>
          <div className="space-y-3">
            {group.settings.map((setting) => (
              <div
                key={setting.key}
                data-testid={`versioning-setting-${setting.key}`}
                className="flex items-baseline justify-between gap-4 border-b border-surface-100 pb-2 last:border-0"
              >
                <div>
                  <div className="text-sm font-medium text-gray-800">{setting.label}</div>
                  <div className="text-xs text-gray-500">{setting.description}</div>
                </div>
                <span className="font-mono text-sm text-gray-900">{setting.display_value}</span>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs text-gray-400">
            Workflow-run retention is configured under the Operations group
            (<span className="font-mono">CALIBER_WORKFLOW_RUN_RETENTION_DAYS</span>).
          </p>
        </div>
      )}
    </div>
  );
}


function ServicesTab(): JSX.Element {
  const query = useApiQuery(["system", "services"], (signal) =>
    caliberApi.getSystemServices(signal),
  );
  const services: SystemService[] = query.data?.services ?? [];
  const checkedAt = query.data?.checked_at_ms
    ? new Date(query.data.checked_at_ms).toLocaleTimeString()
    : null;

  return (
    <div className={`${TAB_WIDTH} space-y-4`}>
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">Backing services</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Live health of the services CALIBER runs on. Probed server-side over the
            internal network{checkedAt ? ` · last checked ${checkedAt}` : ""}.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
          className="inline-flex items-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-600 transition hover:border-slate-300 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${query.isFetching ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {query.error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {query.error.message}
        </div>
      )}

      {query.isLoading && !query.data ? (
        <div className="px-2 py-12 text-center text-sm text-slate-400">Checking services…</div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white" data-testid="services-table">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <th className="px-4 py-3 text-left font-medium">Service</th>
                <th className="px-4 py-3 text-left font-medium">Health</th>
                <th className="px-4 py-3 text-left font-medium">Endpoint</th>
                <th className="px-4 py-3 text-left font-medium">Detail</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {services.map((s) => (
                <tr key={s.key} className="hover:bg-slate-50" data-testid="service-row">
                  <td className="px-4 py-3">
                    <div className="font-medium text-slate-900">{s.name}</div>
                    <div className="mt-0.5 text-xs text-slate-500">{s.description}</div>
                  </td>
                  <td className="px-4 py-3">
                    <HealthDot healthy={s.healthy} />
                  </td>
                  <td className="px-4 py-3">
                    {s.url ? (
                      <a
                        href={s.url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 font-mono text-xs text-caliber-700 hover:underline"
                      >
                        {s.url}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                    ) : (
                      <span className="font-mono text-xs text-slate-500">{s.target || "—"}</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-500">
                    {s.detail}
                    {s.latency_ms !== null ? (
                      <span className="text-slate-400"> · {s.latency_ms}ms</span>
                    ) : null}
                  </td>
                </tr>
              ))}
              {services.length === 0 && !query.isLoading && (
                <tr>
                  <td colSpan={4} className="px-4 py-10 text-center text-sm text-slate-500">
                    No services reported.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function AllureReportTab(): JSX.Element {
  const [url, setUrl] = useState(() => readAllureUrl() || defaultAllureUrl());
  const trimmed = url.trim();
  const normalizedUrl = normalizeAllureUrl(trimmed);

  useEffect(() => {
    writeAllureUrl(trimmed);
  }, [trimmed]);

  return (
    <div className={`${TAB_WIDTH} space-y-4`}>
      <section className="card p-6 space-y-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
            Test reporting
          </p>
          <h2 className="mt-2 text-xl font-semibold text-slate-900">Allure Report</h2>
          <p className="mt-1 text-sm leading-6 text-slate-500">
            Allure is the rich HTML report for the backend (pytest), frontend unit
            (vitest), and end-to-end (playwright) suites. Point this at wherever
            your report is served and open it in one click.
          </p>
        </div>

        <label className="block space-y-2">
          <span className="text-sm font-medium text-slate-700">Allure report URL</span>
          <input
            aria-label="Allure report URL"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="http://localhost:5252"
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          />
          <span className="block text-xs text-slate-400">
            Defaults to the report CALIBER serves in-app — run{" "}
            <code>make allure-report</code> to generate it, then click Open (no
            separate server needed). Edit to point elsewhere (e.g. an
            allure-docker-service or <code>npm run allure:serve</code> on :5252);
            saved in this browser.
          </span>
        </label>

        <div className="flex items-center gap-3">
          {normalizedUrl ? (
            <a
              href={normalizedUrl}
              target="_blank"
              rel="noreferrer noopener"
              className="inline-flex items-center gap-2 rounded-xl bg-caliber-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-caliber-700"
            >
              <ExternalLink className="h-4 w-4" />
              Open Allure report
            </a>
          ) : (
            <button
              type="button"
              disabled
              className="inline-flex cursor-not-allowed items-center gap-2 rounded-xl bg-slate-200 px-4 py-2 text-sm font-semibold text-slate-400"
            >
              <ExternalLink className="h-4 w-4" />
              Open Allure report
            </button>
          )}
          <a
            href="https://allurereport.org/"
            target="_blank"
            rel="noreferrer noopener"
            className="text-sm font-medium text-caliber-700 hover:text-caliber-800"
          >
            About Allure
          </a>
        </div>
      </section>

      <section className="card p-6 space-y-3">
        <h3 className="text-sm font-semibold text-slate-800">Generate the report</h3>
        <p className="text-sm leading-6 text-slate-500">
          Emitting results needs no Java; rendering uses a local JRE or falls back
          to Docker.
        </p>
        <pre className="overflow-auto rounded-xl border border-slate-200/70 bg-slate-950 px-4 py-3 text-xs leading-relaxed text-slate-100">
{`# one command: run every suite + build the combined report CALIBER serves
make allure-report

# …or step by step (each suite emits Allure results):
cd caliber            && make test-allure       # backend (pytest)
cd caliber/caliber-ui && npm test               # frontend unit (vitest)
cd caliber/caliber-ui && npm run test:e2e       # e2e (playwright)
cd caliber/caliber-ui && npm run allure:generate:all

# alternative: serve a live report via Java on http://localhost:5252
make allure`}
        </pre>
      </section>
    </div>
  );
}

function AssistantSettingsTab({
  config,
  isLoading,
  isSaving,
  onSave,
}: {
  config: AssistantConfig | null;
  isLoading: boolean;
  isSaving: boolean;
  onSave: (payload: { model?: string; reasoning?: string }) => void;
}): JSX.Element {
  const [model, setModel] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [defaultSkillMode, setDefaultSkillMode] = useState<SkillRuntimeMode>(() =>
    readDefaultAssistantSkillMode(),
  );

  useEffect(() => {
    if (!config) return;
    setModel(config.model);
    setReasoning(normalizeAssistantReasoningValue(config.reasoning));
  }, [config]);

  // Group the available models by provider so Ollama models the server
  // discovered (via /api/tags) appear under their own labelled section.
  const modelGroups = useMemo(() => {
    const order: string[] = [];
    const byProvider = new Map<string, AssistantConfig["available_models"]>();
    for (const option of config?.available_models ?? []) {
      if (!byProvider.has(option.provider)) {
        byProvider.set(option.provider, []);
        order.push(option.provider);
      }
      byProvider.get(option.provider)!.push(option);
    }
    return order.map((provider) => ({ provider, options: byProvider.get(provider)! }));
  }, [config]);

  const hasChanges =
    (config?.model ?? "") !== model ||
    normalizeAssistantReasoningValue(config?.reasoning) !== reasoning ||
    readDefaultAssistantSkillMode() !== defaultSkillMode;

  const handleSave = (): void => {
    writeDefaultAssistantSkillMode(defaultSkillMode);
    onSave({ model, reasoning });
  };

  const reset = (): void => {
    if (!config) return;
    setModel(config.model);
    setReasoning(normalizeAssistantReasoningValue(config.reasoning));
    setDefaultSkillMode(readDefaultAssistantSkillMode());
  };

  if (isLoading && !config) {
    return <LoadingCard text="Loading assistant settings…" />;
  }

  const providerLabel = (provider: string): string =>
    provider === "openai"
      ? "OpenAI"
      : provider === "anthropic"
        ? "Anthropic"
        : provider === "ollama"
          ? "Ollama (local)"
          : provider;

  return (
    <div className={`${TAB_WIDTH} space-y-6`}>
      <section>
        <div className="card p-6 space-y-5">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
              Aria defaults
            </p>
            <h2 className="mt-2 text-xl font-semibold text-slate-900">
              Configure how Aria starts new work
            </h2>
            <p className="mt-1 text-sm leading-6 text-slate-500">
              Model and rollout controls apply immediately. Default skill mode is a UI preference
              used when a new Aria session is created.
            </p>
          </div>

          <label className="block space-y-2">
            <span className="text-sm font-medium text-slate-700">Model</span>
            <select
              aria-label="Model"
              value={model}
              onChange={(event) => setModel(event.target.value)}
              className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
            >
              {modelGroups.map((group) => (
                <optgroup key={group.provider} label={providerLabel(group.provider)}>
                  {group.options.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>
            <span className="block text-xs text-slate-400">
              Ollama models you have pulled locally appear here automatically when
              the Ollama server is reachable.
            </span>
          </label>

          <div className="space-y-2">
            <span className="text-sm font-medium text-slate-700">Reasoning effort</span>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {ASSISTANT_REASONING_OPTIONS.map((option) => {
                const active = reasoning === option.value;
                return (
                  <button
                    key={option.value || "default"}
                    type="button"
                    aria-label={option.label}
                    onClick={() => setReasoning(option.value)}
                    className={`rounded-xl border px-3 py-3 text-left text-sm transition ${
                      active
                        ? "border-caliber-500 bg-caliber-50 text-caliber-800"
                        : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                    }`}
                  >
                    <span className="block font-medium">{option.label}</span>
                    <span className="mt-1 block text-xs leading-5 text-slate-400">
                      {option.description}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="space-y-2">
            <span className="text-sm font-medium text-slate-700">Default skill mode</span>
            <div className="grid gap-2 sm:grid-cols-3">
              {SKILL_MODE_OPTIONS.map((option) => {
                const active = defaultSkillMode === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    aria-label={option.label}
                    onClick={() => setDefaultSkillMode(option.value)}
                    className={`rounded-xl border px-3 py-3 text-left transition ${
                      active
                        ? "border-caliber-500 bg-caliber-50 text-caliber-800"
                        : "border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                    }`}
                  >
                    <span className="block text-sm font-medium">{option.label}</span>
                    <span className="mt-1 block text-xs leading-5 text-slate-400">{option.help}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </section>

      <div className="flex items-center justify-end gap-3">
        <button
          type="button"
          onClick={reset}
          disabled={isSaving || !hasChanges}
          className="rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reset
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={isSaving || !model || !hasChanges}
          className="rounded-xl bg-caliber-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSaving ? "Saving…" : "Save assistant settings"}
        </button>
      </div>
    </div>
  );
}

function ProviderSettingsTab({
  setup,
  isLoading,
  isSaving,
  onSave,
}: {
  setup: LlmSetupStatus | null;
  isLoading: boolean;
  isSaving: boolean;
  onSave: (payload: LlmSetupUpdate) => Promise<unknown>;
}): JSX.Element {
  const [openaiKey, setOpenaiKey] = useState("");
  const [anthropicKey, setAnthropicKey] = useState("");
  const [gatewayUrl, setGatewayUrl] = useState("");

  // Key fields are write-only: the API returns presence + a masked fingerprint,
  // never the resolved secret, so there is nothing to prefill. Leaving a field
  // blank keeps the currently configured key.
  useEffect(() => {
    if (!setup) return;
    setGatewayUrl(setup.gateway_url ?? "");
  }, [setup]);

  const hasChanges =
    openaiKey.trim() !== "" ||
    anthropicKey.trim() !== "" ||
    gatewayUrl !== (setup?.gateway_url ?? "");

  const handleSave = (): void => {
    const payload: LlmSetupUpdate = {};
    if (openaiKey.trim() !== "") payload.openai_api_key = openaiKey.trim();
    if (anthropicKey.trim() !== "") payload.anthropic_api_key = anthropicKey.trim();
    if (gatewayUrl !== (setup?.gateway_url ?? "")) payload.gateway_url = gatewayUrl;
    void onSave(payload).then(
      () => {
        // Don't leave the entered secret sitting in component state (and so in
        // the DOM) once it's safely stored. Only on success — a failed save
        // must not discard what the operator typed.
        setOpenaiKey("");
        setAnthropicKey("");
      },
      () => {
        // The mutation's onError already surfaces a toast; keep the values so
        // the operator can retry.
      },
    );
  };

  const keyHint = (present: boolean, fingerprint: string): string =>
    present
      ? `Configured${fingerprint ? ` (${fingerprint})` : ""} — leave blank to keep it.`
      : "Not configured.";

  if (isLoading && !setup) {
    return <LoadingCard text="Loading provider settings…" />;
  }

  return (
    <div className={TAB_WIDTH}>
      <section className="card p-6 space-y-5">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
            Provider credentials
          </p>
          <h2 className="mt-2 text-xl font-semibold text-slate-900">Live API keys</h2>
          <p className="mt-1 text-sm leading-6 text-slate-500">
            Keys are write-only: CALIBER reports whether one is configured and its last four
            characters, never the value. Entering a key writes a runtime override on the running
            server; the durable source stays your env / .env.
          </p>
        </div>

        <label className="block space-y-2">
          <span className="text-sm font-medium text-slate-700">OpenAI API key</span>
          <input
            aria-label="OpenAI API key"
            type="password"
            autoComplete="off"
            value={openaiKey}
            onChange={(event) => setOpenaiKey(event.target.value)}
            placeholder="sk-..."
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          />
          <span className="block text-xs text-slate-400" data-testid="openai-key-hint">
            {keyHint(setup?.openai_key_present ?? false, setup?.openai_key_fingerprint ?? "")}
          </span>
        </label>

        <label className="block space-y-2">
          <span className="text-sm font-medium text-slate-700">Anthropic API key</span>
          <input
            aria-label="Anthropic API key"
            type="password"
            autoComplete="off"
            value={anthropicKey}
            onChange={(event) => setAnthropicKey(event.target.value)}
            placeholder="sk-ant-..."
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          />
          <span className="block text-xs text-slate-400" data-testid="anthropic-key-hint">
            {keyHint(
              setup?.anthropic_key_present ?? false,
              setup?.anthropic_key_fingerprint ?? "",
            )}
          </span>
        </label>

        <label className="block space-y-2">
          <span className="text-sm font-medium text-slate-700">Gateway URL</span>
          <input
            aria-label="Gateway URL"
            value={gatewayUrl}
            onChange={(event) => setGatewayUrl(event.target.value)}
            placeholder="http://127.0.0.1:5000/gateway/mlflow/v1"
            className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none focus:border-caliber-400 focus:ring-2 focus:ring-caliber-400/20"
          />
          <span className="block text-xs text-slate-400">
            Leave blank to route provider calls directly instead of through the MLflow gateway.
          </span>
        </label>

        <div className="flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={() => {
              setOpenaiKey("");
              setAnthropicKey("");
              setGatewayUrl(setup?.gateway_url ?? "");
            }}
            disabled={isSaving || !hasChanges}
            className="rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-600 transition hover:border-slate-300 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Reset
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={isSaving || !hasChanges}
            className="rounded-xl bg-caliber-600 px-4 py-2 text-sm font-semibold text-white transition hover:bg-caliber-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSaving ? "Saving…" : "Save provider settings"}
          </button>
        </div>
      </section>
    </div>
  );
}

function LoadingCard({ text }: { text: string }): JSX.Element {
  return <div className="card p-6 text-sm text-slate-500">{text}</div>;
}
