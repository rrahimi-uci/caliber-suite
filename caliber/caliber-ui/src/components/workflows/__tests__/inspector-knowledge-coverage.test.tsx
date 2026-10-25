import { fireEvent } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { Inspector } from "@/components/workflows/Inspector";
import { render, screen } from "@/test/utils";
import { server } from "@/test/server";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";
const KB = `${API_BASE}/knowledge-bases`;

function envelope<T>(data: T): { data: T } {
  return { data };
}

function manifest(node: Record<string, unknown>) {
  return {
    schema_version: 1,
    workflow_id: "knowledge-parent",
    name: "Knowledge parent",
    nodes: { [String(node.id)]: node },
    edges: [],
  } as never;
}

function renderNode(node: Record<string, unknown>) {
  const onChangeNode = vi.fn();
  const view = render(
    <Inspector
      manifest={manifest(node)}
      selectedNodeId={String(node.id)}
      tools={[]}
      onChangeNode={onChangeNode}
      onChangeWorkflow={vi.fn()}
    />,
  );
  return { ...view, onChangeNode };
}

function knowledgeOptions(overrides: Record<string, unknown> = {}) {
  return {
    chunking_strategies: [],
    embedding_models: [],
    retrieval_modes: [],
    graph_retrieval_strengths: [],
    graph_age_seed_modes: [],
    graph_query_presets: [],
    age_enabled: false,
    ...overrides,
  };
}

function knowledgeBase(overrides: Record<string, unknown> = {}) {
  return {
    knowledge_base_id: "KB-1",
    name: "Contracts KB",
    status: "active",
    active_version_id: null,
    active_version_summary: null,
    ...overrides,
  };
}

function queryNode(overrides: Record<string, unknown> = {}) {
  return {
    id: "query",
    type: "knowledge_query",
    knowledge_base_id: "",
    retrieval_modes: [],
    version_ids: [],
    ...overrides,
  };
}

function buildNode(overrides: Record<string, unknown> = {}) {
  return {
    id: "build",
    type: "knowledge_build",
    knowledge_base_id: "KB-1",
    chunking_strategy: "",
    embedding_model: "",
    wait_for_completion: false,
    activate_when_complete: false,
    ...overrides,
  };
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});
afterAll(() => server.close());

