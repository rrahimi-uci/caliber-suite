/**
 * TanStack Query wrapper around the existing `caliberApi` layer.
 *
 * Provides `useApiQuery` and `useApiMutation` that plug into
 * TanStack Query's caching, deduplication, and background refetch
 * while keeping the thin `caliberApi` as the HTTP layer.
 *
 * Migration guide: swap `useApi(fetcher, deps)` calls for
 * `useApiQuery(queryKey, fetcher)` — the `queryKey` replaces `deps`
 * and gives TanStack Query its cache identity.
 */

import {
  useQuery,
  useMutation,
  useQueryClient,
  type UseQueryOptions,
  type UseQueryResult,
  type UseMutationOptions,
  type UseMutationResult,
} from "@tanstack/react-query";

import { ApiError } from "@/api/caliberApi";

/**
 * Wrapper around `useQuery` typed to `ApiError`.
 *
 * Example:
 * ```ts
 * const { data, error, isLoading } = useApiQuery(
 *   ["agents"],
 *   (signal) => caliberApi.listAgents(signal),
 * );
 * ```
 */
export function useApiQuery<T>(
  queryKey: readonly unknown[],
  fetcher: (signal: AbortSignal) => Promise<T>,
  options?: Omit<UseQueryOptions<T, ApiError>, "queryKey" | "queryFn">,
): UseQueryResult<T, ApiError> {
  return useQuery<T, ApiError>({
    queryKey,
    queryFn: ({ signal }) => fetcher(signal),
    ...options,
  });
}

/**
 * Wrapper around `useMutation` typed to `ApiError`.
 *
 * Exposes an `invalidate` helper that callers can fire in `onSuccess`
 * to bust related query caches.
 *
 * Example:
 * ```ts
 * const { mutateAsync } = useApiMutation(
 *   (payload) => caliberApi.registerAgent(payload),
 *   { onSuccess: () => invalidate(["agents"]) },
 * );
 * ```
 */
export function useApiMutation<TData, TVariables>(
  mutationFn: (variables: TVariables) => Promise<TData>,
  options?: Omit<
    UseMutationOptions<TData, ApiError, TVariables>,
    "mutationFn"
  >,
): UseMutationResult<TData, ApiError, TVariables> {
  return useMutation<TData, ApiError, TVariables>({
    mutationFn,
    ...options,
  });
}

/**
 * Invalidate query caches by key prefix.
 *
 * Intended for use in `onSuccess` callbacks after mutations.
 */
export function useInvalidate(): (
  queryKey: readonly unknown[],
) => Promise<void> {
  const queryClient = useQueryClient();
  return (queryKey) => queryClient.invalidateQueries({ queryKey });
}
