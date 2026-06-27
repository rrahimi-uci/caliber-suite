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

  return { data, error, loading, refresh };
}
