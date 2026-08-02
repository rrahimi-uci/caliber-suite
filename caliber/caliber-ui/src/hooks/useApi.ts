/**
 * Minimal data-fetching hook.
 *
 * Just enough plumbing to load data on mount, expose `{ data, error, loading }`,
 * and give callers a `refresh()` they can wire to buttons or live-update
 * events. When the surface needs caching, deduping, or background revalidation,
 * swap this for TanStack Query — but until then a 25-line hook keeps the
 * dependency tree small.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { WORKSPACE_CHANGED_EVENT } from "@/workspace/activeWorkspace";

import { ApiError } from "@/api/caliberApi";

export interface UseApiState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  /** Trigger a refetch. Cancels any in-flight call first. */
  refresh: () => void;
}

/**
 * Run `fetcher` on mount and whenever any value in `deps` changes.
 *
 * The `deps` array is the caller-controlled signal that "the query
 * inputs changed and a refetch is wanted." Without it, a page that
 * builds a `fetcher` from a `useState` filter would have a stale
 * `data` until the user hit a manual refresh — exactly the bug
 * deep-review V2 Finding 1 caught.
 *
 * Stale closures: the `fetcher` itself is held in a ref so a caller
 * that passes a new inline lambda on every render does not trigger
 * an unbounded refetch loop. The `deps` array — which the caller
 * crafts to mirror the inputs the lambda closes over — is the
 * authoritative re-run signal.
 */
export function useApi<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: ReadonlyArray<unknown> = [],
): UseApiState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);

  // Keep the latest fetcher in a ref so the effect below can re-run without
  // making the fetcher itself part of the dependency array (which would
  // trigger infinite re-fetches when callers pass an inline lambda).
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  // Bump this to re-trigger the fetch effect. We pair it with an AbortController
  // so a refresh that races with an in-flight call resolves to the newer result.
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);

    fetcherRef
      .current(controller.signal)
      .then((value) => {
        if (cancelled) return;
        setData(value);
        setError(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof ApiError) {
          setError(err);
        } else {
          setError(
            new ApiError(
              0,
              err instanceof Error ? err.message : "unknown error",
              null,
            ),
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  const refresh = useCallback(() => {
    setTick((n) => n + 1);
  }, []);

  // Refetch when the active workspace changes.
  //
  // `WorkspaceSelector` dispatches WORKSPACE_CHANGED_EVENT and invalidates the
  // TanStack Query cache, but this hook is a second, independent data layer and
  // the event had **no listeners anywhere in the app**. So a switch invalidated
  // half the application: TanStack-backed views refetched while every `useApi`
  // call site kept rendering the previous project's rows, and the next action
  // taken from those rows carried the new project header. Stale data is bad;
  // stale data that is acted on under a different scope is the actual defect.
  //
  // Reusing `tick` rather than adding a parallel path means the refetch races
  // with an in-flight call exactly the way `refresh()` already does — the
  // AbortController above resolves to the newer result.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onWorkspaceChanged = (): void => setTick((n) => n + 1);
    window.addEventListener(WORKSPACE_CHANGED_EVENT, onWorkspaceChanged);
    return () => {
      window.removeEventListener(WORKSPACE_CHANGED_EVENT, onWorkspaceChanged);
    };
  }, []);

  return { data, error, loading, refresh };
}
