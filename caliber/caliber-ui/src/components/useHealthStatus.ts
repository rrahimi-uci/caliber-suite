/**
 * Shared backend-health poll.
 *
 * Extracted from TopBar so every "is the system up?" indicator in the shell
 * reads the same live signal. The Sidebar footer used to hard-code a pulsing
 * green dot labelled "System Online", which stayed green with the backend
 * down — a small surface that quietly teaches users not to trust the status
 * lights.
 */

import { useEffect, useState } from "react";

import { caliberApi } from "@/api/caliberApi";

export type HealthStatus = "ok" | "down" | "loading";

/** Tailwind classes for the status dot, keyed by health state. */
export const HEALTH_DOT: Record<HealthStatus, string> = {
  ok: "bg-emerald-400",
  down: "bg-red-400",
  loading: "bg-slate-400 animate-pulse",
};

/** Human-readable label for the status dot, keyed by health state. */
export const HEALTH_LABEL: Record<HealthStatus, string> = {
  ok: "System Online",
  down: "System Unreachable",
  loading: "Checking…",
};

/** Polls the CALIBER health endpoint and returns "ok" | "down" | "loading". */
export function useHealthStatus(intervalMs = 30_000): HealthStatus {
  const [status, setStatus] = useState<HealthStatus>("loading");

  useEffect(() => {
    let cancelled = false;
    const check = (): void => {
      caliberApi
        .getHealth()
        .then(() => {
          if (!cancelled) setStatus("ok");
        })
        .catch(() => {
          if (!cancelled) setStatus("down");
        });
    };
    check();
    const id = setInterval(check, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [intervalMs]);

  return status;
}
