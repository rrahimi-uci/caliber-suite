/**
 * Tiny date helpers used by multiple pages.
 *
 * Kept dependency-free so we don't pull in date-fns / dayjs for what amounts
 * to a relative-time string. When timezone-aware formatting becomes a
 * requirement (it isn't yet — everything is "X ago"), graduate to a library.
 */

export function relativeTime(iso: string | Date): string {
  const then = typeof iso === "string" ? new Date(iso).getTime() : iso.getTime();
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
