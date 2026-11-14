"use client";

/* One Zustand store for the run view (PLAN §14 slices). The console renders what is in the
 * ledger (database), merged with live `request`/`approval`/`plan` pushes from the WebSocket.
 *
 * W10 contract (CONTRACTS.md): staticWorld, mapBuffers {temps, flags, simTs}, sections,
 * selectedResidentId, selectedSectionId, setSelectedResident, setSelectedSection, threads. */

import { create } from "zustand";
import { asNumber, asString, isRecord } from "@/lib/format";
import type {
  ActionRequest,
  Approval,
  Decision,
  Execution,
  HumanId,
  LedgerEntry,
  MapBuffers,
  Message,
  MetricsSummary,
  Plan,
  Run,
  SectionState,
  StaticWorld,
  TickMsg,
  Verdict,
  WsMessage,
} from "@/lib/types";

export type ConnState = "idle" | "connecting" | "open" | "reconnecting" | "closed";
export type ConsoleTab = "all" | "approval" | "denied" | "messages" | "auditor";

export interface Card {
  requestId: string;
  request: ActionRequest;
  decision?: Decision;
  approval?: Approval;
  execution?: Execution;
  seqs: number[];
  messagesOut: number;
  rollbackRequested: boolean;
  held: boolean;
  untrusted: boolean;
  order: number;
}

export type FeedItem =
  | { kind: "request"; key: string; id: string; order: number }
  | { kind: "plan"; key: string; id: string; order: number }
  | { kind: "event"; key: string; seq: number; order: number };

export interface ForecastInfo {
  simTs?: string;
  windows: { start: string; end: string; peak_short_mw: number }[];
  explanation?: string;
}

export interface Toast {
  id: number;
  kind: "info" | "error" | "ok";
  text: string;
}

interface RunState {
  runId: string | null;
  run: Run | null;
  runError: string | null;
  connection: ConnState;
  backendDown: boolean;

  // clock / tick
  clock: TickMsg | null;
  tick: number;
  halted: boolean;

  // ledger + cards
  ledgerBySeq: Record<number, LedgerEntry>;
  lastSeq: number;
  cardsByRequest: Record<string, Card>;
  plans: Record<string, Plan>;
  feed: FeedItem[];
  forecast: ForecastInfo | null;

  // approvals
  approvals: Record<string, Approval>;
  approvalDrawerOpen: boolean;

  // map (W10 reads these)
  staticWorld: StaticWorld | null;
  mapBuffers: MapBuffers | null;
  sections: Record<string, SectionState>;
  selectedResidentId: string | null;
  selectedSectionId: string | null;

  // messages
  threads: Record<string, Message[]>;

  // humans + UI
  identity: HumanId;
  tab: ConsoleTab;
  highlightSeq: number | null;
  metrics: MetricsSummary | null;
  comparisonOpen: boolean;
  toasts: Toast[];
}

interface RunActions {
  reset: (runId: string) => void;
  setRun: (run: Run | null, error?: string | null) => void;
  setConnection: (c: ConnState) => void;
  setBackendDown: (down: boolean) => void;
  setStaticWorld: (w: StaticWorld) => void;
  ingest: (msgs: WsMessage[]) => void;
  ingestLedger: (entries: LedgerEntry[]) => void;
  upsertApprovals: (list: Approval[]) => void;
  upsertRequests: (list: ActionRequest[]) => void;
  setThread: (threadId: string, messages: Message[]) => void;
  setIdentity: (id: HumanId) => void;
  /** Restore the saved identity after hydration (never during SSR, to avoid a mismatch). */
  loadSavedIdentity: () => void;
  setTab: (t: ConsoleTab) => void;
  focusSeq: (seq: number | null) => void;
  setSelectedResident: (id: string | null) => void;
  setSelectedSection: (id: string | null) => void;
  setApprovalDrawerOpen: (open: boolean) => void;
  setComparisonOpen: (open: boolean) => void;
  setMetrics: (m: MetricsSummary | null) => void;
  setHalted: (h: boolean) => void;
  toast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: number) => void;
}

