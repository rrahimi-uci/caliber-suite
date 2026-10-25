import { fireEvent } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type {
  KnowledgeGraphEntityView,
  KnowledgeGraphRelationshipView,
  KnowledgeQueryVersionResult,
} from "@/api/knowledgeTypes";
import {
  KnowledgeGraphCanvas,
  KnowledgeQueryCompactResultCard,
} from "@/pages/KnowledgeBases";
import { render, screen, within } from "@/test/utils";

function entity(
  id: string,
  overrides: Partial<KnowledgeGraphEntityView> = {},
): KnowledgeGraphEntityView {
  return {
    knowledge_base_entity_id: id,
    knowledge_base_version_id: "v1",
    entity_key: id.toLowerCase(),
    label: id,
    entity_type: "person",
    aliases: [],
    source_keys: [],
    mention_count: 1,
    distance: 3,
    highlighted: false,
    ...overrides,
  } as KnowledgeGraphEntityView;
}

function relationship(
  source: string,
  target: string,
  overrides: Partial<KnowledgeGraphRelationshipView> = {},
): KnowledgeGraphRelationshipView {
  return {
    knowledge_base_relationship_id: `${source}-${target}`,
    knowledge_base_version_id: "v1",
    source_entity_id: source,
    target_entity_id: target,
    source_entity_label: source,
    target_entity_label: target,
    relationship_type: "knows",
    weight: 1,
    ...overrides,
  } as KnowledgeGraphRelationshipView;
}

function queryResult(
  overrides: Record<string, unknown> = {},
): KnowledgeQueryVersionResult {
  return {
    knowledge_base_version_id: "v1",
    knowledge_base_name: "Operations",
    version_number: 2,
    chunking_strategy: "recursive",
    retrieval_mode: "graph_hybrid",
    answer: "Grounded answer",
    answer_error: null,
    timing_ms: { total: 12 },
    graph_context: {
      boosted_chunk_count: 2,
      matched_entities: ["Bob"],
      expanded_entities: ["Platform"],
      age_seed_strategy: "query_entities",
      age_matched_chunk_count: 3,
      fallback_retrieval_mode: null,
      fallback_reason: null,
      age_graph_name: "knowledge_graph",
      age_configured_seed_mode: "entity_then_text",
      age_traversal_hops: 2,
      minimum_relationship_weight: 1,
      strict_age_retrieval: false,
    },
    citations: [
      { chunk_id: "c1", object_store_path: "/objects/c1", label: "runbook.md" },
    ],
    retrieved_chunks: [
      {
        chunk_id: "c1",
        object_store_path: "/objects/c1",
        source_name: "runbook.md",
        score: 0.91,
        score_breakdown: {
          dense: 0.7,
          dense_rerank: 0.2,
          graph_boost: 0.1,
          age_graph: 0.4,
        },
        matched_entity_labels: ["Bob"],
        content: "Restart the worker.",
      },
    ],
    ...overrides,
  } as KnowledgeQueryVersionResult;
}

