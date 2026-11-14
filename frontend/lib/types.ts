import type { FeatureCollection } from "geojson";

/* Wire types mirroring backend/app/models (Pydantic, dumped by_alias). Every field the
 * backend may omit is optional: the UI must render partial documents without crashing. */

export type ActionType =
  | "precool"
  | "thermostat_setback"
  | "ev_pause"
  | "battery_discharge"
  | "business_curtail_request"
  | "send_sms"
  | "feeder_outage"
  | "feeder_restore"
  | "thermostat_restore"
  | "undo";

export type RequestStatus =
  | "proposed"
  | "denied"
  | "needs_approval"
  | "approved"
  | "executing"
  | "verified"
  | "partial"
  | "failed"
  | "rolled_back"
  | "cancelled";

export type Verdict = "allow" | "partial_allow" | "deny" | "needs_approval";
export type Trust = "system" | "untrusted";

export type HumanId = "human:operator" | "human:supervisor" | "human:auditor";

export interface Evidence {
  id: string;
  kind?: string;
  trust?: Trust;
}

export interface WindowHHMM {
  start: string;
  end: string;
}

export interface Targets {
  household_ids?: string[];
  section_id?: string | null;
  business_id?: string | null;
  recipient_ids?: string[];
}

export interface ActionRequest {
  _id: string;
  run_id?: string;
  created_sim_ts?: string;
  sim_ts?: string;
  action_type?: ActionType | string;
  system?: string;
  rung?: number | null;
  proposed_by?: string;
  acting_for?: string;
  plan_id?: string | null;
  plan_version?: number | null;
  step_id?: string | null;
  summary?: string;
  targets?: Targets;
  targets_count?: number;
  params?: Record<string, unknown>;
  window?: WindowHHMM | null;
  evidence?: Evidence[];
  expected_mw?: number;
  status?: RequestStatus | string;
  undo_of?: string | null;
  decision_id?: string | null;
  approval_id?: string | null;
  execution_id?: string | null;
  langsmith_run_id?: string | null;
  trace_url?: string | null;
}

export interface RuleResult {
  rule_id: string;
  name?: string;
  applies?: boolean;
  passed?: boolean;
  effect?: "deny" | "needs_approval" | "none";
  detail?: string;
  counts?: Record<string, number>;
}

export interface BlastRadius {
  homes_real?: number;
  vulnerable_real?: number;
  mw?: number;
}

export interface Decision {
  _id?: string;
  request_id?: string;
  sim_ts?: string;
  verdict: Verdict;
  rule_results?: RuleResult[];
  allowed_count?: number;
  denied_count?: number;
  denied_reasons?: Record<string, number>;
  blast_radius?: BlastRadius;
  policy_version?: string;
  decided_by?: string;
  untrusted_influence?: boolean;
}

export interface Approval {
  _id: string;
  run_id?: string;
  request_id: string;
  decision_id?: string;
  status: "pending" | "approved" | "denied" | "expired";
  summary?: string;
  blast_radius?: BlastRadius;
  rule_results?: RuleResult[];
  untrusted_influence?: boolean;
  requested_sim_ts?: string;
  expires_sim_ts?: string;
  decided_by?: string | null;
  decided_sim_ts?: string | null;
  note?: string | null;
}

export interface Verification {
  applied?: number;
  pending?: number;
  offline?: number;
  overridden?: number;
  failed?: number;
  delivered_mw?: number;
}

export interface Execution {
  _id?: string;
  request_id?: string;
  system?: string;
  status?: "sent" | "verified" | "partial" | "failed" | "rolled_back" | string;
  verification?: Verification;
  attempts?: number;
  sent_sim_ts?: string;
  verified_sim_ts?: string | null;
  undo?: { system?: string; path?: string } | null;
  targets?: number;
  homes_real?: number;
  refused?: boolean;
  detail?: string;
}

export interface SimilarEvent {
  id: string;
  summary?: string;
  lesson?: string;
  score?: number;
}

export interface PlanStep {
  step_id: string;
  rung?: number;
  action_type?: string;
  expected_mw?: number;
  window?: WindowHHMM;
  evidence_ids?: string[];
  params?: Record<string, unknown>;
  selector?: Record<string, unknown>;
}

