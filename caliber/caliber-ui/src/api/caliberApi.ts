/**
 * Thin fetch wrapper around the CALIBER backend.
 *
 * Responsibilities:
 * - Prepend the static-prefix-aware API base path.
 * - Set `Content-Type: application/json` on writes.
 * - Unwrap the `{ data: ... }` envelope so callers see the inner payload.
 * - Translate non-2xx responses into a typed {@link ApiError}.
 *
 * Keep this surface small. Page components shouldn't drop down to `fetch`
 * directly — that's how envelope handling drifts.
 */

import type { GateVerdict, PromptRollbackResult, SkillVersionInfo } from "./versioning";
import type {
  AgentConfig,
  AgentRegisterPayload,
  AgentSkillsResponse,
  AgentUpdatePayload,
  ApiErrorBody,
  CSRFTokenResponse,
  CurrentUserInfo,
  DashboardSummary,
  Envelope,
  EvalDataset,
  EvalDatasetCreatePayload,
  EvalDatasetUpdatePayload,
  EvalExample,
  EvalExampleCreatePayload,
  EvalExampleFromTracePayload,
  EvalRun,
  EvalRunCreatePayload,
  EvalRunSummary,
  GatewayGuardrail,
  GatewayGuardrailCatalog,
  GatewayGuardrailCreateRequest,
  GatewayGuardrailsStatus,
  GatewayStatus,
  GatewayUsage,
  LlmPricing,
  LlmPricingCreatePayload,
  LlmPricingUpdatePayload,
  SystemServicesResponse,
  JobApplyResult,
  JobListFilters,
  JobTargetsResponse,
  Judge,
  JudgeAlignmentPayload,
  JudgeAlignmentResult,
  JudgeCreatePayload,
  JudgeTestRunPayload,
  JudgeTestRunResult,
  JudgeUpdatePayload,
  ReviewItem,
  ReviewQueue,
  ReviewQueueCreatePayload,
  ReviewQueueDetail,
  ReviewQueueUpdatePayload,
  AriaPlan,
  AriaPlanCreatePayload,
  AriaPlanDetail,
  AriaPlanUpdatePayload,
  AriaInteraction,
  AriaInteractionAnswerPayload,
  PromptCalibrationOptions,
  PromptCalibrationRunPayload,
  PromptCalibrationRunResult,
  PromptCreateResult,
  LlmSetupStatus,
  LlmSetupUpdate,
  PromptDetail,
  PromptDraftResult,
  PromptInfo,
  PromptTemplateCatalog,
  PromptTemplatePreviewPayload,
  PromptTemplatePreviewResult,
  PromptVersionInfo,
  PromptVersionDetail,
  PromptAliasResult,
  PromptOptimizationOptions,
  PromptBindPayload,
  PromptOptimizationRunPayload,
  PromptOptimizationRunResult,
  PromptTestRunCreatePayload,
  PromptTestRunDetail,
  PromptTestRunSummary,
  PromptWorkspaceResponse,
  ToolTestRunCreatePayload,
  ToolTestRunDetail,
  ToolTestRunSummary,
  ToolWorkspaceResponse,
  RefinementJob,
  ResourceStatus,
  RuntimeConfigurationInventory,
  RollbackCheckpoint,
  RollbackResponse,
  Skill,
  SkillBindPayload,
  SkillCalibratePayload,
  SkillCreatePayload,
  ProviderReadiness,
  SkillPackage,
  SkillPackageImportPayload,
  SkillSelectionResult,
  SkillTestRunCreatePayload,
  SkillTestRunDetail,
  SkillTestRunSummary,
  SkillUpdatePayload,
  SkillWorkspaceResponse,
  VerificationBatchResult,
  VerificationItemCreatePayload,
  VerificationItem,
  VerificationListFilters,
  VerifyResponse,
  AuditLogPage,
  AuditLogFilters,
} from "./types";
import type {
  CalibrationCase,
  CompileResult,
  McpDiscoverToolsResult,
  McpServer,
  McpServerCreatePayload,
  McpServerToolsResult,
  McpServerUpdatePayload,
  McpTestConnectionResult,
  McpToolCalibrationResult,
  McpToolPolicy,
  McpToolPolicyUpdatePayload,
  McpToolTestCasesResult,
  CopilotEditResult,
  McpToolInvocationResult,
  PreviewResult,
  PromoteResult,
  PromptRenderResult,
  ProposePatchResult,
  ObjectStoreBucket,
  ObjectStoreDeleteResult,
  ObjectStoreExtract,
  ObjectStoreListing,
  ObjectStorePreview,
  ObjectStoreStatus,
  SkillRenderResult,
  ToolCalibrationResult,
  ToolDefinition,
  ToolSource,
  ToolTestCasesResult,
  ToolTestRunResult,
  ToolUpdatePayload,
  ToolUsage,
  PlatformCapabilities,
  ValidationReport,
  WorkflowBakeoffWorksheet,
  WorkflowBenchmarkReport,
  WorkflowBenchmarkReportStatus,
  Workflow,
  WorkflowCalibrationOptions,
  WorkflowCalibrationRunPayload,
  WorkflowCalibrationRunResult,
  WorkflowComponentCatalog,
  WorkflowCronPreview,
  WorkflowDeployment,
  WorkflowManifest,
  WorkflowPatch,
  WorkflowPromotion,
  WorkflowService,
  WorkflowServicePublishPayload,
  WorkflowTemplateCatalog,
  Project,
  ProjectDirectory,
  ProjectStorageConfig,
  WorkflowFile,
  WorkflowFileList,
  WorkflowRun,
  WorkflowRunCheckpoint,
  WorkflowRunHistoryStats,
  WorkflowRunManifest,
  WorkflowSessionMemoryClearResult,
  WorkflowSessionMemoryEntry,
  WorkflowRuntimeApproval,
  WorkflowRunEvent,
  WorkflowRunLineage,
  WorkflowRunResult,
  WorkflowRunTrace,
  WorkflowVersion,
  GraphDiff,
  ObservabilityExperiment,
  ObservabilityMetrics,
  ObservabilityTrace,
  ObservabilityTraceAssessment,
  ObservabilityTraceDetail,
} from "./workflowTypes";
import type {
  AssistantDraft,
  AssistantMessage,
  AssistantRun,
  AssistantAttachment,
  AttachmentCreateBody,
  AssistantQueuedMessage,
  QueuedMessageCreateBody,
  AssistantConfig,
  AssistantIntentExecuteBody,
  AssistantIntentExecuteResult,
  AssistantIntentPlanBody,
  AssistantIntentPlanResult,
  AssistantIntentResolveBody,
  AssistantIntentResolveResult,
  AssistantOperationStatus,
  AssistantSession,
  AssistantMessageSendBody,
  DraftUpdateBody,
  SessionCreateBody,
  SessionUpdateBody,
  TurnResponse,
  ValidationReport as AssistantValidationReport,
  TestReport as AssistantTestReport,
} from "./assistantTypes";
import type {
  KnowledgeBase,
  KnowledgeAgeSeedMode,
  KnowledgeBaseBuildResult,
  KnowledgeBaseChunk,
  KnowledgeBaseEntity,
  KnowledgeBaselineResponse,
  KnowledgeCalibrationRequest,
  KnowledgeCalibrationRunDetail,
  KnowledgeCalibrationRunSummary,
  KnowledgeGraphExploreResult,
  KnowledgeGraphExploreSource,
  KnowledgeBaseRelationship,
  KnowledgeBaseRun,
  KnowledgeBaseRunEvent,
  KnowledgeBaseSource,
  KnowledgeBaseVersion,
  KnowledgeOptions,
  KnowledgeQueryResult,
  KnowledgeSourceSelection,
} from "./knowledgeTypes";
import { getCaliberUserHeader } from "@/auth/localAuth";
import { getActiveProjectId } from "@/workspace/activeWorkspace";

/**
 * The static prefix MLflow is served behind (e.g. `/mlflow` when reverse-
 * proxied). Resolved at module load time so every API call uses the same
 * prefix. Default `""` means "served at the root" — the common dev case.
 *
 * Backend endpoints live under `{prefix}/ajax-api/2.0/mlflow/caliber/...`
 * regardless of where the SPA itself is mounted.
 */
const STATIC_PREFIX: string =
  (typeof window !== "undefined" && window.__CALIBER_STATIC_PREFIX__) || "";

const API_BASE = `${STATIC_PREFIX}/ajax-api/2.0/mlflow/caliber`;

/** Streaming endpoint used by the SSE hook. */
export const EVENT_STREAM_PATH = `${API_BASE}/events/stream`;

/**
 * Error thrown by every {@link request} call on a non-2xx response.
 *
 * Page components catch this directly and decide how to surface it
 * (toast, inline message, retry button) — the API client doesn't impose
 * a presentation.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody | null;

  constructor(status: number, message: string, body: ApiErrorBody | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /**
   * Hard ceiling for this request, in ms. Defaults to {@link readTimeoutMs}
   * for GETs and to "no timeout" for writes (so long-running POSTs such as
   * chat completions or run launches are never cut off). Pass `0` to disable.
   */
  timeoutMs?: number;
}

const WRITE_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

// A page load is a handful of GETs. Without a ceiling, a single hanging route
// (e.g. a slow/unreachable MLflow registry call) leaves the page stuck on its
// loading skeleton until the browser/proxy gives up minutes later. Bounding GET
// reads turns that multi-minute freeze into a fast, recoverable inline error.
let readTimeoutMs = 20000;

/** Tune (or, in tests, shorten) the default GET timeout. */
export function setApiReadTimeoutMs(ms: number): void {
  readTimeoutMs = ms;
}
const CSRF_HEADER = "X-CALIBER-CSRF";
const USER_HEADER = "X-CALIBER-User";
const PROJECT_HEADER = "X-CALIBER-Project";

/**
 * Cached CSRF state.
 *
 * Populated by :func:`bootstrapCsrf`. ``null`` means "bootstrap
 * hasn't completed yet" — write requests in that window fire
 * without the header (the backend rejects them if CSRF is enabled,
 * so the SPA must call ``bootstrapCsrf`` before the first write).
 *
 * Refresh is best-effort: if a write returns 403 with a CSRF-shaped
 * ``detail`` we re-fetch the token and retry once. A second 403
 * surfaces as a normal :class:`ApiError`.
 */
let csrfState: { enabled: boolean; token: string | null } | null = null;
let inflightBootstrap: Promise<CSRFTokenResponse> | null = null;

async function fetchCsrfToken(): Promise<CSRFTokenResponse> {
  const response = await fetch(`${API_BASE}/csrf`, {
    method: "GET",
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new ApiError(response.status, "csrf bootstrap failed", null);
  }
  const json = (await response.json()) as Envelope<CSRFTokenResponse>;
  return json.data;
}

/**
 * Fetch the current CSRF state and cache it for subsequent writes.
 *
 * Idempotent + de-duped: concurrent callers share the in-flight
 * fetch so app-startup races (e.g. multiple components calling on
 * mount) don't issue duplicate ``/csrf`` requests.
 */
export async function bootstrapCsrf(): Promise<CSRFTokenResponse> {
  if (inflightBootstrap) return inflightBootstrap;
  inflightBootstrap = fetchCsrfToken()
    .then((info) => {
      csrfState = { enabled: info.enabled, token: info.token };
      return info;
    })
    .finally(() => {
      inflightBootstrap = null;
    });
  return inflightBootstrap;
}

async function refreshCsrfToken(): Promise<string | null> {
  csrfState = null;
  const info = await bootstrapCsrf();
  return info.token;
}

function isCsrfRejection(parsed: ApiErrorBody | null): boolean {
  if (!parsed) return false;
  return (parsed.detail ?? "").toLowerCase().includes("csrf");
}

async function doFetch(
  path: string,
  options: RequestOptions,
  csrfToken: string | null,
): Promise<Response> {
  const { method = "GET", body, signal, timeoutMs } = options;
  const headers: Record<string, string> = {};
  const user = getCaliberUserHeader();
  if (user) headers[USER_HEADER] = user;
  const projectId = getActiveProjectId();
  if (projectId) headers[PROJECT_HEADER] = projectId;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrfToken && WRITE_METHODS.has(method)) {
    headers[CSRF_HEADER] = csrfToken;
  }
  const init: RequestInit = {
    method,
    headers,
    credentials: "same-origin",
  };
  if (body !== undefined) init.body = JSON.stringify(body);

  const effectiveTimeout = timeoutMs ?? (method === "GET" ? readTimeoutMs : 0);
  if (effectiveTimeout <= 0) {
    if (signal) init.signal = signal;
    return fetch(`${API_BASE}${path}`, init);
  }

  // Abort on either the caller's signal (refresh/unmount) or the timeout,
  // whichever fires first. A timeout surfaces as a clear ApiError; a
  // caller-driven cancel keeps propagating as AbortError so useApi ignores it.
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, effectiveTimeout);
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  init.signal = controller.signal;
  try {
    return await fetch(`${API_BASE}${path}`, init);
  } catch (err) {
    if (timedOut) {
      throw new ApiError(
        0,
        `request to ${path} timed out after ${effectiveTimeout}ms`,
        null,
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const initialToken =
    csrfState && csrfState.enabled && WRITE_METHODS.has(method)
      ? csrfState.token
      : null;

  let response: Response;
  try {
    response = await doFetch(path, options, initialToken);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    // A timeout (or any pre-classified failure) already carries a clear
    // message — surface it as-is instead of re-wrapping as "network error".
    if (err instanceof ApiError) throw err;
    throw new ApiError(
      0,
      `network error: ${err instanceof Error ? err.message : String(err)}`,
      null,
    );
  }

  // CSRF retry path: a 403 with a CSRF-shaped detail likely means
  // the cached token expired. Refresh once and retry.
  if (response.status === 403 && WRITE_METHODS.has(method)) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.clone().json()) as ApiErrorBody;
    } catch {
      // Body wasn't JSON; fall through.
    }
    if (isCsrfRejection(parsed)) {
      try {
        const fresh = await refreshCsrfToken();
        if (fresh) {
          response = await doFetch(path, options, fresh);
        }
      } catch {
        // Refresh failed; fall through and surface the original 403.
      }
    }
  }

  if (!response.ok) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.json()) as ApiErrorBody;
    } catch {
      // Body wasn't JSON — fall through with the status-line message.
    }
    const detail = parsed?.detail ?? response.statusText;
    throw new ApiError(response.status, detail, parsed);
  }

  // 204 No Content is valid for some writes. Callers parameterized as
  // `void` see undefined; everyone else gets the unwrapped envelope.
  if (response.status === 204) {
    return undefined as T;
  }

  const json = (await response.json()) as Envelope<T>;
  return json.data;
}

