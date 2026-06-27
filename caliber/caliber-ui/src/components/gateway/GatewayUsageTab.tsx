/**
 * Gateway → Usage tab. Trace-derived token / cost / latency / error metrics over
 * time plus a per-model rollup. The MLflow gateway API does not expose usage
 * stats in this version, so this reads CALIBER's own MLflow traces (the same
 * aggregation as the Observability monitor), scoped to a time range.
 */

import { useCallback, useState } from "react";
import {
  Area,
  AreaChart,
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { caliberApi } from "@/api/caliberApi";
import type { GatewayUsage } from "@/api/types";
import { useApi } from "@/hooks/useApi";

const _RANGES: { key: string; label: string; ms: number }[] = [
  { key: "1h", label: "1h", ms: 60 * 60 * 1000 },
  { key: "24h", label: "24h", ms: 24 * 60 * 60 * 1000 },
  { key: "7d", label: "7d", ms: 7 * 24 * 60 * 60 * 1000 },
];

function fmtTick(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtCost(n: number): string {
  return `$${n < 0.01 ? n.toFixed(4) : n.toFixed(2)}`;
}

export function GatewayUsageTab(): JSX.Element {
  const [rangeKey, setRangeKey] = useState("24h");
  const range = _RANGES.find((r) => r.key === rangeKey) ?? _RANGES[1]!;
  const sinceMs = Date.now() - range.ms;
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getGatewayUsage({ sinceMs }, signal),
    [sinceMs],
  );
  const { data, error, loading } = useApi<GatewayUsage>(fetcher, [rangeKey]);

  const axisProps = { stroke: "#94a3b8", fontSize: 11, tickLine: false, axisLine: false } as const;

  return (
    <div className="space-y-4" data-testid="gateway-usage">
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-500">
          Trace-derived gateway usage. Costs use the per-model rates from the Pricing tab.
        </p>
        <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white p-0.5 text-xs">
          {_RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              onClick={() => setRangeKey(r.key)}
              className={`rounded-md px-2 py-1 ${
                r.key === rangeKey ? "bg-caliber-600 text-white" : "text-slate-500 hover:bg-slate-50"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          {error.message}
        </div>
      )}
      {loading && !data && (
        <div className="card px-4 py-12 text-center text-sm text-slate-400">Loading usage…</div>
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Stat label="Requests" value={data.totals.count.toLocaleString()} />
            <Stat label="Error rate" value={`${(data.totals.error_rate * 100).toFixed(1)}%`} />
            <Stat label="p50" value={data.totals.p50_ms !== null ? `${Math.round(data.totals.p50_ms)} ms` : "—"} />
            <Stat label="p95" value={data.totals.p95_ms !== null ? `${Math.round(data.totals.p95_ms)} ms` : "—"} />
            <Stat label="Tokens" value={data.totals.tokens.toLocaleString()} />
            <Stat label="Cost" value={fmtCost(data.totals.cost_usd)} />
          </div>

          {data.buckets.length === 0 ? (
            <div
              data-testid="gateway-usage-empty"
              className="card px-4 py-12 text-center text-sm text-slate-400"
            >
              No usage in this range. Route LLM traffic through the gateway (or run traced
              agents/workflows), then refresh.
            </div>
          ) : (
            <div className="grid gap-4 xl:grid-cols-2">
              <ChartCard title="Request volume & errors">
                <AreaChart data={data.buckets}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
                  <YAxis allowDecimals={false} {...axisProps} />
                  <Tooltip labelFormatter={(ts) => fmtTick(Number(ts))} />
                  <Area type="monotone" dataKey="count" name="Requests" stroke="#7c3aed" fill="#ddd6fe" />
                  <Area type="monotone" dataKey="error_count" name="Errors" stroke="#dc2626" fill="#fecaca" />
                </AreaChart>
              </ChartCard>

              <ChartCard title="Tokens & cost">
                <ComposedChart data={data.buckets}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
                  <YAxis yAxisId="left" {...axisProps} />
                  <YAxis yAxisId="right" orientation="right" {...axisProps} />
                  <Tooltip labelFormatter={(ts) => fmtTick(Number(ts))} />
                  <Bar yAxisId="left" dataKey="tokens" name="Tokens" fill="#a5b4fc" />
                  <Line yAxisId="right" type="monotone" dataKey="cost_usd" name="Cost ($)" stroke="#059669" dot={false} />
                </ComposedChart>
              </ChartCard>

              <ChartCard title="Latency (ms)">
                <LineChart data={data.buckets}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
                  <YAxis {...axisProps} />
                  <Tooltip labelFormatter={(ts) => fmtTick(Number(ts))} />
                  <Line type="monotone" dataKey="p50_ms" name="p50" stroke="#0ea5e9" dot={false} />
                  <Line type="monotone" dataKey="p95_ms" name="p95" stroke="#f59e0b" dot={false} />
                </LineChart>
              </ChartCard>

              <ChartCard title="Error rate">
                <AreaChart data={data.buckets}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                  <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
                  <YAxis tickFormatter={(v) => `${Math.round(Number(v) * 100)}%`} {...axisProps} />
                  <Tooltip
                    labelFormatter={(ts) => fmtTick(Number(ts))}
                    formatter={(v: number) => `${(v * 100).toFixed(1)}%`}
                  />
                  <Area type="monotone" dataKey="error_rate" name="Error rate" stroke="#dc2626" fill="#fee2e2" />
                </AreaChart>
              </ChartCard>
            </div>
          )}

          {data.by_model.length > 0 && (
            <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
              <table className="w-full text-sm" data-testid="gateway-usage-by-model">
                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
                    <th className="px-4 py-3 text-left font-medium">Model</th>
                    <th className="px-4 py-3 text-right font-medium">Calls</th>
                    <th className="px-4 py-3 text-right font-medium">Tokens</th>
                    <th className="px-4 py-3 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {data.by_model.map((m) => (
                    <tr key={m.model} className="hover:bg-slate-50">
                      <td className="px-4 py-3 font-mono text-xs text-slate-700">{m.model}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{m.calls.toLocaleString()}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{m.tokens.toLocaleString()}</td>
                      <td className="px-4 py-3 text-right tabular-nums">{fmtCost(m.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="rounded-xl border border-slate-200/70 bg-slate-50/70 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 text-base font-semibold tabular-nums text-slate-800">{value}</div>
    </div>
  );
}

function ChartCard({ title, children }: { title: string; children: JSX.Element }): JSX.Element {
  return (
    <div className="card p-4">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</div>
      <div className="h-56">
        <ResponsiveContainer width="100%" height="100%">
          {children}
        </ResponsiveContainer>
      </div>
    </div>
  );
}
