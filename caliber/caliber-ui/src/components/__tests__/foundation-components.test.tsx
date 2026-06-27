import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RouteErrorBoundary } from "@/components/ErrorBoundary";
import { CollapsiblePanel } from "@/components/CollapsiblePanel";
import { ToolSignature } from "@/components/tools/ToolSignature";
import { useTheme } from "@/components/useTheme";

afterEach(() => {
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
  document.documentElement.style.colorScheme = "light";
  vi.restoreAllMocks();
});

describe("ToolSignature", () => {
  it("renders empty states and known root types", () => {
    const { rerender } = render(<ToolSignature title="Input" schema={null} />);
    expect(screen.getByText("No schema declared.")).toBeInTheDocument();

    rerender(<ToolSignature title="Input" schema={{ type: "object" }} />);
    expect(screen.getByText("No named fields.")).toBeInTheDocument();

    rerender(<ToolSignature title="Input" schema={{ enum: ["a", "b"] }} />);
    expect(screen.getByText("enum")).toBeInTheDocument();
  });

  it("renders field rows, required badge and descriptions", () => {
    render(
      <ToolSignature
        title="Input"
        schema={{
          type: "object",
          required: ["query"],
          properties: {
            query: { type: "string", description: "Search text" },
            limit: { type: ["integer", "null"] },
          },
        }}
      />,
    );
    expect(screen.getByText("query")).toBeInTheDocument();
    expect(screen.getByText("required")).toBeInTheDocument();
    expect(screen.getByText("Search text")).toBeInTheDocument();
    expect(screen.getByText("integer | null")).toBeInTheDocument();
  });
});

describe("CollapsiblePanel", () => {
  it("resizes via the drag handle and persists the width", () => {
    render(
      <CollapsiblePanel
        id="rez"
        side="right"
        title="Inspector"
        resizable
        defaultWidth={300}
        minWidth={200}
        maxWidth={500}
      >
        <div>Body</div>
      </CollapsiblePanel>,
    );
    const handle = screen.getByTestId("rez-resize");
    // Right-dock: dragging the inner edge left by 100px widens 300 → 400.
    fireEvent.mouseDown(handle, { clientX: 800 });
    fireEvent.mouseMove(document, { clientX: 700 });
    fireEvent.mouseUp(document);
    expect(window.localStorage.getItem("caliber.panel.rez.width")).toBe("400");
    expect(screen.getByText("Body")).toBeInTheDocument();
  });

  it("collapses/expands and persists state", async () => {
    const user = userEvent.setup();
    render(
      <CollapsiblePanel id="test" side="left" title="Palette">
        <div>Panel body</div>
      </CollapsiblePanel>,
    );
    expect(screen.getByText("Panel body")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Collapse Palette" }));
    expect(screen.queryByText("Panel body")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("caliber.panel.test.collapsed")).toBe("true");

    await user.click(screen.getByRole("button", { name: "Expand Palette" }));
    expect(screen.getByText("Panel body")).toBeInTheDocument();
  });

  it("reads persisted collapsed state and tolerates storage errors", async () => {
    window.localStorage.setItem("caliber.panel.persisted.collapsed", "true");
    const user = userEvent.setup();
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("disabled");
    });
    render(
      <CollapsiblePanel id="persisted" side="right" title="Inspector">
        <div>Inspector body</div>
      </CollapsiblePanel>,
    );
    expect(screen.queryByText("Inspector body")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand Inspector" }));
    expect(screen.getByText("Inspector body")).toBeInTheDocument();
    expect(setItemSpy).toHaveBeenCalled();
  });
});

describe("RouteErrorBoundary", () => {
  it("shows fallback with error message", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const Boom = () => {
      throw new Error("boom");
    };
    render(
      <RouteErrorBoundary>
        <Boom />
      </RouteErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows generic fallback for non-Error throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const Boom = () => {
      throw "bad";
    };
    render(
      <RouteErrorBoundary>
        <Boom />
      </RouteErrorBoundary>,
    );
    expect(
      screen.getByText("An unexpected error occurred while rendering this page."),
    ).toBeInTheDocument();
  });
});

describe("useTheme", () => {
  it("honors ?theme= query hints before persisted storage", () => {
    window.localStorage.setItem("caliber.theme", "light");
    const before = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.history.replaceState({}, "", "/caliber/?theme=dark");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    window.history.replaceState({}, "", before || "/");
  });

  it("initializes from persisted key, toggles, and syncs mlflow keys", async () => {
    window.localStorage.setItem("caliber.theme", "dark");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    await waitFor(() => {
      expect(document.documentElement.classList.contains("dark")).toBe(true);
    });
    act(() => result.current.toggle());
    expect(result.current.theme).toBe("light");
    expect(window.localStorage.getItem("_mlflow_dark_mode_toggle_enabled")).toBe("false");
    expect(window.localStorage.getItem("databricks-dark-mode-pref")).toBe("light");
  });

  it("falls back to mlflow preference keys when caliber key is missing", () => {
    window.localStorage.setItem("databricks-dark-mode-pref", "dark");
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
  });

  it("picks up storage events from other tabs", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("light");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", { key: "caliber.theme", newValue: "dark" }),
      );
    });
    expect(result.current.theme).toBe("dark");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "_mlflow_dark_mode_toggle_enabled",
          newValue: "false",
        }),
      );
    });
    expect(result.current.theme).toBe("light");
    act(() => {
      window.dispatchEvent(
        new StorageEvent("storage", {
          key: "databricks-dark-mode-pref",
          newValue: "dark",
        }),
      );
    });
    expect(result.current.theme).toBe("dark");
  });
});
