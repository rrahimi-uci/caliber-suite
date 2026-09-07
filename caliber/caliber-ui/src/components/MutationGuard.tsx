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
 *
 * **Why blocking takes three mechanisms.** `pointer-events: none` stops the
 * mouse and nothing else — a `<button>` inside stays focusable and still fires
 * its handler on Enter or Space. `aria-hidden` removes the control from the
 * accessibility tree without removing it from the tab order, so it makes the
 * bypass *harder to notice* rather than closing it. React 18 has no `inert`
 * prop. So the blocked state does all of:
 *
 * 1. moves every focusable descendant out of the tab order (restoring the
 *    original `tabindex` when the block lifts);
 * 2. intercepts click, keydown, and submit in the *capture* phase, before any
 *    child handler runs;
 * 3. keeps the pointer-events/opacity treatment so it also looks unavailable.
 *
 * (1) and (2) are independent on purpose: a control that is somehow focused
 * anyway — programmatic focus, a stray autofocus, a browser quirk — still
 * cannot be activated.
 */

import { useCallback, useEffect, useRef, type ReactNode } from "react";

import { type CaliberScope, canAnyScope, missingScopeReason } from "@/lib/scopes";
import { cn } from "@/lib/utils";

/**
 * Anything the platform can put in the tab order. `[tabindex]` is included so
 * a div made focusable by hand is caught too; the `:not([tabindex="-1"])`
 * filter avoids re-parking elements that were already out.
 */
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button",
  "input",
  "select",
  "textarea",
  "summary",
  "audio[controls]",
  "video[controls]",
  "[contenteditable]",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/** Marks where an element's original tabindex was stashed. */
const SAVED_TABINDEX_ATTR = "data-guard-prev-tabindex";

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
  const shellRef = useRef<HTMLSpanElement | null>(null);

  // Park focusable descendants outside the tab order while blocked, and put
  // them back if the block lifts (scopes arriving, or a `loading` flip) so the
  // control does not stay unreachable once it becomes legitimate.
  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const nodes = Array.from(
      shell.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    );
    for (const node of nodes) {
      if (node.hasAttribute(SAVED_TABINDEX_ATTR)) continue;
      node.setAttribute(
        SAVED_TABINDEX_ATTR,
        node.hasAttribute("tabindex") ? node.getAttribute("tabindex")! : "",
      );
      node.setAttribute("tabindex", "-1");
    }
    return () => {
      for (const node of nodes) {
        const saved = node.getAttribute(SAVED_TABINDEX_ATTR);
        if (saved === null) continue;
        node.removeAttribute(SAVED_TABINDEX_ATTR);
        if (saved === "") node.removeAttribute("tabindex");
        else node.setAttribute("tabindex", saved);
      }
    };
    // Re-runs whenever the blocked subtree is mounted, which is exactly when
    // this element exists at all (the permitted branch returns early below).
  });

  // Capture phase, so this runs before any handler a child registered.
  const swallow = useCallback((event: React.SyntheticEvent) => {
    event.preventDefault();
    event.stopPropagation();
  }, []);

  const swallowActivationKeys = useCallback(
    (event: React.KeyboardEvent) => {
      // Enter and Space are what activate a focused button or link; other keys
      // stay live so the user can still tab away or use a shortcut.
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        event.stopPropagation();
      }
    },
    [],
  );

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
      className={cn("inline-flex flex-col gap-1", className)}
    >
      <span
        ref={shellRef}
        aria-disabled="true"
        onClickCapture={swallow}
        onKeyDownCapture={swallowActivationKeys}
        onSubmitCapture={swallow}
        className="cursor-not-allowed opacity-50"
      >
        {children}
      </span>
      <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400">
        {title}
      </span>
    </span>
  );
}