async function requestEnvelope<T>(
  path: string,
  options: RequestOptions = {},
): Promise<Envelope<T>> {
  const method = options.method ?? "GET";
  const initialToken =
    csrfState && csrfState.enabled && WRITE_METHODS.has(method)
      ? csrfState.token
      : null;

  let response: Response;
  try {
    response = await doFetch(path, options, initialToken);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    // A timeout (or any pre-classified failure) already carries a clear
    // message — surface it as-is instead of re-wrapping as "network error".
    if (err instanceof ApiError) throw err;
    throw new ApiError(
      0,
      `network error: ${err instanceof Error ? err.message : String(err)}`,
      null,
    );
  }

  if (response.status === 403 && WRITE_METHODS.has(method)) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.clone().json()) as ApiErrorBody;
    } catch {
      // Body wasn't JSON; fall through.
    }
    if (isCsrfRejection(parsed)) {
      try {
        const fresh = await refreshCsrfToken();
        if (fresh) {
          response = await doFetch(path, options, fresh);
        }
      } catch {
        // Refresh failed; fall through and surface the original 403.
      }
    }
  }

  if (!response.ok) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.json()) as ApiErrorBody;
    } catch {
      // Body wasn't JSON — fall through with the status-line message.
    }
    const detail = parsed?.detail ?? response.statusText;
    throw new ApiError(response.status, detail, parsed);
  }

  if (response.status === 204) {
    return { data: undefined as T, next_cursor: null };
  }

  return (await response.json()) as Envelope<T>;
}

/**
 * Multipart upload helper for the file/workspace routes (storage doc §4.7).
 *
 * The shared `request`/`doFetch` path JSON-encodes bodies; file uploads need
 * `FormData` (the browser sets the multipart boundary). This mirrors `request`'s
 * CSRF handling + envelope unwrap, but lets the browser own the Content-Type.
 */
async function uploadMultipart<T>(path: string, form: FormData): Promise<T> {
  const send = async (token: string | null): Promise<Response> => {
    const headers: Record<string, string> = {};
    const user = getCaliberUserHeader();
    if (user) headers[USER_HEADER] = user;
    const projectId = getActiveProjectId();
    if (projectId) headers[PROJECT_HEADER] = projectId;
    if (token) headers[CSRF_HEADER] = token;
    return fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers,
      body: form,
      credentials: "same-origin",
    });
  };
  let token = csrfState && csrfState.enabled ? csrfState.token : null;
  let response = await send(token);
  if (response.status === 403) {
    token = await refreshCsrfToken();
    if (token) response = await send(token);
  }
  if (!response.ok) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.json()) as ApiErrorBody;
    } catch {
      // non-JSON error body
    }
    throw new ApiError(response.status, parsed?.detail ?? response.statusText, parsed);
  }
  const json = (await response.json()) as Envelope<T>;
  return json.data;
}

/**
 * GET a non-JSON payload (CSV / JSON file) as a Blob for download.
 *
 * Mirrors `doFetch`'s auth-header handling (user + project) and same-origin
 * credentials so admin-only exports authenticate identically to every other
 * call — a plain `<a href>` would drop the `X-CALIBER-User` header in
 * local-auth mode. Returns the Blob; the caller turns it into a download.
 */
