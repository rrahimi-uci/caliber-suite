/**
 * Gateway → Pricing tab. An editable per-model token-pricing table (USD per 1K
 * input / output / cached tokens). These CALIBER-owned rates override the
 * built-in DEFAULT_MODEL_PRICING and drive cost attribution everywhere — the
 * usage graphs, refinement-job cost, and trace cost.
 */

import { Pencil, Plus, X } from "lucide-react";
import { useCallback, useState } from "react";

import { ApiError, caliberApi } from "@/api/caliberApi";
import type { LlmPricing, LlmPricingCreatePayload } from "@/api/types";
import { useApi } from "@/hooks/useApi";
import { showToast } from "@/lib/toast";

type FormState = {
  provider: string;
  model_id: string;
  prompt_price: string;
  completion_price: string;
  cached_prompt_price: string;
};

const _EMPTY: FormState = {
  provider: "",
  model_id: "",
  prompt_price: "",
  completion_price: "",
  cached_prompt_price: "",
};

function _toPayload(form: FormState): LlmPricingCreatePayload {
  const cached = form.cached_prompt_price.trim();
  return {
    provider: form.provider.trim(),
    model_id: form.model_id.trim(),
    prompt_price: Number(form.prompt_price || 0),
    completion_price: Number(form.completion_price || 0),
    cached_prompt_price: cached === "" ? null : Number(cached),
  };
}