describe("Inspector knowledge query and build failure states", () => {
  it("surfaces independent options, knowledge-base, and version request failures", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json({ detail: "options failed" }, { status: 500 }),
      ),
      http.get(`${KB}`, () =>
        HttpResponse.json(envelope([knowledgeBase()]), { status: 200 }),
      ),
    );
    const optionsFailure = renderNode(queryNode());
    expect(await screen.findByText("options failed")).toBeInTheDocument();
    optionsFailure.unmount();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions())),
      ),
      http.get(`${KB}`, () =>
        HttpResponse.json({ detail: "knowledge bases failed" }, { status: 500 }),
      ),
    );
    const baseFailure = renderNode(queryNode());
    expect(await screen.findByText("knowledge bases failed")).toBeInTheDocument();
    baseFailure.unmount();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions())),
      ),
      http.get(`${KB}`, () =>
        HttpResponse.json(envelope([knowledgeBase()])),
      ),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json({ detail: "versions failed" }, { status: 500 }),
      ),
    );
    renderNode(queryNode({ knowledge_base_id: "KB-1" }));
    expect(await screen.findByText("versions failed")).toBeInTheDocument();
  });

  it("renders knowledge build fallbacks when the catalog is empty or unavailable", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(envelope(knowledgeOptions())),
      ),
      http.get(`${KB}`, () => HttpResponse.json(envelope([]))),
    );
    const empty = renderNode(buildNode({ knowledge_base_id: "" }));
    expect(await screen.findByText("No completed version yet")).toBeInTheDocument();
    expect(screen.getByTestId("inspector-knowledge-build-embedding")).toBeDisabled();
    expect(screen.getByTestId("inspector-knowledge-build-base")).toHaveValue("");
    empty.unmount();

    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json({ detail: "catalog unavailable" }, { status: 503 }),
      ),
      http.get(`${KB}`, () =>
        HttpResponse.json(envelope([knowledgeBase()])),
      ),
    );
    renderNode(buildNode());
    expect(await screen.findByTestId("knowledge-build-profile")).toHaveTextContent(
      "Awaiting build",
    );
    expect(screen.getByTestId("inspector-knowledge-build-embedding")).toBeDisabled();
  });

  it("uses the generic blocked-embedding message when a selected option omits its reason", async () => {
    server.use(
      http.get(`${KB}/options`, () =>
        HttpResponse.json(
          envelope(
            knowledgeOptions({
              embedding_models: [
                {
                  id: "blocked-model",
                  name: "Blocked model",
                  available: false,
                  unavailable_reason: null,
                },
              ],
            }),
          ),
        ),
      ),
      http.get(`${KB}`, () =>
        HttpResponse.json(envelope([knowledgeBase()])),
      ),
    );
    renderNode(buildNode({ embedding_model: "blocked-model" }));
    expect(
      await screen.findByText("This embedding model is unavailable in the current runtime."),
    ).toBeInTheDocument();
    expect(screen.getByTestId("inspector-knowledge-build-embedding")).toBeDisabled();
  });

  it("covers rich knowledge build controls and AGE-aware query presets", async () => {
    const options = knowledgeOptions({
      chunking_strategies: [{ id: "recursive", name: "Recursive" }],
      embedding_models: [{ id: "embed-v2", name: "Embed v2", available: true }],
      retrieval_modes: [
        { id: "dense", name: "Dense", description: "Dense chunks" },
        { id: "graph_hybrid", name: "GraphRAG", description: "Graph hybrid" },
        { id: "age_graph", name: "AGE", description: "AGE traversal" },
      ],
      graph_retrieval_strengths: [{ id: "focused", name: "Focused" }],
      graph_age_seed_modes: [{ id: "query_entities_only", name: "Entities only" }],
      graph_query_presets: [
        {
          id: "age_native",
          label: "AGE native",
          eyebrow: "Graph",
          description: "Prefer graph traversal",
          retrieval_mode: "age_graph",
          age_required: true,
          retrieval_strength: "focused",
          minimum_relationship_weight: 2,
          age_seed_mode: "query_entities_only",
          age_traversal_hops: 2,
          age_candidate_pool_size: 40,
          age_dense_rerank_weight: 0.2,
          strict_age_retrieval: true,
        },
      ],
      default_graph_config: {
        retrieval_strength: "balanced",
        minimum_relationship_weight: 1,
        age_seed_mode: "entity_then_text",
        age_traversal_hops: 1,
        age_candidate_pool_size: 24,
        age_dense_rerank_weight: 0.35,
      },
      age_enabled: true,
      age_graph_name: "knowledge_graph",
    });
    const activeSummary = {
      version_number: 2,
      chunking_strategy: "recursive",
      embedding_model: "embed-v2",
      graph_target: "object_store",
      default_retrieval_mode: "graph_hybrid",
      retrieval_strength: "balanced",
      age_ready: false,
      age_sync_status: "pending",
    };
    const base = knowledgeBase({
      active_version_id: "v2",
      active_version_summary: activeSummary,
    });
    const readyVersion = {
      knowledge_base_version_id: "v1",
      knowledge_base_id: "KB-1",
      version_number: 1,
      chunking_strategy: "recursive",
      embedding_model: "embed-v1",
      graph_config: {
        output_target: "object_store_and_age",
        default_retrieval_mode: "age_graph",
        retrieval_strength: "balanced",
        minimum_relationship_weight: 1,
        age_seed_mode: "entity_then_text",
        age_traversal_hops: 1,
        age_candidate_pool_size: 24,
        age_dense_rerank_weight: 0.35,
      },
      summary: { age_sync_status: "synced" },
    };
    const activeVersion = {
      ...readyVersion,
      knowledge_base_version_id: "v2",
      version_number: 2,
      embedding_model: "embed-v2",
      graph_config: { ...readyVersion.graph_config, output_target: "object_store" },
      summary: { age_sync_status: "pending" },
    };
    server.use(
      http.get(`${KB}/options`, () => HttpResponse.json(envelope(options))),
      http.get(`${KB}`, () => HttpResponse.json(envelope([base]))),
      http.get(`${KB}/KB-1/versions`, () =>
        HttpResponse.json(envelope([activeVersion, readyVersion])),
      ),
    );

    const build = renderNode(
      buildNode({
        knowledge_base_id: "KB-1",
        chunking_strategy: "recursive",
        embedding_model: "embed-v2",
        wait_for_completion: true,
        activate_when_complete: true,
        wait_timeout_seconds: 600,
      }),
    );
    expect(await screen.findByText("Active v2")).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("inspector-knowledge-build-base"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-build-chunker"), {
      target: { value: "recursive" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-build-embedding"), {
      target: { value: "embed-v2" },
    });
    fireEvent.click(screen.getByTestId("inspector-knowledge-build-wait"));
    fireEvent.change(screen.getByTestId("inspector-knowledge-build-timeout"), {
      target: { value: "900" },
    });
    fireEvent.click(screen.getByTestId("inspector-knowledge-build-activate"));
    expect(build.onChangeNode).toHaveBeenCalledWith("build", {
      wait_timeout_seconds: 900,
    });
    build.unmount();

    const query = renderNode(
      queryNode({ knowledge_base_id: "KB-1", retrieval_modes: [] }),
    );
    expect(await screen.findByText("Object-store graph")).toBeInTheDocument();
    expect(screen.getByText(/pin v1 automatically/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "AGE graph" }));
    fireEvent.click(screen.getByTestId("knowledge-query-preset-age_native"));
    fireEvent.change(screen.getByTestId("inspector-knowledge-top-k"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-chat-model"), {
      target: { value: "chat-v2" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-strength"), {
      target: { value: "focused" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-min-weight"), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-age-seed-mode"), {
      target: { value: "query_entities_only" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-age-hops"), {
      target: { value: "2" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-age-pool"), {
      target: { value: "40" },
    });
    fireEvent.change(screen.getByTestId("inspector-knowledge-age-dense-weight"), {
      target: { value: "0.2" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Strict AGE retrieval" }));
    expect(query.onChangeNode).toHaveBeenCalledWith(
      "query",
      expect.objectContaining({ retrieval_modes: ["age_graph"] }),
    );
    expect(query.onChangeNode).toHaveBeenCalledWith("query", {
      top_k: 10,
    });
    expect(query.onChangeNode).toHaveBeenCalledWith("query", {
      chat_model: "chat-v2",
    });
    expect(query.onChangeNode.mock.calls.length).toBeGreaterThan(8);
  });

  it("covers active trigger aliases and successful cron preview rendering", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/knowledge-parent/deployments`, () =>
        HttpResponse.json(
          envelope([
            { alias: "prod", status: "active" },
            { alias: "dev", status: "paused" },
          ]),
        ),
      ),
      http.get(`${API_BASE}/workflow-cron-preview`, () =>
        HttpResponse.json(
          envelope({
            timezone: "UTC",
            fire_times: ["2026-01-02T09:00:00Z", "2026-01-03T09:00:00Z"],
          }),
        ),
      ),
    );

    const cron = renderNode({
      id: "start",
      type: "start",
      trigger: {
        mode: "cron",
        cron: "0 9 * * *",
        timezone: "UTC",
        alias: "dev",
        enabled: true,
      },
    });
    const fireTimes = await screen.findAllByTestId("inspector-start-cron-fire-time");
    expect(fireTimes[0]).toHaveTextContent("2026-01-02 09:00");
    expect(screen.getByText(/Alias dev is not currently active/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("inspector-start-alias-prod"));
    expect(cron.onChangeNode).toHaveBeenCalledWith("start", {
      trigger: expect.objectContaining({ alias: "prod" }),
    });
    cron.unmount();

    const event = renderNode({
      id: "event-start",
      type: "start",
      trigger: { mode: "event", event_name: "ready", alias: "prod", enabled: true },
    });
    expect(await screen.findByText(/targets active deployment alias prod/)).toBeInTheDocument();
    event.unmount();
  });

  it("covers unavailable and empty deployment aliases plus an empty cron preview", async () => {
    server.use(
      http.get(`${API_BASE}/workflows/knowledge-parent/deployments`, () =>
        HttpResponse.json({ detail: "deployment lookup failed" }, { status: 503 }),
      ),
    );
    const unavailable = renderNode({
      id: "unavailable-start",
      type: "start",
      trigger: { mode: "event", event_name: "ticket.created", alias: "prod" },
    });
    expect(
      await screen.findByText(/Deployment aliases could not be loaded right now/),
    ).toBeInTheDocument();
    unavailable.unmount();

    server.use(
      http.get(`${API_BASE}/workflows/knowledge-parent/deployments`, () =>
        HttpResponse.json(envelope([])),
      ),
      http.get(`${API_BASE}/workflow-cron-preview`, () =>
        HttpResponse.json(envelope({ timezone: "UTC", fire_times: [] })),
      ),
    );
    const empty = renderNode({
      id: "empty-start",
      type: "start",
      trigger: { mode: "cron", cron: "0 0 1 1 *", timezone: "UTC", alias: "" },
    });
    expect(
      await screen.findByText(/This expression has no upcoming runs in the next year/),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/Set the deployment alias this trigger should target/),
    ).toBeInTheDocument();
    empty.unmount();

    const noDeployments = renderNode({
      id: "no-deployments-start",
      type: "start",
      trigger: { mode: "event", event_name: "ticket.created", alias: "prod" },
    });
    expect(
      await screen.findByText(/No active deployments exist for this workflow yet/),
    ).toBeInTheDocument();
    noDeployments.unmount();
  });
});
