/* Small display helpers shared by the console components. */

import type { HumanId } from "./types";

export const IDENTITIES: { id: HumanId; label: string; name: string; role: string }[] = [
  { id: "human:operator", label: "Operator", name: "Lakeshore Grid Ops", role: "Starts runs, injects events" },
  { id: "human:supervisor", label: "Supervisor", name: "A. Rahman", role: "Approves, halts, rolls back" },
  { id: "human:auditor", label: "Auditor", name: "J. Okafor", role: "Reads the ledger, asks the Auditor" },
];

const NAMES: Record<string, string> = {
  "human:operator": "Operator",
  "human:supervisor": "A. Rahman",
  "human:auditor": "J. Okafor",
  "agent:forecaster": "Forecaster",
  "agent:planner": "Planner",
  "agent:targeter": "Targeter",
  "agent:negotiator": "Negotiator",
  "agent:executor": "Executor",
  "agent:watcher": "Watcher",
  "agent:restorer": "Restorer",
  "agent:auditor": "Auditor",
  "system:gate": "Policy Gate",
  "sim:world": "World",
};

/** "agent:planner" -> "Planner", "sim:resident:hh_00042" -> "Resident hh_00042". */
export function actorName(id: string | null | undefined): string {
  if (!id) return "unknown";
  if (NAMES[id]) return NAMES[id];
  const parts = id.split(":");
  if (parts[0] === "sim" && parts.length >= 3) return `${cap(parts[1])} ${parts.slice(2).join(":")}`;
  return cap(parts[parts.length - 1].replace(/_/g, " "));
}

export function actorKind(id: string | null | undefined): "agent" | "human" | "system" | "sim" | "other" {
  const k = (id || "").split(":")[0];
  return k === "agent" || k === "human" || k === "system" || k === "sim" ? k : "other";
}

export function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export const ACTION_LABEL: Record<string, string> = {
  precool: "Pre-cool",
  thermostat_setback: "Thermostat setback",
  thermostat_restore: "Thermostat restore",
  ev_pause: "EV charging pause",
  battery_discharge: "Battery discharge",
  business_curtail_request: "Business curtailment",
  send_sms: "SMS offer",
  feeder_outage: "Feeder outage",
  feeder_restore: "Feeder restore",
  undo: "Rollback",
};

export const RUNG_OF: Record<string, number> = {
  precool: 1,
  battery_discharge: 2,
  ev_pause: 3,
  business_curtail_request: 4,
  thermostat_setback: 5,
  send_sms: 6,
  feeder_outage: 7,
};

export function actionLabel(a: string | null | undefined): string {
  if (!a) return "Action";
  return ACTION_LABEL[a] ?? cap(a.replace(/_/g, " "));
}

/** "2031-06-24T17:40:00-04:00" -> "17:40". Falls back to the raw string. */
export function hhmm(ts: string | null | undefined): string {
  if (!ts) return "--:--";
  const m = /T(\d{2}):(\d{2})/.exec(ts) ?? /^(\d{2}):(\d{2})/.exec(ts);
  return m ? `${m[1]}:${m[2]}` : ts;
}

/** Minutes since midnight in the sim's local clock, read straight from the ISO string. */
export function simMinutes(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const m = /T(\d{2}):(\d{2})/.exec(ts);
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

export function num(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "—";
  return n.toLocaleString("en-CA", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

export function mw(n: number | null | undefined, digits = 0): string {
  return `${num(n, digits)} MW`;
}

export function shortHash(h: string | null | undefined, n = 10): string {
  return h ? h.slice(0, n) : "—";
}

export function pct(part: number, whole: number): string {
  if (!whole) return "0%";
  return `${Math.round((100 * part) / whole)}%`;
}

export function asNumber(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

export function asString(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

export function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === "object" && !Array.isArray(v);
}
