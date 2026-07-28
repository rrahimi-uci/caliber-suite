import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { AgentConfig, AgentRegisterPayload } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import { SearchInput } from "@/components/SearchInput";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import { relativeTime } from "@/lib/time";

interface AgentDraft {
  agent_id: string;
  experiment_id: string;
  name: string;
  artifact_types: string;
  skills: string;
  optimize_for: string;
  required_approvals: string;
}

const EMPTY_DRAFT: AgentDraft = {
  agent_id: "",
  experiment_id: "",
  name: "",
  artifact_types: "prompt",
  skills: "",
  optimize_for: "quality",
  required_approvals: "1",
};

function splitValues(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function Agents(): JSX.Element {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [draft, setDraft] = useState<AgentDraft>(EMPTY_DRAFT);

  const query = useApiQuery(["agents"], (signal) =>
    caliberApi.listAgents(signal),
  );
  const meQuery = useApiQuery(["me"], (signal) => caliberApi.getMe(signal));
  const isAdmin = meQuery.data?.is_admin ?? false;
  const createMutation = useApiMutation(
    (payload: AgentRegisterPayload) => caliberApi.registerAgent(payload),
    {
      onSuccess: async (agent) => {
        await invalidate(["agents"]);
        showToast.success(`Registered "${agent.name}"`);
        navigate(`/agents/${encodeURIComponent(agent.agent_id)}`);
      },
      onError: (error) =>
        showToast.error(`Could not register agent: ${error.message}`),
    },
  );

  const allAgents = query.data ?? [];
  const needle = search.trim().toLowerCase();
  const agents = allAgents.filter((agent) =>
    !needle
      ? true
      : [agent.name, agent.agent_id, agent.experiment_id, agent.owner]
          .join(" ")
          .toLowerCase()
          .includes(needle),
  );

  const updateDraft = (field: keyof AgentDraft, value: string): void => {
    setDraft((current) => ({ ...current, [field]: value }));
  };

  const submit = (): void => {
    const requiredApprovals = Number(draft.required_approvals);
    if (
      !draft.agent_id.trim() ||
      !draft.experiment_id.trim() ||
      !draft.name.trim()
    )
      return;
    createMutation.mutate({
      agent_id: draft.agent_id.trim(),
      experiment_id: draft.experiment_id.trim(),
      name: draft.name.trim(),
      artifact_types: splitValues(draft.artifact_types),
      optimizer_config: { skills: splitValues(draft.skills) },
      optimize_for: draft.optimize_for.trim() || "quality",
      required_approvals: Number.isFinite(requiredApprovals)
        ? Math.max(1, Math.floor(requiredApprovals))
        : 1,
    });
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="Agents"
        subtitle="Configure agents managed by CALIBER's refinement pipeline"
        actions={
          isAdmin ? (
            <button
              type="button"
              data-testid="new-agent"
              className="btn-primary"
              onClick={() => setShowCreate((open) => !open)}
            >
              New agent configuration
            </button>
          ) : null
        }
      />

      <div className="rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-xs leading-relaxed text-sky-800">
        These records bind an MLflow experiment to CALIBER evaluation and
        refinement settings. Runtime model, prompt, and orchestration behavior
        remains versioned in workflow manifests.
      </div>

      {!meQuery.isLoading && !isAdmin && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-xs leading-relaxed text-slate-600">
          Agent configurations are read-only for your account. An administrator
          can register or change refinement-fleet records.
        </div>
      )}

      {showCreate && (
        <form
          data-testid="agent-create-form"
          className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <label className="text-xs font-semibold text-slate-700">
              Agent ID
              <input
                className="form-input mt-1.5 w-full"
                value={draft.agent_id}
                onChange={(event) =>
                  updateDraft("agent_id", event.target.value)
                }
                placeholder="support-agent"
                required
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              MLflow experiment ID
              <input
                className="form-input mt-1.5 w-full"
                value={draft.experiment_id}
                onChange={(event) =>
                  updateDraft("experiment_id", event.target.value)
                }
                placeholder="1234567890"
                required
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Display name
              <input
                className="form-input mt-1.5 w-full"
                value={draft.name}
                onChange={(event) => updateDraft("name", event.target.value)}
                placeholder="Support Agent"
                required
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Artifact types (comma-separated)
              <input
                className="form-input mt-1.5 w-full"
                value={draft.artifact_types}
                onChange={(event) =>
                  updateDraft("artifact_types", event.target.value)
                }
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Skills (registered names)
              <input
                className="form-input mt-1.5 w-full"
                value={draft.skills}
                onChange={(event) => updateDraft("skills", event.target.value)}
                placeholder="reasoning, tool-use"
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Required approvals
              <input
                className="form-input mt-1.5 w-full"
                type="number"
                min={1}
                value={draft.required_approvals}
                onChange={(event) =>
                  updateDraft("required_approvals", event.target.value)
                }
              />
            </label>
          </div>
          <div className="mt-5 flex justify-end gap-3">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setShowCreate(false)}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn-primary"
              disabled={createMutation.isPending}
            >
              {createMutation.isPending ? "Registering…" : "Register agent"}
            </button>
          </div>
        </form>
      )}

      <div className="max-w-md">
        <SearchInput
          value={search}
          onChange={setSearch}
          ariaLabel="Search agents"
          placeholder="Search agents…"
        />
      </div>

      {query.isLoading && (
        <p className="text-sm text-slate-500">Loading agents…</p>
      )}
      {query.error && (
        <p
          role="alert"
          className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700"
        >
          {query.error.message}
        </p>
      )}
      {!query.isLoading && !query.error && agents.length === 0 && (
        <div className="rounded-2xl border-2 border-dashed border-slate-200 px-8 py-12 text-center">
          <p className="text-sm font-semibold text-slate-700">
            {allAgents.length === 0
              ? "No agent configurations yet"
              : "No agents match your search"}
          </p>
        </div>
      )}

      {agents.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {agents.map((agent: AgentConfig) => (
            <button
              key={agent.agent_id}
              type="button"
              data-testid={`agent-card-${agent.agent_id}`}
              className="rounded-2xl border border-slate-200 bg-white p-5 text-left shadow-card transition hover:-translate-y-0.5 hover:shadow-card-hover"
              onClick={() =>
                navigate(`/agents/${encodeURIComponent(agent.agent_id)}`)
              }
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate text-sm font-bold text-slate-900">
                    {agent.name}
                  </h2>
                  <p className="mt-1 truncate font-mono text-[11px] text-slate-400">
                    {agent.agent_id}
                  </p>
                </div>
                <span
                  className={`rounded-full px-2 py-1 text-[10px] font-semibold ${
                    agent.enabled
                      ? "bg-emerald-100 text-emerald-700"
                      : "bg-slate-100 text-slate-600"
                  }`}
                >
                  {agent.enabled ? "enabled" : "disabled"}
                </span>
              </div>
              <dl className="mt-4 grid grid-cols-2 gap-3 text-xs">
                <div>
                  <dt className="text-slate-400">Experiment</dt>
                  <dd className="mt-0.5 truncate font-medium text-slate-700">
                    {agent.experiment_id}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-400">Optimize for</dt>
                  <dd className="mt-0.5 font-medium text-slate-700">
                    {agent.optimize_for}
                  </dd>
                </div>
              </dl>
              <p className="mt-4 border-t border-slate-100 pt-3 text-[11px] text-slate-400">
                Updated {relativeTime(agent.updated_at)} · {agent.owner}
              </p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
