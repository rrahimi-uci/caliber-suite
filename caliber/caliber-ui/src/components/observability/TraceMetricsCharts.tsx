/**
 * Monitoring dashboard charts (CALIBER's mirror of MLflow's GenAI monitoring):
 * trace volume + errors, latency p50/p95, and tokens + cost over time. Driven by
 * the time-bucketed payload from ``/observability/metrics`` and rendered on the
 * Observability page's Monitor tab.
 */
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

import type { ObservabilityMetrics } from "@/api/workflowTypes";

function fmtTick(ts: number): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number): string => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtCost(n: number): string {
  return `$${n < 0.01 ? n.toFixed(4) : n.toFixed(2)}`;
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

export function TraceMetricsCharts({ metrics }: { metrics: ObservabilityMetrics }): JSX.Element {
  const { buckets, totals } = metrics;
  if (buckets.length === 0) {
    return (
      <div
        data-testid="observability-metrics-empty"
        className="card px-4 py-12 text-center text-sm text-slate-400"
      >
        No metrics in this range. Run traced agents/workflows, then refresh.
      </div>
    );
  }

  const axisProps = {
    stroke: "#94a3b8",
    fontSize: 11,
    tickLine: false,
    axisLine: false,
  } as const;

  return (
    <div className="space-y-4" data-testid="observability-metrics">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Traces" value={totals.count.toLocaleString()} />
        <Stat label="Error rate" value={`${(totals.error_rate * 100).toFixed(1)}%`} />
        <Stat label="p50" value={totals.p50_ms !== null ? `${Math.round(totals.p50_ms)} ms` : "—"} />
        <Stat label="p95" value={totals.p95_ms !== null ? `${Math.round(totals.p95_ms)} ms` : "—"} />
        <Stat label="Tokens" value={totals.tokens.toLocaleString()} />
        <Stat label="Cost" value={fmtCost(totals.cost_usd)} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <ChartCard title="Trace volume & errors">
          <AreaChart data={buckets}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
            <YAxis allowDecimals={false} {...axisProps} />
            <Tooltip labelFormatter={(ts) => fmtTick(Number(ts))} />
            <Area type="monotone" dataKey="count" name="Traces" stroke="#7c3aed" fill="#ddd6fe" />
            <Area type="monotone" dataKey="error_count" name="Errors" stroke="#dc2626" fill="#fecaca" />
          </AreaChart>
        </ChartCard>

        <ChartCard title="Latency (ms)">
          <LineChart data={buckets}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
            <YAxis {...axisProps} />
            <Tooltip labelFormatter={(ts) => fmtTick(Number(ts))} />
            <Line type="monotone" dataKey="p50_ms" name="p50" stroke="#0ea5e9" dot={false} />
            <Line type="monotone" dataKey="p95_ms" name="p95" stroke="#f59e0b" dot={false} />
          </LineChart>
        </ChartCard>

        <ChartCard title="Tokens & cost">
          <ComposedChart data={buckets}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
            <XAxis dataKey="ts" tickFormatter={fmtTick} {...axisProps} />
            <YAxis yAxisId="left" {...axisProps} />
            <YAxis yAxisId="right" orientation="right" {...axisProps} />
            <Tooltip labelFormatter={(ts) => fmtTick(Number(ts))} />
            <Bar yAxisId="left" dataKey="tokens" name="Tokens" fill="#a5b4fc" />
            <Line
              yAxisId="right"
              type="monotone"
              dataKey="cost_usd"
              name="Cost ($)"
              stroke="#059669"
              dot={false}
            />
          </ComposedChart>
        </ChartCard>

        <ChartCard title="Error rate">
          <AreaChart data={buckets}>
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
    </div>
  );
}
