import type { ToolDefinition } from "@/api/workflowTypes";

/**
 * Says where a tool came from and whether it can do anything right now (alpha gap P1-21).
 *
 * A fresh install lists sixteen tools as active. Eleven are CALIBER's own file, sandbox and
 * memory tools, which looked the same as the ones an admin registers, and four of those
 * (the agent-memory tools) only answer with an error while memory is off.
 */
export function ToolOriginBadges({
  tool,
}: {
  tool: Pick<
    ToolDefinition,
    "builtin" | "availability" | "availability_reason"
  >;
}): JSX.Element | null {
  const unavailable = tool.availability === "unavailable";
  if (!tool.builtin && !unavailable) return null;
  return (
    <>
      {tool.builtin && (
        <span
          data-testid="tool-builtin-badge"
          title="Seeded by CALIBER, not registered by an admin"
          className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600 ring-1 ring-slate-200/60"
        >
          Built-in
        </span>
      )}
      {unavailable && (
        <span
          data-testid="tool-unavailable-badge"
          title={tool.availability_reason ?? "Unavailable"}
          className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-800 ring-1 ring-amber-200/70"
        >
          Unavailable
        </span>
      )}
    </>
  );
}

/** The reason a tool is unavailable, for the line under its description. */
export function ToolUnavailableReason({
  tool,
}: {
  tool: Pick<ToolDefinition, "availability" | "availability_reason">;
}): JSX.Element | null {
  if (tool.availability !== "unavailable" || !tool.availability_reason)
    return null;
  return (
    <span
      data-testid="tool-unavailable-reason"
      className="mt-0.5 block text-amber-800"
    >
      {tool.availability_reason}
    </span>
  );
}
