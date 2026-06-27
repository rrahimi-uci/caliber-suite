/**
 * A docked side panel that collapses to nothing and expands back, with the
 * state persisted per panel id. Uses the shared {@link EdgeToggle} so the
 * collapse control matches the main sidebar (and MLflow): a round chevron on
 * the panel's inner edge, vertically centered.
 *
 * ``side`` is which screen edge the panel docks to: the toggle sits on the
 * opposite (canvas-facing) edge and the border is drawn accordingly.
 *
 * When ``resizable`` is set, the panel's width is drag-adjustable (grab the
 * canvas-facing edge) and the chosen width is persisted per id.
 */

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { EdgeToggle } from "./EdgeToggle";

interface CollapsiblePanelProps {
  /** Stable id used for the persisted collapse + width state. */
  id: string;
  side: "left" | "right";
  title: string;
  /** Expanded width (fixed mode), e.g. ``"w-56"``. Ignored when ``resizable``. */
  widthClass?: string;
  /** Classes for the scrollable body (background, padding). */
  bodyClassName?: string;
  /** Optional test id placed on the outer container. */
  testId?: string;
  /** Bump this value to force the panel open (used by keyboard/graph shortcuts). */
  expandSignal?: number;
  /** Enable drag-to-resize. Width persists to ``caliber.panel.<id>.width``. */
  resizable?: boolean;
  defaultWidth?: number;
  minWidth?: number;
  maxWidth?: number;
  children: ReactNode;
}

function storageKey(id: string): string {
  return `caliber.panel.${id}.collapsed`;
}
function widthKey(id: string): string {
  return `caliber.panel.${id}.width`;
}

function readCollapsed(id: string): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(storageKey(id)) === "true";
}

function readWidth(id: string, fallback: number, min: number, max: number): number {
  if (typeof window === "undefined") return fallback;
  const raw = Number(window.localStorage.getItem(widthKey(id)));
  if (!Number.isFinite(raw) || raw <= 0) return fallback;
  return Math.min(max, Math.max(min, raw));
}

export function CollapsiblePanel({
  id,
  side,
  title,
  widthClass = "w-64",
  bodyClassName = "bg-white p-3",
  testId,
  expandSignal,
  resizable = false,
  defaultWidth = 288,
  minWidth = 240,
  maxWidth = 640,
  children,
}: CollapsiblePanelProps): JSX.Element {
  const [collapsed, setCollapsed] = useState<boolean>(() => readCollapsed(id));
  const [width, setWidth] = useState<number>(() => readWidth(id, defaultWidth, minWidth, maxWidth));
  const widthRef = useRef(width);
  widthRef.current = width;

  useEffect(() => {
    if (expandSignal === undefined) return;
    setCollapsed((prev) => {
      if (!prev) return prev;
      try {
        window.localStorage.setItem(storageKey(id), "false");
      } catch {
        // Storage may be disabled — expanding still works for the session.
      }
      return false;
    });
  }, [expandSignal, id]);

  const toggle = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem(storageKey(id), String(next));
      } catch {
        // Storage may be disabled — collapsing still works for the session.
      }
      return next;
    });
  }, [id]);

  const onResizeStart = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startW = widthRef.current;
      const onMove = (ev: MouseEvent): void => {
        // Right-dock: dragging the (left) inner edge leftward widens it.
        const delta = side === "right" ? startX - ev.clientX : ev.clientX - startX;
        setWidth(Math.min(maxWidth, Math.max(minWidth, startW + delta)));
      };
      const onUp = (): void => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.body.style.userSelect = "";
        document.body.style.cursor = "";
        try {
          window.localStorage.setItem(widthKey(id), String(widthRef.current));
        } catch {
          // Storage may be disabled — width still applies for the session.
        }
      };
      document.body.style.userSelect = "none";
      document.body.style.cursor = "col-resize";
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [side, minWidth, maxWidth, id],
  );

  const border = side === "left" ? "border-r" : "border-l";
  const label = `${collapsed ? "Expand" : "Collapse"} ${title}`;

  // Toggle on the panel's inner (canvas-facing) edge, vertically centered.
  // Expanded: straddle the border (translate outward). Collapsed: keep the
  // button fully inside the row so it isn't clipped by the editor's overflow.
  const togglePos = collapsed
    ? side === "left"
      ? "left-0"
      : "right-0"
    : side === "left"
      ? "right-0 translate-x-1/2"
      : "left-0 -translate-x-1/2";

  if (collapsed) {
    return (
      <div
        data-testid={testId}
        className={`relative w-0 shrink-0 ${border} border-zinc-200`}
        title={title}
      >
        <EdgeToggle
          dock={side}
          collapsed
          onToggle={toggle}
          label={label}
          className={`absolute top-1/2 z-10 -translate-y-1/2 ${togglePos}`}
        />
      </div>
    );
  }

  return (
    <div
      data-testid={testId}
      className={`relative flex shrink-0 flex-col ${resizable ? "" : widthClass} ${border} border-zinc-200`}
      style={resizable ? { width } : undefined}
    >
      {resizable && (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label={`Resize ${title}`}
          data-testid={`${testId ?? id}-resize`}
          onMouseDown={onResizeStart}
          className={`absolute top-0 bottom-0 z-[5] w-1.5 cursor-col-resize transition-colors hover:bg-caliber-300/60 ${
            side === "right" ? "left-0 -translate-x-1/2" : "right-0 translate-x-1/2"
          }`}
        />
      )}
      <div className={`flex-1 overflow-y-auto ${bodyClassName}`}>{children}</div>
      <EdgeToggle
        dock={side}
        collapsed={false}
        onToggle={toggle}
        label={label}
        className={`absolute top-1/2 z-10 -translate-y-1/2 ${togglePos}`}
      />
    </div>
  );
}
