/**
 * `useInvalidate` is listed as an effect dependency by pages that react to live events. A new
 * function on every render made the workflow run event handler re-run on every render, which
 * re-invalidated (and so cancelled) the run's events query in a loop and left the run debugger
 * parked on a stale step after an approval resumed the run.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { useInvalidate } from "@/hooks/useApiQuery";

function setup() {
  const client = new QueryClient();
  const spy = vi.spyOn(client, "invalidateQueries").mockResolvedValue(undefined);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, spy, wrapper };
}

describe("useInvalidate", () => {
  it("returns the same function across re-renders", () => {
    const { wrapper } = setup();
    const { result, rerender } = renderHook(() => useInvalidate(), { wrapper });
    const first = result.current;
    rerender();
    rerender();
    expect(result.current).toBe(first);
  });

  it("invalidates the queries under the given key", async () => {
    const { spy, wrapper } = setup();
    const { result } = renderHook(() => useInvalidate(), { wrapper });
    await result.current(["workflow-run-events", "WR-1"]);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["workflow-run-events", "WR-1"] });
  });
});
