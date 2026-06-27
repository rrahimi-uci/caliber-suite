/**
 * Per-node-type SVG icons for the Workflow Studio graph.
 *
 * Replaces the earlier emoji/unicode glyphs (⊕ ◉ ⇉ 🛡 …), which rendered as
 * empty rectangles wherever the OS lacked the glyph. lucide-react icons draw as
 * crisp SVGs and inherit the node's accent colour via ``currentColor``, so the
 * surrounding tinted ``<span>`` keeps styling them per node type.
 */

import {
  BellRing,
  BookOpen,
  Box,
  Bot,
  Circle,
  Code2,
  Database,
  FileText,
  FolderInput,
  FolderOutput,
  Braces,
  GitBranch,
  Globe,
  LifeBuoy,
  LogOut,
  Merge,
  Play,
  Plug,
  Repeat,
  ShieldCheck,
  Split,
  StickyNote,
  Timer,
  UserCheck,
  Webhook,
  Workflow,
  Wrench,
  type LucideIcon,
} from "lucide-react";

import type { WorkflowNodeType } from "@/api/workflowTypes";

/** Node type → lucide icon. Keep in sync with NODE_PALETTE in workflowGraph.ts. */
export const NODE_ICON_COMPONENTS: Record<WorkflowNodeType, LucideIcon> = {
  start: Play,
  file_input: FileText,
  folder_input: FolderInput,
  input_bucket: Database,
  output_bucket: Database,
  output_folder: FolderOutput,
  wait_until: Timer,
  wait_for_event: BellRing,
  parallel: Split,
  join: Merge,
  for_each: Repeat,
  loop: Repeat,
  error_boundary: LifeBuoy,
  subworkflow: Workflow,
  tool: Wrench,
  mcp_resource: Plug,
  webhook: Webhook,
  api_request: Globe,
  knowledge_query: BookOpen,
  knowledge_build: BookOpen,
  template: Braces,
  external_app: Box,
  python_code: Code2,
  output: LogOut,
  agent: Bot,
  router: GitBranch,
  guardrail: ShieldCheck,
  human_approval: UserCheck,
  note: StickyNote,
};

interface NodeIconProps {
  type: string;
  size?: number;
  className?: string;
}

/** Render the icon for a node type, falling back to a neutral circle. */
export function NodeIcon({ type, size = 16, className }: NodeIconProps): JSX.Element {
  const Icon =
    type in NODE_ICON_COMPONENTS
      ? NODE_ICON_COMPONENTS[type as WorkflowNodeType]
      : Circle;
  return <Icon size={size} strokeWidth={2} className={className} aria-hidden />;
}
