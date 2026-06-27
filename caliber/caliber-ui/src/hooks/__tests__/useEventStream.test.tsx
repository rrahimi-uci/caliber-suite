import { renderHook, act } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { EVENT_STREAM_PATH } from "@/api/caliberApi";
import { useEventStream } from "@/hooks/useEventStream";

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly listeners = new Map<string, Set<(event: MessageEvent) => void>>();
  readonly close = vi.fn();
  onerror: ((event: Event) => void) | null = null;

  constructor(readonly url: string) {
    MockEventSource.instances.push(this);
  }

  static reset(): void {
    MockEventSource.instances = [];
  }

  addEventListener(event: string, handler: (event: MessageEvent) => void): void {
    const handlers = this.listeners.get(event) ?? new Set<(event: MessageEvent) => void>();
    handlers.add(handler);
    this.listeners.set(event, handlers);
  }

  removeEventListener(event: string, handler: (event: MessageEvent) => void): void {
    this.listeners.get(event)?.delete(handler);
  }

  emit(event: string, payload: unknown): void {
    const handlers = this.listeners.get(event);
    const data = typeof payload === "string" ? payload : JSON.stringify(payload);
    handlers?.forEach((handler) => handler({ data } as MessageEvent));
  }

  listenerCount(event: string): number {
    return this.listeners.get(event)?.size ?? 0;
  }
}

describe("useEventStream", () => {
  beforeAll(() => {
    vi.stubGlobal("EventSource", MockEventSource);
  });

  afterEach(() => {
    MockEventSource.reset();
    vi.clearAllMocks();
  });

  afterAll(() => {
    vi.unstubAllGlobals();
  });

  it("subscribes to typed SSE events and delivers matching payloads", () => {
    const { result } = renderHook(() =>
      useEventStream(["workflow.run.step", "workflow.run.node_started"]),
    );

    const stream = MockEventSource.instances[0];
    expect(stream).toBeDefined();
    expect(stream?.url).toBe(EVENT_STREAM_PATH);
    expect(stream?.listenerCount("message")).toBe(1);
    expect(stream?.listenerCount("workflow.run.step")).toBe(1);
    expect(stream?.listenerCount("workflow.run.node_started")).toBe(1);

    act(() => {
      stream?.emit("workflow.run.step", {
        type: "workflow.run.step",
        workflow_run_id: "WR-1",
        step: { node_id: "support_agent", output: "streamed response" },
      });
    });

    expect(result.current).toMatchObject({
      type: "workflow.run.step",
      workflow_run_id: "WR-1",
      step: { node_id: "support_agent", output: "streamed response" },
    });
  });

  it("keeps message as a fallback for untyped SSE frames", () => {
    const { result } = renderHook(() => useEventStream());

    const stream = MockEventSource.instances[0];
    expect(stream?.listenerCount("message")).toBe(1);

    act(() => {
      stream?.emit("message", { workflow_run_id: "WR-2", state: "connected" });
    });

    expect(result.current).toEqual({
      workflow_run_id: "WR-2",
      state: "connected",
      type: "message",
    });
  });

  it("ignores filtered-out events and cleans up listeners on unmount", () => {
    const { result, unmount } = renderHook(() => useEventStream("workflow.run.step"));

    const stream = MockEventSource.instances[0];

    act(() => {
      stream?.emit("workflow.run.node_started", {
        type: "workflow.run.node_started",
        workflow_run_id: "WR-3",
        node_id: "final",
      });
    });

    expect(result.current).toBeNull();

    unmount();

    expect(stream?.listenerCount("message")).toBe(0);
    expect(stream?.listenerCount("workflow.run.step")).toBe(0);
    expect(stream?.close).toHaveBeenCalledTimes(1);
  });
});
