/**
 * Subscribe to the CALIBER SSE event stream.
 *
 * Native `EventSource` does the heavy lifting (auto-reconnect with backoff,
 * line buffering, event-type dispatch). This hook owns the lifecycle: open
 * on mount, close on unmount, hand the caller a `lastEvent` they can react to.
 *
 * The backend emits typed frames (`event: approval.promoted\ndata: {...}\n\n`)
 * — see `caliber.routes.events_stream._format_event`. Per the SSE spec,
 * a frame with an explicit `event:` line ONLY fires that named listener,
 * **not** the default `message` listener. Earlier versions of this hook
 * subscribed only to `"message"` and silently dropped every typed event
 * (deep-review V2 Finding 2). We now register one listener per requested
 * type and keep `"message"` as a fallback for untyped frames.
 */

import { useEffect, useMemo, useState } from "react";

import { EVENT_STREAM_PATH } from "@/api/caliberApi";

export interface CaliberEvent {
  type: string;
  [key: string]: unknown;
}

type EventTypeFilter = string | string[] | undefined;

export function useEventStream(typeFilter?: EventTypeFilter): CaliberEvent | null {
  const [lastEvent, setLastEvent] = useState<CaliberEvent | null>(null);
  const filterKey = useMemo(() => normalizeFilterKey(typeFilter), [typeFilter]);

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource(EVENT_STREAM_PATH);
    const wantedTypes = normalizeFilterFromKey(filterKey);

    function dispatch(rawData: string, fallbackType: string): void {
      try {
        const parsed = JSON.parse(rawData) as CaliberEvent;
        const eventType = parsed.type ?? fallbackType;
        if (wantedTypes && !wantedTypes.has(eventType)) return;
        setLastEvent({ ...parsed, type: eventType });
      } catch {
        // Malformed frame — log once and move on; the stream continues.
        console.warn("caliber: dropping malformed SSE frame", rawData);
      }
    }

    // Track the (eventName, handler) pairs we register so the cleanup
    // below removes them. Using arrow functions inline would close over
    // the wrong reference and leak listeners across re-subscriptions.
    const registrations: Array<{ event: string; handler: (e: MessageEvent) => void }> = [];

    function listen(eventName: string): void {
      const handler = (e: MessageEvent): void => dispatch(e.data, eventName);
      source.addEventListener(eventName, handler);
      registrations.push({ event: eventName, handler });
    }

    // Always keep the default ``message`` listener as a fallback —
    // some frames (or future server versions) may omit ``event:``.
    listen("message");

    // For typed frames, the browser only fires the listener registered
    // under the exact event-name. ``wantedTypes`` is the caller's
    // filter (e.g. ``["approval.promoted", "approval.rejected"]``); we
    // register one listener per name so all of those frames reach the
    // dispatch.
    if (wantedTypes) {
      for (const name of wantedTypes) listen(name);
    }

    source.onerror = () => {
      // EventSource auto-reconnects; we don't surface transient errors as
      // hard failures. A persistent failure shows up as stale `lastEvent`
      // — the UI layer can render that staleness if it cares.
    };

    return () => {
      for (const { event, handler } of registrations) {
        source.removeEventListener(event, handler);
      }
      source.close();
    };
  }, [filterKey]);

  return lastEvent;
}

/**
 * Stable cache key for the effect's dependency list. Without this an inline
 * array argument (the common case) re-allocates each render and the
 * subscription closes/reopens on every parent re-render.
 */
function normalizeFilterKey(filter: EventTypeFilter): string {
  if (filter === undefined) return "*";
  if (Array.isArray(filter)) return [...filter].sort().join("|");
  return filter;
}

function normalizeFilterFromKey(filterKey: string): Set<string> | null {
  if (filterKey === "*" || filterKey === "") return null;
  return new Set(filterKey.split("|"));
}
