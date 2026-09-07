/**
 * Canonical CALIBER scope vocabulary for the SPA.
 *
 * The authoritative definitions live in ``caliber/src/caliber/auth.py``
 * (``SCOPE_VIEWER``/``SCOPE_OPERATOR``/``SCOPE_APPROVER``/``SCOPE_ADMIN``).
 * They are mirrored here — not re-derived — so that a client-side check and a
 * server-side ``require_scopes`` call can never disagree about the *spelling*
 * of a scope. The prefixed form (``caliber.admin``) is the only correct one;
 * the bare form (``admin``) is never issued by the server and matching against
 * it silently fails closed, which is how every admin came to be labelled
 * "Viewer" in the assistant panel.
 *
 * **Inheritance is deliberately not implemented here.** ``scopes_for_user``
 * (auth.py) already expands the hierarchy before it responds, so ``GET /me``
 * returns the *effective* set: an admin's payload literally contains all four
 * scopes, an approver's contains approver + viewer. A membership test is
 * therefore both correct and complete, and re-implementing ``_SCOPE_IMPLIES``
 * client-side would create a second source of truth that drifts the first time
 * the hierarchy changes.
 *
 * Client-side gating is an affordance, never a guarantee: the server's 401/403
 * remains authoritative. Hiding or disabling a control that would 403 stops
 * users learning their permissions from error dialogs; it does not replace the
 * check.
 */

/** Read access. Every authenticated user carries at least this. */
export const SCOPE_VIEWER = "caliber.viewer" as const;
/** Agent/artifact CRUD and run execution. */
export const SCOPE_OPERATOR = "caliber.operator" as const;
/** Approval-flow authority; sits between operator and admin. */
export const SCOPE_APPROVER = "caliber.approver" as const;
/** Full authority — implies every other scope. */
export const SCOPE_ADMIN = "caliber.admin" as const;

/** Every scope the system defines, most-privileged first. */
export const ALL_SCOPES = [
  SCOPE_ADMIN,
  SCOPE_APPROVER,
  SCOPE_OPERATOR,
  SCOPE_VIEWER,
] as const;

export type CaliberScope = (typeof ALL_SCOPES)[number];

/**
 * Does this scope set contain `scope`?
 *
 * An exact membership test. `scopes` is the effective set from ``GET /me``, so
 * no hierarchy expansion is needed or wanted (see the module docstring).
 * `undefined`/`null` — the shape of "identity has not loaded yet" — is treated
 * as "no", so a control is never offered before authority is known.
 */
export function hasScope(
  scopes: readonly string[] | null | undefined,
  scope: CaliberScope,
): boolean {
  return Array.isArray(scopes) && scopes.includes(scope);
}

/**
 * Does this scope set contain *at least one* of `required`?
 *
 * An empty `required` list returns `false` rather than `true`: a call site that
 * has not stated what it needs must not be read as "needs nothing", which is
 * the same defensive stance ``require_scopes`` takes server-side when handed an
 * empty set.
 */
export function canAnyScope(
  scopes: readonly string[] | null | undefined,
  required: readonly CaliberScope[],
): boolean {
  if (required.length === 0) return false;
  return required.some((scope) => hasScope(scopes, scope));
}

/** Display metadata for the caller's highest-privilege scope. */
export interface AccessLevel {
  scope: CaliberScope | null;
  label: string;
  /** Short explanation used as the badge tooltip and guard reason. */
  summary: string;
}

const LEVELS: ReadonlyArray<AccessLevel & { scope: CaliberScope }> = [
  {
    scope: SCOPE_ADMIN,
    label: "Admin",
    summary: "Full authority, including settings and other users' sessions.",
  },
  {
    scope: SCOPE_APPROVER,
    label: "Approver",
    summary: "Can approve gated work in addition to reading it.",
  },
  {
    scope: SCOPE_OPERATOR,
    label: "Operator",
    summary: "Can author, run, and publish.",
  },
  {
    scope: SCOPE_VIEWER,
    label: "Viewer",
    summary: "Read-only access.",
  },
];

/**
 * The caller's highest-privilege level, for display.
 *
 * Returns the `null`-scoped "No access" level for an anonymous caller — the
 * server returns an empty scope list rather than a 401 for those, so the SPA
 * renders a degraded state and must be able to name it.
 */
export function accessLevel(
  scopes: readonly string[] | null | undefined,
): AccessLevel {
  for (const level of LEVELS) {
    if (hasScope(scopes, level.scope)) return level;
  }
  return {
    scope: null,
    label: "No access",
    summary: "Not signed in, or no scopes granted.",
  };
}

/** Human-readable name for a scope, for use in permission messages. */
export function scopeLabel(scope: CaliberScope): string {
  return LEVELS.find((level) => level.scope === scope)?.label ?? scope;
}

/**
 * "Requires Operator or Admin" — the reason text a blocked control shows.
 *
 * Stating the requirement is the whole point: a control that is merely
 * greyed out teaches nothing, and one that 403s teaches it too late.
 */
export function missingScopeReason(
  required: readonly CaliberScope[],
): string {
  if (required.length === 0) return "Not available.";
  const names = required.map(scopeLabel);
  const list =
    names.length === 1
      ? names[0]
      : `${names.slice(0, -1).join(", ")} or ${names[names.length - 1]}`;
  return `Requires ${list} access.`;
}
