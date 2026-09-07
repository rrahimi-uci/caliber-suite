/**
 * AccessBadge — surfaces the current user's access level (scopes) so it's clear
 * which Aria actions are permitted. Authoring/publishing requires the operator
 * scope; approvers additionally clear gated work; admins additionally manage
 * settings and other users' sessions.
 *
 * The level is derived by ``accessLevel`` from the canonical vocabulary in
 * ``@/lib/scopes`` rather than by matching strings here. This badge previously
 * compared the bare names ``"admin"``/``"operator"``, which the server never
 * issues — it issues ``caliber.admin``/``caliber.operator`` — so every match
 * failed and every user, including admins, was labelled "Viewer".
 */

import { caliberApi } from "@/api/caliberApi";
import type { CurrentUserInfo } from "@/api/types";
import { useApiQuery } from "@/hooks/useApiQuery";
import { SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, accessLevel } from "@/lib/scopes";
import { cn } from "@/lib/utils";

const TONES: Record<string, string> = {
  [SCOPE_ADMIN]: "bg-purple-50 text-purple-700 dark:bg-purple-500/15 dark:text-purple-200",
  [SCOPE_APPROVER]: "bg-amber-50 text-amber-700 dark:bg-amber-500/15 dark:text-amber-200",
  [SCOPE_OPERATOR]: "bg-caliber-50 text-caliber-700 dark:bg-caliber-500/15 dark:text-caliber-200",
};

const NEUTRAL_TONE = "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300";

export function AccessBadge(): JSX.Element | null {
  const { data } = useApiQuery<CurrentUserInfo>(
    ["assistant", "me"],
    (signal) => caliberApi.getMe(signal),
  );
  if (!data) return null;
  const scopes = data.scopes ?? [];
  const level = accessLevel(scopes);
  const tone = (level.scope && TONES[level.scope]) || NEUTRAL_TONE;
  return (
    <span
      data-testid="assistant-access-badge"
      title={`${level.summary} Scopes: ${scopes.join(", ") || "none"}`}
      className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold", tone)}
    >
      {level.label}
    </span>
  );
}
