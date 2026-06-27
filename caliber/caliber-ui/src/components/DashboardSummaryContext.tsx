/**
 * Context wrapper around the shared dashboard summary.
 *
 * App fetches the summary once via :func:`useDashboardSummary` and
 * exposes the result through this context so:
 *
 * 1. The sidebar badges always have access to the latest counts,
 *    regardless of which route mounted (V3 review Finding 4).
 * 2. The Dashboard page reads the same data without firing a second
 *    ``/dashboard/summary`` request — fewer round-trips, no risk of
 *    sidebar and Dashboard disagreeing about the numbers.
 */

import type { ReactNode } from "react";
import { createContext, useContext } from "react";

import type { UseApiState } from "@/hooks/useApi";
import type { DashboardSummary } from "@/api/types";

type DashboardSummaryContextValue = UseApiState<DashboardSummary>;

const DashboardSummaryContext = createContext<DashboardSummaryContextValue | null>(null);

export function DashboardSummaryProvider({
  value,
  children,
}: {
  value: DashboardSummaryContextValue;
  children: ReactNode;
}): JSX.Element {
  return (
    <DashboardSummaryContext.Provider value={value}>
      {children}
    </DashboardSummaryContext.Provider>
  );
}

/**
 * Read the App-scoped dashboard summary. Throws if used outside the
 * provider so a forgotten-wrapping bug surfaces immediately rather
 * than as a confusing "data is always null" runtime symptom.
 */
export function useDashboardSummaryContext(): DashboardSummaryContextValue {
  const ctx = useContext(DashboardSummaryContext);
  if (ctx === null) {
    throw new Error(
      "useDashboardSummaryContext used outside <DashboardSummaryProvider>",
    );
  }
  return ctx;
}
