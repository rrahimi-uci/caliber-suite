export function workflowRunPath(runId: string): string {
  return `/workflow-runs/${encodeURIComponent(runId.trim())}`;
}

export function workflowRunUrl(
  runId: string,
  origin?: string | null,
): string {
  const path = workflowRunPath(runId);
  const baseOrigin =
    origin
    ?? (typeof window !== "undefined" ? window.location?.origin ?? null : null);
  if (!baseOrigin) return path;
  try {
    return new URL(path, baseOrigin).toString();
  } catch {
    return path;
  }
}