async function downloadFile(path: string, accept: string): Promise<Blob> {
  const headers: Record<string, string> = { Accept: accept };
  const user = getCaliberUserHeader();
  if (user) headers[USER_HEADER] = user;
  const projectId = getActiveProjectId();
  if (projectId) headers[PROJECT_HEADER] = projectId;
  const response = await fetch(`${API_BASE}${path}`, {
    method: "GET",
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    let parsed: ApiErrorBody | null = null;
    try {
      parsed = (await response.json()) as ApiErrorBody;
    } catch {
      // non-JSON error body
    }
    throw new ApiError(response.status, parsed?.detail ?? response.statusText, parsed);
  }
  return response.blob();
}

/* -------------------------------------------------------------------------- */
/* Typed endpoint functions                                                   */
/* -------------------------------------------------------------------------- */

function buildQuery(params: Record<string, string | undefined>): string {
  const entries = Object.entries(params).filter(
    (entry): entry is [string, string] =>
      entry[1] !== undefined && entry[1] !== "",
  );
  if (entries.length === 0) return "";
  const qs = new URLSearchParams(entries).toString();
  return `?${qs}`;
}

export const caliberApi = {
  /** GET /health */
  getHealth(signal?: AbortSignal): Promise<{ status: string; version: string }> {
    return request<{ status: string; version: string }>("/health", { signal });
  },

  /** GET /capabilities */
  getCapabilities(signal?: AbortSignal): Promise<PlatformCapabilities> {
    return request<PlatformCapabilities>("/capabilities", { signal });
  },

  /** GET /readiness — which providers are real vs simulated ('fake'). */
  getProviderReadiness(signal?: AbortSignal): Promise<ProviderReadiness> {
    return request<ProviderReadiness>("/readiness", { signal });
  },

  /** GET /dashboard/summary */
  getDashboardSummary(signal?: AbortSignal): Promise<DashboardSummary> {
    return request<DashboardSummary>("/dashboard/summary", { signal });
  },

  /** GET /me */
  getMe(signal?: AbortSignal): Promise<CurrentUserInfo> {
    return request<CurrentUserInfo>("/me", { signal });
  },

  /** GET /settings/runtime */
  getRuntimeConfiguration(signal?: AbortSignal): Promise<RuntimeConfigurationInventory> {
    return request<RuntimeConfigurationInventory>("/settings/runtime", { signal });
  },

  /** GET /verification-queue */
  listVerificationItems(
    filters: VerificationListFilters = {},
    signal?: AbortSignal,
  ): Promise<VerificationItem[]> {
    const query = buildQuery({
      status: filters.status,
      severity: filters.severity,
      agent_id: filters.agent_id,
    });
    return request<VerificationItem[]>(`/verification-queue${query}`, {
      signal,
    });
  },

  /** GET /verification-queue/{id} */
  getVerificationItem(
    itemId: string,
    signal?: AbortSignal,
  ): Promise<VerificationItem> {
    return request<VerificationItem>(
      `/verification-queue/${encodeURIComponent(itemId)}`,
      {
        signal,
      },
    );
  },

  /** POST /verification-queue */
  createVerificationItem(payload: VerificationItemCreatePayload): Promise<VerificationItem> {
    return request<VerificationItem>("/verification-queue", {
      method: "POST",
      body: stripNulls({ ...payload }),
    });
  },

  /** POST /verification-queue/batch — bulk verify or dismiss. */
  batchVerificationAction(
    action: "verify" | "dismiss",
    itemIds: string[],
    reason?: string,
  ): Promise<VerificationBatchResult> {
    return request<VerificationBatchResult>("/verification-queue/batch", {
      method: "POST",
      body: { action, item_ids: itemIds, reason },
    });
  },

  /** POST /verification-queue/{id}/verify */
  verifyItem(
    itemId: string,
    payload: {
      refinement_target?: string | null;
      verification_notes?: string | null;
      severity?: "critical" | "standard" | null;
    } = {},
  ): Promise<VerifyResponse> {
    return request<VerifyResponse>(
      `/verification-queue/${encodeURIComponent(itemId)}/verify`,
      {
        method: "POST",
        body: stripNulls(payload),
      },
    );
  },

  /** POST /verification-queue/{id}/dismiss */
  dismissItem(
    itemId: string,
    payload: { reason?: string | null; duplicate_of_id?: string | null } = {},
  ): Promise<VerificationItem> {
    return request<VerificationItem>(
      `/verification-queue/${encodeURIComponent(itemId)}/dismiss`,
      {
        method: "POST",
        body: stripNulls(payload),
      },
    );
  },

  /** POST /verification-queue/{id}/duplicate */
  markDuplicate(
    itemId: string,
    payload: { duplicate_of_id: string; reason?: string | null },
  ): Promise<VerificationItem> {
    return request<VerificationItem>(
      `/verification-queue/${encodeURIComponent(itemId)}/duplicate`,
      {
        method: "POST",
        body: stripNulls(payload),
      },
    );
  },

  /** GET /jobs */
  listJobs(
    filters: JobListFilters = {},
    signal?: AbortSignal,
  ): Promise<RefinementJob[]> {
    const query = buildQuery({
      status: filters.status,
      stage: filters.stage,
      agent_id: filters.agent_id,
      workflow_id: filters.workflow_id,
    });
    return request<RefinementJob[]>(`/jobs${query}`, { signal });
  },

  /** GET /jobs/{id} */
  getJob(jobId: string, signal?: AbortSignal): Promise<RefinementJob> {
    return request<RefinementJob>(`/jobs/${encodeURIComponent(jobId)}`, {
      signal,
    });
  },

  /** GET /jobs/{id}/targets */
  getJobTargets(
    jobId: string,
    signal?: AbortSignal,
  ): Promise<JobTargetsResponse> {
    return request<JobTargetsResponse>(
      `/jobs/${encodeURIComponent(jobId)}/targets`,
      {
        signal,
      },
    );
  },

  /**
   * POST /jobs/{id}/apply — promote a `candidate_ready` job's candidate.
   *
   * Replaces the removed human-feedback approval governance: an operator
   * applies a candidate directly. The backend returns 409 if the job is
   * not in `candidate_ready`.
   */
  applyJob(jobId: string): Promise<JobApplyResult> {
    return request<JobApplyResult>(
      `/jobs/${encodeURIComponent(jobId)}/apply`,
      { method: "POST" },
    );
  },

  /** GET /agents */
  listAgents(signal?: AbortSignal): Promise<AgentConfig[]> {
    return request<AgentConfig[]>("/agents", { signal });
  },

  /** GET /agents/{id} */
  getAgent(agentId: string, signal?: AbortSignal): Promise<AgentConfig> {
    return request<AgentConfig>(`/agents/${encodeURIComponent(agentId)}`, {
      signal,
    });
  },

  /** POST /agents */
  registerAgent(payload: AgentRegisterPayload): Promise<AgentConfig> {
    return request<AgentConfig>("/agents", { method: "POST", body: payload });
  },

  /** PATCH /agents/{id} */
  updateAgent(
    agentId: string,
    payload: AgentUpdatePayload,
  ): Promise<AgentConfig> {
    return request<AgentConfig>(`/agents/${encodeURIComponent(agentId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** DELETE /agents/{id} — removes the agent and cascades its dependent rows. */
  deleteAgent(agentId: string): Promise<{ agent_id: string; deleted: boolean }> {
    return request<{ agent_id: string; deleted: boolean }>(
      `/agents/${encodeURIComponent(agentId)}`,
      { method: "DELETE" },
    );
  },

  /** GET /agents/{id}/checkpoints */
  listCheckpoints(
    agentId: string,
    signal?: AbortSignal,
  ): Promise<RollbackCheckpoint[]> {
    return request<RollbackCheckpoint[]>(
      `/agents/${encodeURIComponent(agentId)}/checkpoints`,
      { signal },
    );
  },

  /** POST /agents/{id}/rollback */
  rollbackAgent(
    agentId: string,
    checkpointId?: string,
  ): Promise<RollbackResponse> {
    return request<RollbackResponse>(
      `/agents/${encodeURIComponent(agentId)}/rollback`,
      {
        method: "POST",
        body: checkpointId ? { checkpoint_id: checkpointId } : {},
      },
    );
  },

  /** GET /agents/{id}/skills */
  getAgentSkills(
    agentId: string,
    signal?: AbortSignal,
  ): Promise<AgentSkillsResponse> {
    return request<AgentSkillsResponse>(
      `/agents/${encodeURIComponent(agentId)}/skills`,
      { signal },
    );
  },

  /** GET /skills */
  listSkills(
    filters: { status?: ResourceStatus | "all"; tag?: string } = {},
    signal?: AbortSignal,
  ): Promise<Skill[]> {
    const query = buildQuery({ status: filters.status, tag: filters.tag });
    return request<Skill[]>(`/skills${query}`, { signal });
  },

  /** GET /prompts */
  listPrompts(signal?: AbortSignal): Promise<PromptInfo[]> {
    return request<PromptInfo[]>("/prompts", { signal });
  },

  /** POST /prompts */
  createPrompt(payload: {
    name: string;
    template: string;
    commit_message?: string;
    tags?: Record<string, string>;
    target_alias?: string;
  }): Promise<PromptCreateResult> {
    return request<PromptCreateResult>("/prompts", {
      method: "POST",
      body: payload,
    });
  },

  /** POST /prompts/{name}/versions */
  createPromptVersion(
    name: string,
    payload: {
      template: string;
      commit_message?: string;
      tags?: Record<string, string>;
      target_alias?: string;
      // When false, registers the version WITHOUT rotating the live alias
      // (a draft the developer can evaluate before promoting). Defaults true.
      promote?: boolean;
    },
  ): Promise<PromptCreateResult> {
    return request<PromptCreateResult>(
      `/prompts/${encodeURIComponent(name)}/versions`,
      {
        method: "POST",
        body: payload,
      },
    );
  },

  /** POST /prompts/{name}/aliases/{alias} — audited promote carrying the gate verdict/override. */
  promotePrompt(
    name: string,
    version: number,
    opts: {
      alias?: string;
      gate_state?: string;
      gate_score?: number;
      overridden?: boolean;
      override_reason?: string;
    } = {},
  ): Promise<PromptAliasResult> {
    const { alias = "prod", ...gate } = opts;
    return request<PromptAliasResult>(
      `/prompts/${encodeURIComponent(name)}/aliases/${encodeURIComponent(alias)}`,
      { method: "POST", body: { version, ...gate } },
    );
  },

  /** POST /prompts/{name}/rollback — roll the live alias back to the exact prior version. */
  rollbackPrompt(name: string, alias = "prod"): Promise<PromptRollbackResult> {
    return request<PromptRollbackResult>(
      `/prompts/${encodeURIComponent(name)}/rollback`,
      { method: "POST", body: { alias } },
    );
  },

  /** GET /gate-verdicts/{artifactType}/{versionKey} */
  getGateVerdict(artifactType: string, versionKey: string): Promise<GateVerdict> {
    return request<GateVerdict>(
      `/gate-verdicts/${encodeURIComponent(artifactType)}/${encodeURIComponent(versionKey)}`,
    );
  },

  /** POST /gate-verdicts/{artifactType}/{versionKey} */
  recordGateVerdict(
    artifactType: string,
    versionKey: string,
    payload: Partial<GateVerdict> & { state: string },
  ): Promise<GateVerdict> {
    return request<GateVerdict>(
      `/gate-verdicts/${encodeURIComponent(artifactType)}/${encodeURIComponent(versionKey)}`,
      { method: "POST", body: payload },
    );
  },

  /** POST /skills/{skillId}/rollback — restore the prior content as a new version. */
  rollbackSkill(skillId: string): Promise<Skill> {
    return request<Skill>(`/skills/${encodeURIComponent(skillId)}/rollback`, {
      method: "POST",
    });
  },

  /** GET /skills/{skillId}/versions — content version history, newest first. */
  listSkillVersions(skillId: string): Promise<SkillVersionInfo[]> {
    return request<SkillVersionInfo[]>(`/skills/${encodeURIComponent(skillId)}/versions`);
  },

  /** POST /knowledge-bases/{id}/rollback — re-activate the prior active version. */
  rollbackKnowledgeBase(knowledgeBaseId: string): Promise<KnowledgeBase> {
    return request<KnowledgeBase>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/rollback`,
      { method: "POST" },
    );
  },

  /** GET /prompts/{name} */
  getPrompt(name: string, alias = "prod"): Promise<PromptDetail> {
    const query = buildQuery({ alias });
    return request<PromptDetail>(`/prompts/${encodeURIComponent(name)}${query}`);
  },

  /** GET /prompts/{name}/workspace — runtime facts + computed lifecycle status. */
  getPromptWorkspace(
    name: string,
    signal?: AbortSignal,
  ): Promise<PromptWorkspaceResponse> {
    return request<PromptWorkspaceResponse>(
      `/prompts/${encodeURIComponent(name)}/workspace`,
      { signal },
    );
  },

  /** GET /settings/llm — current LLM provider, gateway, and credential presence */
  getLlmSetup(signal?: AbortSignal): Promise<LlmSetupStatus> {
    return request<LlmSetupStatus>("/settings/llm", { signal });
  },

  /** PATCH /settings/llm — apply GPT/Claude keys + gateway URL at runtime (admin) */
  updateLlmSetup(payload: LlmSetupUpdate): Promise<LlmSetupStatus> {
    return request<LlmSetupStatus>("/settings/llm", {
      method: "PATCH",
      body: payload,
    });
  },

  /** GET /prompts/template-library */
  getPromptTemplateLibrary(signal?: AbortSignal): Promise<PromptTemplateCatalog> {
    return request<PromptTemplateCatalog>("/prompts/template-library", { signal });
  },

  /** POST /prompts/template-library/preview */
  previewPromptTemplate(
    payload: PromptTemplatePreviewPayload,
  ): Promise<PromptTemplatePreviewResult> {
    return request<PromptTemplatePreviewResult>("/prompts/template-library/preview", {
      method: "POST",
      body: payload,
    });
  },

  /** POST /assistant/prompt-draft — assistant drafts a prompt from a description */
  draftPromptFromDescription(payload: {
    description: string;
  }): Promise<PromptDraftResult> {
    return request<PromptDraftResult>("/assistant/prompt-draft", {
      method: "POST",
      body: payload,
    });
  },

  /** DELETE /prompts/{name} — permanently delete a prompt + all versions (admin) */
  deletePrompt(name: string): Promise<{ deleted: string }> {
    return request<{ deleted: string }>(
      `/prompts/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    );
  },

  /** GET /prompts/{name}/versions */
  listPromptVersions(name: string): Promise<PromptVersionInfo[]> {
    return request<PromptVersionInfo[]>(`/prompts/${encodeURIComponent(name)}/versions`);
  },

  /** GET /prompts/{name}/versions/{version} */
  getPromptVersion(name: string, version: number): Promise<PromptVersionDetail> {
    return request<PromptVersionDetail>(
      `/prompts/${encodeURIComponent(name)}/versions/${encodeURIComponent(String(version))}`,
    );
  },

  /** POST /prompts/{name}/aliases/{alias} */
  setPromptAlias(name: string, alias: string, version: number): Promise<PromptAliasResult> {
    return request<PromptAliasResult>(
      `/prompts/${encodeURIComponent(name)}/aliases/${encodeURIComponent(alias)}`,
      {
        method: "POST",
        body: { version },
      },
    );
  },

  /** POST /prompts/{agentId}/test-render — render prompt template with variables */
  testRenderPrompt(
    agentId: string,
    variables: Record<string, string>,
  ): Promise<PromptRenderResult> {
    return request<PromptRenderResult>(
      `/prompts/${encodeURIComponent(agentId)}/test-render`,
      { method: "POST", body: { variables } },
    );
  },

  /** GET /prompts/calibration/options */
  getPromptCalibrationOptions(
    signal?: AbortSignal,
  ): Promise<PromptCalibrationOptions> {
    return request<PromptCalibrationOptions>("/prompts/calibration/options", {
      signal,
    });
  },

  /** POST /prompts/calibration/runs */
  createPromptCalibrationRun(
    payload: PromptCalibrationRunPayload,
  ): Promise<PromptCalibrationRunResult> {
    return request<PromptCalibrationRunResult>("/prompts/calibration/runs", {
      method: "POST",
      body: payload,
    });
  },

  /** GET /prompts/optimization/options */
  getPromptOptimizationOptions(
    signal?: AbortSignal,
  ): Promise<PromptOptimizationOptions> {
    return request<PromptOptimizationOptions>("/prompts/optimization/options", {
      signal,
    });
  },

  /** POST /prompts/optimization/runs */
  createPromptOptimizationRun(
    payload: PromptOptimizationRunPayload,
  ): Promise<PromptOptimizationRunResult> {
    return request<PromptOptimizationRunResult>("/prompts/optimization/runs", {
      method: "POST",
      body: payload,
    });
  },

  /** POST /prompts/test-runs — persist a completed ad-hoc prompt-test run. */
  savePromptTestRun(
    payload: PromptTestRunCreatePayload,
  ): Promise<PromptTestRunSummary> {
    return request<PromptTestRunSummary>("/prompts/test-runs", {
      method: "POST",
      body: payload,
    });
  },

  /** GET /prompts/test-runs — newest-first run history (summaries). */
  listPromptTestRuns(
    agentId?: string,
    limit?: number,
    signal?: AbortSignal,
  ): Promise<PromptTestRunSummary[]> {
    const query = buildQuery({
      agent_id: agentId,
      limit: limit !== undefined ? String(limit) : undefined,
    });
    return request<PromptTestRunSummary[]>(`/prompts/test-runs${query}`, {
      signal,
    });
  },

  /** GET /prompts/test-runs/{id} — full run incl. per-case results. */
  getPromptTestRun(
    testRunId: string,
    signal?: AbortSignal,
  ): Promise<PromptTestRunDetail> {
    return request<PromptTestRunDetail>(
      `/prompts/test-runs/${encodeURIComponent(testRunId)}`,
      { signal },
    );
  },

  /** POST /prompts/{name}/baseline — pin a run as the comparison baseline. */
  setPromptBaseline(
    name: string,
    testRunId: string,
  ): Promise<{ baseline_run_id: string }> {
    return request<{ baseline_run_id: string }>(
      `/prompts/${encodeURIComponent(name)}/baseline`,
      { method: "POST", body: { test_run_id: testRunId } },
    );
  },

  /** POST /prompts/{name}/bind — record where the prompt target is wired in. */
  bindPrompt(
    name: string,
    payload: PromptBindPayload,
  ): Promise<{ bound_to: Record<string, unknown>; status: string }> {
    return request<{ bound_to: Record<string, unknown>; status: string }>(
      `/prompts/${encodeURIComponent(name)}/bind`,
      { method: "POST", body: payload },
    );
  },

  /** GET /skills/{id} */
  getSkill(skillId: string, signal?: AbortSignal): Promise<Skill> {
    return request<Skill>(`/skills/${encodeURIComponent(skillId)}`, { signal });
  },

  /** GET /skills/{id}/package */
  getSkillPackage(skillId: string, signal?: AbortSignal): Promise<SkillPackage> {
    return request<SkillPackage>(`/skills/${encodeURIComponent(skillId)}/package`, { signal });
  },

  /** Direct URL for GET /skills/{id}/package.zip */
  skillPackageZipUrl(skillId: string): string {
    return `${API_BASE}/skills/${encodeURIComponent(skillId)}/package.zip`;
  },

  /** POST /skills */
  createSkill(payload: SkillCreatePayload): Promise<Skill> {
    return request<Skill>("/skills", { method: "POST", body: payload });
  },

  /** POST /skills/import-package — create a skill from an uploaded package. */
  importSkillPackage(payload: SkillPackageImportPayload): Promise<Skill> {
    return request<Skill>("/skills/import-package", { method: "POST", body: payload });
  },

  /** PATCH /skills/{id} */
  updateSkill(skillId: string, payload: SkillUpdatePayload): Promise<Skill> {
    return request<Skill>(`/skills/${encodeURIComponent(skillId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** POST /skills/{id}/test-render — render skill content with variables */
  testRenderSkill(
    skillId: string,
    variables: Record<string, string>,
  ): Promise<SkillRenderResult> {
    return request<SkillRenderResult>(
      `/skills/${encodeURIComponent(skillId)}/test-render`,
      { method: "POST", body: { variables } },
    );
  },

  /**
   * POST /skills/{id}/test-selection — does this skill auto-select for a query?
   * The core "trigger test" unit: runs the deterministic selection scorer for
   * this one skill and reports whether it triggers + the matched-signal reason.
   */
  testSkillSelection(
    skillId: string,
    body: { user_message: string; artifact_type?: string; session_goal?: string },
  ): Promise<SkillSelectionResult> {
    return request<SkillSelectionResult>(
      `/skills/${encodeURIComponent(skillId)}/test-selection`,
      { method: "POST", body },
    );
  },

  /** GET /skills/{id}/workspace — runtime facts + computed lifecycle status. */
  getSkillWorkspace(
    skillId: string,
    signal?: AbortSignal,
  ): Promise<SkillWorkspaceResponse> {
    return request<SkillWorkspaceResponse>(
      `/skills/${encodeURIComponent(skillId)}/workspace`,
      { signal },
    );
  },

  /** POST /skills/test-runs — persist a completed skill-test run (durable). */
  saveSkillTestRun(
    payload: SkillTestRunCreatePayload,
  ): Promise<SkillTestRunSummary> {
    return request<SkillTestRunSummary>("/skills/test-runs", {
      method: "POST",
      body: payload,
    });
  },

  /** GET /skills/test-runs — newest-first run history (summaries). */
  listSkillTestRuns(
    skillId?: string,
    kind?: string,
    limit?: number,
    signal?: AbortSignal,
  ): Promise<SkillTestRunSummary[]> {
    const query = buildQuery({
      skill_id: skillId,
      kind,
      limit: limit !== undefined ? String(limit) : undefined,
    });
    return request<SkillTestRunSummary[]>(`/skills/test-runs${query}`, {
      signal,
    });
  },

  /** GET /skills/test-runs/{id} — full run incl. per-case results. */
  getSkillTestRun(
    testRunId: string,
    signal?: AbortSignal,
  ): Promise<SkillTestRunDetail> {
    return request<SkillTestRunDetail>(
      `/skills/test-runs/${encodeURIComponent(testRunId)}`,
      { signal },
    );
  },

  /** POST /skills/{id}/baseline — pin a run as the comparison baseline. */
  setSkillBaseline(
    skillId: string,
    testRunId: string,
  ): Promise<{ baseline_run_id: string }> {
    return request<{ baseline_run_id: string }>(
      `/skills/${encodeURIComponent(skillId)}/baseline`,
      { method: "POST", body: { test_run_id: testRunId } },
    );
  },

  /** POST /skills/{id}/bind — record where the skill is wired in. */
  bindSkill(
    skillId: string,
    payload: SkillBindPayload,
  ): Promise<{ bound_to: Record<string, unknown>; status: string }> {
    return request<{ bound_to: Record<string, unknown>; status: string }>(
      `/skills/${encodeURIComponent(skillId)}/bind`,
      { method: "POST", body: payload },
    );
  },

  /**
   * POST /skills/{id}/calibrate — agent-free calibrate. The route auto-provisions
   * the hidden skill target and queues the refinement job, so no agent is picked.
   */
  calibrateSkill(
    skillId: string,
    payload: SkillCalibratePayload = {},
  ): Promise<{ item: Record<string, unknown>; job: Record<string, unknown> }> {
    return request<{ item: Record<string, unknown>; job: Record<string, unknown> }>(
      `/skills/${encodeURIComponent(skillId)}/calibrate`,
      { method: "POST", body: payload },
    );
  },

  /** GET /eval-datasets */
  listEvalDatasets(
    filters: { status?: ResourceStatus | "all"; tag?: string } = {},
    signal?: AbortSignal,
  ): Promise<EvalDataset[]> {
    const query = buildQuery({ status: filters.status, tag: filters.tag });
    return request<EvalDataset[]>(`/eval-datasets${query}`, { signal });
  },

  /** GET /eval-datasets/{id} */
  getEvalDataset(
    datasetId: string,
    signal?: AbortSignal,
  ): Promise<EvalDataset> {
    return request<EvalDataset>(
      `/eval-datasets/${encodeURIComponent(datasetId)}`,
      {
        signal,
      },
    );
  },

  /** POST /eval-datasets */
  createEvalDataset(payload: EvalDatasetCreatePayload): Promise<EvalDataset> {
    return request<EvalDataset>("/eval-datasets", {
      method: "POST",
      body: payload,
    });
  },

  /** PATCH /eval-datasets/{id} */
  updateEvalDataset(
    datasetId: string,
    payload: EvalDatasetUpdatePayload,
  ): Promise<EvalDataset> {
    return request<EvalDataset>(
      `/eval-datasets/${encodeURIComponent(datasetId)}`,
      {
        method: "PATCH",
        body: payload,
      },
    );
  },

  /**
   * POST /eval-datasets/{id}/sync — push the dataset's current examples to
   * MLflow's native GenAI dataset registry (MLflow 3.14). Returns the dataset
   * with refreshed ``mlflow_*`` linkage fields.
   */
  syncEvalDataset(datasetId: string): Promise<EvalDataset> {
    return request<EvalDataset>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/sync`,
      { method: "POST" },
    );
  },

  /** POST /eval-datasets/{id}/restore — restore a prior version's example set as a new head. */
  restoreEvalDataset(datasetId: string, version: number): Promise<EvalDataset> {
    return request<EvalDataset>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/restore`,
      { method: "POST", body: { version } },
    );
  },

  /** GET /eval-datasets/{id}/examples */
  listEvalExamples(
    datasetId: string,
    options: { version?: number; includeSuperseded?: boolean } = {},
    signal?: AbortSignal,
  ): Promise<EvalExample[]> {
    const query = buildQuery({
      version:
        options.version !== undefined ? String(options.version) : undefined,
      include_superseded: options.includeSuperseded ? "true" : undefined,
    });
    return request<EvalExample[]>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/examples${query}`,
      { signal },
    );
  },

  /** POST /eval-datasets/{id}/examples */
  appendEvalExample(
    datasetId: string,
    payload: EvalExampleCreatePayload,
  ): Promise<EvalExample> {
    return request<EvalExample>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/examples`,
      { method: "POST", body: payload },
    );
  },

  /** POST /eval-datasets/{id}/examples/{example_id}/supersede */
  supersedeEvalExample(
    datasetId: string,
    exampleId: string,
  ): Promise<EvalExample> {
    return request<EvalExample>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/examples/${encodeURIComponent(
        exampleId,
      )}/supersede`,
      { method: "POST", body: {} },
    );
  },

  /** POST /eval-datasets/{id}/examples/from-trace — capture a trace as an example. */
  addEvalExampleFromTrace(
    datasetId: string,
    payload: EvalExampleFromTracePayload,
  ): Promise<EvalExample> {
    return request<EvalExample>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/examples/from-trace`,
      { method: "POST", body: payload },
    );
  },

  /**
   * POST /eval-datasets/{id}/examples/{example_id}/revise — edit a row.
   *
   * Supersedes the old example and appends a replacement carrying the new
   * content atomically (one version bump), honouring the append-only model.
   * Returns the new (replacement) example.
   */
  reviseEvalExample(
    datasetId: string,
    exampleId: string,
    payload: EvalExampleCreatePayload,
  ): Promise<EvalExample> {
    return request<EvalExample>(
      `/eval-datasets/${encodeURIComponent(datasetId)}/examples/${encodeURIComponent(
        exampleId,
      )}/revise`,
      { method: "POST", body: payload },
    );
  },

  /* ---------------------------------------------------------------------- */
  /* Custom LLM judges (MLflow 3.14 make_judge)                              */
  /* ---------------------------------------------------------------------- */

  /** GET /judges */
  listJudges(
    filters: { status?: ResourceStatus | "all" } = {},
    signal?: AbortSignal,
  ): Promise<Judge[]> {
    const query = buildQuery({ status: filters.status });
    return request<Judge[]>(`/judges${query}`, { signal });
  },

  /** GET /judges/{id} */
  getJudge(judgeId: string, signal?: AbortSignal): Promise<Judge> {
    return request<Judge>(`/judges/${encodeURIComponent(judgeId)}`, { signal });
  },

  /** POST /judges */
  createJudge(payload: JudgeCreatePayload): Promise<Judge> {
    return request<Judge>("/judges", { method: "POST", body: payload });
  },

  /** PATCH /judges/{id} */
  updateJudge(judgeId: string, payload: JudgeUpdatePayload): Promise<Judge> {
    return request<Judge>(`/judges/${encodeURIComponent(judgeId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** POST /judges/{id}/test-run — run the judge once on a sample (no persistence). */
  testRunJudge(judgeId: string, payload: JudgeTestRunPayload): Promise<JudgeTestRunResult> {
    return request<JudgeTestRunResult>(
      `/judges/${encodeURIComponent(judgeId)}/test-run`,
      { method: "POST", body: payload },
    );
  },

  /** POST /judges/{id}/alignment — judge-vs-human agreement + Cohen's kappa. */
  alignJudge(judgeId: string, payload: JudgeAlignmentPayload): Promise<JudgeAlignmentResult> {
    return request<JudgeAlignmentResult>(
      `/judges/${encodeURIComponent(judgeId)}/alignment`,
      { method: "POST", body: payload },
    );
  },

  /* ---------------------------------------------------------------------- */
  /* Review queues (structured human review)                                 */
  /* ---------------------------------------------------------------------- */

  /** GET /review-queues */
  listReviewQueues(
    filters: { status?: ResourceStatus | "all" } = {},
    signal?: AbortSignal,
  ): Promise<ReviewQueue[]> {
    const query = buildQuery({ status: filters.status });
    return request<ReviewQueue[]>(`/review-queues${query}`, { signal });
  },

  /** GET /review-queues/{id} — queue + its items. */
  getReviewQueue(queueId: string, signal?: AbortSignal): Promise<ReviewQueueDetail> {
    return request<ReviewQueueDetail>(
      `/review-queues/${encodeURIComponent(queueId)}`,
      { signal },
    );
  },

  /** POST /review-queues */
  createReviewQueue(payload: ReviewQueueCreatePayload): Promise<ReviewQueue> {
    return request<ReviewQueue>("/review-queues", { method: "POST", body: payload });
  },

  /** PATCH /review-queues/{id} */
  updateReviewQueue(
    queueId: string,
    payload: ReviewQueueUpdatePayload,
  ): Promise<ReviewQueue> {
    return request<ReviewQueue>(`/review-queues/${encodeURIComponent(queueId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** POST /review-queues/{id}/items — enqueue traces for review. */
  addReviewItems(
    queueId: string,
    payload: { trace_ids: string[]; experiment_id?: string; assigned_to?: string },
  ): Promise<ReviewItem[]> {
    return request<ReviewItem[]>(
      `/review-queues/${encodeURIComponent(queueId)}/items`,
      { method: "POST", body: payload },
    );
  },

  /** POST /review-queues/{id}/items/{item_id}/submit — answer + write back to trace. */
  submitReviewItem(
    queueId: string,
    itemId: string,
    answers: Record<string, unknown>,
  ): Promise<ReviewItem> {
    return request<ReviewItem>(
      `/review-queues/${encodeURIComponent(queueId)}/items/${encodeURIComponent(
        itemId,
      )}/submit`,
      { method: "POST", body: { answers } },
    );
  },

  /* ---------------------------------------------------------------------- */
  /* Aria goal-plans (agentic orchestration)                                 */
  /* ---------------------------------------------------------------------- */

  /** GET /aria/plans[?session_id=] — the caller's plans, optionally scoped to a chat session. */
  listAriaPlans(sessionId?: string | null, signal?: AbortSignal): Promise<AriaPlan[]> {
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
    return request<AriaPlan[]>(`/aria/plans${query}`, { signal });
  },

  /** GET /aria/plans/{id} — plan + its steps. */
  getAriaPlan(planId: string, signal?: AbortSignal): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>(`/aria/plans/${encodeURIComponent(planId)}`, { signal });
  },

  /** POST /aria/plans — decompose a goal into a draft plan. */
  createAriaPlan(payload: AriaPlanCreatePayload): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>("/aria/plans", { method: "POST", body: payload });
  },

  /** PATCH /aria/plans/{id} — edit autonomy / cancel (draft only). */
  updateAriaPlan(planId: string, payload: AriaPlanUpdatePayload): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>(`/aria/plans/${encodeURIComponent(planId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** POST /aria/plans/{id}/approve — approve the plan shape. */
  approveAriaPlan(planId: string): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>(`/aria/plans/${encodeURIComponent(planId)}/approve`, {
      method: "POST",
    });
  },

  /** POST /aria/plans/{id}/execute — run/resume the plan (pauses at gates). */
  executeAriaPlan(planId: string): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>(`/aria/plans/${encodeURIComponent(planId)}/execute`, {
      method: "POST",
    });
  },

  /** POST /aria/plans/{id}/poll — advance any steps parked on async jobs. */
  pollAriaPlan(planId: string): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>(`/aria/plans/${encodeURIComponent(planId)}/poll`, {
      method: "POST",
    });
  },

  /** GET /aria/plans/{id}/interactions — the plan's interactions. */
  listAriaInteractions(planId: string, signal?: AbortSignal): Promise<AriaInteraction[]> {
    return request<AriaInteraction[]>(
      `/aria/plans/${encodeURIComponent(planId)}/interactions`,
      { signal },
    );
  },

  /** POST /aria/interactions/{id}/answer — answer a pause and resume the plan. */
  answerAriaInteraction(
    interactionId: string,
    payload: AriaInteractionAnswerPayload,
  ): Promise<AriaPlanDetail> {
    return request<AriaPlanDetail>(
      `/aria/interactions/${encodeURIComponent(interactionId)}/answer`,
      { method: "POST", body: payload },
    );
  },

  /* ---------------------------------------------------------------------- */
  /* Evaluations (scorecard)                                                 */
  /* ---------------------------------------------------------------------- */

  /** GET /evaluations[?dataset_id=] — run summaries (no heavy rows). */
  listEvaluations(
    datasetId?: string,
    signal?: AbortSignal,
  ): Promise<EvalRunSummary[]> {
    const query = datasetId ? `?dataset_id=${encodeURIComponent(datasetId)}` : "";
    return request<EvalRunSummary[]>(`/evaluations${query}`, { signal });
  },

  /** GET /evaluations/{run_id} — one run with its per-example scorecard. */
  getEvaluation(runId: string, signal?: AbortSignal): Promise<EvalRun> {
    return request<EvalRun>(`/evaluations/${encodeURIComponent(runId)}`, { signal });
  },

  /** POST /evaluations — run a dataset through the scorers now. */
  createEvaluation(payload: EvalRunCreatePayload): Promise<EvalRun> {
    return request<EvalRun>("/evaluations", { method: "POST", body: payload });
  },

  /** GET /gateway — MLflow AI Gateway discovery + routing status. */
  getGatewayStatus(signal?: AbortSignal): Promise<GatewayStatus> {
    return request<GatewayStatus>("/gateway", { signal });
  },

  /** GET /gateway/guardrails — gateway guardrails + per-endpoint coverage. */
  getGatewayGuardrails(signal?: AbortSignal): Promise<GatewayGuardrailsStatus> {
    return request<GatewayGuardrailsStatus>("/gateway/guardrails", { signal });
  },

  /** GET /gateway/guardrails/catalog — buildable scorer templates + existing scorers. */
  getGatewayGuardrailCatalog(signal?: AbortSignal): Promise<GatewayGuardrailCatalog> {
    return request<GatewayGuardrailCatalog>("/gateway/guardrails/catalog", { signal });
  },

  /** POST /gateway/guardrails — define a new guardrail (native scorer or existing). */
  createGatewayGuardrail(payload: GatewayGuardrailCreateRequest): Promise<GatewayGuardrail> {
    return request<GatewayGuardrail>("/gateway/guardrails", { method: "POST", body: payload });
  },

  /** DELETE /gateway/guardrails/{id} — delete a guardrail. */
  deleteGatewayGuardrail(
    guardrailId: string,
  ): Promise<{ guardrail_id: string; deleted: boolean }> {
    return request(`/gateway/guardrails/${encodeURIComponent(guardrailId)}`, {
      method: "DELETE",
    });
  },

  /** POST /gateway/endpoints/{id}/guardrails — attach an existing guardrail. */
  attachGatewayGuardrail(
    endpointId: string,
    payload: { guardrail_id: string; execution_order?: number },
  ): Promise<{ endpoint_id: string; guardrail_id: string; attached: boolean }> {
    return request(`/gateway/endpoints/${encodeURIComponent(endpointId)}/guardrails`, {
      method: "POST",
      body: payload,
    });
  },

  /** DELETE /gateway/endpoints/{id}/guardrails/{gid} — detach a guardrail. */
  detachGatewayGuardrail(
    endpointId: string,
    guardrailId: string,
  ): Promise<{ endpoint_id: string; guardrail_id: string; detached: boolean }> {
    return request(
      `/gateway/endpoints/${encodeURIComponent(endpointId)}/guardrails/${encodeURIComponent(guardrailId)}`,
      { method: "DELETE" },
    );
  },

  /** PATCH /gateway/endpoints/{id}/guardrails/{gid} — reorder / enable-disable. */
  updateGatewayGuardrailConfig(
    endpointId: string,
    guardrailId: string,
    payload: { execution_order?: number; enabled?: boolean },
  ): Promise<Record<string, unknown>> {
    return request(
      `/gateway/endpoints/${encodeURIComponent(endpointId)}/guardrails/${encodeURIComponent(guardrailId)}`,
      { method: "PATCH", body: payload },
    );
  },

  /** GET /gateway/usage — trace-derived token/cost/latency/error metrics + by-model. */
  getGatewayUsage(
    params: { sinceMs?: number; experimentId?: string } = {},
    signal?: AbortSignal,
  ): Promise<GatewayUsage> {
    const query = buildQuery({
      since_ms: params.sinceMs != null ? String(params.sinceMs) : undefined,
      experiment_id: params.experimentId,
    });
    return request<GatewayUsage>(`/gateway/usage${query}`, { signal });
  },

  /* ---- Per-model LLM pricing (gateway cost config) --------------------- */

  /** GET /llm-pricing — per-model token pricing rows. */
  listLlmPricing(
    filters: { status?: ResourceStatus | "all" } = {},
    signal?: AbortSignal,
  ): Promise<LlmPricing[]> {
    return request<LlmPricing[]>(`/llm-pricing${buildQuery({ status: filters.status })}`, {
      signal,
    });
  },

  /** POST /llm-pricing — add a per-model rate. */
  createLlmPricing(payload: LlmPricingCreatePayload): Promise<LlmPricing> {
    return request<LlmPricing>("/llm-pricing", { method: "POST", body: payload });
  },

  /** PATCH /llm-pricing/{id} — edit / archive a rate. */
  updateLlmPricing(pricingId: string, payload: LlmPricingUpdatePayload): Promise<LlmPricing> {
    return request<LlmPricing>(`/llm-pricing/${encodeURIComponent(pricingId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** GET /system/services — backing-service URLs + live health. */
  getSystemServices(signal?: AbortSignal): Promise<SystemServicesResponse> {
    return request<SystemServicesResponse>("/system/services", { signal });
  },

  /* ---------------------------------------------------------------------- */
  /* Workflow Studio                                                         */
  /* ---------------------------------------------------------------------- */

  /** GET /workflows */
  listWorkflows(status?: string, signal?: AbortSignal): Promise<Workflow[]> {
    return request<Workflow[]>(`/workflows${buildQuery({ status })}`, { signal });
  },

  /** POST /workflows */
  createWorkflow(
    payload: { name: string; description?: string; owner?: string },
  ): Promise<Workflow> {
    return request<Workflow>("/workflows", { method: "POST", body: payload });
  },

  /** GET /workflows/{id} */
  getWorkflow(workflowId: string, signal?: AbortSignal): Promise<Workflow> {
    return request<Workflow>(`/workflows/${encodeURIComponent(workflowId)}`, { signal });
  },

  /** GET /workflow-components */
  listWorkflowComponents(signal?: AbortSignal): Promise<WorkflowComponentCatalog> {
    return request<WorkflowComponentCatalog>("/workflow-components", { signal });
  },

  /** GET /workflow-templates */
  listWorkflowTemplates(signal?: AbortSignal): Promise<WorkflowTemplateCatalog> {
    return request<WorkflowTemplateCatalog>("/workflow-templates", { signal });
  },

  /** GET /workflow-cron-preview — next fire times for a Start-trigger cron expression. */
  previewWorkflowCron(
    expr: string,
    timezone?: string,
    count?: number,
    signal?: AbortSignal,
  ): Promise<WorkflowCronPreview> {
    const query = buildQuery({
      expr,
      tz: timezone,
      count: count === undefined ? undefined : String(count),
    });
    return request<WorkflowCronPreview>(`/workflow-cron-preview${query}`, { signal });
  },

  /** GET /audit-log — filtered, paginated page of audit entries (admin-only). */
  listAuditLog(filters: AuditLogFilters = {}, signal?: AbortSignal): Promise<AuditLogPage> {
    const query = buildQuery({
      actor: filters.actor,
      action: filters.action,
      entity_type: filters.entity_type,
      entity_id: filters.entity_id,
      since: filters.since,
      until: filters.until,
      limit: filters.limit === undefined ? undefined : String(filters.limit),
      offset: filters.offset === undefined ? undefined : String(filters.offset),
    });
    return request<AuditLogPage>(`/audit-log${query}`, { signal });
  },

  /** GET /audit-log/export — the filtered entries as a CSV or JSON Blob (admin-only). */
  exportAuditLog(filters: AuditLogFilters = {}, format: "csv" | "json" = "csv"): Promise<Blob> {
    const query = buildQuery({
      actor: filters.actor,
      action: filters.action,
      entity_type: filters.entity_type,
      entity_id: filters.entity_id,
      since: filters.since,
      until: filters.until,
      format,
    });
    const accept = format === "json" ? "application/json" : "text/csv";
    return downloadFile(`/audit-log/export${query}`, accept);
  },

  /** GET /workflow-benchmark-reports */
  listWorkflowBenchmarkReports(
    status?: WorkflowBenchmarkReportStatus,
    signal?: AbortSignal,
  ): Promise<WorkflowBenchmarkReport[]> {
    return request<WorkflowBenchmarkReport[]>(
      `/workflow-benchmark-reports${buildQuery({ status })}`,
      { signal },
    );
  },

  /** POST /workflow-benchmark-reports */
  createWorkflowBenchmarkReport(payload: {
    report_id?: string;
    name: string;
    status?: WorkflowBenchmarkReportStatus;
    worksheet: WorkflowBakeoffWorksheet;
  }): Promise<WorkflowBenchmarkReport> {
    return request<WorkflowBenchmarkReport>("/workflow-benchmark-reports", {
      method: "POST",
      body: payload,
    });
  },

  /** PATCH /workflow-benchmark-reports/{id} */
  updateWorkflowBenchmarkReport(
    reportId: string,
    payload: {
      name?: string;
      status?: WorkflowBenchmarkReportStatus;
      worksheet?: WorkflowBakeoffWorksheet;
    },
  ): Promise<WorkflowBenchmarkReport> {
    return request<WorkflowBenchmarkReport>(
      `/workflow-benchmark-reports/${encodeURIComponent(reportId)}`,
      {
        method: "PATCH",
        body: payload,
      },
    );
  },

  /** DELETE /workflow-benchmark-reports/{id} */
  deleteWorkflowBenchmarkReport(reportId: string): Promise<void> {
    return request<void>(
      `/workflow-benchmark-reports/${encodeURIComponent(reportId)}`,
      {
        method: "DELETE",
      },
    );
  },

  /** PATCH /workflows/{id} */
  updateWorkflow(
    workflowId: string,
    payload: Partial<Pick<Workflow, "name" | "description" | "owner" | "status">>,
  ): Promise<Workflow> {
    return request<Workflow>(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** DELETE /workflows/{id} */
  deleteWorkflow(workflowId: string): Promise<void> {
    return request<void>(`/workflows/${encodeURIComponent(workflowId)}`, {
      method: "DELETE",
    });
  },

  /** GET /workflows/{id}/versions */
  listWorkflowVersions(workflowId: string, signal?: AbortSignal): Promise<WorkflowVersion[]> {
    return request<WorkflowVersion[]>(
      `/workflows/${encodeURIComponent(workflowId)}/versions`,
      { signal },
    );
  },

  /** POST /workflows/{id}/versions */
  createWorkflowVersion(
    workflowId: string,
    manifest: WorkflowManifest,
  ): Promise<WorkflowVersion> {
    return request<WorkflowVersion>(
      `/workflows/${encodeURIComponent(workflowId)}/versions`,
      { method: "POST", body: { manifest } },
    );
  },

  /** GET /workflow-versions/{id} */
  getWorkflowVersion(versionId: string, signal?: AbortSignal): Promise<WorkflowVersion> {
    return request<WorkflowVersion>(
      `/workflow-versions/${encodeURIComponent(versionId)}`,
      { signal },
    );
  },

  /** PATCH /workflow-versions/{id} — optimistic-locked draft update. */
  updateWorkflowVersion(
    versionId: string,
    manifest: WorkflowManifest,
    manifestHash: string,
  ): Promise<WorkflowVersion> {
    return request<WorkflowVersion>(
      `/workflow-versions/${encodeURIComponent(versionId)}`,
      { method: "PATCH", body: { manifest, manifest_hash: manifestHash } },
    );
  },

  /** POST /workflow-versions/{id}/validate */
  validateWorkflowVersion(versionId: string): Promise<ValidationReport> {
    return request<ValidationReport>(
      `/workflow-versions/${encodeURIComponent(versionId)}/validate`,
      { method: "POST" },
    );
  },

  /** POST /workflow-versions/{id}/compile */
  compileWorkflowVersion(versionId: string): Promise<CompileResult> {
    return request<CompileResult>(
      `/workflow-versions/${encodeURIComponent(versionId)}/compile`,
      { method: "POST" },
    );
  },

  /** POST /workflow-versions/{id}/publish */
  publishWorkflowVersion(versionId: string): Promise<WorkflowVersion> {
    return request<WorkflowVersion>(
      `/workflow-versions/${encodeURIComponent(versionId)}/publish`,
      { method: "POST" },
    );
  },

  /**
   * POST /workflow-versions/{id}/restore — clone any prior version into a new
   * editable draft. Returns the freshly created draft version.
   */
  restoreWorkflowVersion(versionId: string): Promise<WorkflowVersion> {
    return request<WorkflowVersion>(
      `/workflow-versions/${encodeURIComponent(versionId)}/restore`,
      { method: "POST" },
    );
  },

  /**
   * GET /workflow-versions/{base}/diff/{other} — structured graph diff oriented
   * base -> other (older -> newer), so added/removed read naturally.
   */
  diffWorkflowVersions(
    baseVersionId: string,
    otherVersionId: string,
    signal?: AbortSignal,
  ): Promise<GraphDiff> {
    return request<GraphDiff>(
      `/workflow-versions/${encodeURIComponent(baseVersionId)}/diff/${encodeURIComponent(otherVersionId)}`,
      { signal },
    );
  },

  /** GET /workflow-versions/{id}/export/python — raw generated Python source (text). */
  async exportWorkflowPython(versionId: string): Promise<string> {
    const res = await doFetch(
      `/workflow-versions/${encodeURIComponent(versionId)}/export/python`,
      {},
      null,
    );
    if (!res.ok) {
      throw new ApiError(res.status, `export failed: ${res.statusText}`, null);
    }
    return res.text();
  },

  /** POST /workflow-versions/{id}/preview-run.
   * Pass `manifest` to preview an unsaved in-memory edit (the copilot iterate
   * loop) instead of the stored version. */
  previewWorkflowVersion(
    versionId: string,
    input: string,
    sessionId?: string,
    manifest?: WorkflowManifest,
  ): Promise<PreviewResult> {
    return request<PreviewResult>(
      `/workflow-versions/${encodeURIComponent(versionId)}/preview-run`,
      { method: "POST", body: stripNulls({ input, session_id: sessionId, manifest }) },
    );
  },

  /** POST /workflow-versions/{id}/run */
  runWorkflowVersion(
    versionId: string,
    input: string,
    sessionId?: string,
    alias = "manual",
    manifest?: WorkflowManifest,
  ): Promise<WorkflowRunResult> {
    return request<WorkflowRunResult>(
      `/workflow-versions/${encodeURIComponent(versionId)}/run`,
      { method: "POST", body: stripNulls({ input, session_id: sessionId, alias, manifest }) },
    );
  },

  /** POST /workflow-runs */
  createWorkflowRun(payload: {
    workflow_version_id?: string;
    workflow_id?: string;
    alias?: string;
    input?: unknown;
    session_id?: string;
    source?: string;
    priority?: number;
    idempotency_key?: string;
    manifest?: WorkflowManifest;
  }): Promise<WorkflowRun> {
    return request<WorkflowRun>("/workflow-runs", {
      method: "POST",
      body: stripNulls(payload),
    });
  },

  /** POST /workflows/{id}/trigger — start a run via the Start node's event trigger */
  triggerWorkflowEvent(
    workflowId: string,
    payload: {
      alias?: string;
      event_name?: string;
      input?: unknown;
      idempotency_key?: string;
    } = {},
  ): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflows/${encodeURIComponent(workflowId)}/trigger`,
      { method: "POST", body: stripNulls(payload) },
    );
  },

  /** GET /workflow-runs/{id} */
  getWorkflowRun(runId: string, signal?: AbortSignal): Promise<WorkflowRun> {
    return request<WorkflowRun>(`/workflow-runs/${encodeURIComponent(runId)}`, {
      signal,
    });
  },

  /** GET /workflow-runs/{id}/lineage */
  getWorkflowRunLineage(
    runId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowRunLineage> {
    return request<WorkflowRunLineage>(
      `/workflow-runs/${encodeURIComponent(runId)}/lineage`,
      { signal },
    );
  },

  /** GET /workflow-runs/{id}/manifest */
  getWorkflowRunManifest(
    runId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowRunManifest> {
    return request<WorkflowRunManifest>(
      `/workflow-runs/${encodeURIComponent(runId)}/manifest`,
      { signal },
    );
  },

  /** GET /workflow-runs/{id}/events[?after=&limit=] */
  listWorkflowRunEvents(
    runId: string,
    options: { after?: number; limit?: number } = {},
    signal?: AbortSignal,
  ): Promise<WorkflowRunEvent[]> {
    const query = buildQuery({
      after: options.after !== undefined ? String(options.after) : undefined,
      limit: options.limit !== undefined ? String(options.limit) : undefined,
    });
    return request<WorkflowRunEvent[]>(
      `/workflow-runs/${encodeURIComponent(runId)}/events${query}`,
      { signal },
    );
  },

  /** GET /workflow-runs/{id}/trace — the run's MLflow span tree. */
  getWorkflowRunTrace(runId: string, signal?: AbortSignal): Promise<WorkflowRunTrace> {
    return request<WorkflowRunTrace>(
      `/workflow-runs/${encodeURIComponent(runId)}/trace`,
      { signal },
    );
  },

  /** GET /observability/metrics — time-bucketed trace metrics for monitoring. */
  getObservabilityMetrics(
    options: { experimentId?: string; sinceMs?: number } = {},
    signal?: AbortSignal,
  ): Promise<ObservabilityMetrics> {
    const query = buildQuery({
      experiment_id: options.experimentId || undefined,
      since_ms: options.sinceMs !== undefined ? String(options.sinceMs) : undefined,
    });
    return request<ObservabilityMetrics>(`/observability/metrics${query}`, { signal });
  },

  /** GET /observability/experiments — MLflow experiments for the filter. */
  async listObservabilityExperiments(
    signal?: AbortSignal,
  ): Promise<ObservabilityExperiment[]> {
    const result = await request<{ experiments: ObservabilityExperiment[] }>(
      `/observability/experiments`,
      { signal },
    );
    return result.experiments ?? [];
  },

  /** GET /observability/traces[?limit=&status=&experiment_id=&session=&since_ms=] */
  async listObservabilityTraces(
    options: {
      limit?: number;
      status?: string;
      experimentId?: string;
      session?: string;
      sinceMs?: number;
    } = {},
    signal?: AbortSignal,
  ): Promise<ObservabilityTrace[]> {
    const query = buildQuery({
      limit: options.limit !== undefined ? String(options.limit) : undefined,
      status: options.status || undefined,
      experiment_id: options.experimentId || undefined,
      session: options.session || undefined,
      since_ms: options.sinceMs !== undefined ? String(options.sinceMs) : undefined,
    });
    const result = await request<{ traces: ObservabilityTrace[] }>(
      `/observability/traces${query}`,
      { signal },
    );
    return result.traces ?? [];
  },

  /** GET /observability/traces/{traceId} — the trace's full detail. */
  getObservabilityTrace(
    traceId: string,
    signal?: AbortSignal,
  ): Promise<ObservabilityTraceDetail> {
    return request<ObservabilityTraceDetail>(
      `/observability/traces/${encodeURIComponent(traceId)}`,
      { signal },
    );
  },

  /** POST /observability/traces/{traceId}/feedback — attach a human assessment. */
  submitObservabilityFeedback(
    traceId: string,
    body: { name?: string; value: boolean | number | string; rationale?: string },
  ): Promise<{ assessments: ObservabilityTraceAssessment[] }> {
    return request<{ assessments: ObservabilityTraceAssessment[] }>(
      `/observability/traces/${encodeURIComponent(traceId)}/feedback`,
      { method: "POST", body },
    );
  },

  /** GET /workflow-runs/{id}/checkpoints[?after=&limit=] */
  listWorkflowRunCheckpoints(
    runId: string,
    options: { after?: number; limit?: number } = {},
    signal?: AbortSignal,
  ): Promise<WorkflowRunCheckpoint[]> {
    const query = buildQuery({
      after: options.after !== undefined ? String(options.after) : undefined,
      limit: options.limit !== undefined ? String(options.limit) : undefined,
    });
    return request<WorkflowRunCheckpoint[]>(
      `/workflow-runs/${encodeURIComponent(runId)}/checkpoints${query}`,
      { signal },
    );
  },

  /** GET /workflows/{id}/session-memory?session_id=...&node_id=... */
  listWorkflowSessionMemory(
    workflowId: string,
    sessionId: string,
    options: { node_id?: string } = {},
    signal?: AbortSignal,
  ): Promise<WorkflowSessionMemoryEntry[]> {
    const query = buildQuery({
      session_id: sessionId,
      node_id: options.node_id,
    });
    return request<WorkflowSessionMemoryEntry[]>(
      `/workflows/${encodeURIComponent(workflowId)}/session-memory${query}`,
      { signal },
    );
  },

  /** DELETE /workflows/{id}/session-memory?session_id=...&node_id=... */
  clearWorkflowSessionMemory(
    workflowId: string,
    sessionId: string,
    options: { node_id?: string } = {},
  ): Promise<WorkflowSessionMemoryClearResult> {
    const query = buildQuery({
      session_id: sessionId,
      node_id: options.node_id,
    });
    return request<WorkflowSessionMemoryClearResult>(
      `/workflows/${encodeURIComponent(workflowId)}/session-memory${query}`,
      { method: "DELETE" },
    );
  },

  /** GET /workflow-runs/{id}/approvals */
  listWorkflowRunApprovals(
    runId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowRuntimeApproval[]> {
    return request<WorkflowRuntimeApproval[]>(
      `/workflow-runs/${encodeURIComponent(runId)}/approvals`,
      { signal },
    );
  },

  /** POST /workflow-runs/{id}/cancel */
  cancelWorkflowRun(runId: string, reason?: string): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflow-runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST", body: stripNulls({ reason }) },
    );
  },

  /** POST /workflow-runs/{id}/retry */
  retryWorkflowRun(
    runId: string,
    payload: { reason?: string; checkpoint_id?: string } = {},
  ): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflow-runs/${encodeURIComponent(runId)}/retry`,
      { method: "POST", body: stripNulls(payload) },
    );
  },

  /** POST /workflow-runs/{id}/approval/approve */
  approveWorkflowRunApproval(
    runId: string,
    payload: { runtime_approval_id?: string; reason?: string } = {},
  ): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflow-runs/${encodeURIComponent(runId)}/approval/approve`,
      { method: "POST", body: stripNulls(payload) },
    );
  },

  /** POST /workflow-runs/{id}/approval/reject */
  rejectWorkflowRunApproval(
    runId: string,
    payload: { runtime_approval_id?: string; reason?: string } = {},
  ): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflow-runs/${encodeURIComponent(runId)}/approval/reject`,
      { method: "POST", body: stripNulls(payload) },
    );
  },

  /** POST /workflow-runs/{id}/resume */
  resumeWorkflowRun(
    runId: string,
    payload: { event_name?: string; event_payload?: unknown } = {},
  ): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflow-runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body: stripNulls(payload) },
    );
  },

  /** POST /workflow-runs/resume-by-event */
  resumeWorkflowRunByEvent(payload: {
    event_name: string;
    event_payload?: unknown;
    workflow_id?: string;
  }): Promise<WorkflowRun> {
    return request<WorkflowRun>("/workflow-runs/resume-by-event", {
      method: "POST",
      body: stripNulls(payload),
    });
  },

  /** POST /workflow-versions/{id}/propose-patch */
  proposeWorkflowPatch(
    versionId: string,
    evidence: Record<string, unknown>,
  ): Promise<ProposePatchResult> {
    return request<ProposePatchResult>(
      `/workflow-versions/${encodeURIComponent(versionId)}/propose-patch`,
      { method: "POST", body: { evidence } },
    );
  },

  /** POST /workflow-versions/{id}/copilot-edit — natural-language manifest edit. */
  copilotEditWorkflow(
    versionId: string,
    body: { instruction: string; manifest?: WorkflowManifest },
    signal?: AbortSignal,
  ): Promise<CopilotEditResult> {
    return request<CopilotEditResult>(
      `/workflow-versions/${encodeURIComponent(versionId)}/copilot-edit`,
      { method: "POST", body, signal },
    );
  },

  /** POST /workflow-versions/{id}/plan-build — author a workflow from a plain-language goal. */
  planBuildWorkflow(
    versionId: string,
    body: { goal: string; manifest?: WorkflowManifest },
    signal?: AbortSignal,
  ): Promise<CopilotEditResult> {
    return request<CopilotEditResult>(
      `/workflow-versions/${encodeURIComponent(versionId)}/plan-build`,
      { method: "POST", body, signal },
    );
  },

  /** GET /workflows/{id}/patches */
  listWorkflowPatches(workflowId: string, signal?: AbortSignal): Promise<WorkflowPatch[]> {
    return request<WorkflowPatch[]>(
      `/workflows/${encodeURIComponent(workflowId)}/patches`,
      { signal },
    );
  },

  /** GET /workflows/{id}/calibration/options */
  getWorkflowCalibrationOptions(
    workflowId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowCalibrationOptions> {
    return request<WorkflowCalibrationOptions>(
      `/workflows/${encodeURIComponent(workflowId)}/calibration/options`,
      { signal },
    );
  },

  /** POST /workflows/{id}/calibration/runs */
  createWorkflowCalibrationRun(
    workflowId: string,
    payload: WorkflowCalibrationRunPayload,
  ): Promise<WorkflowCalibrationRunResult> {
    return request<WorkflowCalibrationRunResult>(
      `/workflows/${encodeURIComponent(workflowId)}/calibration/runs`,
      { method: "POST", body: payload },
    );
  },

  /** POST /workflows/import */
  importWorkflow(
    payload: { manifest?: WorkflowManifest; manifest_yaml?: string; name?: string },
  ): Promise<{ workflow: Workflow; version: WorkflowVersion }> {
    return request<{ workflow: Workflow; version: WorkflowVersion }>("/workflows/import", {
      method: "POST",
      body: payload,
    });
  },

  /** GET /workflows/{id}/runs */
  listWorkflowRuns(workflowId: string, signal?: AbortSignal): Promise<WorkflowRun[]> {
    return request<WorkflowRun[]>(
      `/workflows/${encodeURIComponent(workflowId)}/runs`,
      { signal },
    );
  },

  /** GET /workflows/{id}/runs/stats[?search=&artifact_persistence=] */
  getWorkflowRunHistoryStats(
    workflowId: string,
    options: {
      search?: string;
      artifactPersistence?: "failed" | "persisted";
    } = {},
    signal?: AbortSignal,
  ): Promise<WorkflowRunHistoryStats> {
    const query = buildQuery({
      search: options.search,
      artifact_persistence: options.artifactPersistence,
    });
    return request<WorkflowRunHistoryStats>(
      `/workflows/${encodeURIComponent(workflowId)}/runs/stats${query}`,
      { signal },
    );
  },

  /** GET /workflows/{id}/runs[?search=&artifact_persistence=&limit=&cursor=] */
  listWorkflowRunsPage(
    workflowId: string,
    options: {
      search?: string;
      artifactPersistence?: "failed" | "persisted";
      limit?: number;
      cursor?: string | null;
    } = {},
    signal?: AbortSignal,
  ): Promise<Envelope<WorkflowRun[]>> {
    const query = buildQuery({
      search: options.search,
      artifact_persistence: options.artifactPersistence,
      limit: options.limit !== undefined ? String(options.limit) : undefined,
      cursor: options.cursor ?? undefined,
    });
    return requestEnvelope<WorkflowRun[]>(
      `/workflows/${encodeURIComponent(workflowId)}/runs${query}`,
      { signal },
    );
  },

  /** GET /workflow-runs/by-trace/{traceId} */
  getWorkflowRunByTrace(traceId: string, signal?: AbortSignal): Promise<WorkflowRun> {
    return request<WorkflowRun>(
      `/workflow-runs/by-trace/${encodeURIComponent(traceId)}`,
      { signal },
    );
  },

  /** GET /workflows/{id}/deployments */
  listWorkflowDeployments(
    workflowId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowDeployment[]> {
    return request<WorkflowDeployment[]>(
      `/workflows/${encodeURIComponent(workflowId)}/deployments`,
      { signal },
    );
  },

  /** POST /workflows/{id}/deployments/{alias}/promote */
  promoteWorkflow(
    workflowId: string,
    alias: string,
    versionId: string,
  ): Promise<PromoteResult> {
    return request<PromoteResult>(
      `/workflows/${encodeURIComponent(workflowId)}/deployments/${encodeURIComponent(alias)}/promote`,
      { method: "POST", body: { version_id: versionId } },
    );
  },

  /** POST /workflows/{id}/deployments/{alias}/rollback */
  rollbackWorkflow(workflowId: string, alias: string): Promise<WorkflowDeployment> {
    return request<WorkflowDeployment>(
      `/workflows/${encodeURIComponent(workflowId)}/deployments/${encodeURIComponent(alias)}/rollback`,
      { method: "POST" },
    );
  },

  /**
   * GET /workflows/{id}/service
   *
   * Resolves to `null` when the workflow has not been published as a service
   * (the backend returns 404). Any other failure propagates as an ApiError.
   */
  async getWorkflowService(
    workflowId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowService | null> {
    try {
      return await request<WorkflowService>(
        `/workflows/${encodeURIComponent(workflowId)}/service`,
        { signal },
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  },

  /** POST /workflows/{id}/service */
  publishWorkflowService(
    workflowId: string,
    payload: WorkflowServicePublishPayload = {},
  ): Promise<WorkflowService> {
    return request<WorkflowService>(
      `/workflows/${encodeURIComponent(workflowId)}/service`,
      { method: "POST", body: payload },
    );
  },

  /** DELETE /workflows/{id}/service */
  unpublishWorkflowService(workflowId: string): Promise<{ status: string }> {
    return request<{ status: string }>(
      `/workflows/${encodeURIComponent(workflowId)}/service`,
      { method: "DELETE" },
    );
  },

  /** GET /workflows/{id}/promotions */
  listWorkflowPromotions(
    workflowId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowPromotion[]> {
    return request<WorkflowPromotion[]>(
      `/workflows/${encodeURIComponent(workflowId)}/promotions`,
      { signal },
    );
  },

  /** POST /workflow-promotions/{id}/approve */
  approveWorkflowPromotion(promotionId: string): Promise<PromoteResult> {
    return request<PromoteResult>(
      `/workflow-promotions/${encodeURIComponent(promotionId)}/approve`,
      { method: "POST" },
    );
  },

  /** POST /workflow-promotions/{id}/reject */
  rejectWorkflowPromotion(
    promotionId: string,
    reason?: string,
  ): Promise<WorkflowPromotion> {
    return request<WorkflowPromotion>(
      `/workflow-promotions/${encodeURIComponent(promotionId)}/reject`,
      { method: "POST", body: stripNulls({ reason }) },
    );
  },

  /** GET /tools */
  listTools(status?: string, signal?: AbortSignal): Promise<ToolDefinition[]> {
    return request<ToolDefinition[]>(`/tools${buildQuery({ status })}`, { signal });
  },

  /** POST /tools */
  registerTool(payload: {
    name: string;
    version?: string;
    description?: string;
    module_path: string;
    callable_name: string;
    input_schema?: Record<string, unknown> | null;
    output_schema?: Record<string, unknown> | null;
    side_effect_level?: string;
    requires_approval?: boolean;
    allow_in_preview?: boolean;
    secret_refs?: string[];
    owner?: string;
  }): Promise<ToolDefinition> {
    return request<ToolDefinition>("/tools", { method: "POST", body: payload });
  },

  /** GET /tools/{id} */
  getTool(toolId: string, signal?: AbortSignal): Promise<ToolDefinition> {
    return request<ToolDefinition>(`/tools/${encodeURIComponent(toolId)}`, { signal });
  },

  /** PATCH /tools/{id} */
  updateTool(
    toolId: string,
    payload: ToolUpdatePayload,
  ): Promise<ToolDefinition> {
    return request<ToolDefinition>(`/tools/${encodeURIComponent(toolId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** GET /tools/{id}/usage */
  getToolUsage(toolId: string, signal?: AbortSignal): Promise<ToolUsage> {
    return request<ToolUsage>(`/tools/${encodeURIComponent(toolId)}/usage`, { signal });
  },

  /** GET /tools/{id}/source — real implementation source + signature + docstring */
  getToolSource(toolId: string, signal?: AbortSignal): Promise<ToolSource> {
    return request<ToolSource>(`/tools/${encodeURIComponent(toolId)}/source`, { signal });
  },

  // --- Object Store (MinIO / S3 console) ---
  getObjectStoreStatus(signal?: AbortSignal): Promise<ObjectStoreStatus> {
    return request<ObjectStoreStatus>("/object-store/status", { signal });
  },
  listObjectStoreBuckets(signal?: AbortSignal): Promise<ObjectStoreBucket[]> {
    return request<ObjectStoreBucket[]>("/object-store/buckets", { signal });
  },
  createObjectStoreBucket(name: string): Promise<{ name: string }> {
    return request<{ name: string }>("/object-store/buckets", { method: "POST", body: { name } });
  },
  deleteObjectStoreBucket(bucket: string): Promise<void> {
    return request<void>(`/object-store/buckets/${encodeURIComponent(bucket)}`, { method: "DELETE" });
  },
  listObjectStoreObjects(
    bucket: string,
    prefix = "",
    opts: { token?: string; recursive?: boolean } = {},
    signal?: AbortSignal,
  ): Promise<ObjectStoreListing> {
    const qs = new URLSearchParams();
    if (prefix) qs.set("prefix", prefix);
    if (opts.token) qs.set("token", opts.token);
    if (opts.recursive) qs.set("recursive", "true");
    const suffix = qs.toString() ? `?${qs}` : "";
    return request<ObjectStoreListing>(
      `/object-store/buckets/${encodeURIComponent(bucket)}/objects${suffix}`,
      { signal },
    );
  },
  uploadObjectStoreObject(
    bucket: string,
    file: File,
    prefix = "",
  ): Promise<{ bucket: string; key: string; size: number }> {
    const form = new FormData();
    form.append("file", file, file.name);
    if (prefix) form.append("prefix", prefix);
    return uploadMultipart(`/object-store/buckets/${encodeURIComponent(bucket)}/objects`, form);
  },
  createObjectStoreFolder(bucket: string, prefix: string, name: string): Promise<{ prefix: string }> {
    return request<{ prefix: string }>(`/object-store/buckets/${encodeURIComponent(bucket)}/folders`, {
      method: "POST",
      body: { prefix, name },
    });
  },
  deleteObjectStoreObject(bucket: string, key: string): Promise<void> {
    return request<void>(
      `/object-store/buckets/${encodeURIComponent(bucket)}/object?key=${encodeURIComponent(key)}`,
      { method: "DELETE" },
    );
  },
  /** Bulk-delete explicit object keys (multi-select). */
  deleteObjectStoreObjects(bucket: string, keys: string[]): Promise<ObjectStoreDeleteResult> {
    return request<ObjectStoreDeleteResult>(
      `/object-store/buckets/${encodeURIComponent(bucket)}/objects/delete`,
      { method: "POST", body: { keys } },
    );
  },
  /** Delete a whole folder — every object under the prefix (recursive). */
  deleteObjectStoreFolder(bucket: string, prefix: string): Promise<ObjectStoreDeleteResult> {
    return request<ObjectStoreDeleteResult>(
      `/object-store/buckets/${encodeURIComponent(bucket)}/objects/delete`,
      { method: "POST", body: { prefix } },
    );
  },
  getObjectStoreObjectPreview(
    bucket: string,
    key: string,
    maxBytes = 256 * 1024,
    signal?: AbortSignal,
  ): Promise<ObjectStorePreview> {
    const qs = new URLSearchParams({ key, max_bytes: String(maxBytes) });
    return request<ObjectStorePreview>(
      `/object-store/buckets/${encodeURIComponent(bucket)}/object/preview?${qs}`,
      { signal },
    );
  },
  /** Server-side content extraction for Office documents (Word/PowerPoint → text,
   * Excel → rows) so the browser can preview their content inline. */
  getObjectStoreObjectExtract(
    bucket: string,
    key: string,
    signal?: AbortSignal,
  ): Promise<ObjectStoreExtract> {
    const qs = new URLSearchParams({ key });
    return request<ObjectStoreExtract>(
      `/object-store/buckets/${encodeURIComponent(bucket)}/object/extract?${qs}`,
      { signal },
    );
  },
  /** Browser-navigable URL for the in-app Allure report served by the backend. */
  allureReportUrl(): string {
    return `${API_BASE}/observability/allure-report/`;
  },

  /** Browser-navigable download URL (streams via the backend with Content-Disposition). */
  objectStoreDownloadUrl(bucket: string, key: string): string {
    return `${API_BASE}/object-store/buckets/${encodeURIComponent(bucket)}/object?key=${encodeURIComponent(key)}`;
  },

  /** Browser-navigable VIEW URL — streamed inline so the browser renders the file
   * (PDF / text / markdown / images / JSON) in a new tab instead of downloading. */
  objectStoreViewUrl(bucket: string, key: string): string {
    return `${this.objectStoreDownloadUrl(bucket, key)}&disposition=inline`;
  },

  // --- Knowledge Bases ---
  getKnowledgeOptions(signal?: AbortSignal): Promise<KnowledgeOptions> {
    return request<KnowledgeOptions>("/knowledge-bases/options", { signal });
  },
  listKnowledgeBases(
    filters: { status?: "active" | "archived" | "all"; visibility?: "project" | "user" | "public" } = {},
    signal?: AbortSignal,
  ): Promise<KnowledgeBase[]> {
    const qs = new URLSearchParams();
    if (filters.status) qs.set("status", filters.status);
    if (filters.visibility) qs.set("visibility", filters.visibility);
    const suffix = qs.toString() ? `?${qs}` : "";
    return request<KnowledgeBase[]>(`/knowledge-bases${suffix}`, { signal });
  },
  createKnowledgeBase(body: {
    name: string;
    description?: string;
    source_bucket: string;
    sources: KnowledgeSourceSelection[];
    chunking_strategy: string;
    embedding_model: string;
    chunking_config?: Record<string, unknown>;
    graph_config?: Record<string, unknown>;
  }): Promise<KnowledgeBaseBuildResult> {
    return request<KnowledgeBaseBuildResult>("/knowledge-bases", {
      method: "POST",
      body,
    });
  },
  getKnowledgeBase(knowledgeBaseId: string, signal?: AbortSignal): Promise<KnowledgeBase> {
    return request<KnowledgeBase>(`/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, { signal });
  },
  updateKnowledgeBase(
    knowledgeBaseId: string,
    body: { name?: string; description?: string; status?: "active" | "archived" },
  ): Promise<KnowledgeBase> {
    return request<KnowledgeBase>(`/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`, {
      method: "PATCH",
      body,
    });
  },
  /**
   * DELETE /knowledge-bases/{id} — permanently remove a KB and its data
   * (versions, sources, chunks, graph). Mirrors {@link deleteAgent}/
   * {@link deleteMcpServer}: the backend may answer with the deleted-id envelope
   * or a bare 204, so the (optional) payload is surfaced as-is for callers that
   * want to confirm the id.
   */
  deleteKnowledgeBase(
    knowledgeBaseId: string,
  ): Promise<{ knowledge_base_id?: string; deleted?: boolean } | undefined> {
    return request<{ knowledge_base_id?: string; deleted?: boolean } | undefined>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}`,
      { method: "DELETE" },
    );
  },
  listKnowledgeBaseVersions(knowledgeBaseId: string, signal?: AbortSignal): Promise<KnowledgeBaseVersion[]> {
    return request<KnowledgeBaseVersion[]>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/versions`,
      { signal },
    );
  },
  createKnowledgeBaseVersion(
    knowledgeBaseId: string,
    body: {
      sources?: KnowledgeSourceSelection[];
      chunking_strategy: string;
      embedding_model: string;
      chunking_config?: Record<string, unknown>;
      graph_config?: Record<string, unknown>;
    },
  ): Promise<KnowledgeBaseBuildResult> {
    return request<KnowledgeBaseBuildResult>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/versions`,
      { method: "POST", body },
    );
  },
  activateKnowledgeBaseVersion(
    knowledgeBaseId: string,
    versionId: string,
  ): Promise<KnowledgeBase> {
    return request<KnowledgeBase>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/versions/${encodeURIComponent(versionId)}/activate`,
      { method: "POST", body: {} },
    );
  },
  getKnowledgeBaseVersion(versionId: string, signal?: AbortSignal): Promise<KnowledgeBaseVersion> {
    return request<KnowledgeBaseVersion>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}`,
      { signal },
    );
  },
  syncKnowledgeBaseVersionToAge(versionId: string): Promise<KnowledgeBaseVersion> {
    return request<KnowledgeBaseVersion>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}/age-sync`,
      { method: "POST", body: {} },
    );
  },
  listKnowledgeBaseSources(versionId: string, signal?: AbortSignal): Promise<KnowledgeBaseSource[]> {
    return request<KnowledgeBaseSource[]>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}/sources`,
      { signal },
    );
  },
  listKnowledgeBaseChunks(
    versionId: string,
    opts: { q?: string; sourceKey?: string; limit?: number } = {},
    signal?: AbortSignal,
  ): Promise<KnowledgeBaseChunk[]> {
    const qs = new URLSearchParams();
    if (opts.q) qs.set("q", opts.q);
    if (opts.sourceKey) qs.set("source_key", opts.sourceKey);
    if (opts.limit) qs.set("limit", String(opts.limit));
    const suffix = qs.toString() ? `?${qs}` : "";
    return request<KnowledgeBaseChunk[]>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}/chunks${suffix}`,
      { signal },
    );
  },
  listKnowledgeBaseEntities(versionId: string, signal?: AbortSignal): Promise<KnowledgeBaseEntity[]> {
    return request<KnowledgeBaseEntity[]>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}/entities`,
      { signal },
    );
  },
  listKnowledgeBaseRelationships(
    versionId: string,
    signal?: AbortSignal,
  ): Promise<KnowledgeBaseRelationship[]> {
    return request<KnowledgeBaseRelationship[]>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}/relationships`,
      { signal },
    );
  },
  getKnowledgeBaseGraph(
    versionId: string,
    opts: {
      source?: KnowledgeGraphExploreSource;
      q?: string;
      entityType?: string;
      minimumRelationshipWeight?: number;
      traversalHops?: number;
      ageSeedMode?: KnowledgeAgeSeedMode;
      strictAgeRetrieval?: boolean;
      nodeLimit?: number;
    } = {},
    signal?: AbortSignal,
  ): Promise<KnowledgeGraphExploreResult> {
    const qs = new URLSearchParams();
    if (opts.source) qs.set("source", opts.source);
    if (opts.q) qs.set("q", opts.q);
    if (opts.entityType) qs.set("entity_type", opts.entityType);
    if (typeof opts.minimumRelationshipWeight === "number") {
      qs.set("minimum_relationship_weight", String(opts.minimumRelationshipWeight));
    }
    if (typeof opts.traversalHops === "number") {
      qs.set("traversal_hops", String(opts.traversalHops));
    }
    if (opts.ageSeedMode) {
      qs.set("age_seed_mode", opts.ageSeedMode);
    }
    if (opts.strictAgeRetrieval) {
      qs.set("strict_age_retrieval", "true");
    }
    if (typeof opts.nodeLimit === "number") {
      qs.set("node_limit", String(opts.nodeLimit));
    }
    const suffix = qs.toString() ? `?${qs}` : "";
    return request<KnowledgeGraphExploreResult>(
      `/knowledge-base-versions/${encodeURIComponent(versionId)}/graph${suffix}`,
      { signal },
    );
  },
  listKnowledgeBaseRuns(knowledgeBaseId: string, signal?: AbortSignal): Promise<KnowledgeBaseRun[]> {
    return request<KnowledgeBaseRun[]>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/runs`,
      { signal },
    );
  },
  listKnowledgeBaseRunEvents(runId: string, signal?: AbortSignal): Promise<KnowledgeBaseRunEvent[]> {
    return request<KnowledgeBaseRunEvent[]>(
      `/knowledge-runs/${encodeURIComponent(runId)}/events`,
      { signal },
    );
  },
  queryKnowledge(body: {
    version_ids: string[];
    question: string;
    history?: Array<{ role: "user" | "assistant"; content: string }>;
    top_k?: number;
    chat_model?: string;
    retrieval_modes?: Array<"dense" | "hybrid" | "graph_hybrid" | "age_graph">;
    graph_overrides?: {
      retrieval_strength?: "conservative" | "balanced" | "aggressive";
      minimum_relationship_weight?: number;
      age_seed_mode?: "entity_then_text" | "query_entities_only" | "query_text_only" | "query_entities_and_text";
      age_traversal_hops?: number;
      age_candidate_pool_size?: number;
      age_dense_rerank_weight?: number;
      strict_age_retrieval?: boolean;
    };
  }): Promise<KnowledgeQueryResult> {
    return request<KnowledgeQueryResult>("/knowledge/query", {
      method: "POST",
      body,
    });
  },

  // --- Knowledge-base calibration (retrieval-quality test runs) ---

  /** POST /knowledge-bases/{id}/calibrate — score a version, persist a run. */
  calibrateKnowledgeBase(
    knowledgeBaseId: string,
    payload: KnowledgeCalibrationRequest,
  ): Promise<KnowledgeCalibrationRunSummary> {
    return request<KnowledgeCalibrationRunSummary>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/calibrate`,
      { method: "POST", body: payload },
    );
  },

  /** GET /knowledge-bases/{id}/test-runs — newest-first run summaries. */
  listKnowledgeBaseTestRuns(
    knowledgeBaseId: string,
    limit?: number,
    signal?: AbortSignal,
  ): Promise<KnowledgeCalibrationRunSummary[]> {
    const query = buildQuery({
      limit: limit !== undefined ? String(limit) : undefined,
    });
    return request<KnowledgeCalibrationRunSummary[]>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/test-runs${query}`,
      { signal },
    );
  },

  /** GET /knowledge/test-runs/{id} — full run incl. per-question results. */
  getKnowledgeBaseTestRun(
    testRunId: string,
    signal?: AbortSignal,
  ): Promise<KnowledgeCalibrationRunDetail> {
    return request<KnowledgeCalibrationRunDetail>(
      `/knowledge/test-runs/${encodeURIComponent(testRunId)}`,
      { signal },
    );
  },

  /** POST /knowledge-bases/{id}/baseline — pin a run as the KB baseline. */
  setKnowledgeBaseBaseline(
    knowledgeBaseId: string,
    testRunId: string,
  ): Promise<KnowledgeBaselineResponse> {
    return request<KnowledgeBaselineResponse>(
      `/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/baseline`,
      { method: "POST", body: { test_run_id: testRunId } },
    );
  },

  /** POST /tools/{id}/archive */
  archiveTool(toolId: string): Promise<ToolDefinition> {
    return request<ToolDefinition>(
      `/tools/${encodeURIComponent(toolId)}/archive`,
      { method: "POST" },
    );
  },

  /** POST /tools/{id}/test-run — sandbox-isolated test invocation */
  testRunTool(
    toolId: string,
    input: Record<string, unknown>,
  ): Promise<ToolTestRunResult> {
    return request<ToolTestRunResult>(
      `/tools/${encodeURIComponent(toolId)}/test-run`,
      { method: "POST", body: { input } },
    );
  },

  /** PUT /tools/{id}/test-cases — persist saved calibration test cases */
  saveToolTestCases(
    toolId: string,
    testCases: CalibrationCase[],
  ): Promise<ToolTestCasesResult> {
    return request<ToolTestCasesResult>(
      `/tools/${encodeURIComponent(toolId)}/test-cases`,
      { method: "PUT", body: { test_cases: testCases } },
    );
  },

  /** POST /tools/{id}/calibrate — score saved test cases, return pass-rate */
  calibrateTool(toolId: string): Promise<ToolCalibrationResult> {
    return request<ToolCalibrationResult>(
      `/tools/${encodeURIComponent(toolId)}/calibrate`,
      { method: "POST" },
    );
  },

  /** GET /tools/{id}/workspace — runtime facts + computed lifecycle status. */
  getToolWorkspace(
    toolId: string,
    signal?: AbortSignal,
  ): Promise<ToolWorkspaceResponse> {
    return request<ToolWorkspaceResponse>(
      `/tools/${encodeURIComponent(toolId)}/workspace`,
      { signal },
    );
  },

  /** POST /tools/test-runs — persist a completed tool-test run (durable). */
  saveToolTestRun(
    payload: ToolTestRunCreatePayload,
  ): Promise<ToolTestRunSummary> {
    return request<ToolTestRunSummary>("/tools/test-runs", {
      method: "POST",
      body: payload,
    });
  },

  /** GET /tools/test-runs — newest-first run history (summaries). */
  listToolTestRuns(
    toolId?: string,
    kind?: string,
    limit?: number,
    signal?: AbortSignal,
  ): Promise<ToolTestRunSummary[]> {
    const query = buildQuery({
      tool_id: toolId,
      kind,
      limit: limit !== undefined ? String(limit) : undefined,
    });
    return request<ToolTestRunSummary[]>(`/tools/test-runs${query}`, {
      signal,
    });
  },

  /** GET /tools/test-runs/{id} — full run incl. per-case results. */
  getToolTestRun(
    testRunId: string,
    signal?: AbortSignal,
  ): Promise<ToolTestRunDetail> {
    return request<ToolTestRunDetail>(
      `/tools/test-runs/${encodeURIComponent(testRunId)}`,
      { signal },
    );
  },

  /** POST /tools/{id}/baseline — pin a run as the comparison baseline. */
  setToolBaseline(
    toolId: string,
    testRunId: string,
  ): Promise<{ baseline_run_id: string }> {
    return request<{ baseline_run_id: string }>(
      `/tools/${encodeURIComponent(toolId)}/baseline`,
      { method: "POST", body: { test_run_id: testRunId } },
    );
  },

  // ── MCP Servers ──────────────────────────────────────────────────────

  /** GET /mcp-servers — list all registered MCP servers */
  listMcpServers(
    status?: string,
    signal?: AbortSignal,
  ): Promise<McpServer[]> {
    const q = status ? `?status=${encodeURIComponent(status)}` : "";
    return request<McpServer[]>(`/mcp-servers${q}`, { signal });
  },

  /** GET /mcp-servers/{id} */
  getMcpServer(
    serverId: string,
    signal?: AbortSignal,
  ): Promise<McpServer> {
    return request<McpServer>(
      `/mcp-servers/${encodeURIComponent(serverId)}`,
      { signal },
    );
  },

  /** POST /mcp-servers — register a new MCP server (admin) */
  createMcpServer(
    payload: McpServerCreatePayload,
  ): Promise<McpServer> {
    return request<McpServer>("/mcp-servers", {
      method: "POST",
      body: payload,
    });
  },

  /** PATCH /mcp-servers/{id} — update server config (admin) */
  updateMcpServer(
    serverId: string,
    payload: McpServerUpdatePayload,
  ): Promise<McpServer> {
    return request<McpServer>(
      `/mcp-servers/${encodeURIComponent(serverId)}`,
      { method: "PATCH", body: payload },
    );
  },

  /** DELETE /mcp-servers/{id} — remove server (admin) */
  deleteMcpServer(serverId: string): Promise<void> {
    return request<null>(
      `/mcp-servers/${encodeURIComponent(serverId)}`,
      { method: "DELETE" },
    ) as unknown as Promise<void>;
  },

  /** POST /mcp-servers/{id}/test-connection — test connectivity and discover tools */
  testMcpConnection(
    serverId: string,
  ): Promise<McpTestConnectionResult> {
    return request<McpTestConnectionResult>(
      `/mcp-servers/${encodeURIComponent(serverId)}/test-connection`,
      { method: "POST" },
    );
  },

  /** POST /mcp-servers/{id}/discover-tools — refresh discovered tools */
  discoverMcpTools(
    serverId: string,
  ): Promise<McpDiscoverToolsResult> {
    return request<McpDiscoverToolsResult>(
      `/mcp-servers/${encodeURIComponent(serverId)}/discover-tools`,
      { method: "POST" },
    );
  },

  /** GET /mcp-servers/{id}/tools — list discovered tools with effective policy */
  listMcpTools(
    serverId: string,
    signal?: AbortSignal,
  ): Promise<McpServerToolsResult> {
    return request<McpServerToolsResult>(
      `/mcp-servers/${encodeURIComponent(serverId)}/tools`,
      { signal },
    );
  },

  /** PATCH /mcp-servers/{id}/tools/{tool}/policy — update one tool policy */
  updateMcpToolPolicy(
    serverId: string,
    toolName: string,
    payload: McpToolPolicyUpdatePayload,
  ): Promise<{ server_id: string; tool_name: string; policy: McpToolPolicy }> {
    return request<{ server_id: string; tool_name: string; policy: McpToolPolicy }>(
      `/mcp-servers/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(toolName)}/policy`,
      { method: "PATCH", body: payload },
    );
  },

  /** POST /mcp-servers/{id}/invoke-tool — invoke a tool on the server */
  invokeMcpTool(
    serverId: string,
    toolName: string,
    args: Record<string, unknown>,
  ): Promise<McpToolInvocationResult> {
    return request<McpToolInvocationResult>(
      `/mcp-servers/${encodeURIComponent(serverId)}/invoke-tool`,
      { method: "POST", body: { tool_name: toolName, arguments: args } },
    );
  },

  /** PUT /mcp-servers/{id}/tools/{tool}/test-cases — persist calibration cases */
  saveMcpToolTestCases(
    serverId: string,
    toolName: string,
    testCases: CalibrationCase[],
  ): Promise<McpToolTestCasesResult> {
    return request<McpToolTestCasesResult>(
      `/mcp-servers/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(toolName)}/test-cases`,
      { method: "PUT", body: { test_cases: testCases } },
    );
  },

  /** POST /mcp-servers/{id}/tools/{tool}/calibrate — score saved test cases */
  calibrateMcpTool(
    serverId: string,
    toolName: string,
  ): Promise<McpToolCalibrationResult> {
    return request<McpToolCalibrationResult>(
      `/mcp-servers/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(toolName)}/calibrate`,
      { method: "POST" },
    );
  },

  /* ------------------------------------------------------------------ */
  /* Assistant                                                          */
  /* ------------------------------------------------------------------ */

  /** GET /assistant/sessions */
  listAssistantSessions(
    owner?: string,
  ): Promise<AssistantSession[]> {
    const qs = owner ? `?owner=${encodeURIComponent(owner)}` : "";
    return request<AssistantSession[]>(`/assistant/sessions${qs}`);
  },

  /** POST /assistant/sessions */
  createAssistantSession(
    body: SessionCreateBody,
  ): Promise<AssistantSession> {
    return request<AssistantSession>("/assistant/sessions", {
      method: "POST",
      body,
    });
  },

  /** GET /assistant/sessions/{id} */
  getAssistantSession(
    sessionId: string,
  ): Promise<AssistantSession> {
    return request<AssistantSession>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}`,
    );
  },

  /** PATCH /assistant/sessions/{id} */
  updateAssistantSession(
    sessionId: string,
    body: SessionUpdateBody,
  ): Promise<AssistantSession> {
    return request<AssistantSession>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}`,
      { method: "PATCH", body },
    );
  },

  /** GET /assistant/sessions/{id}/messages */
  listAssistantMessages(
    sessionId: string,
  ): Promise<AssistantMessage[]> {
    return request<AssistantMessage[]>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/messages`,
    );
  },

  /** POST /assistant/sessions/{id}/messages */
  sendAssistantMessage(
    sessionId: string,
    body: AssistantMessageSendBody,
  ): Promise<TurnResponse> {
    return request<TurnResponse>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/messages`,
      { method: "POST", body },
    );
  },

  /** GET /assistant/sessions/{id}/attachments */
  listAssistantAttachments(
    sessionId: string,
  ): Promise<AssistantAttachment[]> {
    return request<AssistantAttachment[]>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/attachments`,
    );
  },

  /** POST /assistant/sessions/{id}/attachments */
  createAssistantAttachment(
    sessionId: string,
    body: AttachmentCreateBody,
  ): Promise<AssistantAttachment> {
    return request<AssistantAttachment>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/attachments`,
      { method: "POST", body },
    );
  },

  /** POST /assistant/sessions/{id}/attachments/upload (multipart) */
  uploadAssistantAttachment(
    sessionId: string,
    file: File,
    bucket?: string,
  ): Promise<AssistantAttachment> {
    const form = new FormData();
    form.append("file", file);
    if (bucket) form.append("bucket", bucket);
    return uploadMultipart<AssistantAttachment>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/attachments/upload`,
      form,
    );
  },

  /** DELETE /assistant/attachments/{id} */
  deleteAssistantAttachment(
    attachmentId: string,
  ): Promise<void> {
    return request<void>(
      `/assistant/attachments/${encodeURIComponent(attachmentId)}`,
      { method: "DELETE" },
    );
  },

  /** GET /assistant/sessions/{id}/queue */
  listAssistantQueue(
    sessionId: string,
  ): Promise<AssistantQueuedMessage[]> {
    return request<AssistantQueuedMessage[]>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/queue`,
    );
  },

  /** POST /assistant/sessions/{id}/queue */
  enqueueAssistantMessage(
    sessionId: string,
    body: QueuedMessageCreateBody,
  ): Promise<AssistantQueuedMessage> {
    return request<AssistantQueuedMessage>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/queue`,
      { method: "POST", body },
    );
  },

  /** DELETE /assistant/queue/{queueId} */
  cancelAssistantQueued(
    queueId: string,
  ): Promise<void> {
    return request<void>(
      `/assistant/queue/${encodeURIComponent(queueId)}`,
      { method: "DELETE" },
    );
  },

  /** POST /assistant/sessions/{id}/intent/resolve */
  resolveAssistantIntent(
    sessionId: string,
    body: AssistantIntentResolveBody,
  ): Promise<AssistantIntentResolveResult> {
    return request<AssistantIntentResolveResult>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/intent/resolve`,
      { method: "POST", body },
    );
  },

  /** POST /assistant/sessions/{id}/plans */
  createAssistantPlan(
    sessionId: string,
    body: AssistantIntentPlanBody,
  ): Promise<AssistantIntentPlanResult> {
    return request<AssistantIntentPlanResult>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/plans`,
      { method: "POST", body },
    );
  },

  /** GET /assistant/sessions/{id}/plans/latest */
  getAssistantLatestPlan(
    sessionId: string,
  ): Promise<AssistantIntentPlanResult> {
    return request<AssistantIntentPlanResult>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/plans/latest`,
    );
  },

  /** POST /assistant/sessions/{id}/plans/execute */
  executeAssistantPlan(
    sessionId: string,
    body: AssistantIntentExecuteBody,
  ): Promise<AssistantIntentExecuteResult> {
    return request<AssistantIntentExecuteResult>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/plans/execute`,
      { method: "POST", body },
    );
  },

  /** GET /assistant/sessions/{id}/operations/{operationId} */
  getAssistantOperation(
    sessionId: string,
    operationId: string,
  ): Promise<AssistantOperationStatus> {
    return request<AssistantOperationStatus>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/operations/${encodeURIComponent(operationId)}`,
    );
  },

  /** GET /assistant/sessions/{id}/drafts */
  listAssistantDrafts(
    sessionId: string,
  ): Promise<AssistantDraft[]> {
    return request<AssistantDraft[]>(
      `/assistant/sessions/${encodeURIComponent(sessionId)}/drafts`,
    );
  },

  /** GET /assistant/drafts/{id} */
  getAssistantDraft(
    draftId: string,
  ): Promise<AssistantDraft> {
    return request<AssistantDraft>(
      `/assistant/drafts/${encodeURIComponent(draftId)}`,
    );
  },

  /** PATCH /assistant/drafts/{id} */
  updateAssistantDraft(
    draftId: string,
    body: DraftUpdateBody,
  ): Promise<AssistantDraft> {
    return request<AssistantDraft>(
      `/assistant/drafts/${encodeURIComponent(draftId)}`,
      { method: "PATCH", body },
    );
  },

  /** POST /assistant/drafts/{id}/validate */
  validateAssistantDraft(
    draftId: string,
  ): Promise<AssistantValidationReport> {
    return request<AssistantValidationReport>(
      `/assistant/drafts/${encodeURIComponent(draftId)}/validate`,
      { method: "POST" },
    );
  },

  /** POST /assistant/drafts/{id}/test */
  testAssistantDraft(
    draftId: string,
  ): Promise<AssistantTestReport> {
    return request<AssistantTestReport>(
      `/assistant/drafts/${encodeURIComponent(draftId)}/test`,
      { method: "POST" },
    );
  },

  /** POST /assistant/drafts/{id}/approve */
  approveAssistantDraft(
    draftId: string,
  ): Promise<AssistantDraft> {
    return request<AssistantDraft>(
      `/assistant/drafts/${encodeURIComponent(draftId)}/approve`,
      { method: "POST" },
    );
  },

  /** POST /assistant/drafts/{id}/publish */
  publishAssistantDraft(
    draftId: string,
  ): Promise<Record<string, unknown>> {
    return request<Record<string, unknown>>(
      `/assistant/drafts/${encodeURIComponent(draftId)}/publish`,
      { method: "POST" },
    );
  },

  /** GET /assistant/runs/{id} */
  getAssistantRun(
    runId: string,
  ): Promise<AssistantRun> {
    return request<AssistantRun>(
      `/assistant/runs/${encodeURIComponent(runId)}`,
    );
  },

  /** GET /assistant/config */
  getAssistantConfig(): Promise<AssistantConfig> {
    return request<AssistantConfig>("/assistant/config");
  },

  /** PATCH /assistant/config */
  updateAssistantConfig(
    body: {
      model?: string;
      reasoning?: string;
      disabled_intents?: string[];
      disabled_domains?: string[];
    },
  ): Promise<AssistantConfig> {
    return request<AssistantConfig>("/assistant/config", {
      method: "PATCH",
      body,
    });
  },

  /* ----- File / workspace storage (storage doc §4.7) ------------------- */

  /** GET /workflow-runs/{runId}/files[?kind=] */
  listRunFiles(
    runId: string,
    kind?: string,
    signal?: AbortSignal,
  ): Promise<WorkflowFileList> {
    const query = buildQuery({ kind });
    return request<WorkflowFileList>(
      `/workflow-runs/${encodeURIComponent(runId)}/files${query}`,
      { signal },
    );
  },

  /** POST /workflow-runs/{runId}/files (multipart upload) */
  uploadRunFile(
    runId: string,
    file: File,
    kind = "input",
    metadata?: Record<string, unknown>,
  ): Promise<WorkflowFile> {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("kind", kind);
    if (metadata) form.append("metadata", JSON.stringify(metadata));
    return uploadMultipart<WorkflowFile>(
      `/workflow-runs/${encodeURIComponent(runId)}/files`,
      form,
    );
  },

  /** Direct URL for GET /workflow-runs/{runId}/files/{fileId}/content (download). */
  runFileContentUrl(runId: string, fileId: string): string {
    return `${API_BASE}/workflow-runs/${encodeURIComponent(
      runId,
    )}/files/${encodeURIComponent(fileId)}/content`;
  },

  /** POST /workflow-runs/{runId}/artifacts — register a file as an artifact. */
  registerWorkflowArtifact(
    runId: string,
    fileId: string,
    opts: { artifact_type?: string; display_name?: string; summary?: string } = {},
  ): Promise<WorkflowFile> {
    return request<WorkflowFile>(
      `/workflow-runs/${encodeURIComponent(runId)}/artifacts`,
      { method: "POST", body: { file_id: fileId, ...opts } },
    );
  },

  /** POST /playground-runs/{runId}/files (multipart upload). */
  uploadPlaygroundFile(
    runId: string,
    file: File,
    kind = "input",
  ): Promise<WorkflowFile> {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("kind", kind);
    return uploadMultipart<WorkflowFile>(
      `/playground-runs/${encodeURIComponent(runId)}/files`,
      form,
    );
  },

  /** GET /playground-runs/{runId}/files[?kind=] */
  listPlaygroundFiles(
    runId: string,
    kind?: string,
    signal?: AbortSignal,
  ): Promise<WorkflowFileList> {
    const query = buildQuery({ kind });
    return request<WorkflowFileList>(
      `/playground-runs/${encodeURIComponent(runId)}/files${query}`,
      { signal },
    );
  },

  /* ----- Projects / workspaces ----------------------------------------- */

  /** GET /projects/storage */
  getProjectStorageConfig(signal?: AbortSignal): Promise<ProjectStorageConfig> {
    return request<ProjectStorageConfig>("/projects/storage", { signal });
  },

  /** GET /projects[?status=] */
  listProjects(status?: string, signal?: AbortSignal): Promise<Project[]> {
    return request<Project[]>(`/projects${buildQuery({ status })}`, { signal });
  },

  /** POST /projects */
  createProject(payload: {
    name: string;
    description?: string;
    storage_backend?: "local" | "s3";
  }): Promise<Project> {
    return request<Project>("/projects", { method: "POST", body: payload });
  },

  /** GET /projects/{id} */
  getProject(projectId: string, signal?: AbortSignal): Promise<Project> {
    return request<Project>(`/projects/${encodeURIComponent(projectId)}`, { signal });
  },

  /** PATCH /projects/{id} */
  updateProject(
    projectId: string,
    payload: { name?: string; description?: string; status?: string },
  ): Promise<Project> {
    return request<Project>(`/projects/${encodeURIComponent(projectId)}`, {
      method: "PATCH",
      body: payload,
    });
  },

  /** GET /projects/{id}/files */
  listProjectFiles(projectId: string, signal?: AbortSignal): Promise<WorkflowFileList> {
    return request<WorkflowFileList>(
      `/projects/${encodeURIComponent(projectId)}/files`,
      { signal },
    );
  },

  /** POST /projects/{id}/folders */
  createProjectFolder(projectId: string, path: string): Promise<ProjectDirectory> {
    return request<ProjectDirectory>(
      `/projects/${encodeURIComponent(projectId)}/folders`,
      { method: "POST", body: { path } },
    );
  },

  /** POST /projects/{id}/files (multipart upload) */
  uploadProjectFile(
    projectId: string,
    file: File,
    kind = "input",
    path?: string,
  ): Promise<WorkflowFile> {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("kind", kind);
    if (path?.trim()) {
      form.append("path", path.trim());
    }
    return uploadMultipart<WorkflowFile>(
      `/projects/${encodeURIComponent(projectId)}/files`,
      form,
    );
  },

  /** Direct URL for GET /projects/{id}/files/{fileId}/content (download). */
  projectFileContentUrl(projectId: string, fileId: string): string {
    return `${API_BASE}/projects/${encodeURIComponent(
      projectId,
    )}/files/${encodeURIComponent(fileId)}/content`;
  },

  /** DELETE /projects/{id}/files/{fileId} (soft-delete). */
  deleteProjectFile(projectId: string, fileId: string): Promise<void> {
    return request<void>(
      `/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(fileId)}`,
      { method: "DELETE" },
    );
  },
};

function stripNulls<T extends Record<string, unknown>>(obj: T): Partial<T> {
  const out: Partial<T> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v === undefined || v === null || v === "") continue;
    (out as Record<string, unknown>)[k] = v;
  }
  return out;
}

/* -------------------------------------------------------------------------- */
/* Globals                                                                    */
/* -------------------------------------------------------------------------- */

declare global {
  // The SPA shell sets this on the `window` object at boot time so the API
  // client knows what prefix the deployment is behind. Optional — the
  // default empty string covers the common "served at root" case.
  var __CALIBER_STATIC_PREFIX__: string | undefined;
  interface Window {
    __CALIBER_STATIC_PREFIX__?: string;
  }
}