describe("knowledge graph visualization state coverage", () => {
  it("covers loading, local fallback, selected tail, and trail navigation states", () => {
    const loading = render(
      <KnowledgeGraphCanvas
        entities={[]}
        relationships={[]}
        isLoading
        searchQuery=""
        sourceMode="age"
      />,
    );
    expect(screen.getByText("Loading the graph neighborhood for this version…")).toBeInTheDocument();
    expect(screen.getByText(/preparing the AGE-backed neighborhood/)).toBeInTheDocument();
    loading.unmount();

    const onSelectEntity = vi.fn();
    const entities = Array.from({ length: 16 }, (_, index) =>
      entity(`Tail-${index}`, {
        label: index === 15 ? "A very long graph entity label" : `Tail-${index}`,
        distance: index === 0 ? 0 : index === 1 ? 1 : index === 2 ? 2 : undefined,
        highlighted: index === 0,
        entity_type: index === 3 ? "service" : "person",
        aliases: index === 4 ? ["tail-alias"] : [],
        source_keys: index === 5 ? ["tail-source"] : [],
        mention_count: index + 1,
      }),
    );
    render(
      <KnowledgeGraphCanvas
        entities={entities}
        relationships={[
          relationship("Tail-0", "Tail-1", { relationship_type: "owns", weight: 4 }),
          relationship("Tail-2", "Tail-3", { relationship_type: "uses", weight: 1 }),
        ]}
        searchQuery="tail-source"
        sourceMode="local"
        selectedEntityId="Tail-15"
        onSelectEntity={onSelectEntity}
      />,
    );
    expect(screen.getByText("Version graph explorer")).toBeInTheDocument();
    expect(screen.getByText("Version graph artifacts")).toBeInTheDocument();
    expect(screen.getByTestId("graph-node-Tail-15")).toBeInTheDocument();
    expect(screen.getByText("A very long graph entity label".slice(0, 18) + "…")).toBeInTheDocument();
    expect(screen.getByText("No visible trails match the current node and graph filters.")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByTestId("graph-node-Tail-15"), { key: "Enter" });
    expect(onSelectEntity).toHaveBeenCalledWith("Tail-15");
  });

  it("covers graph fallback metadata and every retrieved-score decoration", () => {
    render(
      <KnowledgeQueryCompactResultCard
        result={queryResult({
          retrieval_mode: "age_graph",
          timing_ms: {},
          answer: "AGE answer",
          graph_context: {
            matched_entities: ["Alice"],
            expanded_entities: ["Bob"],
            age_seed_strategy: "unknown_seed_strategy",
            age_matched_chunk_count: null,
            fallback_retrieval_mode: "dense",
            fallback_reason: "graph unavailable",
            age_fallback_reason: "AGE timeout",
            age_graph_name: null,
            age_configured_seed_mode: "unknown_seed_mode",
            age_traversal_hops: 0,
            minimum_relationship_weight: 0,
            strict_age_retrieval: false,
          },
          citations: [{ chunk_id: "c1", object_store_path: "/objects/c1", label: "runbook.md" }],
          retrieved_chunks: [
            {
              chunk_id: "c3",
              object_store_path: "/objects/c3",
              source_name: "plain.md",
              score: 0,
              score_breakdown: { dense: 0, dense_rerank: 0, graph_boost: 0, age_graph: 0 },
              matched_entity_labels: [],
              content: "Plain chunk",
            },
            {
              chunk_id: "c4",
              object_store_path: "/objects/c4",
              source_name: "rich.md",
              score: 0.8,
              score_breakdown: { dense: 0.4, dense_rerank: 0.2, graph_boost: 0.1, age_graph: 0.3 },
              matched_entity_labels: ["Alice"],
              content: "Rich chunk",
            },
          ],
        })}
      />,
    );
    expect(screen.getByText("Fell back to Dense chunks")).toBeInTheDocument();
    expect(screen.getByText("AGE retrieval seeded")).toBeInTheDocument();
    expect(screen.getByText(/Graph metadata available/)).toBeInTheDocument();
    expect(screen.getByText(/seed Entity first, then question text/)).toBeInTheDocument();
    expect(screen.getByText(/0 hop traversal/)).toBeInTheDocument();
    expect(screen.getByText(/min weight 0/)).toBeInTheDocument();
    expect(screen.getByText(/fallback AGE timeout/)).toBeInTheDocument();
    expect(screen.getByText("plain.md")).toBeInTheDocument();
    expect(screen.getByText("rich.md")).toBeInTheDocument();
    expect(screen.getByText("dense-rerank 0.200")).toBeInTheDocument();
    expect(screen.getByText("graph +0.100")).toBeInTheDocument();
    expect(screen.getByText("age 0.30")).toBeInTheDocument();
    expect(screen.getAllByText("Alice")).toHaveLength(2);
  });

  it("covers sparse graph-query results without citations or retrieved chunks", () => {
    render(
      <KnowledgeQueryCompactResultCard
        result={queryResult({
          knowledge_base_name: "Sparse",
          version_number: 1,
          retrieval_mode: "graph_hybrid",
          answer: null,
          answer_error: null,
          timing_ms: {},
          graph_context: {
            boosted_chunk_count: null,
            matched_entities: [],
            expanded_entities: [],
            age_seed_strategy: null,
            age_matched_chunk_count: null,
            fallback_retrieval_mode: null,
            fallback_reason: null,
            age_fallback_reason: null,
            age_graph_name: null,
            age_configured_seed_mode: null,
            age_traversal_hops: null,
            minimum_relationship_weight: null,
            strict_age_retrieval: false,
          },
          citations: [],
          retrieved_chunks: [],
        })}
      />,
    );
    expect(screen.getByText("No answer returned.")).toBeInTheDocument();
    expect(screen.getByText("No direct entity matches for this question.")).toBeInTheDocument();
    expect(screen.queryByText("Citations")).not.toBeInTheDocument();
    expect(screen.queryByText("Retrieved chunks")).not.toBeInTheDocument();
  });

  it("renders empty, local, AGE, selected, highlighted, and ringed graph states", async () => {
    const onSelectEntity = vi.fn();
    const empty = render(
      <KnowledgeGraphCanvas
        entities={[]}
        relationships={[]}
        isLoading={false}
        searchQuery=""
        onSelectEntity={onSelectEntity}
      />,
    );
    expect(screen.getByText("No graph nodes are available for this version yet.")).toBeInTheDocument();
    empty.unmount();

    const entities = [
      entity("Seed", { distance: 0, highlighted: true, mention_count: 10, aliases: ["primary"] }),
      entity("Inner", { distance: 1, mention_count: 5 }),
      entity("Middle", { distance: 2, mention_count: 4 }),
      entity("Outer", { distance: undefined, source_keys: ["needle-key"] }),
      ...Array.from({ length: 11 }, (_, index) => entity(`Entity-${index}`, { distance: 3, mention_count: index + 1 })),
    ];
    render(
      <KnowledgeGraphCanvas
        entities={entities}
        relationships={[
          relationship("Seed", "Inner", { weight: 3, relationship_type: "owns" }),
          relationship("Inner", "Middle", { weight: 2 }),
          relationship("Middle", "Outer", { weight: 1 }),
          relationship("Seed", "missing", { weight: 9 }),
        ]}
        searchQuery="needle"
        sourceMode="age"
        sourceSummary="AGE result"
        graphName="knowledge_graph"
        ageSeedStrategy="query_entities"
        selectedEntityId="Entity-10"
        onSelectEntity={onSelectEntity}
      />,
    );
    expect(screen.getByText("AGE-backed")).toBeInTheDocument();
    expect(screen.getByText("AGE result")).toBeInTheDocument();
    expect(screen.getByText("knowledge_graph")).toBeInTheDocument();
    expect(screen.getByTestId("graph-node-Entity-10")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("graph-node-Seed"));
    fireEvent.keyDown(screen.getByTestId("graph-node-Inner"), { key: "Enter" });
    fireEvent.keyDown(screen.getByTestId("graph-node-Middle"), { key: " " });
    fireEvent.keyDown(screen.getByTestId("graph-node-Entity-10"), { key: "Escape" });
    expect(onSelectEntity).toHaveBeenCalledWith("Seed");
    expect(onSelectEntity).toHaveBeenCalledWith("Inner");
    expect(onSelectEntity).toHaveBeenCalledWith("Middle");
    expect(onSelectEntity).not.toHaveBeenCalledWith("Outer");
  });

  it("renders selected relationship trails and handles a missing selection", () => {
    const onSelectEntity = vi.fn();
    const selected = render(
      <KnowledgeGraphCanvas
        entities={[
          entity("Center", { highlighted: true, distance: 0, label: "Center" }),
          entity("Neighbor", { distance: 1, label: "Neighbor" }),
          entity("Other", { distance: 2, label: "Other" }),
        ]}
        relationships={[
          relationship("Center", "Neighbor", {
            relationship_type: "supports",
            weight: 2.5,
          }),
          relationship("Neighbor", "Other", {
            relationship_type: "references",
            weight: 1,
          }),
        ]}
        searchQuery="center"
        selectedEntityId="Center"
        onSelectEntity={onSelectEntity}
      />,
    );
    expect(screen.getByText("Center relationship trails")).toBeInTheDocument();
    const trails = screen.getByTestId("knowledge-graph-canvas-trails");
    expect(within(trails).getByRole("button", { name: /Neighbor.*supports/ })).toBeInTheDocument();
    fireEvent.click(within(trails).getByRole("button", { name: /Neighbor.*supports/ }));
    expect(onSelectEntity).toHaveBeenCalledWith("Neighbor");
    selected.unmount();

    render(
      <KnowledgeGraphCanvas
        entities={[entity("Only", { highlighted: true, distance: 0 })]}
        relationships={[]}
        searchQuery=""
        selectedEntityId="missing"
      />,
    );
    expect(screen.queryByText("Only relationship trails")).not.toBeInTheDocument();
    expect(screen.getByText("Relationship trails in view")).toBeInTheDocument();
  });

  it("covers dense, hybrid, and AGE query result presentation branches", () => {
    const dense = render(
      <KnowledgeQueryCompactResultCard
        result={queryResult({
          retrieval_mode: "dense",
          answer: null,
          answer_error: "No answer",
          citations: [],
          retrieved_chunks: [],
          graph_context: {},
        })}
      />,
    );
    expect(screen.getByText("No answer")).toBeInTheDocument();
    expect(screen.queryByText("Graph context")).not.toBeInTheDocument();
    dense.unmount();

    const hybrid = render(
      <KnowledgeQueryCompactResultCard
        result={queryResult({
          retrieval_mode: "graph_hybrid",
          answer: null,
          answer_error: null,
          citations: [],
          retrieved_chunks: [
            {
              chunk_id: "c2",
              object_store_path: "/objects/c2",
              source_name: "empty.md",
              score: 0.2,
              score_breakdown: {},
              matched_entity_labels: [],
              content: "No direct match",
            },
          ],
          graph_context: {
            matched_entities: [],
            expanded_entities: [],
            boosted_chunk_count: null,
          },
        })}
      />,
    );
    expect(screen.getByText("No answer returned.")).toBeInTheDocument();
    expect(screen.getByText("No direct entity matches for this question.")).toBeInTheDocument();
    expect(screen.getByText("empty.md")).toBeInTheDocument();
    hybrid.unmount();

    render(
      <KnowledgeQueryCompactResultCard
        result={queryResult({
          retrieval_mode: "age_graph",
          graph_context: {
            matched_entities: [],
            expanded_entities: [],
            age_seed_strategy: null,
            age_matched_chunk_count: 0,
            fallback_reason: "AGE unavailable",
            age_fallback_reason: "AGE unavailable",
            fallback_retrieval_mode: null,
            age_graph_name: null,
            age_configured_seed_mode: null,
            age_traversal_hops: null,
            minimum_relationship_weight: null,
            strict_age_retrieval: true,
          },
        })}
      />,
    );
    expect(screen.getByText(/AGE retrieval seeded/)).toBeInTheDocument();
    expect(screen.getByText(/Strict AGE mode prevented fallback/)).toBeInTheDocument();
    expect(screen.getByText(/fallback AGE unavailable/)).toBeInTheDocument();
  });
});
