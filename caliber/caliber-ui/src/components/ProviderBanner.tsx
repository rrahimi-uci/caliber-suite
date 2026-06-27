/**
 * Provider-readiness banner (golden-path roadmap, Wave 3 — honesty about
 * simulated vs real providers). When any provider runs in ``fake`` mode the
 * banner names them so users never mistake simulated results for real ones.
 * Dismissible per session; renders nothing when all providers are real.
 */

import { useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { ProviderReadiness } from "@/api/types";

const DISMISS_KEY = "caliber.provider_banner_dismissed";

function readDismissed(): boolean {
  if (typeof window === "undefined") return false;
  return window.sessionStorage.getItem(DISMISS_KEY) === "1";
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

  if (dismissed || !readiness || readiness.simulated.length === 0) {
    return null;
  }

  function dismiss(): void {
    if (typeof window !== "undefined") {
      window.sessionStorage.setItem(DISMISS_KEY, "1");
    }
    setDismissed(true);
  }

  const names = readiness.simulated.join(", ");
  return (
    <div
      role="status"
      data-testid="provider-banner"
      className="flex items-center gap-3 bg-amber-50 border-b border-amber-200/70 px-5 py-2 text-xs text-amber-800"
    >
      <svg className="w-4 h-4 shrink-0 text-amber-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
        <path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      </svg>
      <span className="flex-1">
        Simulated providers: <strong className="font-semibold">{names}</strong> are running in fake mode — results
        are demonstrative, not real. Configure <code className="font-mono">CALIBER_*_PROVIDER</code> for real behavior.
      </span>
      <button
        type="button"
        onClick={dismiss}
        className="rounded-md px-2 py-0.5 font-medium text-amber-700 hover:bg-amber-100 transition-colors"
      >
        Dismiss
      </button>
    </div>
  );
}
