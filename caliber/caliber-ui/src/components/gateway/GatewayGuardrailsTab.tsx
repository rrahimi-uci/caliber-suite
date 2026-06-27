/**
 * Gateway → Guardrails tab. Lists the scorer-based gateway guardrails read from
 * the MLflow tracking server + per-endpoint coverage, and lets operators:
 *  - DEFINE new guardrails (PII / toxicity / custom guidelines / regex, backed by
 *    native no-extra-deps MLflow scorers, or any already-registered scorer),
 *  - DELETE guardrails, and
 *  - attach / detach them on an endpoint.
 *
 * CALIBER and MLflow's own gateway UI share one tracking store, so anything
 * defined here is immediately visible in MLflow and vice versa — there is no sync.
 */

import { Plus, ShieldCheck, ShieldX, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type {
  GatewayGuardrailCatalog,
  GatewayGuardrailCreateRequest,
  GatewayGuardrailsStatus,
  GatewayScorerTemplate,
} from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { showToast } from "@/lib/toast";

const STAGES = ["BEFORE", "AFTER"] as const;
const ACTIONS = ["VALIDATION", "SANITIZATION"] as const;

export function GatewayGuardrailsTab(): JSX.Element {
  const guardrailsFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getGatewayGuardrails(signal),
    [],
  );
  const { data, error, loading, refresh } = useApi<GatewayGuardrailsStatus>(guardrailsFetcher, []);
  const catalogFetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getGatewayGuardrailCatalog(signal),
    [],
  );
  const { data: catalog, refresh: refreshCatalog } = useApi<GatewayGuardrailCatalog>(
    catalogFetcher,
    [],
  );
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);

  const run = useCallback(
    async (fn: () => Promise<unknown>, ok: string): Promise<boolean> => {
      setBusy(true);
      try {
        await fn();
        showToast.success(ok);
        refresh();
        refreshCatalog();
        return true;
      } catch (err) {
        showToast.error(err instanceof ApiError ? err.message : "Gateway request failed");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [refresh, refreshCatalog],
  );

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        <div className="font-medium">Failed to load guardrails</div>
        <div className="mt-0.5 text-xs">{error.message}</div>
      </div>
    );
  }
  if (loading && !data) {
    return <div className="px-2 py-12 text-center text-sm text-slate-400">Loading guardrails…</div>;
  }
  if (!data) return <></>;

  if (!data.configured || !data.reachable) {
    return (
      <div
        data-testid="gateway-guardrails-unavailable"
        className="rounded-xl border border-amber-200/70 bg-amber-50 px-4 py-4 text-sm text-amber-800"
      >
        <div className="font-semibold">Gateway guardrails unavailable</div>
        <p className="mt-1 text-xs">
          {data.error ||
            "The gateway-guardrail API needs MLflow ≥3.13 with the gateway enabled on the tracking server."}
        </p>
      </div>
    );
  }

  const endpoints = data.coverage.map((c) => ({ id: c.endpoint_id, name: c.endpoint }));

  const attachedIds = (endpointId: string): Set<string> =>
    new Set(
      data.coverage
        .find((c) => c.endpoint_id === endpointId)
        ?.guardrails.map((g) => g.guardrail_id) ?? [],
    );

  return (
    <div className="space-y-5" data-testid="gateway-guardrails">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-slate-500">
          Guardrails defined here live in the shared MLflow tracking store — they show up in
          MLflow's gateway UI too.
        </p>
        <button
          type="button"
          disabled={busy}
          onClick={() => setCreating((v) => !v)}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-caliber-300 bg-caliber-50 px-3 py-1.5 text-xs font-medium text-caliber-700 hover:bg-caliber-100 disabled:opacity-50"
          data-testid="new-guardrail-toggle"
        >
          {creating ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
          {creating ? "Close" : "New guardrail"}
        </button>
      </div>

      {creating && (
        <CreateGuardrailForm
          catalog={catalog}
          endpoints={endpoints}
          busy={busy}
          onCreate={async (payload) => {
            const ok = await run(
              () => caliberApi.createGatewayGuardrail(payload),
              "Guardrail created.",
            );
            if (ok) setCreating(false);
          }}
        />
      )}

      {data.guardrails.length === 0 && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-6 text-center text-sm text-slate-500">
          No guardrails configured on the gateway yet.
        </div>
      )}

      {data.guardrails.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                <th className="px-4 py-3 text-left font-medium">Guardrail</th>
                <th className="px-4 py-3 text-left font-medium">Stage</th>
                <th className="px-4 py-3 text-left font-medium">Action</th>
                <th className="px-4 py-3 text-left font-medium">Scorer</th>
                <th className="px-4 py-3 text-right font-medium">Manage</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.guardrails.map((g) => (
                <tr key={g.guardrail_id} className="hover:bg-slate-50" data-testid="guardrail-row">
                  <td className="px-4 py-3 font-medium text-slate-900">{g.name}</td>
                  <td className="px-4 py-3">
                    <StageBadge stage={g.stage} />
                  </td>
                  <td className="px-4 py-3 text-xs text-slate-600">{g.action || "—"}</td>
                  <td className="px-4 py-3 font-mono text-xs text-slate-600">{g.scorer || "—"}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        if (
                          window.confirm(
                            `Delete guardrail "${g.name}"? It will be detached from every endpoint.`,
                          )
                        ) {
                          void run(
                            () => caliberApi.deleteGatewayGuardrail(g.guardrail_id),
                            "Guardrail deleted.",
                          );
                        }
                      }}
                      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-red-500 hover:bg-red-50 disabled:opacity-50"
                      aria-label={`Delete ${g.name}`}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="space-y-3">
        <h3 className="text-sm font-semibold text-slate-700">Per-endpoint coverage</h3>
        {data.coverage.length === 0 && (
          <p className="text-xs text-slate-400">No gateway endpoints to protect.</p>
        )}
        {data.coverage.map((cov) => {
          const attached = attachedIds(cov.endpoint_id);
          const available = data.guardrails.filter((g) => !attached.has(g.guardrail_id));
          return (
            <div
              key={cov.endpoint_id}
              className="rounded-xl border border-slate-200 bg-white p-4"
              data-testid="guardrail-coverage"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold text-slate-900">{cov.endpoint}</span>
                <AttachControl
                  available={available.map((g) => ({ id: g.guardrail_id, name: g.name }))}
                  disabled={busy}
                  onAttach={(guardrailId) =>
                    run(
                      () =>
                        caliberApi.attachGatewayGuardrail(cov.endpoint_id, {
                          guardrail_id: guardrailId,
                        }),
                      "Guardrail attached.",
                    )
                  }
                />
              </div>
              {cov.guardrails.length === 0 ? (
                <p className="mt-2 flex items-center gap-1.5 text-xs text-amber-600">
                  <ShieldX className="h-3.5 w-3.5" /> No guardrails on this endpoint.
                </p>
              ) : (
                <ul className="mt-2 space-y-1.5">
                  {cov.guardrails.map((g) => (
                    <li
                      key={g.guardrail_id}
                      className="flex items-center justify-between rounded-lg bg-slate-50 px-3 py-1.5 text-xs"
                    >
                      <span className="flex items-center gap-1.5 text-slate-700">
                        <ShieldCheck className="h-3.5 w-3.5 text-emerald-500" />
                        {g.name || g.guardrail_id}
                        {g.execution_order != null && (
                          <span className="text-slate-400">· order {g.execution_order}</span>
                        )}
                      </span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          run(
                            () => caliberApi.detachGatewayGuardrail(cov.endpoint_id, g.guardrail_id),
                            "Guardrail detached.",
                          )
                        }
                        className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-red-500 hover:bg-red-50 disabled:opacity-50"
                        aria-label={`Detach ${g.name || g.guardrail_id}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StageBadge({ stage }: { stage: string }): JSX.Element {
  const cls = stage === "BEFORE" ? "bg-sky-100 text-sky-700" : "bg-violet-100 text-caliber-purple";
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${cls}`}>
      {stage || "—"}
    </span>
  );
}

function AttachControl({
  available,
  disabled,
  onAttach,
}: {
  available: { id: string; name: string }[];
  disabled: boolean;
  onAttach: (guardrailId: string) => void;
}): JSX.Element {
  const [value, setValue] = useState("");
  if (available.length === 0) {
    return <span className="text-[11px] text-slate-400">All guardrails attached</span>;
  }
  return (
    <div className="flex items-center gap-1.5">
      <select
        aria-label="Attach guardrail"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="rounded-md border border-slate-200 bg-white px-2 py-1 text-xs"
      >
        <option value="">Attach guardrail…</option>
        {available.map((g) => (
          <option key={g.id} value={g.id}>
            {g.name}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={disabled || !value}
        onClick={() => {
          if (value) onAttach(value);
          setValue("");
        }}
        className="rounded-md border border-caliber-300 bg-caliber-50 px-2 py-1 text-xs font-medium text-caliber-700 hover:bg-caliber-100 disabled:opacity-50"
      >
        Attach
      </button>
    </div>
  );
}

const SOURCE_EXISTING = "__existing__";

function CreateGuardrailForm({
  catalog,
  endpoints,
  busy,
  onCreate,
}: {
  catalog: GatewayGuardrailCatalog | null;
  endpoints: { id: string; name: string }[];
  busy: boolean;
  onCreate: (payload: GatewayGuardrailCreateRequest) => void;
}): JSX.Element {
  const templates = useMemo(() => catalog?.templates ?? [], [catalog]);
  const scorers = catalog?.scorers ?? [];
  const [source, setSource] = useState<string>("");
  const [name, setName] = useState("");
  const [stage, setStage] = useState<string>("BEFORE");
  const [action, setAction] = useState<string>("VALIDATION");
  const [actionEndpointId, setActionEndpointId] = useState<string>("");
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [existingKey, setExistingKey] = useState<string>("");

  // Initialize the source to the first template once the catalog arrives.
  const effectiveSource = source || templates[0]?.type || "";
  const template: GatewayScorerTemplate | undefined = useMemo(
    () => templates.find((t) => t.type === effectiveSource),
    [templates, effectiveSource],
  );

  // Adopt the selected template's suggested stage/action (initial load + type change).
  // Manual stage/action edits survive because this only re-runs when the type changes.
  useEffect(() => {
    if (template && effectiveSource !== SOURCE_EXISTING) {
      setStage(template.default_stage);
      setAction(template.default_action);
    }
  }, [effectiveSource, template]);

  const selectSource = (next: string): void => {
    setSource(next);
    setConfig({}); // stage/action follow via the effect above
  };

  const setField = (field: string, value: unknown): void =>
    setConfig((prev) => ({ ...prev, [field]: value }));

  const submit = (): void => {
    const trimmed = name.trim();
    if (!trimmed) {
      showToast.error("Name is required.");
      return;
    }
    const base = {
      name: trimmed,
      stage,
      action,
      action_endpoint_id: action === "SANITIZATION" && actionEndpointId ? actionEndpointId : null,
    };
    if (effectiveSource === SOURCE_EXISTING) {
      if (!existingKey) {
        showToast.error("Pick a scorer.");
        return;
      }
      const [scorerId, version] = existingKey.split("::");
      onCreate({ ...base, scorer_id: scorerId, scorer_version: Number(version) });
      return;
    }
    if (!template) {
      showToast.error("Pick a guardrail type.");
      return;
    }
    onCreate({ ...base, scorer_type: template.type, config });
  };

  return (
    <div
      className="space-y-4 rounded-xl border border-caliber-200 bg-caliber-50/40 p-4"
      data-testid="create-guardrail-form"
    >
      <div className="text-sm font-semibold text-slate-800">Define a guardrail</div>

      <label className="block space-y-1">
        <span className="text-xs font-medium text-slate-600">Type</span>
        <select
          aria-label="Guardrail type"
          value={effectiveSource}
          onChange={(e) => selectSource(e.target.value)}
          className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
        >
          {templates.map((t) => (
            <option key={t.type} value={t.type}>
              {t.label}
              {t.deterministic ? "" : " · LLM judge"}
            </option>
          ))}
          {scorers.length > 0 && (
            <option value={SOURCE_EXISTING}>Existing registered scorer…</option>
          )}
        </select>
      </label>

      {template && effectiveSource !== SOURCE_EXISTING && (
        <p className="text-xs text-slate-500">{template.summary}</p>
      )}

      {effectiveSource === SOURCE_EXISTING ? (
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Scorer</span>
          <select
            aria-label="Existing scorer"
            value={existingKey}
            onChange={(e) => setExistingKey(e.target.value)}
            className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">Select a scorer…</option>
            {scorers.map((s) => (
              <option key={s.scorer_id} value={`${s.scorer_id}::${s.version}`}>
                {s.name} (v{s.version})
              </option>
            ))}
          </select>
        </label>
      ) : (
        template?.fields.map((field) => (
          <div key={field.name} className="space-y-1">
            <label className="block space-y-1">
              <span className="text-xs font-medium text-slate-600">
                {field.label}
                {field.required && <span className="text-red-500"> *</span>}
              </span>
              {field.type === "textarea" && (
                <textarea
                  aria-label={field.label}
                  rows={3}
                  placeholder={field.placeholder ?? ""}
                  value={(config[field.name] as string) ?? ""}
                  onChange={(e) => setField(field.name, e.target.value)}
                  className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm font-mono"
                />
              )}
              {field.type === "text" && (
                <input
                  type="text"
                  aria-label={field.label}
                  placeholder={field.placeholder ?? ""}
                  value={(config[field.name] as string) ?? ""}
                  onChange={(e) => setField(field.name, e.target.value)}
                  className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
                />
              )}
              {field.type === "select" && (
                <select
                  aria-label={field.label}
                  value={(config[field.name] as string) ?? field.options[0] ?? ""}
                  onChange={(e) => setField(field.name, e.target.value)}
                  className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
                >
                  {field.options.map((o) => (
                    <option key={o} value={o}>
                      {o}
                    </option>
                  ))}
                </select>
              )}
              {field.type === "boolean" && (
                <span className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    aria-label={field.label}
                    checked={Boolean(config[field.name])}
                    onChange={(e) => setField(field.name, e.target.checked)}
                    className="h-4 w-4 rounded border-slate-300"
                  />
                  <span className="text-xs text-slate-500">{field.help}</span>
                </span>
              )}
            </label>
            {field.type === "multiselect" && (
              <div className="flex flex-wrap gap-2">
                {field.options.map((o) => {
                  const selected = ((config[field.name] as string[]) ?? []).includes(o);
                  return (
                    <button
                      key={o}
                      type="button"
                      onClick={() => {
                        const cur = (config[field.name] as string[]) ?? [];
                        setField(
                          field.name,
                          selected ? cur.filter((x) => x !== o) : [...cur, o],
                        );
                      }}
                      className={`rounded-full border px-2.5 py-0.5 text-xs ${
                        selected
                          ? "border-caliber-300 bg-caliber-100 text-caliber-700"
                          : "border-slate-200 bg-white text-slate-500"
                      }`}
                    >
                      {o}
                    </button>
                  );
                })}
              </div>
            )}
            {field.type !== "boolean" && field.help && (
              <p className="text-[11px] text-slate-400">{field.help}</p>
            )}
          </div>
        ))
      )}

      <div className="grid grid-cols-2 gap-3">
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Name</span>
          <input
            type="text"
            aria-label="Guardrail name"
            placeholder="e.g. block-pii-output"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
          />
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Stage</span>
          <select
            aria-label="Stage"
            value={stage}
            onChange={(e) => setStage(e.target.value)}
            className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
          >
            {STAGES.map((s) => (
              <option key={s} value={s}>
                {s === "BEFORE" ? "BEFORE (check input)" : "AFTER (check output)"}
              </option>
            ))}
          </select>
        </label>
        <label className="block space-y-1">
          <span className="text-xs font-medium text-slate-600">Action</span>
          <select
            aria-label="Action"
            value={action}
            onChange={(e) => setAction(e.target.value)}
            className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
          >
            {ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a === "VALIDATION" ? "VALIDATION (block)" : "SANITIZATION (rewrite)"}
              </option>
            ))}
          </select>
        </label>
        {action === "SANITIZATION" && (
          <label className="block space-y-1">
            <span className="text-xs font-medium text-slate-600">Rewrite endpoint</span>
            <select
              aria-label="Rewrite endpoint"
              value={actionEndpointId}
              onChange={(e) => setActionEndpointId(e.target.value)}
              className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-sm"
            >
              <option value="">Default</option>
              {endpoints.map((ep) => (
                <option key={ep.id} value={ep.id}>
                  {ep.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      <div className="flex justify-end">
        <button
          type="button"
          disabled={busy || templates.length === 0}
          onClick={submit}
          className="rounded-md border border-caliber-300 bg-caliber-100 px-3 py-1.5 text-sm font-medium text-caliber-700 hover:bg-caliber-200 disabled:opacity-50"
          data-testid="create-guardrail-submit"
        >
          Create guardrail
        </button>
      </div>
    </div>
  );
}