export function GatewayPricingTab(): JSX.Element {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.listLlmPricing({ status: "all" }, signal),
    [],
  );
  const { data, error, loading, refresh } = useApi<LlmPricing[]>(fetcher, []);
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        <div className="font-medium">Failed to load pricing</div>
        <div className="mt-0.5 text-xs">{error.message}</div>
      </div>
    );
  }

  const rows = data ?? [];

  return (
    <div className="space-y-4" data-testid="gateway-pricing">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-500">
          Rates are USD per 1K tokens and override CALIBER's built-in defaults for cost attribution.
        </p>
        <button
          type="button"
          onClick={() => setAdding((v) => !v)}
          className="inline-flex items-center gap-1.5 rounded-lg border border-caliber-300 bg-caliber-50 px-3 py-1.5 text-sm font-medium text-caliber-700 hover:bg-caliber-100"
        >
          <Plus className="h-4 w-4" /> Add rate
        </button>
      </div>

      {adding && (
        <PricingForm
          title="New rate"
          initial={_EMPTY}
          submitLabel="Create"
          onCancel={() => setAdding(false)}
          onSubmit={async (form) => {
            await caliberApi.createLlmPricing(_toPayload(form));
            setAdding(false);
            refresh();
          }}
        />
      )}

      <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <th className="px-4 py-3 text-left font-medium">Provider</th>
              <th className="px-4 py-3 text-left font-medium">Model</th>
              <th className="px-4 py-3 text-right font-medium">$/1K in</th>
              <th className="px-4 py-3 text-right font-medium">$/1K out</th>
              <th className="px-4 py-3 text-right font-medium">$/1K cached</th>
              <th className="px-4 py-3 text-left font-medium">Status</th>
              <th className="px-4 py-3 text-right font-medium">Edit</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-10 text-center text-sm text-slate-500">
                  No custom rates — CALIBER uses its built-in defaults. Add a rate to override.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <tr key={row.pricing_id} className="hover:bg-slate-50" data-testid="pricing-row">
                <td className="px-4 py-3 text-slate-700">{row.provider}</td>
                <td className="px-4 py-3 font-mono text-xs text-slate-700">{row.model_id}</td>
                <td className="px-4 py-3 text-right tabular-nums">{row.prompt_price}</td>
                <td className="px-4 py-3 text-right tabular-nums">{row.completion_price}</td>
                <td className="px-4 py-3 text-right tabular-nums text-slate-500">
                  {row.cached_prompt_price ?? "—"}
                </td>
                <td className="px-4 py-3">
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                      row.status === "active"
                        ? "bg-emerald-100 text-emerald-700"
                        : "bg-slate-200 text-slate-500"
                    }`}
                  >
                    {row.status}
                  </span>
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    type="button"
                    onClick={() => setEditingId(row.pricing_id)}
                    className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-slate-500 hover:bg-slate-100"
                    aria-label={`Edit ${row.provider}/${row.model_id}`}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {editingId && (
        <EditPricing
          row={rows.find((r) => r.pricing_id === editingId)}
          onClose={() => setEditingId(null)}
          onSaved={() => {
            setEditingId(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function EditPricing({
  row,
  onClose,
  onSaved,
}: {
  row: LlmPricing | undefined;
  onClose: () => void;
  onSaved: () => void;
}): JSX.Element {
  if (!row) return <></>;
  return (
    <PricingForm
      title={`Edit ${row.provider}/${row.model_id}`}
      initial={{
        provider: row.provider,
        model_id: row.model_id,
        prompt_price: String(row.prompt_price),
        completion_price: String(row.completion_price),
        cached_prompt_price: row.cached_prompt_price != null ? String(row.cached_prompt_price) : "",
      }}
      submitLabel="Save"
      statusToggle={{ current: row.status }}
      onCancel={onClose}
      onSubmit={async (form, status) => {
        await caliberApi.updateLlmPricing(row.pricing_id, {
          ..._toPayload(form),
          ...(status ? { status } : {}),
        });
        onSaved();
      }}
    />
  );
}

function PricingForm({
  title,
  initial,
  submitLabel,
  statusToggle,
  onCancel,
  onSubmit,
}: {
  title: string;
  initial: FormState;
  submitLabel: string;
  statusToggle?: { current: "active" | "archived" };
  onCancel: () => void;
  onSubmit: (form: FormState, status?: "active" | "archived") => Promise<void>;
}): JSX.Element {
  const [form, setForm] = useState<FormState>(initial);
  const [status, setStatus] = useState<"active" | "archived">(statusToggle?.current ?? "active");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const set = (k: keyof FormState) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));
  const canSubmit = Boolean(form.provider.trim() && form.model_id.trim()) && !submitting;

  const submit = async (): Promise<void> => {
    setSubmitting(true);
    setErr(null);
    try {
      await onSubmit(form, statusToggle ? status : undefined);
      showToast.success("Pricing saved.");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "save failed");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rounded-xl border border-caliber-200 bg-white p-4" data-testid="pricing-form">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-900">{title}</h3>
        <button type="button" onClick={onCancel} aria-label="Close" className="text-slate-400 hover:text-slate-700">
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Field label="Provider" value={form.provider} onChange={set("provider")} placeholder="openai" />
        <Field label="Model" value={form.model_id} onChange={set("model_id")} placeholder="gpt-4o" />
        <Field label="$/1K in" value={form.prompt_price} onChange={set("prompt_price")} placeholder="0.0025" />
        <Field
          label="$/1K out"
          value={form.completion_price}
          onChange={set("completion_price")}
          placeholder="0.01"
        />
        <Field
          label="$/1K cached"
          value={form.cached_prompt_price}
          onChange={set("cached_prompt_price")}
          placeholder="(optional)"
        />
      </div>
      {statusToggle && (
        <label className="mt-3 block text-xs text-slate-500">
          Status
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value as "active" | "archived")}
            className="ml-2 rounded-md border border-slate-200 px-2 py-1 text-sm"
          >
            <option value="active">active</option>
            <option value="archived">archived</option>
          </select>
        </label>
      )}
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
      <div className="mt-3 flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="rounded-lg px-3 py-1.5 text-sm text-slate-600">
          Cancel
        </button>
        <button
          type="button"
          disabled={!canSubmit}
          onClick={submit}
          className="rounded-lg bg-caliber-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-caliber-700 disabled:opacity-50"
        >
          {submitLabel}
        </button>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
}): JSX.Element {
  return (
    <div>
      <label className="mb-1 block text-xs text-slate-500">{label}</label>
      <input
        className="w-full rounded-md border border-slate-200 px-3 py-1.5 text-sm"
        value={value}
        onChange={onChange}
        placeholder={placeholder}
      />
    </div>
  );
}
