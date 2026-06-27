/**
 * Gateway → Endpoints tab. The original read-only discovery surface: status
 * cards (reachability + routing) and the table of configured LLM endpoints.
 */

import { CheckCircle2, Network, Plug, ShieldCheck, XCircle } from "lucide-react";
import { useCallback } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { GatewayStatus } from "@/api/types";
import { CopyButton } from "@/components/CopyButton";
import { useApi } from "@/hooks/useApi";

export function GatewayEndpointsTab(): JSX.Element {
  const fetcher = useCallback((signal: AbortSignal) => caliberApi.getGatewayStatus(signal), []);
  const { data, error, loading } = useApi<GatewayStatus>(fetcher, []);

  if (error) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
        <div className="font-medium">Failed to load gateway status</div>
        <div className="mt-0.5 text-xs">{error.message}</div>
      </div>
    );
  }
  if (loading && !data) {
    return <div className="px-2 py-12 text-center text-sm text-slate-400">Loading gateway…</div>;
  }
  if (!data) return <></>;

  if (!data.configured) {
    return (
      <div
        data-testid="gateway-not-configured"
        className="rounded-xl border border-amber-200/70 bg-amber-50 px-4 py-4 text-sm text-amber-800"
      >
        <div className="font-semibold">No gateway configured</div>
        <p className="mt-1 text-xs">
          Set <code className="font-mono">CALIBER_GATEWAY_URI</code> (e.g.{" "}
          <code className="font-mono">http://mlflow-gateway:5002</code>) to discover an MLflow AI
          Gateway. The <code className="font-mono">app</code> profile ships one — bring it up with{" "}
          <code className="font-mono">./start.sh</code>.
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3" data-testid="gateway-status">
        <StatusCard
          icon={<Network className="h-4 w-4" />}
          label="Gateway"
          value={data.gateway_uri || "—"}
          tone="neutral"
        />
        <StatusCard
          icon={data.reachable ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
          label="Reachability"
          value={data.reachable ? "Reachable" : "Unreachable"}
          tone={data.reachable ? "good" : "bad"}
        />
        <StatusCard
          icon={<Plug className="h-4 w-4" />}
          label="CALIBER routing"
          value={data.routing_through_gateway ? "Through gateway" : "Direct to provider"}
          tone={data.routing_through_gateway ? "good" : "neutral"}
          hint={
            data.routing_through_gateway
              ? undefined
              : "Set llm_base_url to route LLM calls through the gateway."
          }
        />
      </div>

      {!data.reachable && data.error && (
        <div className="mt-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
          Gateway unreachable at <span className="font-mono">{data.gateway_uri}</span>: {data.error}
        </div>
      )}

      <div className="mt-3 overflow-x-auto rounded-xl border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <th className="px-4 py-3 text-left font-medium">Endpoint</th>
              <th className="px-4 py-3 text-left font-medium">Type</th>
              <th className="px-4 py-3 text-left font-medium">Provider</th>
              <th className="px-4 py-3 text-left font-medium">Model</th>
              <th className="px-4 py-3 text-left font-medium">URL</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {data.endpoints.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-10 text-center text-sm text-slate-500">
                  {data.reachable
                    ? "No endpoints configured on the gateway."
                    : "Endpoints unavailable while the gateway is unreachable."}
                </td>
              </tr>
            )}
            {data.endpoints.map((ep) => (
              <tr key={ep.name} className="hover:bg-slate-50" data-testid="gateway-endpoint-row">
                <td className="px-4 py-3 font-medium text-slate-900">{ep.name}</td>
                <td className="px-4 py-3 text-xs text-slate-600">{ep.endpoint_type || "—"}</td>
                <td className="px-4 py-3 text-slate-700">{ep.provider || "—"}</td>
                <td className="px-4 py-3 font-mono text-xs text-slate-700">
                  {ep.model ? (
                    <span className="group inline-flex items-center gap-1">
                      {ep.model}
                      <CopyButton
                        value={ep.model}
                        label="Copy model"
                        className="opacity-0 group-hover:opacity-100"
                      />
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="px-4 py-3 font-mono text-xs text-slate-500">{ep.endpoint_url || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 px-4 py-3 text-xs text-slate-600">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-caliber-600" />
        <span>
          Guardrails (toxicity / PII / jailbreak) attach at the gateway, so they apply to every
          routed call without changing CALIBER — manage them in the Guardrails tab. Routing is
          opt-in via <code className="font-mono">CALIBER_LLM_BASE_URL</code>.
        </span>
      </div>
    </>
  );
}

export function StatusCard({
  icon,
  label,
  value,
  tone,
  hint,
}: {
  icon: JSX.Element;
  label: string;
  value: string;
  tone: "good" | "bad" | "neutral";
  hint?: string;
}): JSX.Element {
  const toneCls =
    tone === "good" ? "text-emerald-600" : tone === "bad" ? "text-red-600" : "text-slate-700";
  return (
    <div className="rounded-xl border border-slate-200/70 bg-white px-4 py-3">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
        <span className={toneCls}>{icon}</span>
        {label}
      </div>
      <div className={`mt-1 break-all text-sm font-semibold ${toneCls}`}>{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-slate-400">{hint}</div> : null}
    </div>
  );
}
