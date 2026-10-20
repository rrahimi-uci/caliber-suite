/**
 * ``/tools/:toolId`` is the workspace now: a tool has one page. This route stays so every link
 * and bookmark made while the detail page existed lands where the tool is managed (its Spec,
 * Sandbox, Fixtures, Test Runs, Hardening and Publish stages, with edit and versions in Publish).
 */

import { Navigate, useParams } from "react-router-dom";

export function ToolDetail(): JSX.Element {
  const { toolId } = useParams<{ toolId: string }>();
  return <Navigate to={`/tools?tool=${encodeURIComponent(toolId ?? "")}`} replace />;
}
