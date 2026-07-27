/**
 * Shared backend-health poll.
 *
 * Extracted from TopBar so every "is the system up?" indicator in the shell
 * reads the same live signal. The Sidebar footer used to hard-code a pulsing
 * green dot labelled "System Online", which stayed green with the backend
 * down — a small surface that quietly teaches users not to trust the status
 * lights.
 *
 * Built on `useApiQuery` rather than a bare `useEffect` + `setInterval` so the
 * TopBar and Sidebar indicators share **one** in-flight request and one cache
 * entry instead of each running its own poll cycle. That is the same
 * deduplication every other data surface in the app relies on.
 */

import { caliberApi } from "@/api/caliberApi";
import { useApiQuery } from "@/hooks/useApiQuery";

export type HealthStatus = "ok" | "down" | "loading";

/** Query key for the shared health poll. */
export const HEALTH_QUERY_KEY = ["health"] as const;

/** Default poll interval for the shell health indicators. */
export const HEALTH_POLL_INTERVAL_MS = 30_000;

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

/**
 * Tooltip text. Deliberately narrower than the label: `/health` proves the API
 * process and its database are reachable, not that workers, the scheduler,
 * MLflow, object storage, or providers are healthy. Claiming whole-platform
 * health from a database `SELECT 1` is the kind of overstatement this
 * indicator existed to fix.
 */
export const HEALTH_TITLE: Record<HealthStatus, string> = {
  ok: "API and database reachable",
  down: "API or database unreachable",
  loading: "Checking API and database…",
};

/** Polls the CALIBER health endpoint and returns "ok" | "down" | "loading". */
export function useHealthStatus(
  intervalMs = HEALTH_POLL_INTERVAL_MS,
): HealthStatus {
  const { isSuccess, isError } = useApiQuery(
    HEALTH_QUERY_KEY,
    (signal) => caliberApi.getHealth(signal),
    {
      refetchInterval: intervalMs,
      // A failed health check is itself the signal — retrying would only delay
      // the "down" state the indicator exists to show.
      retry: false,
      staleTime: intervalMs,
    },
  );

  if (isError) return "down";
  if (isSuccess) return "ok";
  return "loading";
}