export type RunStore = RunState & RunActions;

const IDENTITY_KEY = "cooldown.identity";

function loadIdentity(): HumanId {
  try {
    const v = typeof window !== "undefined" ? window.localStorage.getItem(IDENTITY_KEY) : null;
    if (v === "human:operator" || v === "human:supervisor" || v === "human:auditor") return v;
  } catch {
    /* storage blocked */
  }
  return "human:operator";
}

const initial = (): RunState => ({
  runId: null,
  run: null,
  runError: null,
  connection: "idle",
  backendDown: false,
  clock: null,
  tick: 0,
  halted: false,
  ledgerBySeq: {},
  lastSeq: 0,
  cardsByRequest: {},
  plans: {},
  feed: [],
  forecast: null,
  approvals: {},
  approvalDrawerOpen: false,
  staticWorld: null,
  mapBuffers: null,
  sections: {},
  selectedResidentId: null,
  selectedSectionId: null,
  threads: {},
  identity: "human:operator",
  tab: "all",
  highlightSeq: null,
  metrics: null,
  comparisonOpen: false,
  toasts: [],
});

// ------------------------------------------------------------------ decoding

export function b64ToBytes(b64: string | null | undefined): Uint8Array {
  if (!b64) return new Uint8Array(0);
  try {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch {
    return new Uint8Array(0);
  }
}

// ------------------------------------------------------------------ merging helpers

const STATUS_RANK: Record<string, number> = {
  proposed: 0,
  needs_approval: 1,
  approved: 2,
  denied: 3,
  cancelled: 3,
  executing: 3,
  verified: 4,
  partial: 4,
  failed: 4,
  rolled_back: 5,
};

function advance(current: string | undefined, next: string | undefined): string | undefined {
  if (!next) return current;
  if (!current) return next;
  return (STATUS_RANK[next] ?? 0) >= (STATUS_RANK[current] ?? 0) ? next : current;
}

/** Copy only the keys of `fill` that `base` lacks (or has as null/undefined). */
function fillMissing<T extends object>(base: T, fill: Partial<T>): T {
  const out = { ...base } as Record<string, unknown>;
  for (const [k, v] of Object.entries(fill)) {
    if (v !== undefined && (out[k] === undefined || out[k] === null)) out[k] = v;
  }
  return out as T;
}

function stripUndef<T extends object>(o: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v;
  return out as Partial<T>;
}

const VERDICTS: Verdict[] = ["partial_allow", "needs_approval", "allow", "deny"];

function decisionFrom(p: Record<string, unknown>, e: LedgerEntry, requestId: string): Decision | undefined {
  const src = isRecord(p.decision) ? (p.decision as Record<string, unknown>) : p;
  let verdict = asString(src.verdict) as Verdict | undefined;
  if (!verdict) {
    const m = /\b(partial[_ ]allow|needs[_ ]approval|allow(?:ed)?|den(?:y|ied))\b/i.exec(e.summary || "");
    if (m) {
      const w = m[1].toLowerCase().replace(" ", "_");
      verdict = w.startsWith("partial") ? "partial_allow" : w.startsWith("needs") ? "needs_approval" : w.startsWith("allow") ? "allow" : "deny";
    }
  }
  if (!verdict || !VERDICTS.includes(verdict)) return undefined;
  return {
    ...(src as Partial<Decision>),
    _id: (src._id as string) ?? e.refs?.decision_id ?? undefined,
    request_id: requestId,
    sim_ts: e.sim_ts,
    verdict,
  } as Decision;
}

function statusFromVerdict(v: Verdict): string {
  return v === "deny" ? "denied" : v === "needs_approval" ? "needs_approval" : "approved";
}

function newCard(requestId: string, order: number): Card {
  return {
    requestId,
    request: { _id: requestId },
    seqs: [],
    messagesOut: 0,
    rollbackRequested: false,
    held: false,
    untrusted: false,
    order,
  };
}

function hasUntrusted(req: ActionRequest, d?: Decision): boolean {
  return !!(d?.untrusted_influence || req.evidence?.some((e) => e.trust === "untrusted"));
}

/** Ledger types that belong to a request card rather than the event stream. */
const CARD_TYPES = new Set([
  "request_proposed",
  "decision",
  "approval_requested",
  "approval_decided",
  "execution_sent",
  "execution_verified",
  "execution_failed",
  "rollback_requested",
  "message_out",
]);

// ------------------------------------------------------------------ reducer

type Draft = Pick<
  RunState,
  | "ledgerBySeq"
  | "lastSeq"
  | "cardsByRequest"
  | "plans"
  | "feed"
  | "approvals"
  | "approvalDrawerOpen"
  | "threads"
  | "halted"
  | "clock"
  | "tick"
  | "mapBuffers"
  | "sections"
  | "metrics"
  | "comparisonOpen"
  | "forecast"
>;

class Reducer {
  d: Draft;
  touched = { ledger: false, cards: false, plans: false, feed: false, approvals: false, threads: false, sections: false };
  feedKeys: Set<string>;
  copied = new Set<string>();

  constructor(s: RunState) {
    this.d = {
      ledgerBySeq: s.ledgerBySeq,
      lastSeq: s.lastSeq,
      cardsByRequest: s.cardsByRequest,
      plans: s.plans,
      feed: s.feed,
      approvals: s.approvals,
      approvalDrawerOpen: s.approvalDrawerOpen,
      threads: s.threads,
      halted: s.halted,
      clock: s.clock,
      tick: s.tick,
      mapBuffers: s.mapBuffers,
      sections: s.sections,
      metrics: s.metrics,
      comparisonOpen: s.comparisonOpen,
      forecast: s.forecast,
    };
    this.feedKeys = new Set(s.feed.map((f) => f.key));
  }

  private cards(): Record<string, Card> {
    if (!this.touched.cards) {
      this.d.cardsByRequest = { ...this.d.cardsByRequest };
      this.touched.cards = true;
    }
    return this.d.cardsByRequest;
  }

  private pushFeed(item: FeedItem) {
    if (this.feedKeys.has(item.key)) return;
    if (!this.touched.feed) {
      this.d.feed = [...this.d.feed];
      this.touched.feed = true;
    }
    this.d.feed.push(item);
    this.feedKeys.add(item.key);
  }

  private card(requestId: string, order: number): Card {
    const cards = this.cards();
    const existing = cards[requestId];
    if (existing && this.copied.has(requestId)) return existing; // already private to this pass
    const c = existing ? { ...existing } : newCard(requestId, order);
    if (!existing) this.pushFeed({ kind: "request", key: `r:${requestId}`, id: requestId, order });
    cards[requestId] = c;
    this.copied.add(requestId);
    return c;
  }

  private nextOrder(): number {
    return this.d.lastSeq + 0.5;
  }

  ledger(e: LedgerEntry) {
    if (typeof e?.seq !== "number") return;
    if (this.d.ledgerBySeq[e.seq]) return;
    if (!this.touched.ledger) {
      this.d.ledgerBySeq = { ...this.d.ledgerBySeq };
      this.touched.ledger = true;
    }
    this.d.ledgerBySeq[e.seq] = e;
    if (e.seq > this.d.lastSeq) this.d.lastSeq = e.seq;

    const p = (isRecord(e.payload) ? e.payload : {}) as Record<string, unknown>;
    const rid = e.refs?.request_id ?? undefined;

    if (e.type === "halt") this.d.halted = true;
    if (e.type === "resume") this.d.halted = false;

    if (e.type === "plan_created") {
      const planId = asString(p.plan_id) ?? `plan_v${e.refs?.plan_version ?? e.seq}`;
      const prev = this.d.plans[planId];
      const fromLedger: Plan = {
        _id: planId,
        version: e.refs?.plan_version ?? asNumber(p.version),
        sim_ts: e.sim_ts,
        created_by: e.actor,
        acting_for: e.acting_for ?? undefined,
        source: asString(p.source),
        trigger: asString(p.trigger),
        rationale: asString(p.rationale) ?? e.summary,
        expected_shortfall_mw: asNumber(p.expected_shortfall_mw),
        steps: Array.isArray(p.steps) ? (p.steps as Plan["steps"]) : undefined,
        evidence_ids: Array.isArray(p.evidence_ids) ? (p.evidence_ids as string[]) : undefined,
        similar_events: Array.isArray(p.similar_events) ? (p.similar_events as Plan["similar_events"]) : undefined,
        langsmith_run_id: e.langsmith_run_id ?? undefined,
      };
      this.plan(prev ? fillMissing(prev, stripUndef(fromLedger)) : fromLedger, e.seq);
      return;
    }

    if (e.type === "world_event" && p.kind === "forecast") {
      const windows = Array.isArray(p.windows) ? (p.windows as ForecastInfo["windows"]) : [];
      this.d.forecast = { simTs: e.sim_ts, windows, explanation: asString(p.explanation) };
      return;
    }

    // Mirror SMS traffic into threads so the Messages tab works even without `message` pushes.
    if (e.type === "message_out" || e.type === "message_in") {
      const inbound = e.type === "message_in";
      const thread = asString(inbound ? p.thread_id : p.to);
      if (thread) {
        const quoted = /"(.*)"\s*$/.exec(e.summary || "");
        this.message({
          _id: e.refs?.message_id ?? `seq:${e.seq}`,
          thread_id: thread,
          from: e.actor,
          body: asString(p.body) ?? (quoted ? quoted[1] : e.summary),
          direction: inbound ? "in" : "out",
          trust: inbound ? "untrusted" : "system",
          sim_ts: e.sim_ts,
          request_id: rid ?? null,
          decision: asString(p.decision) ?? null,
        });
      }
      if (inbound) {
        this.pushFeed({ kind: "event", key: `e:${e.seq}`, seq: e.seq, order: e.seq });
        return;
      }
    }

    if (rid && CARD_TYPES.has(e.type)) {
      const c = this.card(rid, e.seq);
      if (!c.seqs.includes(e.seq)) c.seqs = [...c.seqs, e.seq].sort((a, b) => a - b);
      this.cardLedger(c, e, p);
      c.untrusted = c.untrusted || hasUntrusted(c.request, c.decision);
      return;
    }

    // Everything else is an event row in the console stream (message_in, contingency, halt, ...).
    this.pushFeed({ kind: "event", key: `e:${e.seq}`, seq: e.seq, order: e.seq });
  }

  private cardLedger(c: Card, e: LedgerEntry, p: Record<string, unknown>) {
    const refs = e.refs ?? {};
    switch (e.type) {
      case "request_proposed": {
        const evidence = Array.isArray(p.evidence) ? (p.evidence as ActionRequest["evidence"]) : undefined;
        const fromLedger: ActionRequest = {
          _id: c.requestId,
          summary: e.summary,
          proposed_by: e.actor,
          acting_for: e.acting_for ?? undefined,
          created_sim_ts: e.sim_ts,
          action_type: asString(p.action_type),
          system: asString(p.system),
          rung: asNumber(p.rung),
          step_id: asString(p.step_id),
          params: isRecord(p.params) ? p.params : undefined,
          window: isRecord(p.window) ? (p.window as unknown as ActionRequest["window"]) : undefined,
          expected_mw: asNumber(p.expected_mw),
          evidence,
          targets_count: asNumber(p.targets_count),
          targets: { section_id: asString(p.section_id) ?? null, business_id: asString(p.business_id) ?? null },
          plan_version: refs.plan_version ?? undefined,
          undo_of: asString(p.undo_of),
          langsmith_run_id: e.langsmith_run_id ?? undefined,
        };
        c.request = fillMissing(c.request, stripUndef(fromLedger));
        c.request.status = advance(c.request.status, "proposed");
        if (p.untrusted_evidence === true) c.untrusted = true;
        break;
      }
      case "decision": {
        const d = decisionFrom(p, e, c.requestId);
        if (d) {
          c.decision = c.decision ? { ...d, ...c.decision } : d;
          c.request = { ...c.request, status: advance(c.request.status, statusFromVerdict(d.verdict)) };
        }
        break;
      }
      case "approval_requested": {
        const src = isRecord(p.approval) ? (p.approval as Partial<Approval>) : (p as Partial<Approval>);
        const id = refs.approval_id ?? src._id;
        if (id) {
          const a: Approval = {
            ...(src as Approval),
            _id: id,
            request_id: c.requestId,
            status: (src.status as Approval["status"]) ?? "pending",
            summary: src.summary ?? e.summary,
            requested_sim_ts: src.requested_sim_ts ?? e.sim_ts,
          };
          this.approval(this.d.approvals[id] ? { ...a, ...this.d.approvals[id] } : a);
        }
        c.request = { ...c.request, status: advance(c.request.status, "needs_approval") };
        break;
      }
      case "approval_decided": {
        const id = refs.approval_id;
        const st = (asString(p.status) ?? asString(p.decision) ?? (/deni/i.test(e.summary) ? "denied" : "approved")) as Approval["status"];
        if (id) {
          const prev = this.d.approvals[id];
          this.approval({
            ...(prev ?? { _id: id, request_id: c.requestId, summary: e.summary }),
            status: st,
            decided_by: prev?.decided_by ?? e.actor,
            decided_sim_ts: prev?.decided_sim_ts ?? e.sim_ts,
            note: prev?.note ?? asString(p.note) ?? null,
          } as Approval);
        }
        c.request = { ...c.request, status: advance(c.request.status, st === "approved" ? "approved" : "denied") };
        break;
      }
      case "execution_sent": {
        c.execution = {
          ...(c.execution ?? {}),
          _id: refs.execution_id ?? c.execution?._id,
          request_id: c.requestId,
          status: c.execution?.status && c.execution.status !== "sent" ? c.execution.status : "sent",
          system: asString(p.system) ?? c.execution?.system,
          attempts: asNumber(p.attempts) ?? c.execution?.attempts,
          targets: asNumber(p.targets) ?? c.execution?.targets,
          homes_real: asNumber(p.homes_real) ?? c.execution?.homes_real,
          undo: isRecord(p.undo) ? (p.undo as Execution["undo"]) : c.execution?.undo,
          sent_sim_ts: c.execution?.sent_sim_ts ?? e.sim_ts,
        };
        c.held = false;
        c.request = { ...c.request, status: advance(c.request.status, "executing") };
        break;
      }
      case "execution_verified":
      case "execution_failed": {
        if (e.type === "execution_failed" && p.refused === true) {
          // Executor refusal (R15 kill switch / R01). "held until resume" keeps the request alive.
          const held = /held until resume/i.test(e.summary);
          c.held = held;
          c.execution = { ...(c.execution ?? {}), refused: true, detail: asString(p.detail) ?? e.summary, status: held ? c.execution?.status : "failed" };
          if (!held) c.request = { ...c.request, status: advance(c.request.status, "failed") };
          break;
        }
        const st = asString(p.status) ?? (e.type === "execution_failed" ? "failed" : "verified");
        c.execution = {
          ...(c.execution ?? {}),
          _id: refs.execution_id ?? c.execution?._id,
          request_id: c.requestId,
          status: st,
          verification: isRecord(p.verification) ? (p.verification as Execution["verification"]) : c.execution?.verification,
          verified_sim_ts: e.sim_ts,
        };
        c.request = { ...c.request, status: advance(c.request.status, st === "rolled_back" ? "rolled_back" : st) };
        break;
      }
      case "rollback_requested": {
        const undoOf = asString(p.undo_of) ?? asString(p.execution_id);
        // The entry is about the new undo request; mark the original card too when we can find it.
        c.request = fillMissing(c.request, { action_type: "undo", summary: e.summary, proposed_by: e.actor, undo_of: undoOf });
        if (undoOf) {
          const cards = this.cards();
          for (const [k, other] of Object.entries(cards)) {
            if (other.execution?._id === undoOf && k !== c.requestId) {
              cards[k] = { ...other, rollbackRequested: true };
              this.copied.add(k);
            }
          }
        }
        break;
      }
      case "message_out":
        c.messagesOut += 1;
        break;
    }
  }

  plan(plan: Plan, order?: number) {
    if (!this.touched.plans) {
      this.d.plans = { ...this.d.plans };
      this.touched.plans = true;
    }
    const prev = this.d.plans[plan._id];
    this.d.plans[plan._id] = prev ? { ...prev, ...stripUndef(plan) } : plan;
    this.pushFeed({ kind: "plan", key: `p:${plan._id}`, id: plan._id, order: order ?? this.nextOrder() });
  }

  approval(a: Approval) {
    if (!a?._id) return;
    if (!this.touched.approvals) {
      this.d.approvals = { ...this.d.approvals };
      this.touched.approvals = true;
    }
    const prev = this.d.approvals[a._id];
    if (a.status === "pending" && !prev) this.d.approvalDrawerOpen = true;
    this.d.approvals[a._id] = prev ? { ...prev, ...stripUndef(a) } : a;
    if (a.request_id) {
      const c = this.card(a.request_id, this.nextOrder());
      c.approval = this.d.approvals[a._id];
      if (a.status === "pending") c.request = { ...c.request, status: advance(c.request.status, "needs_approval") };
    }
  }

  request(r: ActionRequest & { decision?: Decision; execution?: Execution; approval?: Approval }) {
    if (!r?._id) return;
    const { decision, execution, approval, ...req } = r;
    const c = this.card(req._id, this.nextOrder());
    const prevStatus = c.request.status;
    c.request = { ...c.request, ...stripUndef(req) };
    // A live push is authoritative, but never let a stale push move a card backwards.
    c.request.status = advance(prevStatus, req.status);
    if (decision && isRecord(decision) && decision.verdict) c.decision = { ...(c.decision ?? {}), ...decision };
    if (execution && isRecord(execution)) c.execution = { ...(c.execution ?? {}), ...execution };
    if (approval && isRecord(approval) && approval._id) this.approval(approval as Approval);
    c.untrusted = c.untrusted || hasUntrusted(c.request, c.decision);
  }

  message(m: Message) {
    if (!m?.thread_id) return;
    if (!this.touched.threads) {
      this.d.threads = { ...this.d.threads };
      this.touched.threads = true;
    }
    const list = this.d.threads[m.thread_id] ?? [];
    const i = m._id ? list.findIndex((x) => x._id === m._id) : -1;
    if (i >= 0) {
      // Keep the fuller copy (ledger summaries are clipped; `message` pushes carry the full body).
      if ((m.body?.length ?? 0) > (list[i].body?.length ?? 0)) {
        const next = [...list];
        next[i] = { ...list[i], ...m };
        this.d.threads[m.thread_id] = next;
      }
      return;
    }
    this.d.threads[m.thread_id] = [...list, m];
  }

  ws(msg: WsMessage) {
    switch (msg.type) {
      case "tick": {
        const t = msg as TickMsg;
        this.d.clock = t;
        if (typeof t.tick === "number") this.d.tick = t.tick;
        if (typeof t.halted === "boolean") this.d.halted = t.halted;
        break;
      }
      case "ledger":
        if (isRecord(msg.entry)) this.ledger(msg.entry as unknown as LedgerEntry);
        break;
      case "approval":
        if (isRecord(msg.approval)) this.approval(msg.approval as unknown as Approval);
        break;
      case "request":
        if (isRecord(msg.request)) this.request(msg.request as unknown as ActionRequest);
        break;
      case "plan":
        if (isRecord(msg.plan) && (msg.plan as unknown as Plan)._id) this.plan(msg.plan as unknown as Plan);
        break;
      case "map": {
        const m = msg as { sim_ts: string; temps: string; flags: string };
        this.d.mapBuffers = { temps: b64ToBytes(m.temps), flags: b64ToBytes(m.flags), simTs: m.sim_ts };
        break;
      }
      case "sections": {
        const states = (msg as { states?: SectionState[] }).states;
        if (Array.isArray(states)) {
          const next: Record<string, SectionState> = { ...this.d.sections };
          for (const s of states) if (s?.id) next[s.id] = { ...next[s.id], ...s };
          this.d.sections = next;
        }
        break;
      }
      case "message":
        if (isRecord(msg.entry)) this.message(msg.entry as unknown as Message);
        break;
      case "control": {
        const c = (msg as { control?: { halted?: boolean } }).control;
        if (c && typeof c.halted === "boolean") this.d.halted = c.halted;
        break;
      }
      case "metrics": {
        const s = (msg as { summary?: MetricsSummary }).summary;
        if (isRecord(s)) {
          this.d.metrics = s;
          this.d.comparisonOpen = true;
        }
        break;
      }
    }
  }
}

// ------------------------------------------------------------------ store

let toastSeq = 0;

export const useRunStore = create<RunStore>()((set, get) => ({
  ...initial(),

  reset: (runId) =>
    set((s) => ({
      ...initial(),
      runId,
      identity: s.identity,
      staticWorld: s.staticWorld, // static geometry is shared by all runs
    })),

  setRun: (run, error = null) =>
    set((s) => ({
      run,
      runError: error,
      halted: s.halted,
    })),

  setConnection: (connection) => set({ connection }),
  setBackendDown: (backendDown) => set({ backendDown }),
  setStaticWorld: (staticWorld) => set({ staticWorld }),

  ingest: (msgs) => {
    if (!msgs.length) return;
    const r = new Reducer(get());
    for (const m of msgs) {
      try {
        r.ws(m);
      } catch (err) {
        console.warn("ws message dropped", m?.type, err);
      }
    }
    set(r.d);
  },

  ingestLedger: (entries) => {
    if (!entries.length) return;
    const r = new Reducer(get());
    for (const e of [...entries].sort((a, b) => a.seq - b.seq)) r.ledger(e);
    set(r.d);
  },

  upsertApprovals: (list) => {
    if (!list.length) return;
    const r = new Reducer(get());
    for (const a of list) r.approval(a);
    set(r.d);
  },

  upsertRequests: (list) => {
    if (!list.length) return;
    const r = new Reducer(get());
    for (const q of list) r.request(q);
    set(r.d);
  },

  setThread: (threadId, messages) =>
    set((s) => {
      const merged = new Map<string, Message>();
      for (const m of [...messages, ...(s.threads[threadId] ?? [])]) merged.set(m._id ?? `${m.sim_ts}:${m.body}`, m);
      const list = [...merged.values()].sort((a, b) => (a.sim_ts ?? "").localeCompare(b.sim_ts ?? ""));
      return { threads: { ...s.threads, [threadId]: list } };
    }),

  setIdentity: (identity) => {
    try {
      window.localStorage.setItem(IDENTITY_KEY, identity);
    } catch {
      /* storage blocked */
    }
    set({ identity });
  },

  loadSavedIdentity: () => set({ identity: loadIdentity() }),

  setTab: (tab) => set({ tab }),
  focusSeq: (highlightSeq) => set(highlightSeq === null ? { highlightSeq } : { highlightSeq, tab: "all" }),
  setSelectedResident: (selectedResidentId) => set({ selectedResidentId }),
  setSelectedSection: (selectedSectionId) => set({ selectedSectionId }),
  setApprovalDrawerOpen: (approvalDrawerOpen) => set({ approvalDrawerOpen }),
  setComparisonOpen: (comparisonOpen) => set({ comparisonOpen }),
  setMetrics: (metrics) => set({ metrics }),
  setHalted: (halted) => set({ halted }),

  toast: (kind, text) => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts.slice(-3), { id, kind, text }] }));
    setTimeout(() => get().dismissToast(id), kind === "error" ? 7000 : 4000);
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

// ------------------------------------------------------------------ selectors

export const isSupervisor = (s: RunState) => s.identity === "human:supervisor";

export function pendingApprovals(approvals: Record<string, Approval>): Approval[] {
  return Object.values(approvals)
    .filter((a) => a.status === "pending")
    .sort((a, b) => (a.requested_sim_ts ?? "").localeCompare(b.requested_sim_ts ?? ""));
}
