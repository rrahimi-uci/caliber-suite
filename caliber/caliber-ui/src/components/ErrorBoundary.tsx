/**
 * Route-level error boundary.
 *
 * Catches render errors in page components so a single broken page
 * doesn't take down the entire SPA. Provides a "Try again" button
 * that resets the boundary and re-mounts the failed subtree.
 */

import { ErrorBoundary as ReactErrorBoundary } from "react-error-boundary";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";

function ErrorFallback({
  error,
  resetErrorBoundary,
}: {
  error: unknown;
  resetErrorBoundary: () => void;
}): JSX.Element {
  const message =
    error instanceof Error
      ? error.message
      : "An unexpected error occurred while rendering this page.";
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center min-h-[40vh] gap-4 text-center px-4"
    >
      <div className="rounded-full bg-red-100 p-3">
        <svg
          className="h-6 w-6 text-red-600"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="15" y1="9" x2="9" y2="15" />
          <line x1="9" y1="9" x2="15" y2="15" />
        </svg>
      </div>
      <div>
        <h2 className="text-lg font-semibold text-gray-900">
          Something went wrong
        </h2>
        <p className="text-sm text-gray-500 mt-1 max-w-md">
          {message}
        </p>
      </div>
      <Button variant="outline" onClick={resetErrorBoundary}>
        Try again
      </Button>
    </div>
  );
}

export function RouteErrorBoundary({
  children,
}: {
  children: ReactNode;
}): JSX.Element {
  return (
    <ReactErrorBoundary FallbackComponent={ErrorFallback}>
      {children}
    </ReactErrorBoundary>
  );
}