export interface Plan {
  _id: string;
  run_id?: string;
  version?: number;
  sim_ts?: string;
  created_by?: string;
  acting_for?: string;
  source?: "llm" | "cache" | "fallback" | string;
  trigger?: string;
  rationale?: string;
  expected_shortfall_mw?: number;
  steps?: PlanStep[];
  evidence_ids?: string[];
  similar_events?: (SimilarEvent | string)[];
  langsmith_run_id?: string | null;
  trace_url?: string | null;
}

export interface Message {
  _id: string;
  run_id?: string;
  thread_id: string;
  from?: string;
  to?: string;
  body: string;
  direction?: "out" | "in";
  trust?: Trust;
  sim_ts?: string;
  request_id?: string | null;
  offer?: Record<string, unknown> | null;
  decision?: string | null;
}

export interface LedgerEntry {
  _id?: string;
  run_id?: string;
  seq: number;
  sim_ts?: string;
  wall_ts?: string;
  type: string;
  actor: string;
  acting_for?: string | null;
  summary: string;
  refs?: {
    request_id?: string | null;
    decision_id?: string | null;
    approval_id?: string | null;
    execution_id?: string | null;
    plan_version?: number | null;
    message_id?: string | null;
  };
  payload?: Record<string, unknown>;
  payload_digest?: string;
  prev_hash?: string;
  hash?: string;
  langsmith_run_id?: string | null;
}

export interface Run {
  _id: string;
  scenario_id?: string;
  mode?: "governed" | "baseline";
  seed?: number;
  status?: string;
  sim_ts?: string;
  speed?: number;
  scale?: number;
  households?: number;
  baseline_run_id?: string | null;
  policy_version?: string | null;
}

export interface GeneratedScenario {
  _id: string;
  id: string;
  title: string;
  summary?: string;
  date?: string;
  sim_start?: string;
  sim_end?: string;
  weather?: {
    source?: string;
    profile_c?: Record<string, number>;
  };
  target_peak_mw: number;
  supply_mw: number;
  supply_schedule_mw?: Record<string, number>;
  contingencies?: {
    at: string;
    type: string;
    mw: number;
    label?: string;
    duration_min?: number;
  }[];
  budget_cad?: number;
  t_safe_c?: number;
  designer?: {
    agent?: string;
    prompt?: string;
    reasoning?: string;
    assumptions?: string[];
    source?: "llm" | "cache" | "fallback" | string;
    langsmith_run_id?: string | null;
  };
}

export interface TickMsg {
  type: "tick";
  sim_ts: string;
  tick?: number;
  outdoor_c?: number;
  demand_mw?: number;
  supply_mw?: number;
  shortfall_mw?: number;
  halted?: boolean;
  status?: string;
  paused_reason?: string | null;
  speed?: number;
}

export interface SectionState {
  id: string;
  cut?: boolean;
  cut_minutes_today?: number;
  protected?: boolean;
  [k: string]: unknown;
}

export interface MapBuffers {
  temps: Uint8Array;
  flags: Uint8Array;
  simTs: string;
}

/** GET /world/static (CONTRACTS.md, W8). */
export interface StaticWorld {
  sections: FeatureCollection;
  facilities: FeatureCollection;
  neighbourhoods?: FeatureCollection;
  households: {
    ids: string[];
    lon: number[];
    lat: number[];
    vulnerable: (boolean | number)[];
    featured: Record<string, number>;
  };
  residents: Record<string, unknown>[];
}

export interface AuditAnswer {
  answer: string;
  timeline?: { seq: number; sim_ts?: string; actor?: string; summary?: string }[];
  rules_cited?: string[];
  source?: string;
}

export interface VerifyResult {
  ok: boolean;
  checked?: number;
  first_broken_seq?: number | null;
}

export type MetricsSummary = Record<string, unknown>;

export type WsMessage =
  | TickMsg
  | { type: "ledger"; entry: LedgerEntry }
  | { type: "approval"; approval: Approval }
  | { type: "request"; request: ActionRequest & { decision?: Decision; execution?: Execution; approval?: Approval } }
  | { type: "plan"; plan: Plan }
  | { type: "map"; sim_ts: string; temps: string; flags: string }
  | { type: "sections"; states: SectionState[] }
  | { type: "message"; entry: Message }
  | { type: "metrics"; summary: MetricsSummary }
  | { type: "pong" }
  | { type: string; [k: string]: unknown };
