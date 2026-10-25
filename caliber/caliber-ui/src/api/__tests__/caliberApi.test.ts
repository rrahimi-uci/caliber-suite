import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearLocalAuthSession,
  createLocalAuthSession,
  getStoredAuthSession,
  saveLocalAuthSession,
} from "@/auth/localAuth";
import { setActiveProjectId } from "@/workspace/activeWorkspace";

const API_BASE = "/ajax-api/2.0/mlflow/caliber";

function envelope<T>(data: T): { data: T } {
  return { data };
}

function ok<T>(data: T): Response {
  return new Response(JSON.stringify(envelope(data)), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function jsonError(status: number, detail: string): Response {
  return new Response(JSON.stringify({ detail, status_code: status }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function textError(status: number, statusText: string): Response {
  return new Response("error", { status, statusText });
}

function deferredBody<T>(value: T): {
  response: Response;
  started: Promise<void>;
  resolve: () => void;
} {
  let markStarted!: () => void;
  let resolveBody!: () => void;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  const body = new Promise<void>((resolve) => {
    resolveBody = resolve;
  });
  const response = {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async (): Promise<T> => {
      markStarted();
      await body;
      return value;
    },
  } as unknown as Response;
  return { response, started, resolve: resolveBody };
}

function deferredBlob(value: Blob): {
  response: Response;
  started: Promise<void>;
  resolve: () => void;
} {
  let markStarted!: () => void;
  let resolveBody!: () => void;
  const started = new Promise<void>((resolve) => {
    markStarted = resolve;
  });
  const body = new Promise<void>((resolve) => {
    resolveBody = resolve;
  });
  const response = {
    ok: true,
    status: 200,
    statusText: "OK",
    blob: async (): Promise<Blob> => {
      markStarted();
      await body;
      return value;
    },
  } as unknown as Response;
  return { response, started, resolve: resolveBody };
}

async function loadApi(): Promise<typeof import("@/api/caliberApi")> {
  vi.resetModules();
  return import("@/api/caliberApi");
}

describe("caliberApi", () => {
  beforeEach(() => {
    clearLocalAuthSession();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    clearLocalAuthSession();
    setActiveProjectId(null);
  });

  it("deduplicates CSRF bootstrap and sends CSRF/user headers on writes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        ok({ enabled: true, token: "csrf-1", ttl_seconds: 120 }),
      )
      .mockResolvedValueOnce(ok({ agent_id: "support-agent" }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    saveLocalAuthSession(createLocalAuthSession("admin"));

    const [first, second] = await Promise.all([
      api.bootstrapCsrf(),
      api.bootstrapCsrf(),
    ]);
    expect(first.token).toBe("csrf-1");
    expect(second.token).toBe("csrf-1");

    await api.caliberApi.registerAgent({
      agent_id: "support-agent",
      experiment_id: "exp-1",
      name: "Support Agent",
    } as never);

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers["X-CALIBER-CSRF"]).toBe("csrf-1");
    // "@admin", not "@local-admin": identityForUsername no longer maps the old default
    // username to a privileged identity, because there is no default credential.
    // The header is still sent for trusted_header deployments; in the shipped session
    // mode the backend ignores it entirely (C1).
    expect(headers["X-CALIBER-User"]).toBe("@admin");
  });

  it("refreshes CSRF token and retries write requests after a CSRF-shaped 403", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        ok({ enabled: true, token: "csrf-old", ttl_seconds: 60 }),
      )
      .mockResolvedValueOnce(jsonError(403, "csrf token expired"))
      .mockResolvedValueOnce(
        ok({ enabled: true, token: "csrf-new", ttl_seconds: 60 }),
      )
      .mockResolvedValueOnce(ok({ skill_id: "sk-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await api.bootstrapCsrf();
    await api.caliberApi.createSkill({
      name: "reasoning",
      content: "Think step by step.",
    } as never);

    expect(fetchMock).toHaveBeenCalledTimes(4);
    const [, finalInit] = fetchMock.mock.calls[3] as [string, RequestInit];
    const headers = finalInit.headers as Record<string, string>;
    expect(headers["X-CALIBER-CSRF"]).toBe("csrf-new");
  });

  it("converts network failures into ApiError while preserving AbortError", async () => {
    const failingFetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new DOMException("aborted", "AbortError"));
    vi.stubGlobal("fetch", failingFetch);

    const api = await loadApi();
    await expect(api.caliberApi.getHealth()).rejects.toMatchObject({
      name: "ApiError",
      status: 0,
    });
    await expect(api.caliberApi.getHealth()).rejects.toMatchObject({
      name: "AbortError",
    });
  });

  it("surfaces JSON/non-JSON API errors and handles 204 responses", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonError(500, "boom"))
      .mockResolvedValueOnce(textError(500, "Upstream failure"))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await expect(api.caliberApi.getMe()).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
      message: "boom",
    });
    await expect(api.caliberApi.getDashboardSummary()).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
      message: "Upstream failure",
    });
    await expect(
      api.caliberApi.deleteWorkflow("WF-1"),
    ).resolves.toBeUndefined();
  });

  it("clears stale browser display state when an API request returns 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonError(401, "session expired")),
    );
    saveLocalAuthSession(createLocalAuthSession("admin"));
    const api = await loadApi();

    await expect(api.caliberApi.getMe()).rejects.toMatchObject({
      name: "ApiError",
      status: 401,
    });
    expect(getStoredAuthSession()).toBeNull();
  });

  it("does not let a stale 401 clear a newer login generation", async () => {
    let resolveRequest!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        }),
      ),
    );
    saveLocalAuthSession({
      ...createLocalAuthSession("user-a"),
      createdAt: "2026-07-30T00:00:00.000Z",
    });
    const api = await loadApi();

    const staleRequest = api.caliberApi.getMe();
    saveLocalAuthSession({
      ...createLocalAuthSession("user-b"),
      // Timestamp equality must not collapse two distinct login generations.
      createdAt: "2026-07-30T00:00:00.000Z",
    });
    resolveRequest(jsonError(401, "old session expired"));

    await expect(staleRequest).rejects.toMatchObject({ status: 401 });
    expect(getStoredAuthSession()?.username).toBe("user-b");
  });

  it("does not let a stale 401 clear a reconciled session with the same generation", async () => {
    let resolveRequest!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        }),
      ),
    );
    const confirmed = createLocalAuthSession("user-a");
    saveLocalAuthSession(confirmed);
    const api = await loadApi();

    const staleRequest = api.caliberApi.getMe();
    // An ambiguous logout reconciliation can confirm the same cookie identity and
    // preserve its generation while advancing the epoch. The older request still
    // belongs to the pre-reconciliation boundary and must not erase it.
    saveLocalAuthSession(confirmed);
    resolveRequest(jsonError(401, "old session expired"));

    await expect(staleRequest).rejects.toMatchObject({ status: 401 });
    expect(getStoredAuthSession()).toEqual(confirmed);
  });

  it("rejects a stale successful response after the auth boundary changes", async () => {
    let resolveRequest!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveRequest = resolve;
        }),
      ),
    );
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    const api = await loadApi();

    const staleRequest = api.caliberApi.getMe();
    saveLocalAuthSession(createLocalAuthSession("user-b"));
    resolveRequest(ok({ user_id: "user-a" }));

    await expect(staleRequest).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
    });
    expect(getStoredAuthSession()?.username).toBe("user-b");
  });

  it("accepts a completed login cookie mutation across a competing display boundary", async () => {
    let resolveLogin!: (response: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockReturnValue(
        new Promise<Response>((resolve) => {
          resolveLogin = resolve;
        }),
      ),
    );
    const api = await loadApi();

    const login = api.caliberApi.login("user-b", "password-b");
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    resolveLogin(ok({ user_id: "user-b", expires_at: "2026-08-01T00:00:00Z" }));

    await expect(login).resolves.toMatchObject({ user_id: "user-b" });
  });

  it("checks the auth fence after a delayed JSON body has been consumed", async () => {
    const body = deferredBody(envelope({ user_id: "user-a" }));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(body.response));
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    const api = await loadApi();

    const request = api.caliberApi.getMe();
    await body.started;
    saveLocalAuthSession(createLocalAuthSession("user-b"));
    body.resolve();

    await expect(request).rejects.toMatchObject({ status: 409 });
  });

  it("does not replay a CSRF-rejected write under a newer auth boundary", async () => {
    let resolveWrite!: (response: Response) => void;
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        ok({ enabled: true, token: "csrf-a", ttl_seconds: 60 }),
      )
      .mockReturnValueOnce(
        new Promise<Response>((resolve) => {
          resolveWrite = resolve;
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    const api = await loadApi();
    await api.bootstrapCsrf();

    const staleWrite = api.caliberApi.createSkill({
      name: "old",
      content: "old",
    } as never);
    saveLocalAuthSession(createLocalAuthSession("user-b"));
    resolveWrite(jsonError(403, "csrf token expired"));

    await expect(staleWrite).rejects.toMatchObject({ status: 409 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("downloads workflow exports with trusted-user and project headers", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("name: support", { status: 200 }))
      .mockResolvedValueOnce(new Response("def run(): pass", { status: 200 }))
      .mockResolvedValueOnce(new Response('{"kind":"bundle"}', { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    saveLocalAuthSession(createLocalAuthSession("admin"));
    setActiveProjectId("PRJ-1");
    const api = await loadApi();

    await api.caliberApi.downloadWorkflowVersionManifest("WFV/1");
    await api.caliberApi.downloadWorkflowVersionPython("WFV/1");
    await api.caliberApi.downloadWorkflowDeploymentBundle("WFV/1");

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      `${API_BASE}/workflow-versions/WFV%2F1/export/manifest`,
      `${API_BASE}/workflow-versions/WFV%2F1/export/python`,
      `${API_BASE}/workflow-versions/WFV%2F1/export/deployment-bundle`,
    ]);
    for (const [, init] of fetchMock.mock.calls as Array<
      [string, RequestInit]
    >) {
      const headers = init.headers as Record<string, string>;
      expect(headers["X-CALIBER-User"]).toBe("@admin");
      expect(headers["X-CALIBER-Project"]).toBe("PRJ-1");
    }
  });

  it("sends checkpoint-aware retry payloads for workflow recovery", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ workflow_run_id: "WR-RETRY" }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();

    await api.caliberApi.retryWorkflowRun("WR/1", {
      reason: "resume from checkpoint",
      checkpoint_id: "CP/2",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe(`${API_BASE}/workflow-runs/WR%2F1/retry`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({
        reason: "resume from checkpoint",
        checkpoint_id: "CP/2",
      }),
    );
  });

  it("sends workflow-scoped external event payloads for wait-event recovery", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ workflow_run_id: "WR-EVENT" }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();

    await api.caliberApi.resumeWorkflowRunByEvent({
      workflow_id: "WF/1",
      event_name: "ticket.approved",
      event_payload: { ticket_id: "T-42", approved: true },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toBe(`${API_BASE}/workflow-runs/resume-by-event`);
    expect(init.method).toBe("POST");
    expect(init.body).toBe(
      JSON.stringify({
        workflow_id: "WF/1",
        event_name: "ticket.approved",
        event_payload: { ticket_id: "T-42", approved: true },
      }),
    );
  });

  it("builds encoded query strings and path params across endpoint wrappers", async () => {
    const fetchMock = vi.fn(async () => ok({}));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await api.caliberApi.getCapabilities();
    await api.caliberApi.getRuntimeConfiguration();
    await api.caliberApi.listVerificationItems({
      status: "pending",
      severity: "critical",
      agent_id: "agent/alpha",
    });
    await api.caliberApi.getVerificationItem("FB/1");
    await api.caliberApi.createVerificationItem({
      agent_id: "support-agent",
      category: "hallucination",
      free_text: "Incorrect refund policy",
      severity: "critical",
    } as never);
    await api.caliberApi.batchVerificationAction(
      "dismiss",
      ["FB-1"],
      "not actionable",
    );
    await api.caliberApi.verifyItem("FB-2", {
      refinement_target: "prompt",
      verification_notes: "",
      severity: "standard",
    });
    await api.caliberApi.dismissItem("FB-3", { reason: "duplicate" });
    await api.caliberApi.markDuplicate("FB-4", {
      duplicate_of_id: "FB-1",
      reason: null,
    });
    await api.caliberApi.listJobs({
      status: "queued",
      stage: "diagnosis",
      agent_id: "support-agent",
      workflow_id: "WF/1",
    } as never);
    await api.caliberApi.getJob("JOB/1");
    await api.caliberApi.getJobTargets("JOB/1");
    await api.caliberApi.applyJob("JOB/1");
    await api.caliberApi.listAgents();
    await api.caliberApi.getAgent("support/agent");
    await api.caliberApi.updateAgent("support/agent", { enabled: false });
    await api.caliberApi.listCheckpoints("support/agent");
    await api.caliberApi.rollbackAgent("support/agent");
    await api.caliberApi.getAgentSkills("support/agent");
    await api.caliberApi.listSkills({ status: "active", tag: "core" });
    await api.caliberApi.listPrompts();
    await api.caliberApi.createPrompt({
      name: "support-agent",
      template: "You are helpful.",
      commit_message: "initial",
    });
    await api.caliberApi.createPromptVersion("support-agent", {
      template: "You are very helpful.",
      commit_message: "iterate",
    });
    await api.caliberApi.getPrompt("support-agent", "staging");
    await api.caliberApi.listPromptVersions("support-agent");
    await api.caliberApi.getPromptVersion("support/agent", 2);
    await api.caliberApi.setPromptAlias("support/agent", "prod", 2);
    await api.caliberApi.testRenderPrompt("support-agent", {
      customer: "Acme",
    });
    await api.caliberApi.getPromptCalibrationOptions();
    await api.caliberApi.createPromptCalibrationRun({
      agent_id: "support-agent",
    } as never);
    await api.caliberApi.getPromptOptimizationOptions();
    await api.caliberApi.createPromptOptimizationRun({
      agent_id: "support-agent",
    } as never);
    await api.caliberApi.getSkill("sk/1");
    await api.caliberApi.getSkillPackage("sk/1");
    await api.caliberApi.createSkill({
      name: "reasoning",
      content: "Think step by step.",
    } as never);
    await api.caliberApi.updateSkill("sk/1", {
      description: "updated",
    } as never);
    await api.caliberApi.testRenderSkill("sk/1", { topic: "refunds" });
    await api.caliberApi.listEvalDatasets({ status: "active", tag: "support" });
    await api.caliberApi.getEvalDataset("ds/1");
    await api.caliberApi.createEvalDataset({ name: "ds" } as never);
    await api.caliberApi.updateEvalDataset("ds/1", {
      description: "next",
    } as never);
    await api.caliberApi.listEvalExamples("ds/1", {
      version: 3,
      includeSuperseded: true,
    });
    await api.caliberApi.appendEvalExample("ds/1", {
      input: {},
      expected: {},
    } as never);
    await api.caliberApi.supersedeEvalExample("ds/1", "ex/1");
    await api.caliberApi.getAssistantDraft("draft/1");
    await api.caliberApi.updateAssistantDraft("draft/1", {
      title: "Updated draft",
    } as never);
    await api.caliberApi.validateAssistantDraft("draft/1");
    await api.caliberApi.testAssistantDraft("draft/1");
    await api.caliberApi.approveAssistantDraft("draft/1");
    await api.caliberApi.publishAssistantDraft("draft/1");
    await api.caliberApi.getAssistantRun("run/1");
    await api.caliberApi.getAssistantConfig();
    await api.caliberApi.updateAssistantConfig({
      model: "gpt-5-pro",
      reasoning: "high",
    });
    await api.caliberApi.listRunFiles("RUN/1", "artifact");
    await api.caliberApi.uploadRunFile(
      "RUN/1",
      new File(["trace"], "trace.json", { type: "application/json" }),
      "artifact",
      { step: "final" },
    );
    await api.caliberApi.registerWorkflowArtifact("RUN/1", "FILE/1", {
      artifact_type: "trace",
      display_name: "Trace",
      summary: "Final run trace",
    });
    await api.caliberApi.uploadPlaygroundFile(
      "PGR/1",
      new File(["input"], "input.txt", { type: "text/plain" }),
      "input",
    );
    await api.caliberApi.listPlaygroundFiles("PGR/1", "input");
    await api.caliberApi.listWorkflowComponents();
    await api.caliberApi.listWorkflowTemplates();
    await api.caliberApi.getKnowledgeOptions();
    await api.caliberApi.listKnowledgeBases({
      status: "all",
      visibility: "project",
    });
    await api.caliberApi.createKnowledgeBase({
      name: "KB",
      source_bucket: "reports",
      sources: [{ kind: "file", path: "docs/guide.md" }],
      chunking_strategy: "recursive",
      embedding_model: "BAAI/bge-m3",
    });
    await api.caliberApi.getKnowledgeBase("KB/1");
    await api.caliberApi.updateKnowledgeBase("KB/1", { status: "archived" });
    await api.caliberApi.listKnowledgeBaseVersions("KB/1");
    await api.caliberApi.createKnowledgeBaseVersion("KB/1", {
      chunking_strategy: "markdown",
      embedding_model: "BAAI/bge-m3",
    });
    await api.caliberApi.activateKnowledgeBaseVersion("KB/1", "KBV/2");
    await api.caliberApi.getKnowledgeBaseVersion("KBV/2");
    await api.caliberApi.syncKnowledgeBaseVersionToAge("KBV/2");
    await api.caliberApi.listKnowledgeBaseSources("KBV/2");
    await api.caliberApi.listKnowledgeBaseChunks("KBV/2", {
      q: "refund",
      sourceKey: "docs/guide.md",
      limit: 5,
    });
    await api.caliberApi.listKnowledgeBaseEntities("KBV/2");
    await api.caliberApi.listKnowledgeBaseRelationships("KBV/2");
    await api.caliberApi.getKnowledgeBaseGraph("KBV/2", {
      source: "age",
      q: "Guide",
      entityType: "document",
      minimumRelationshipWeight: 2,
      traversalHops: 2,
      ageSeedMode: "query_text_only",
      strictAgeRetrieval: true,
      nodeLimit: 16,
    });
    await api.caliberApi.listKnowledgeBaseRuns("KB/1");
    await api.caliberApi.listKnowledgeBaseRunEvents("KBR/1");
    await api.caliberApi.queryKnowledge({
      version_ids: ["KBV/2"],
      question: "What changed?",
      retrieval_modes: ["age_graph"],
      graph_overrides: {
        retrieval_strength: "aggressive",
        age_traversal_hops: 2,
        strict_age_retrieval: true,
      },
    });
    await api.caliberApi.getProjectStorageConfig();
    await api.caliberApi.listProjects("active");
    await api.caliberApi.createProject({
      name: "Support workspace",
      description: "Support files",
      storage_backend: "s3",
    });
    await api.caliberApi.getProject("PRJ/1");
    await api.caliberApi.updateProject("PRJ/1", {
      name: "Support workspace v2",
    });
    await api.caliberApi.archiveProject("PRJ/1");
    await api.caliberApi.restoreProject("PRJ/1");
    await api.caliberApi.transferProjectOwnership("PRJ/1", "@new-owner");
    await api.caliberApi.listProjectFiles("PRJ/1");
    await api.caliberApi.createProjectFolder("PRJ/1", "service/2026");
    await api.caliberApi.deleteProjectFile("PRJ/1", "FILE/1");

    expect(api.caliberApi.skillPackageZipUrl("sk/1")).toContain(
      "/skills/sk%2F1/package.zip",
    );
    expect(api.caliberApi.projectFileContentUrl("PRJ/1", "FILE/9")).toContain(
      "/projects/PRJ%2F1/files/FILE%2F9/content",
    );
    expect(api.caliberApi.runFileContentUrl("RUN/1", "FILE/9")).toContain(
      "/workflow-runs/RUN%2F1/files/FILE%2F9/content",
    );
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/verification-queue?status=pending&severity=critical&agent_id=agent%2Falpha`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/jobs?status=queued&stage=diagnosis&agent_id=support-agent&workflow_id=WF%2F1`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url) === `${API_BASE}/jobs/JOB%2F1/apply` &&
          (init as RequestInit | undefined)?.method === "POST",
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) === `${API_BASE}/prompts/support-agent?alias=staging`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/eval-datasets/ds%2F1/examples?version=3&include_superseded=true`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/workflow-runs/RUN%2F1/files?kind=artifact`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/playground-runs/PGR%2F1/files?kind=input`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) => String(url) === `${API_BASE}/workflow-components`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) => String(url) === `${API_BASE}/workflow-templates`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) => String(url) === `${API_BASE}/projects?status=active`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) => String(url) === `${API_BASE}/projects/PRJ%2F1/folders`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/knowledge-bases?status=all&visibility=project`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/knowledge-base-versions/KBV%2F2/age-sync`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/knowledge-base-versions/KBV%2F2/chunks?q=refund&source_key=docs%2Fguide.md&limit=5`,
      ),
    ).toBe(true);
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          `${API_BASE}/knowledge-base-versions/KBV%2F2/graph?source=age&q=Guide&entity_type=document&minimum_relationship_weight=2&traversal_hops=2&age_seed_mode=query_text_only&strict_age_retrieval=true&node_limit=16`,
      ),
    ).toBe(true);
  });

  it("sends exact requests for project archive/restore/transfer-ownership", async () => {
    // GitHub Copilot review: the giant all-endpoints smoke test above only
    // proves these three calls resolve at all -- its catch-all mock
    // response doesn't check the URL, HTTP verb, or (for transfer) the
    // body, so a typo'd route or a malformed request would still pass
    // there. This pins the exact request each one sends (the Python SDK's
    // own test already does the equivalent).
    const requests: Array<{ method: string; url: string; body: unknown }> =
      [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({
        method: String(init?.method ?? "GET"),
        url: String(input),
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      return ok({ project_id: "PRJ/1" });
    });
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await api.caliberApi.archiveProject("PRJ/1");
    await api.caliberApi.restoreProject("PRJ/1");
    await api.caliberApi.transferProjectOwnership("PRJ/1", "@new-owner");

    expect(requests).toEqual([
      { method: "POST", url: `${API_BASE}/projects/PRJ%2F1/archive`, body: null },
      { method: "POST", url: `${API_BASE}/projects/PRJ%2F1/restore`, body: null },
      {
        method: "POST",
        url: `${API_BASE}/projects/PRJ%2F1/transfer-ownership`,
        body: { new_owner_user_id: "@new-owner" },
      },
    ]);
  });

  it("covers optional query omissions and default endpoint states", async () => {
    const fetchMock = vi.fn(async () => ok({}));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await api.caliberApi.listSkills();
    await api.caliberApi.listEvalDatasets();
    await api.caliberApi.listEvalExamples("DS-1");
    await api.caliberApi.listPromptTestRuns();
    await api.caliberApi.listSkillTestRuns();
    await api.caliberApi.listReleasesTimeline();
    await api.caliberApi.listReleaseOperations();
    await api.caliberApi.listSystemEffects();
    await api.caliberApi.listWebhookDeadLetters();
    await api.caliberApi.listKnowledgeBases();
    await api.caliberApi.listKnowledgeBaseChunks("KBV-1");
    await api.caliberApi.getKnowledgeBaseGraph("KBV-1");
    await api.caliberApi.getGatewayUsage();
    await api.caliberApi.listLlmPricing();
    await api.caliberApi.listAuditLog();
    await api.caliberApi.listWorkflowRunEvents("WR-1");
    await api.caliberApi.getObservabilityMetrics();
    await api.caliberApi.listObservabilityExperiments();
    await api.caliberApi.listObservabilityTraces();
    await api.caliberApi.listWorkflowRunCheckpoints("WR-1");
    await api.caliberApi.listWorkflowSessionMemory("WF-1", "session-1");
    await api.caliberApi.listWorkflowRunsPage("WF-1");
    await api.caliberApi.listObjectStoreObjects("bucket-1");
    await api.caliberApi.listMcpServers();
    await api.caliberApi.listOpenApiIntegrations();
    await api.caliberApi.listOpenApiOperations("oas-1");
    await api.caliberApi.listOpenApiDependencies("oas-1");
    await api.caliberApi.getOpenApiGraph("oas-1");
    await api.caliberApi.listAssistantSessions();

    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls).toContain(`${API_BASE}/skills`);
    expect(urls).toContain(`${API_BASE}/eval-datasets/DS-1/examples`);
    expect(urls).toContain(`${API_BASE}/knowledge-base-versions/KBV-1/graph`);
    expect(urls).toContain(`${API_BASE}/workflow-runs/WR-1/events`);
    expect(urls).toContain(`${API_BASE}/openapi-integrations/oas-1/dependencies`);
    expect(urls).toContain(`${API_BASE}/assistant/sessions`);
    expect(urls.every((url) => !url.endsWith("?") && !url.includes("undefined"))).toBe(true);
  });

  it("uploads multipart files with CSRF refresh and trims project path", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        ok({ enabled: true, token: "csrf-old", ttl_seconds: 30 }),
      )
      .mockResolvedValueOnce(jsonError(403, "csrf token expired"))
      .mockResolvedValueOnce(
        ok({ enabled: true, token: "csrf-new", ttl_seconds: 30 }),
      )
      .mockResolvedValueOnce(
        ok({
          file_id: "FILE-1",
          file_ref: "wf://FILE-1",
          name: "notes.txt",
          kind: "input",
          relative_path: "docs/notes.txt",
          media_type: "text/plain",
          size_bytes: 9,
          metadata_: {},
          uploaded_by: "@test",
          created_at: new Date().toISOString(),
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await api.bootstrapCsrf();
    await api.caliberApi.uploadProjectFile(
      "PRJ/1",
      new File(["hello"], "notes.txt", { type: "text/plain" }),
      "input",
      " docs ",
    );

    expect(fetchMock).toHaveBeenCalledTimes(4);
    const [, init] = fetchMock.mock.calls[3] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers["X-CALIBER-CSRF"]).toBe("csrf-new");
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.signal?.aborted).toBe(true);
    expect(init.body).toBeInstanceOf(FormData);
    const form = init.body as FormData;
    expect(form.get("path")).toBe("docs");
  });

  it("times out a hung GET instead of hanging forever", async () => {
    // A fetch that never resolves on its own — only the abort signal ends it,
    // exactly like a backend route stuck 'pending'.
    const fetchMock = vi.fn(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    api.setApiReadTimeoutMs(50);

    await expect(api.caliberApi.getMe()).rejects.toThrow(/timed out/i);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps the GET deadline active through JSON body parsing", async () => {
    vi.useFakeTimers();
    const body = deferredBody(envelope({ user_id: "admin" }));
    let fetchSignal: AbortSignal | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init: RequestInit) => {
        fetchSignal = init.signal ?? null;
        return Promise.resolve(body.response);
      }),
    );
    const api = await loadApi();
    api.setApiReadTimeoutMs(50);

    const request = api.caliberApi.getMe();
    const rejection = expect(request).rejects.toMatchObject({
      status: 0,
      message: expect.stringMatching(/timed out/i),
    });
    await body.started;
    await vi.advanceTimersByTimeAsync(51);

    await rejection;
    expect(fetchSignal).not.toBeNull();
    expect(fetchSignal?.aborted).toBe(true);
  });

  it("keeps download deadlines and auth fences active through blob consumption", async () => {
    vi.useFakeTimers();
    const timedBody = deferredBlob(new Blob(["manifest"]));
    let fetchSignal: AbortSignal | null = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init: RequestInit) => {
        fetchSignal = init.signal ?? null;
        return Promise.resolve(timedBody.response);
      }),
    );
    const api = await loadApi();
    api.setApiReadTimeoutMs(50);

    const timedDownload =
      api.caliberApi.downloadWorkflowVersionManifest("WFV-1");
    const timedRejection = expect(timedDownload).rejects.toMatchObject({
      status: 0,
      message: expect.stringMatching(/timed out/i),
    });
    await timedBody.started;
    await vi.advanceTimersByTimeAsync(51);
    await timedRejection;
    expect(fetchSignal).not.toBeNull();
    expect(fetchSignal?.aborted).toBe(true);

    vi.useRealTimers();
    const staleBody = deferredBlob(new Blob(["manifest"]));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(staleBody.response));
    saveLocalAuthSession(createLocalAuthSession("user-a"));
    const staleDownload =
      api.caliberApi.downloadWorkflowVersionManifest("WFV-2");
    await staleBody.started;
    saveLocalAuthSession(createLocalAuthSession("user-b"));
    staleBody.resolve();

    await expect(staleDownload).rejects.toMatchObject({ status: 409 });
  });

  it("keeps caller cancellation wired through delayed body consumption", async () => {
    const body = deferredBody(envelope({ user_id: "admin" }));
    const caller = new AbortController();
    let fetchSignal: AbortSignal | null = null;
    let underlyingCancelled = false;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init: RequestInit) => {
        fetchSignal = init.signal ?? null;
        fetchSignal?.addEventListener(
          "abort",
          () => {
            underlyingCancelled = true;
          },
          { once: true },
        );
        return Promise.resolve(body.response);
      }),
    );
    const api = await loadApi();

    const request = api.caliberApi.getMe(caller.signal);
    const rejection = expect(request).rejects.toMatchObject({
      name: "AbortError",
    });
    await body.started;
    expect(fetchSignal?.aborted).toBe(false);

    caller.abort();

    await rejection;
    expect(fetchSignal?.aborted).toBe(true);
    expect(underlyingCancelled).toBe(true);
  });

  it("reuses a prompt release operation id after an ambiguous network failure", async () => {
    const releaseBodies: Array<Record<string, unknown>> = [];
    let releaseAttempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init: RequestInit = {}) => {
        if (url.endsWith("/auth/csrf")) {
          return Promise.resolve(
            ok({ enabled: false, token: "", ttl_seconds: 0 }),
          );
        }
        releaseBodies.push(
          JSON.parse(String(init.body)) as Record<string, unknown>,
        );
        releaseAttempts += 1;
        if (releaseAttempts === 1)
          return Promise.reject(new Error("connection reset"));
        return Promise.resolve(
          ok({
            name: "support-agent",
            alias: "prod",
            version: 5,
            release_status: "applied",
          }),
        );
      }),
    );
    const api = await loadApi();

    await expect(
      api.caliberApi.promotePrompt("support-agent", 5, { expected_version: 4 }),
    ).rejects.toMatchObject({ status: 0 });
    await api.caliberApi.promotePrompt("support-agent", 5, {
      expected_version: 4,
    });

    expect(releaseBodies).toHaveLength(2);
    expect(releaseBodies[0]).toMatchObject({ version: 5, expected_version: 4 });
    expect(releaseBodies[0].operation_id).toMatch(/^REL-ui-/);
    expect(releaseBodies[1].operation_id).toBe(releaseBodies[0].operation_id);
  });

  it("covers bare health, degraded readiness, token-list mapping, and empty queries", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "ok", version: "1", db: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "readiness degraded",
            data: { simulated: ["llm"], ready: false },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        ok({ tokens: [{ token_id: "tok-1", name: "CI", scopes: ["read"] }] }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ data: [], next_cursor: "next" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await expect(api.caliberApi.getHealth()).resolves.toMatchObject({
      status: "ok",
      db: "ok",
    });
    await expect(api.caliberApi.getProviderReadiness()).resolves.toEqual({
      simulated: ["llm"],
      ready: false,
    });
    await expect(api.caliberApi.listAccessTokens()).resolves.toEqual([
      { token_id: "tok-1", name: "CI", scopes: ["read"] },
    ]);
    const page = await api.caliberApi.listWorkflowRunsPage("WF/1");
    expect(page).toEqual({ data: [], next_cursor: "next" });
    expect(String(fetchMock.mock.calls[3]?.[0])).toBe(
      `${API_BASE}/workflows/WF%2F1/runs`,
    );
  });

  it("covers disabled-CSRF, non-JSON retry, envelope, bare, and multipart fallbacks", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        ok({ enabled: false, token: "", ttl_seconds: 0 }),
      )
      .mockResolvedValueOnce(ok({ agent_id: "agent-1" }))
      .mockResolvedValueOnce(textError(403, "Forbidden"))
      .mockResolvedValueOnce(textError(403, "Forbidden"))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(ok({ file_id: "FILE-1" }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await expect(api.bootstrapCsrf()).resolves.toMatchObject({ enabled: false });
    await api.caliberApi.registerAgent({ agent_id: "agent-1" } as never);
    await expect(api.caliberApi.getMe()).rejects.toMatchObject({
      status: 403,
      message: "Forbidden",
    });

    // A CSRF-shaped status-line without a JSON detail falls through to the
    // original response instead of attempting an unsafe refresh.
    await expect(api.caliberApi.createSkill({ name: "x" } as never)).rejects.toMatchObject({
      status: 403,
      message: "Forbidden",
    });

    await expect(api.caliberApi.listWorkflowRunsPage("WF-1")).resolves.toEqual({
      data: undefined,
      next_cursor: null,
    });
    await expect(api.caliberApi.deleteWorkflow("WF-1")).resolves.toBeUndefined();
    await expect(
      api.caliberApi.uploadProjectFile(
        "PRJ-1",
        new File(["x"], "x.txt", { type: "text/plain" }),
        "input",
      ),
    ).resolves.toEqual({ file_id: "FILE-1" });
  });

  it("preserves readiness and export errors when degraded responses are not usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "still down" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(textError(502, "Export unavailable"));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await expect(api.caliberApi.getProviderReadiness()).rejects.toMatchObject({
      status: 503,
      message: "still down",
    });
    await expect(
      api.caliberApi.downloadWorkflowVersionManifest("WFV-1"),
    ).rejects.toMatchObject({ status: 502, message: "Export unavailable" });
  });

  it("does not retry an ordinary forbidden write or a CSRF refresh without a token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonError(403, "permission denied"))
      .mockResolvedValueOnce(jsonError(403, "csrf token expired"))
      .mockResolvedValueOnce(ok({ enabled: false, token: "", ttl_seconds: 0 }));
    vi.stubGlobal("fetch", fetchMock);

    const api = await loadApi();
    await expect(api.caliberApi.createSkill({ name: "x", content: "y" } as never)).rejects.toMatchObject({
      status: 403,
      message: "permission denied",
    });
    await expect(api.caliberApi.createSkill({ name: "x", content: "y" } as never)).rejects.toMatchObject({
      status: 403,
      message: "csrf token expired",
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("returns the request-envelope shape for a no-content page response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    const api = await loadApi();

    await expect(api.caliberApi.listWorkflowRunsPage("WF-1")).resolves.toEqual({
      data: undefined,
      next_cursor: null,
    });
  });

  it("surfaces non-JSON multipart failures without inventing a JSON detail", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(textError(500, "Upload failed")));
    const api = await loadApi();

    await expect(
      api.caliberApi.uploadProjectFile(
        "PRJ-1",
        new File(["data"], "input.txt", { type: "text/plain" }),
        "input",
      ),
    ).rejects.toMatchObject({ status: 500, message: "Upload failed" });
  });

  it("clears a failed CSRF bootstrap and allows a later retry", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonError(503, "csrf unavailable"))
      .mockResolvedValueOnce(ok({ enabled: true, token: "csrf-recovered", ttl_seconds: 60 }));
    vi.stubGlobal("fetch", fetchMock);
    const api = await loadApi();

    await expect(api.bootstrapCsrf()).rejects.toMatchObject({
      status: 503,
      message: "csrf bootstrap failed",
    });
    await expect(api.bootstrapCsrf()).resolves.toMatchObject({ token: "csrf-recovered" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("handles enabled CSRF without a token and pre-aborted caller signals", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ enabled: true, token: null, ttl_seconds: 60 }))
      .mockResolvedValueOnce(ok({ agent_id: "agent-1" }));
    vi.stubGlobal("fetch", fetchMock);
    const api = await loadApi();

    await api.bootstrapCsrf();
    await api.caliberApi.registerAgent({ agent_id: "agent-1" } as never);
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(init.headers).not.toHaveProperty("X-CALIBER-CSRF");

    const caller = new AbortController();
    caller.abort();
    await expect(api.caliberApi.getMe(caller.signal)).rejects.toMatchObject({
      name: "AbortError",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("preserves an ApiError caller cancellation reason", async () => {
    const api = await loadApi();
    const caller = {
      aborted: true,
      reason: new api.ApiError(0, "caller deadline", null),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    } as unknown as AbortSignal;
    vi.stubGlobal("fetch", vi.fn());

    await expect(api.caliberApi.getMe(caller)).rejects.toMatchObject({
      name: "ApiError",
      status: 0,
      message: "caller deadline",
    });
    expect(fetch).not.toHaveBeenCalled();
  });
});
