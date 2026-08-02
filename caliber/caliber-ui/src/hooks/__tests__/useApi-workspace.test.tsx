/**
 * A workspace switch must refetch every data layer, not just TanStack Query.
 *
 * `WorkspaceSelector` dispatches WORKSPACE_CHANGED_EVENT and invalidates the
 * query cache. `useApi` is a second, independent data layer, and the event had
 * no listeners anywhere in the app — so a switch invalidated half the
 * application while every `useApi` call site kept rendering the previous
 * project's rows.
 */

import { renderHook, waitFor, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useApi } from "@/hooks/useApi";
import { WORKSPACE_CHANGED_EVENT } from "@/workspace/activeWorkspace";

describe("useApi + workspace changes", () => {
  it("refetches when the active workspace changes", async () => {
    const fetcher = vi.fn(async () => "rows-for-project-a");
    const { result } = renderHook(() => useApi(fetcher, []));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetcher).toHaveBeenCalledTimes(1);

    act(() => {
      window.dispatchEvent(new Event(WORKSPACE_CHANGED_EVENT));
    });

    // Before this listener existed the count stayed at 1 and the hook kept
    // serving project A's data while requests carried project B's header.
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });

  it("stops listening once unmounted", async () => {
    const fetcher = vi.fn(async () => "rows");
    const { result, unmount } = renderHook(() => useApi(fetcher, []));

    await waitFor(() => expect(result.current.loading).toBe(false));
    unmount();

    act(() => {
      window.dispatchEvent(new Event(WORKSPACE_CHANGED_EVENT));
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
