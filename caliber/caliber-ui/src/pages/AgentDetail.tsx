import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { caliberApi } from "@/api/caliberApi";
import type { AgentConfig, AgentUpdatePayload } from "@/api/types";
import { PageHeader } from "@/components/PageHeader";
import {
  useApiMutation,
  useApiQuery,
  useInvalidate,
} from "@/hooks/useApiQuery";
import { showToast } from "@/lib/toast";
import { relativeTime } from "@/lib/time";

interface AgentEditDraft {
  name: string;
  artifactTypes: string;
  skills: string;
  optimizeFor: string;
  collaborationMode: string;
  requiredApprovals: string;
  evalThresholds: string;
  optimizerConfig: string;
  approvalPolicy: string;
}

function splitValues(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function agentDraft(agent: AgentConfig): AgentEditDraft {
  const skills = Array.isArray(agent.optimizer_config.skills)
    ? agent.optimizer_config.skills.filter(
        (item): item is string => typeof item === "string",
      )
    : [];
  return {
    name: agent.name,
    artifactTypes: agent.artifact_types.join(", "),
    skills: skills.join(", "),
    optimizeFor: agent.optimize_for,
    collaborationMode: agent.collaboration_mode ?? "",
    requiredApprovals: String(agent.required_approvals),
    evalThresholds: JSON.stringify(agent.eval_thresholds, null, 2),
    optimizerConfig: JSON.stringify(agent.optimizer_config, null, 2),
    approvalPolicy: JSON.stringify(agent.approval_policy, null, 2),
  };
}

function parseObject(label: string, value: string): Record<string, unknown> {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label} must be a JSON object`);
  }
  return parsed as Record<string, unknown>;
}

export function AgentDetail(): JSX.Element {
  const { agentId = "" } = useParams();
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<AgentEditDraft | null>(null);
  const [formError, setFormError] = useState("");
  const [showPreflight, setShowPreflight] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const agentQuery = useApiQuery(["agents", agentId], (signal) =>
    caliberApi.getAgent(agentId, signal),
  );
  const meQuery = useApiQuery(["me"], (signal) => caliberApi.getMe(signal));
  const isAdmin = meQuery.data?.is_admin ?? false;
  const skillsQuery = useApiQuery(["agents", agentId, "skills"], (signal) =>
    caliberApi.getAgentSkills(agentId, signal),
  );
  const historyQuery = useApiQuery(
    ["agents", agentId, "audit-history"],
    (signal) =>
      caliberApi.listAuditLog(
        { entity_type: "agent", entity_id: agentId, limit: 100 },
        signal,
      ),
    { retry: false },
  );

  useEffect(() => {
    if (agentQuery.data && !editing) setDraft(agentDraft(agentQuery.data));
  }, [agentQuery.data, editing]);

  const saveMutation = useApiMutation(
    (payload: AgentUpdatePayload) => caliberApi.updateAgent(agentId, payload),
    {
      onSuccess: async (agent) => {
        await Promise.all([
          invalidate(["agents"]),
          invalidate(["agents", agentId]),
          invalidate(["agents", agentId, "skills"]),
          invalidate(["agents", agentId, "audit-history"]),
        ]);
        setDraft(agentDraft(agent));
        setEditing(false);
        showToast.success("Agent configuration saved");
      },
      onError: (error) => showToast.error(`Save failed: ${error.message}`),
    },
  );
  const enabledMutation = useApiMutation(
    (enabled: boolean) => caliberApi.updateAgent(agentId, { enabled }),
    {
      onSuccess: async (agent) => {
        await Promise.all([
          invalidate(["agents"]),
          invalidate(["agents", agentId]),
        ]);
        showToast.success(agent.enabled ? "Agent enabled" : "Agent disabled");
      },
      onError: (error) =>
        showToast.error(`Status update failed: ${error.message}`),
    },
  );
  const deleteMutation = useApiMutation(() => caliberApi.deleteAgent(agentId), {
    onSuccess: async () => {
      await invalidate(["agents"]);
      showToast.success("Agent and dependent refinement records deleted");
      navigate("/agents");
    },
    onError: (error) => showToast.error(`Delete failed: ${error.message}`),
  });

  if (agentQuery.isLoading)
    return <p className="text-sm text-slate-500">Loading agent…</p>;
  if (agentQuery.error || !agentQuery.data) {
    return (
      <div
        role="alert"
        className="rounded-xl border border-red-200 bg-red-50 p-5 text-sm text-red-700"
      >
        {agentQuery.error?.message ?? "Agent not found"}
      </div>
    );
  }

  const agent = agentQuery.data;
  const updateDraft = (field: keyof AgentEditDraft, value: string): void => {
    setDraft((current) => (current ? { ...current, [field]: value } : current));
    setFormError("");
  };

  const save = (): void => {
    if (!draft) return;
    try {
      const optimizerConfig = parseObject(
        "Optimizer configuration",
        draft.optimizerConfig,
      );
      optimizerConfig.skills = splitValues(draft.skills);
      saveMutation.mutate({
        name: draft.name.trim(),
        artifact_types: splitValues(draft.artifactTypes),
        eval_thresholds: parseObject(
          "Evaluation thresholds",
          draft.evalThresholds,
        ),
        optimizer_config: optimizerConfig,
        approval_policy: parseObject("Approval policy", draft.approvalPolicy),
        optimize_for: draft.optimizeFor.trim() || "quality",
        collaboration_mode: draft.collaborationMode.trim() || null,
        required_approvals: Math.max(
          1,
          Number.parseInt(draft.requiredApprovals, 10) || 1,
        ),
      });
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
    }
  };

  const preflightChecks = [
    {
      label: "MLflow experiment binding",
      ok: Boolean(agent.experiment_id.trim()),
      detail: agent.experiment_id,
    },
    {
      label: "Referenced skills resolve",
      ok: (skillsQuery.data?.missing.length ?? 0) === 0 && !skillsQuery.error,
      detail: skillsQuery.error
        ? skillsQuery.error.message
        : skillsQuery.data?.missing.length
          ? `Missing: ${skillsQuery.data.missing.join(", ")}`
          : `${skillsQuery.data?.skills.length ?? 0} skill references resolved`,
    },
    {
      label: "Configuration enabled",
      ok: agent.enabled,
      detail: agent.enabled
        ? "Eligible for refinement work"
        : "Disabled agents are not claimed by workers",
    },
  ];

  return (
    <div className="space-y-6 animate-fade-in">
      <button
        type="button"
        className="text-xs font-semibold text-caliber-purple hover:underline"
        onClick={() => navigate("/agents")}
      >
        ← Agents
      </button>
      <PageHeader
        title={agent.name}
        subtitle={`${agent.agent_id} · MLflow experiment ${agent.experiment_id}`}
        actions={
          <div className="flex gap-2">
            <button
              type="button"
              data-testid="agent-preflight"
              className="btn-secondary"
              onClick={() => setShowPreflight((visible) => !visible)}
            >
              Configuration preflight
            </button>
            {isAdmin && (
              <button
                type="button"
                className="btn-primary"
                onClick={() => {
                  setDraft(agentDraft(agent));
                  setEditing(true);
                }}
              >
                Edit
              </button>
            )}
          </div>
        }
      />

      <div className="rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-xs leading-relaxed text-sky-800">
        Preflight checks stored configuration and skill references only. It does
        not invoke a model, run tools, or prove end-to-end workflow behavior;
        use a workflow preview or evaluation run for that.
      </div>

      {showPreflight && (
        <section
          data-testid="agent-preflight-results"
          className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card"
        >
          <h2 className="text-sm font-bold text-slate-900">
            Configuration preflight
          </h2>
          <div className="mt-4 space-y-3">
            {preflightChecks.map((check) => (
              <div
                key={check.label}
                className="flex items-start justify-between gap-4 rounded-xl bg-slate-50 p-3 text-xs"
              >
                <div>
                  <p className="font-semibold text-slate-700">{check.label}</p>
                  <p className="mt-0.5 text-slate-500">{check.detail}</p>
                </div>
                <span
                  className={`rounded-full px-2 py-0.5 font-semibold ${check.ok ? "bg-emerald-100 text-emerald-700" : "bg-red-100 text-red-700"}`}
                >
                  {check.ok ? "pass" : "fail"}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      {editing && draft ? (
        <section
          data-testid="agent-edit-form"
          className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card"
        >
          <h2 className="text-sm font-bold text-slate-900">
            Edit configuration
          </h2>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="text-xs font-semibold text-slate-700">
              Display name
              <input
                className="form-input mt-1.5 w-full"
                value={draft.name}
                onChange={(event) => updateDraft("name", event.target.value)}
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Artifact types
              <input
                className="form-input mt-1.5 w-full"
                value={draft.artifactTypes}
                onChange={(event) =>
                  updateDraft("artifactTypes", event.target.value)
                }
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Skill names
              <input
                className="form-input mt-1.5 w-full"
                value={draft.skills}
                onChange={(event) => updateDraft("skills", event.target.value)}
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Optimize for
              <input
                className="form-input mt-1.5 w-full"
                value={draft.optimizeFor}
                onChange={(event) =>
                  updateDraft("optimizeFor", event.target.value)
                }
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Collaboration mode
              <input
                className="form-input mt-1.5 w-full"
                value={draft.collaborationMode}
                onChange={(event) =>
                  updateDraft("collaborationMode", event.target.value)
                }
              />
            </label>
            <label className="text-xs font-semibold text-slate-700">
              Required approvals
              <input
                type="number"
                min={1}
                className="form-input mt-1.5 w-full"
                value={draft.requiredApprovals}
                onChange={(event) =>
                  updateDraft("requiredApprovals", event.target.value)
                }
              />
            </label>
            {(
              [
                ["evalThresholds", "Evaluation thresholds JSON"],
                ["optimizerConfig", "Optimizer configuration JSON"],
                ["approvalPolicy", "Approval policy JSON"],
              ] as const
            ).map(([field, label]) => (
              <label
                key={field}
                className="text-xs font-semibold text-slate-700 sm:col-span-2"
              >
                {label}
                <textarea
                  className="form-input mt-1.5 min-h-28 w-full font-mono text-xs"
                  value={draft[field]}
                  onChange={(event) => updateDraft(field, event.target.value)}
                />
              </label>
            ))}
          </div>
          {formError && (
            <p role="alert" className="mt-3 text-xs text-red-600">
              {formError}
            </p>
          )}
          <div className="mt-5 flex justify-end gap-3">
            <button
              type="button"
              className="btn-secondary"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={saveMutation.isPending || !draft.name.trim()}
              onClick={save}
            >
              {saveMutation.isPending ? "Saving…" : "Save configuration"}
            </button>
          </div>
        </section>
      ) : (
        <section className="grid gap-4 md:grid-cols-3">
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card">
            <p className="text-xs text-slate-400">Status</p>
            <p className="mt-2 text-sm font-bold text-slate-900">
              {agent.enabled ? "Enabled" : "Disabled"}
            </p>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card">
            <p className="text-xs text-slate-400">Artifact types</p>
            <p className="mt-2 text-sm font-bold text-slate-900">
              {agent.artifact_types.join(", ") || "None configured"}
            </p>
          </div>
          <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card">
            <p className="text-xs text-slate-400">Required approvals</p>
            <p className="mt-2 text-sm font-bold text-slate-900">
              {agent.required_approvals}
            </p>
          </div>
        </section>
      )}

      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-card">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="text-sm font-bold text-slate-900">
              Revision history
            </h2>
            <p className="mt-1 text-xs text-slate-500">
              Audit-backed changes, not immutable configuration snapshots.
              Restore/rollback is not supported for agent configurations.
            </p>
          </div>
          <span className="text-xs text-slate-400">
            Updated {relativeTime(agent.updated_at)}
          </span>
        </div>
        {historyQuery.isLoading && (
          <p className="mt-4 text-xs text-slate-500">Loading history…</p>
        )}
        {historyQuery.error && (
          <p className="mt-4 rounded-xl bg-slate-50 p-3 text-xs text-slate-600">
            Revision history requires audit-log access:{" "}
            {historyQuery.error.message}
          </p>
        )}
        {historyQuery.data && historyQuery.data.entries.length === 0 && (
          <p className="mt-4 text-xs text-slate-500">
            No audit revisions recorded.
          </p>
        )}
        <div className="mt-4 space-y-2">
          {historyQuery.data?.entries.map((entry) => (
            <details
              key={entry.log_id}
              className="rounded-xl border border-slate-100 bg-slate-50 px-4 py-3 text-xs"
            >
              <summary className="cursor-pointer font-semibold text-slate-700">
                {entry.action.replaceAll("_", " ")} · {entry.actor} ·{" "}
                {relativeTime(entry.timestamp)}
              </summary>
              <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-[11px] text-slate-600">
                {JSON.stringify(entry.details ?? {}, null, 2)}
              </pre>
            </details>
          ))}
        </div>
      </section>

      <section className="rounded-2xl border border-amber-200 bg-amber-50 p-5">
        <h2 className="text-sm font-bold text-amber-900">Lifecycle controls</h2>
        <p className="mt-1 text-xs leading-relaxed text-amber-800">
          The current agent contract has enable/disable and permanent deletion,
          but no soft-archive state. Disable retains configuration and audit
          history. Permanent deletion also removes dependent refinement records.
        </p>
        {isAdmin ? (
          <div className="mt-4 flex flex-wrap gap-3">
            <button
              type="button"
              data-testid="agent-toggle-enabled"
              className="btn-secondary"
              disabled={enabledMutation.isPending}
              onClick={() => enabledMutation.mutate(!agent.enabled)}
            >
              {agent.enabled ? "Disable agent" : "Enable agent"}
            </button>
            {!confirmDelete ? (
              <button
                type="button"
                className="rounded-lg border border-red-200 bg-white px-3 py-2 text-xs font-semibold text-red-600 hover:bg-red-50"
                onClick={() => setConfirmDelete(true)}
              >
                Delete permanently…
              </button>
            ) : (
              <div className="flex items-center gap-2">
                <span className="text-xs font-semibold text-red-700">
                  Delete agent and dependent records?
                </span>
                <button
                  type="button"
                  data-testid="confirm-delete-agent"
                  className="rounded-lg bg-red-600 px-3 py-2 text-xs font-semibold text-white"
                  disabled={deleteMutation.isPending}
                  onClick={() => deleteMutation.mutate(undefined)}
                >
                  {deleteMutation.isPending ? "Deleting…" : "Confirm delete"}
                </button>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => setConfirmDelete(false)}
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        ) : (
          <p className="mt-4 text-xs font-medium text-amber-800">
            Administrator access is required to enable, disable, edit, or
            permanently delete this configuration.
          </p>
        )}
      </section>
    </div>
  );
}
