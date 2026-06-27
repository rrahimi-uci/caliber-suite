/**
 * Shared dashboard-summary fetcher.
 *
 * Lifted out of ``Overview.tsx`` (V3 review Finding 4) so the sidebar
 * badge counts populate regardless of which route the user landed on
 * directly. The Dashboard page reads the same hook and renders cards
 * from the same data — there's exactly one ``/dashboard/summary``
 * call active per page at any time, and the SSE subscription means
 * counts stay fresh as state changes flow through the system.
 */

import { useCallback, useEffect } from "react";

import { caliberApi } from "@/api/caliberApi";
import type { DashboardSummary } from "@/api/types";
import type { UseApiState } from "@/hooks/useApi";
import { useApi } from "@/hooks/useApi";
import { useEventStream } from "@/hooks/useEventStream";

/** Events that should trigger a fresh summary fetch. */
const REFRESH_TRIGGERS = [
  "verification.verified",
  "verification.dismissed",
  "verification.duplicate",
  "job.advanced",
  "job.candidate_ready",
  "job.applied",
  "job.failed",
  "agent.rolled_back",
];

export function useDashboardSummary(): UseApiState<DashboardSummary> {
  const fetcher = useCallback(
    (signal: AbortSignal) => caliberApi.getDashboardSummary(signal),
    [],
  );
  const state = useApi(fetcher);
  const { refresh } = state;

  // Re-fetch whenever a relevant event lands. SSE frames don't carry
  // the new counts directly — the summary endpoint stays the single
  // source of truth.
  const lastEvent = useEventStream(REFRESH_TRIGGERS);
  useEffect(() => {
    if (!lastEvent) return;
    refresh();
  }, [lastEvent, refresh]);

  return state;
}
