/**
 * Read-only / interactive React Flow canvas — n8n-inspired.
 *
 * Zinc-gray canvas with subtle dot grid, monochromatic edges, indigo connection
 * line, and workflow-builder smoothstep routing. Supports drag-drop from the
 * palette and connection-drop-on-empty for quick node creation.
 */

import { useCallback, useMemo, useRef } from "react";
import {
  Background,
  BackgroundVariant,
  ConnectionLineType,
  ConnectionMode,
  Controls,
  MiniMap,
  ReactFlow,
  useReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type OnConnect,
  type OnConnectEnd,
  type XYPosition,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type {
  ValidationReport,
  WorkflowComponent,
  WorkflowManifest,
  WorkflowNodeType,
} from "@/api/workflowTypes";
import { CaliberNode } from "@/components/workflows/CaliberNode";
import {
  manifestToFlow,
  nodeExecutionColor,
  type NodeExecutionBadge,
  nodeColor,
  nodeValidationSummary,
  type FlowNodeData,
  type FlowNodePosition,
} from "@/lib/workflowGraph";

const NODE_TYPES = { caliber: CaliberNode };

/** n8n-style edge: zinc stroke, rounded bends. */
const DEFAULT_EDGE_OPTIONS = {
  type: "smoothstep" as const,
  style: { stroke: "#A1A1AA", strokeWidth: 1.5 },
  pathOptions: { borderRadius: 16 },
};

interface CanvasProps {
  manifest: WorkflowManifest;
  selectedNodeId?: string | null;
  /** Full multi-selection set (marquee / shift-click). Falls back to [selectedNodeId]. */
  selectedNodeIds?: string[];
  nodePositions?: Record<string, FlowNodePosition>;
  onSelectNode?: (nodeId: string | null) => void;
  /** Reports the current selection ids (React Flow marquee / shift-click). */
  onSelectionChange?: (nodeIds: string[]) => void;
  onNodeDoubleClick?: (nodeId: string) => void;
  onEdgeClick?: (edgeId: string, screenPosition?: { x: number; y: number }) => void;
  onConnect?: (connection: { source: string; target: string }) => void;
  onNodePositionChange?: (nodeId: string, position: XYPosition) => void;
  executionPath?: string[];
  className?: string;
  /** Inject quick-add callback into every node's data. */
  onQuickAdd?: (nodeId: string) => void;
  /** Inject duplicate callback into every node's data (node toolbar). */
  onDuplicate?: (nodeId: string) => void;
  /** Inject view/edit-code callback into every node's data (node toolbar). */
  onViewCode?: (nodeId: string) => void;
  /** Per-node execution status from preview/live runs; drives node badges + outlines. */
  nodeExecutionByNode?: Record<string, NodeExecutionBadge>;
  /** Drop handler for palette drag-and-drop. */
  onDropNode?: (type: string, position: { x: number; y: number }) => void;
  /** Called when user drops a connection line on empty canvas space. */
  onConnectionDrop?: (
    sourceId: string,
    flowPosition: { x: number; y: number },
    screenPosition?: { x: number; y: number },
  ) => void;
  /** Latest validation report so nodes can surface inline readiness and issues. */
  validationReport?: ValidationReport | null;
  /** Optional server-backed component metadata keyed by node type. */
  componentSpecs?: ReadonlyMap<WorkflowNodeType, WorkflowComponent> | null;
}

function CanvasInner({
  manifest,
  selectedNodeId,
  selectedNodeIds,
  nodePositions,
  onSelectNode,
  onSelectionChange,
  onNodeDoubleClick,
  onEdgeClick,
  onConnect,
  onNodePositionChange,
  executionPath,
  className,
  onQuickAdd,
  onDuplicate,
  onViewCode,
  nodeExecutionByNode,
  onDropNode,
  onConnectionDrop,
  validationReport,
  componentSpecs,
}: CanvasProps): JSX.Element {
  const connectSourceRef = useRef<string | null>(null);
  const { screenToFlowPosition } = useReactFlow();
  const { nodes, edges } = useMemo(
    () => manifestToFlow(manifest, nodePositions, componentSpecs ?? null),
    [componentSpecs, manifest, nodePositions],
  );

  const pathSet = useMemo(() => new Set(executionPath ?? []), [executionPath]);
  const selectedSet = useMemo(
    () =>
      new Set(
        selectedNodeIds && selectedNodeIds.length > 0
          ? selectedNodeIds
          : selectedNodeId
            ? [selectedNodeId]
            : [],
      ),
    [selectedNodeIds, selectedNodeId],
  );

  const decoratedNodes: Node<FlowNodeData>[] = nodes.map((n) => {
    const executionBadge = nodeExecutionByNode?.[n.id];
    const outlineColor = executionBadge
      ? nodeExecutionColor(executionBadge.status)
      : pathSet.has(n.id)
        ? nodeExecutionColor("completed")
        : null;

    return {
      ...n,
      data: {
        ...n.data,
        onQuickAdd,
        onDuplicate,
        onViewCode,
        executionBadge,
        validationSummary: nodeValidationSummary(
          n.data.node,
          validationReport,
          n.data.componentSpec ?? null,
          manifest,
        ),
      },
      selected: selectedSet.has(n.id),
      style: outlineColor ? { boxShadow: `0 0 0 3px ${outlineColor}` } : undefined,
    };
  });

  const decoratedEdges: Edge[] = edges.map((e) => ({
    ...e,
    animated: e.animated || (pathSet.has(e.source) && pathSet.has(e.target)),
    style: pathSet.has(e.source) && pathSet.has(e.target)
      ? { stroke: "#22c55e", strokeWidth: 2.5 }
      : e.style,
  }));

  const handleConnectStart = useCallback(
    (_event: unknown, params: { nodeId: string | null }) => {
      connectSourceRef.current = params.nodeId;
    },
    [],
  );

  const handleConnect: OnConnect = useCallback(
    (c) => {
      connectSourceRef.current = null;
      if (c.source && c.target) onConnect?.({ source: c.source, target: c.target });
    },
    [onConnect],
  );

  const handleConnectEnd: OnConnectEnd = useCallback(
    (event) => {
      const sourceId = connectSourceRef.current;
      connectSourceRef.current = null;
      if (!sourceId || !onConnectionDrop) return;
      // If dropped on a node handle, handleConnect already fired — skip.
      const target = (event as MouseEvent).target as HTMLElement | null;
      if (target?.closest(".react-flow__handle")) return;
      // Dropped on empty canvas → open quick-add at that position.
      const { clientX, clientY } = event as MouseEvent;
      const flowPos = screenToFlowPosition({ x: clientX, y: clientY });
      onConnectionDrop(sourceId, flowPos, { x: clientX, y: clientY });
    },
    [onConnectionDrop, screenToFlowPosition],
  );

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const type = e.dataTransfer.getData("application/caliber-node-type");
      if (!type || !onDropNode) return;
      // Convert the drop point to flow coordinates so the node lands under the
      // cursor regardless of the current canvas pan/zoom.
      const position = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      onDropNode(type, position);
    },
    [onDropNode, screenToFlowPosition],
  );

  return (
    <div
      className={className ?? "h-full w-full"}
      data-testid="wf-canvas"
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      <ReactFlow
        nodes={decoratedNodes}
        edges={decoratedEdges}
        nodeTypes={NODE_TYPES}
        defaultEdgeOptions={DEFAULT_EDGE_OPTIONS}
        connectionLineType={ConnectionLineType.SmoothStep}
        connectionLineStyle={{ stroke: "#4F46E5", strokeWidth: 2, strokeDasharray: "6 3" }}
        onNodeClick={(_e, node) => onSelectNode?.(node.id)}
        onSelectionChange={
          onSelectionChange
            ? ({ nodes: selNodes }) => onSelectionChange(selNodes.map((n) => n.id))
            : undefined
        }
        onNodeDoubleClick={(_e, node) => onNodeDoubleClick?.(node.id)}
        onEdgeClick={(event, edge) => {
          const maybeMouse = event as unknown as { clientX?: number; clientY?: number };
          const x = maybeMouse.clientX;
          const y = maybeMouse.clientY;
          if (typeof x === "number" && Number.isFinite(x) && typeof y === "number" && Number.isFinite(y)) {
            onEdgeClick?.(edge.id, { x, y });
            return;
          }
          onEdgeClick?.(edge.id);
        }}
        onNodeDragStop={(_event, node) =>
          onNodePositionChange?.(node.id, { x: node.position.x, y: node.position.y })
        }
        onPaneClick={() => onSelectNode?.(null)}
        onConnectStart={handleConnectStart}
        onConnect={handleConnect}
        onConnectEnd={handleConnectEnd}
        connectionMode={ConnectionMode.Loose}
        fitView
        proOptions={{ hideAttribution: true }}
        snapToGrid
        snapGrid={[20, 20]}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#A1A1AA" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          nodeColor={(n) => {
            const nd = n.data as FlowNodeData | undefined;
            return nd?.node ? nodeColor(nd.node.type) : "#6B7280";
          }}
          maskColor="rgba(0,0,0,.06)"
          style={{ borderRadius: 8, border: "1px solid #E4E4E7" }}
        />
      </ReactFlow>
    </div>
  );
}

/** Public export — wraps CanvasInner in ReactFlowProvider for useReactFlow(). */
export function Canvas(props: CanvasProps): JSX.Element {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}
