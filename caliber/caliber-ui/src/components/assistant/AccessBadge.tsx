/**
 * AccessBadge — surfaces the current user's access level (scopes) so it's clear
 * which Aria actions are permitted. Authoring/publishing requires the operator
 * scope; admins additionally manage settings and other users' sessions.
 */

import { caliberApi } from "@/api/caliberApi";
import type { CurrentUserInfo } from "@/api/types";
import { useApiQuery } from "@/hooks/useApiQuery";
import { cn } from "@/lib/utils";

function levelFor(scopes: string[]): { label: string; tone: string } {
  if (scopes.includes("admin")) {
    return { label: "Admin", tone: "bg-purple-50 text-purple-700 dark:bg-purple-500/15 dark:text-purple-200" };
  }
  if (scopes.includes("operator")) {
    return { label: "Operator", tone: "bg-caliber-50 text-caliber-700 dark:bg-caliber-500/15 dark:text-caliber-200" };
  }
  return { label: "Viewer", tone: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300" };
}

export function AccessBadge(): JSX.Element | null {
  const { data } = useApiQuery<CurrentUserInfo>(
    ["assistant", "me"],
    (signal) => caliberApi.getMe(signal),
  );
  if (!data) return null;
  const level = levelFor(data.scopes ?? []);
  return (
    <span
      data-testid="assistant-access-badge"
      title={`Access level — scopes: ${(data.scopes ?? []).join(", ") || "none"}`}
      className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold", level.tone)}
    >
      {level.label}
    </span>
  );
}
