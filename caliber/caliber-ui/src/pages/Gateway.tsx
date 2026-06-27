/**
 * Gateway — the MLflow AI Gateway operations surface, organized into tabs:
 *
 * - **Endpoints** — discovery: the gateway's configured LLM endpoints + routing.
 * - **Guardrails** — the scorer-based gateway guardrails + per-endpoint coverage;
 *   attach / detach existing guardrails (managed on the MLflow tracking server).
 * - **Pricing** — editable per-model token rates that drive CALIBER's cost math.
 * - **Usage** — trace-derived token / cost / latency / error metrics + by-model.
 */

import { BarChart3, DollarSign, Network, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { PageHeader } from "@/components/PageHeader";
import { PageTabs } from "@/components/PageTabs";
import { GatewayEndpointsTab } from "@/components/gateway/GatewayEndpointsTab";
import { GatewayGuardrailsTab } from "@/components/gateway/GatewayGuardrailsTab";
import { GatewayPricingTab } from "@/components/gateway/GatewayPricingTab";
import { GatewayUsageTab } from "@/components/gateway/GatewayUsageTab";

type Tab = "endpoints" | "guardrails" | "pricing" | "usage";

const TABS = [
  { key: "endpoints", label: "Endpoints", icon: <Network className="h-4 w-4" /> },
  { key: "guardrails", label: "Guardrails", icon: <ShieldCheck className="h-4 w-4" /> },
  { key: "pricing", label: "Pricing", icon: <DollarSign className="h-4 w-4" /> },
  { key: "usage", label: "Usage", icon: <BarChart3 className="h-4 w-4" /> },
];

export function Gateway(): JSX.Element {
  const [tab, setTab] = useState<Tab>("endpoints");

  return (
    <div className="space-y-5">
      <PageHeader
        title="LLM Gateway"
        subtitle="The MLflow AI Gateway fronts your LLM providers behind one set of named endpoints — a single key boundary, guardrails, per-model cost, and usage for every routed call."
      />
      <PageTabs tabs={TABS} active={tab} onChange={(k) => setTab(k as Tab)} />
      {tab === "endpoints" && <GatewayEndpointsTab />}
      {tab === "guardrails" && <GatewayGuardrailsTab />}
      {tab === "pricing" && <GatewayPricingTab />}
      {tab === "usage" && <GatewayUsageTab />}
    </div>
  );
}
