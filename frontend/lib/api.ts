/* REST client for the Cooldown backend (PLAN §13). Every call has a timeout and throws
 * ApiError with a readable message, so the UI can show "backend down" instead of crashing. */

import type {
  ActionRequest,
  Approval,
  AuditAnswer,
  HumanId,
  LedgerEntry,
  MetricsSummary,
  Message,
  Run,
  StaticWorld,
  VerifyResult,
} from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
export const WS_URL = (process.env.NEXT_PUBLIC_WS_URL || `${API_URL.replace(/^http/, "ws")}/ws`).replace(/\/$/, "");
/** Optional: the LangSmith project page URL (https://smith.langchain.com/o/<org>/projects/p/<project>). */
export const LANGSMITH_PROJECT_URL = (process.env.NEXT_PUBLIC_LANGSMITH_PROJECT_URL || "").replace(/\/$/, "");

export const SCENARIO_ID = "heatwave_2031";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
  /** True when the backend could not be reached at all. */
  get offline(): boolean {
    return this.status === 0;
  }
}

type Method = "GET" | "POST";

async function request<T>(method: Method, path: string, body?: unknown, timeoutMs = 15000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
      cache: "no-store",
    });
  } catch (e) {
    const aborted = e instanceof DOMException && e.name === "AbortError";
    throw new ApiError(aborted ? `Backend timed out (${path})` : `Backend unreachable at ${API_URL}`, 0);
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j?.detail === "string" ? j.detail : JSON.stringify(j?.detail ?? j);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(`${res.status}: ${detail}`, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T>(path: string, timeoutMs?: number) => request<T>("GET", path, undefined, timeoutMs);
const post = <T>(path: string, body: unknown = {}, timeoutMs?: number) => request<T>("POST", path, body, timeoutMs);

/** Unwrap list endpoints that may return either a bare array or {items|entries|...: []}. */
function asList<T>(v: unknown, ...keys: string[]): T[] {
  if (Array.isArray(v)) return v as T[];
  if (v && typeof v === "object") {
    for (const k of [...keys, "items", "results", "data"]) {
      const inner = (v as Record<string, unknown>)[k];
      if (Array.isArray(inner)) return inner as T[];
    }
  }
  return [];
}

const enc = encodeURIComponent;

export const api = {
  /** Governed runs also start a fast headless baseline (same seed) so /compare has numbers. */
  createRun: (mode: "governed" | "baseline", seed = 42, by: HumanId = "human:operator") =>
    post<{ run_id: string; baseline_run_id?: string }>(
      "/runs",
      { scenario_id: SCENARIO_ID, mode, seed, by, with_baseline: mode === "governed" },
      60000,
    ),
  getRun: (runId: string) => get<Run>(`/runs/${enc(runId)}`),
  pause: (runId: string, by?: HumanId) => post(`/runs/${enc(runId)}/pause`, by ? { by } : {}),
  resume: (runId: string, by?: HumanId) => post(`/runs/${enc(runId)}/resume`, by ? { by } : {}),
  setSpeed: (runId: string, x: number) => post(`/runs/${enc(runId)}/speed`, { x }),
  inject: (runId: string, event: "generator_trip" | "injection_message", by?: HumanId) =>
    post(`/runs/${enc(runId)}/inject`, by ? { event, by } : { event }),

  ledger: async (runId: string, afterSeq = 0) =>
    asList<LedgerEntry>(await get<unknown>(`/runs/${enc(runId)}/ledger?after_seq=${afterSeq}`, 20000), "entries", "ledger"),
  approvals: async (runId: string) =>
    asList<Approval>(await get<unknown>(`/runs/${enc(runId)}/approvals`), "approvals"),
  /** Optional endpoint; callers must tolerate a 404. */
  requests: async (runId: string) =>
    asList<ActionRequest>(await get<unknown>(`/runs/${enc(runId)}/requests`), "requests"),
  decideApproval: (approvalId: string, decision: "approved" | "denied", by: HumanId, note?: string) =>
    post<Approval>(`/approvals/${enc(approvalId)}/decide`, { decision, by, note: note || null }),

  halt: (runId: string, by: HumanId, reason: string, scope = "all") =>
    post("/control/halt", { run_id: runId, scope, by, reason }),
  resumeAgents: (runId: string, by: HumanId) => post("/control/resume", { run_id: runId, by }),
  rollback: (executionId: string, by: HumanId, reason: string) =>
    post<ActionRequest>(`/executions/${enc(executionId)}/rollback`, { by, reason }),

  auditAsk: (runId: string, question: string, by: HumanId) =>
    post<AuditAnswer>("/audit/ask", { run_id: runId, question, by }, 60000),
  verifyLedger: (runId: string) => get<VerifyResult>(`/ledger/verify?run_id=${enc(runId)}`, 30000),

  metrics: (runId: string) => get<MetricsSummary>(`/runs/${enc(runId)}/metrics`),
  compare: (runId: string, baselineRunId?: string | null) =>
    get<Record<string, unknown>>(
      `/runs/${enc(runId)}/compare${baselineRunId ? `?baseline_run_id=${enc(baselineRunId)}` : ""}`,
    ),

  worldStatic: () => get<StaticWorld>("/world/static", 30000),
  resident: (residentId: string, runId?: string) =>
    get<Record<string, unknown>>(`/residents/${enc(residentId)}${runId ? `?run_id=${enc(runId)}` : ""}`),
  residentThread: async (residentId: string, runId?: string) =>
    asList<Message>(
      await get<unknown>(`/residents/${enc(residentId)}/thread${runId ? `?run_id=${enc(runId)}` : ""}`),
      "messages",
      "thread",
    ),
};

/** Link for the "Trace" button: a direct URL when the backend gives one, else the LangSmith app. */
export function traceUrl(x: { trace_url?: string | null; langsmith_run_id?: string | null } | undefined | null): string | null {
  if (!x) return null;
  if (x.trace_url) return x.trace_url;
  if (!x.langsmith_run_id) return null;
  // Same shape as langsmith Client.get_run_url(): <project page>/r/<run id>.
  if (LANGSMITH_PROJECT_URL) return `${LANGSMITH_PROJECT_URL}/r/${x.langsmith_run_id}?poll=true`;
  return "https://smith.langchain.com/";
}
