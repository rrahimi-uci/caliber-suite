/**
 * Core fetch-lifecycle behavior of `useApi`. Complements
 * `useApi-workspace.test.tsx`, which covers the workspace-changed-event
 * refetch specifically.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/caliberApi";
import { useApi } from "@/hooks/useApi";

describe("useApi", () => {
  it("starts in a loading state, then resolves data with no error", async () => {
    const fetcher = vi.fn(async () => ({ ok: true }));
    const { result } = renderHook(() => useApi(fetcher));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ ok: true });
    expect(result.current.error).toBeNull();
  });

  it("surfaces an ApiError from the fetcher as-is", async () => {
    const err = new ApiError(404, "not found", null);
    const fetcher = vi.fn(async () => {
      throw err;
    });
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe(err);
    expect(result.current.data).toBeNull();
  });

  it("wraps a plain Error from the fetcher into an ApiError(0, message)", async () => {
    const fetcher = vi.fn(async () => {
      throw new Error("network exploded");
    });
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeInstanceOf(ApiError);
    expect(result.current.error?.status).toBe(0);
    expect(result.current.error?.message).toBe("network exploded");
  });

  it("wraps a non-Error throw into a generic 'unknown error' ApiError", async () => {
    const fetcher = vi.fn(async () => {
      throw "just a string";
    });
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error?.message).toBe("unknown error");
  });

  it("refetches on refresh(), racing the previous call via AbortController", async () => {
    const fetcher = vi.fn(async () => "value");
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetcher).toHaveBeenCalledTimes(1);

    act(() => {
      result.current.refresh();
    });

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    expect(result.current.data).toBe("value");
  });

  it("refetches when a value in deps changes", async () => {
    const fetcher = vi.fn(async () => "value");
    const { result, rerender } = renderHook(({ id }) => useApi(fetcher, [id]), {
      initialProps: { id: "a" },
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetcher).toHaveBeenCalledTimes(1);

    rerender({ id: "b" });
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });

  it("aborts the in-flight fetch and ignores its resolution after unmount", async () => {
    let capturedSignal: AbortSignal | undefined;
    const fetcher = vi.fn(
      (signal: AbortSignal) =>
        new Promise<string>((resolve) => {
          capturedSignal = signal;
          setTimeout(() => resolve("late"), 50);
        }),
    );
    const { unmount } = renderHook(() => useApi(fetcher));

    unmount();
    expect(capturedSignal?.aborted).toBe(true);
    // Letting the timer fire after unmount must not throw (the `cancelled`
    // guard drops the result rather than calling setState on an unmounted hook).
    await new Promise((resolve) => setTimeout(resolve, 60));
  });
});
