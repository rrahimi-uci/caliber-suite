/**
 * MutationGuard — render a mutating control only when the caller may use it,
 * and say why when they may not.
 *
 * The default is *disclose, don't hide*: a blocked control renders inert with
 * an explicit reason ("Requires Operator or Admin access") so a user learns
 * their permissions from the interface rather than from a 403 dialog after
 * committing to an action. Pass ``hideWhenForbidden`` for the narrower case
 * where the control's existence is itself the disclosure — an action naming an
 * object the caller should not know about.
 *
 * This is an affordance, not an authorization boundary. The server's
 * ``require_scopes`` check remains the guarantee; this component only stops the
 * UI offering work it knows will be refused.
 */

import type { ReactNode } from "react";

import { type CaliberScope, canAnyScope, missingScopeReason } from "@/lib/scopes";
import { cn } from "@/lib/utils";

export interface MutationGuardProps {
  /** The caller's effective scopes, from ``GET /me``. */
  scopes: readonly string[] | null | undefined;
  /** Any one of these grants the action. */
  requires: readonly CaliberScope[];
  /** The control to gate. */
  children: ReactNode;
  /**
   * Render nothing at all when forbidden, instead of a disabled control with a
   * reason. Use only when naming the action would leak something.
   */
  hideWhenForbidden?: boolean;
  /**
   * Override the generated reason — for cases where the scope requirement is
   * true but not the most useful thing to tell this user.
   */
  reason?: string;
  /**
   * Identity is still loading. Treated as forbidden (never offer an action
   * before authority is known) but explained as pending rather than denied.
   */
  loading?: boolean;
  className?: string;
}

export function MutationGuard({
  scopes,
  requires,
  children,
  hideWhenForbidden = false,
  reason,
  loading = false,
  className,
}: MutationGuardProps): JSX.Element | null {
  const permitted = !loading && canAnyScope(scopes, requires);
  if (permitted) return <>{children}</>;
  if (hideWhenForbidden) return null;

  const title = loading
    ? "Checking your access…"
    : (reason ?? missingScopeReason(requires));

  return (
    <span
      data-testid="mutation-guard-blocked"
      data-blocked-reason={title}
      title={title}
      // ``inert`` is not yet in every supported browser, so block interaction
      // three ways: pointer events off, focus removed from the tab order, and
      // aria-disabled so assistive technology announces the state. The reason
      // is a real, readable element rather than only a tooltip.
      className={cn("inline-flex flex-col gap-1", className)}
    >
      <span
        aria-disabled="true"
        className="pointer-events-none cursor-not-allowed opacity-50 [&_*]:pointer-events-none"
        // Everything inside is decorative once blocked; the reason below is
        // what carries the meaning.
        aria-hidden="true"
      >
        {children}
      </span>
      <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
        {title}
      </span>
    </span>
  );
}
