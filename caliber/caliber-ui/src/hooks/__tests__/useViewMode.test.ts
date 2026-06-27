import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useViewMode } from "@/hooks/useViewMode";

afterEach(() => {
  window.localStorage.clear();
});

describe("useViewMode", () => {
  it("defaults to grid when nothing is persisted", () => {
    const { result } = renderHook(() => useViewMode("widgets"));
    expect(result.current[0]).toBe("grid");
  });

  it("persists the choice to localStorage under the namespaced key", () => {
    const { result } = renderHook(() => useViewMode("widgets"));

    act(() => {
      result.current[1]("list");
    });

    expect(result.current[0]).toBe("list");
    expect(window.localStorage.getItem("caliber.viewmode.widgets")).toBe("list");
  });

  it("reads the persisted choice back on a fresh mount", () => {
    window.localStorage.setItem("caliber.viewmode.widgets", "list");

    const { result } = renderHook(() => useViewMode("widgets"));

    expect(result.current[0]).toBe("list");
  });

  it("keeps separate state per key", () => {
    window.localStorage.setItem("caliber.viewmode.alpha", "list");

    const alpha = renderHook(() => useViewMode("alpha"));
    const beta = renderHook(() => useViewMode("beta"));

    expect(alpha.result.current[0]).toBe("list");
    expect(beta.result.current[0]).toBe("grid");
  });

  it("ignores a corrupt persisted value and falls back to grid", () => {
    window.localStorage.setItem("caliber.viewmode.widgets", "banana");

    const { result } = renderHook(() => useViewMode("widgets"));

    expect(result.current[0]).toBe("grid");
  });
});
