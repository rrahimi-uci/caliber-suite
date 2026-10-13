/**
 * Provider-readiness banner (golden-path roadmap, Wave 3 — honesty about
 * simulated vs real providers). When any provider runs in ``fake`` mode the
 * banner names them so users never mistake simulated results for real ones.
 *
 * It can be dismissed for the session only when what is simulated does not feed a
 * decision. If the *evaluation* or the *promoter* is simulated, the scores a user
 * is about to act on are canned, or the "release" they trigger changes nothing, so
 * the banner stays (alpha gap P0-4). It also keeps rendering when readiness
 * reports a missing dependency (the endpoint then answers 503) and names why, even
 * when every provider is real, and then it cannot be dismissed either (alpha gap P1-04).
 * Renders nothing when all providers are real and nothing is blocking.
 */

import { useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { ProviderReadiness } from "@/api/types";

const DISMISS_KEY = "caliber.provider_banner_dismissed";

/** Simulated providers whose output a user decides on; their banner is permanent. */
const DECISION_PROVIDERS = new Set(["eval", "promoter"]);

function readDismissed(): boolean {
  if (typeof window === "undefined") return false;
  return window.sessionStorage.getItem(DISMISS_KEY) === "1";
}

/** Plain-language labels for the readiness checks that can block, keyed by check name. */
const BLOCKER_LABELS: Record<string, string> = {
  mlflow: "MLflow unavailable",
  database: "Database unavailable",
  schema: "Database schema is not current",
  object_store: "Object storage unavailable",
  event_bus: "Event bus unavailable",
  workflow_queue: "Workflow queue unhealthy",
};

/**
 * Turn a readiness blocker (`"<check>: <detail>"`) into a sentence a user can act on.
 * An unknown check keeps its raw text, so a new server-side check is never hidden.
 */
export function describeBlocker(blocker: string): string {
  const separator = blocker.indexOf(":");
  if (separator <= 0) return blocker;
  const check = blocker.slice(0, separator).trim();
  const detail = blocker.slice(separator + 1).trim();
  const label = BLOCKER_LABELS[check];
  if (!label) return blocker;
  return detail ? `${label} (${detail})` : label;
}

export function ProviderBanner(): JSX.Element | null {
  const [readiness, setReadiness] = useState<ProviderReadiness | null>(null);
  const [dismissed, setDismissed] = useState<boolean>(readDismissed);

  useEffect(() => {
    let cancelled = false;
    caliberApi
      .getProviderReadiness()
      .then((value) => {
        if (!cancelled) setReadiness(value);
      })
      .catch(() => {
        // Readiness is best-effort; if it fails we simply show no banner.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!readiness) return null;
  const blockers = readiness.ready === false ? (readiness.blockers ?? []) : [];
  const simulated = readiness.simulated ?? [];
  if (simulated.length === 0 && blockers.length === 0) {
    return null;
  }
  const decisionsAreSimulated = simulated.some((name) => DECISION_PROVIDERS.has(name));
  // A missing dependency is never dismissed away: the product is not usable until it is fixed.
  const dismissible = !decisionsAreSimulated && blockers.length === 0;
  if (dismissed && dismissible) {
    return null;
  }

  function dismiss(): void {
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem(DISMISS_KEY, "1");
    }
    setDismissed(true);
  }

  const names = simulated.join(", ");
  const onlyBlockers = simulated.length === 0;
  return (
    <div
      role="status"
      data-testid="provider-banner"
      className={`flex items-center gap-3 border-b px-5 py-2 text-xs ${
        onlyBlockers
          ? "bg-red-50 border-red-200/70 text-red-800"
          : "bg-amber-50 border-amber-200/70 text-amber-800"
      }`}
    >
      <svg className={`w-4 h-4 shrink-0 ${onlyBlockers ? "text-red-500" : "text-amber-500"}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      </svg>
      <span className="flex-1">
        {simulated.length > 0 && (
          <>
            Simulated providers: <strong className="font-semibold">{names}</strong> are running in fake mode — results
            are demonstrative, not real.
            {simulated.includes("eval") && (
              <>
                {" "}
                <strong className="font-semibold">Evaluation scores are canned, not measurements.</strong>
              </>
            )}
            {simulated.includes("promoter") && (
              <>
                {" "}
                <strong className="font-semibold">Applying a candidate will not change the prompt registry.</strong>
              </>
            )}{" "}
            Configure <code className="font-mono">CALIBER_*_PROVIDER</code> for real behavior.
          </>
        )}
        {blockers.length > 0 && (
          <span data-testid="provider-banner-blockers" className={`block text-red-700 ${onlyBlockers ? "" : "mt-0.5"}`}>
            Not ready: {blockers.map(describeBlocker).join("; ")}
          </span>
        )}
      </span>
      {dismissible && (
        <button
          type="button"
          onClick={dismiss}
          className="rounded-md px-2 py-0.5 font-medium text-amber-700 hover:bg-amber-100 transition-colors"
        >
          Dismiss
        </button>
      )}
    </div>
  );
}
