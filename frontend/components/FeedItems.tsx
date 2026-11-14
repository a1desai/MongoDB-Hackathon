"use client";

/* Non-request items in the console stream: Planner cards and compact ledger event rows. */

import { memo, useState } from "react";
import { traceUrl } from "@/lib/api";
import { actionLabel, actorName, asString, hhmm, isRecord, num } from "@/lib/format";
import type { LedgerEntry, Plan, SimilarEvent } from "@/lib/types";
import { useRunStore } from "@/store/useRunStore";
import { Chip, cx, Dot, Icon, Label } from "./ui";

function PlanCardImpl({ plan, highlight }: { plan: Plan; highlight?: boolean }) {
  const [open, setOpen] = useState(true);
  const trace = traceUrl(plan);
  const steps = plan.steps ?? [];
  const total = steps.reduce((a, s) => a + (s.expected_mw ?? 0), 0);
  const similar = (plan.similar_events ?? []).map((e) => (typeof e === "string" ? ({ id: e } as SimilarEvent) : e));
  return (
    <article
      className={cx(
        "glass animate-card-in relative overflow-hidden rounded-xl p-3.5",
        "bg-[radial-gradient(120%_100%_at_0%_0%,rgba(34,211,238,0.10),transparent_60%)]",
        highlight && "seq-flash",
      )}
    >
      <header className="flex items-start gap-2.5">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-cyan-400/30 bg-cyan-400/10 text-cyan-200">
          <Icon.Plan size={14} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-400">
            <span className="font-medium text-slate-200">{actorName(plan.created_by ?? "agent:planner")}</span>
            {plan.acting_for && (
              <span>
                for <span className="text-slate-300">{actorName(plan.acting_for)}</span>
              </span>
            )}
            <span className="text-slate-600">·</span>
            <span className="font-semibold text-white">Plan v{plan.version ?? "?"}</span>
            {plan.source && (
              <Chip tone={plan.source === "fallback" ? "neutral" : "cyan"} mono title="llm = model output · fallback = deterministic planner">
                {plan.source}
              </Chip>
            )}
            {plan.trigger && <Chip mono>{plan.trigger}</Chip>}
          </div>
          <div className="mt-1 font-mono text-[11px] text-slate-400">
            {num(total)} MW planned for a <span className="text-amber-200">{num(plan.expected_shortfall_mw ?? 0)} MW</span> shortfall
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="rounded p-1 text-slate-500 transition hover:bg-white/[0.06] hover:text-slate-200"
            aria-label={open ? "Collapse plan" : "Expand plan"}
          >
            {open ? <Icon.ChevronDown size={13} /> : <Icon.ChevronRight size={13} />}
          </button>
          <span className="font-mono text-[10px] text-slate-500">{hhmm(plan.sim_ts)}</span>
        </div>
      </header>

      {open && (
        <div className="animate-fade-in">
          {plan.rationale && (
            <p className="mt-2.5 border-l-2 border-cyan-400/40 pl-2.5 text-[12.5px] leading-relaxed text-slate-200">{plan.rationale}</p>
          )}
          {steps.length > 0 && (
            <div className="mt-3 space-y-1">
              {steps.map((s) => (
                <div key={s.step_id} className="flex items-center gap-2 rounded-md bg-white/[0.025] px-2 py-1 text-xs">
                  <Chip tone={(s.rung ?? 0) >= 7 ? "red" : "cyan"} mono>
                    R{s.rung ?? "?"}
                  </Chip>
                  <span className="text-slate-200">{actionLabel(s.action_type)}</span>
                  {s.window && (
                    <span className="font-mono text-[10.5px] text-slate-500">
                      {s.window.start}–{s.window.end}
                    </span>
                  )}
                  <span className="ml-auto font-mono text-[11px] text-cyan-200">{num(s.expected_mw ?? 0)} MW</span>
                </div>
              ))}
            </div>
          )}
          {similar.length > 0 && (
            <div className="mt-3">
              <Label className="mb-1.5">Similar past events · Atlas Vector Search</Label>
              <div className="space-y-1">
                {similar.slice(0, 3).map((e) => (
                  <div key={e.id} className="rounded-md border border-white/[0.06] bg-black/20 px-2 py-1.5 text-[11.5px] text-slate-300">
                    <span className="mr-1.5 font-mono text-[10px] text-cyan-300/80">{e.id}</span>
                    {typeof e.score === "number" && <span className="mr-1.5 font-mono text-[10px] text-slate-500">{e.score.toFixed(2)}</span>}
                    {e.summary ?? e.lesson ?? ""}
                  </div>
                ))}
              </div>
            </div>
          )}
          {(plan.evidence_ids?.length ?? 0) > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {plan.evidence_ids!.slice(0, 10).map((id) => (
                <Chip key={id} mono tone={id.startsWith("msg_") ? "orange" : "neutral"}>
                  {id.length > 18 ? `${id.slice(0, 16)}…` : id}
                </Chip>
              ))}
            </div>
          )}
        </div>
      )}
      <footer className="mt-2 flex items-center justify-end gap-2">
        {trace ? (
          <a
            href={trace}
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-slate-400 transition hover:bg-white/[0.06] hover:text-cyan-200"
          >
            Trace <Icon.External size={11} />
          </a>
        ) : (
          <span className="text-[11px] text-slate-600">{plan.source === "fallback" ? "deterministic fallback" : "no trace"}</span>
        )}
      </footer>
    </article>
  );
}

export const PlanCard = memo(PlanCardImpl);

const EVENT_STYLE: Record<string, { tone: "neutral" | "cyan" | "orange" | "red" | "green" | "violet"; label: string }> = {
  contingency: { tone: "red", label: "Contingency" },
  message_in: { tone: "orange", label: "Untrusted" },
  message_out: { tone: "cyan", label: "Message" },
  halt: { tone: "red", label: "HALT" },
  resume: { tone: "green", label: "Resume" },
  replan_triggered: { tone: "cyan", label: "Replan" },
  world_event: { tone: "neutral", label: "World" },
  audit_query: { tone: "violet", label: "Audit" },
};

function EventRowImpl({ entry, highlight }: { entry: LedgerEntry; highlight?: boolean }) {
  const setSelectedResident = useRunStore((s) => s.setSelectedResident);
  const style = EVENT_STYLE[entry.type] ?? { tone: "neutral" as const, label: entry.type.replace(/_/g, " ") };
  const p = isRecord(entry.payload) ? entry.payload : {};
  const residentId = asString(p.resident_id);
  const untrusted = entry.type === "message_in" || p.trust === "untrusted";
  const big = entry.type === "contingency" || entry.type === "halt" || untrusted;

  // Background world chatter (archetype reactions, folded events) stays one quiet line.
  if (entry.type === "world_event" && !highlight) {
    return (
      <div className="animate-card-in flex items-center gap-2 px-2 py-0.5 text-[11px] text-slate-500" title={entry.summary}>
        <Dot tone="neutral" className="opacity-60" />
        <span className="shrink-0 text-slate-400">{actorName(entry.actor)}</span>
        <span className="min-w-0 flex-1 truncate">{entry.summary}</span>
        <span className="shrink-0 font-mono text-[10px] text-slate-600">
          {hhmm(entry.sim_ts)} #{entry.seq}
        </span>
      </div>
    );
  }

  return (
    <div
      className={cx(
        "animate-card-in flex items-start gap-2.5 rounded-lg border px-3 py-2",
        untrusted
          ? "border-orange-400/30 bg-orange-400/[0.07]"
          : entry.type === "contingency" || entry.type === "halt"
            ? "border-rose-500/30 bg-rose-500/[0.08]"
            : entry.type === "resume"
              ? "border-emerald-400/25 bg-emerald-400/[0.05]"
              : "border-white/[0.06] bg-white/[0.02]",
        highlight && "seq-flash",
      )}
    >
      <span className="mt-0.5 shrink-0">
        {entry.type === "contingency" ? (
          <Icon.Bolt size={14} className="text-rose-300" />
        ) : entry.type === "halt" ? (
          <Icon.Octagon size={14} className="text-rose-300" />
        ) : untrusted ? (
          <Icon.Message size={14} className="text-orange-300" />
        ) : entry.type === "replan_triggered" ? (
          <Icon.Sparkle size={14} className="text-cyan-300" />
        ) : entry.type === "audit_query" ? (
          <Icon.Search size={14} className="text-violet-300" />
        ) : (
          <Dot tone={style.tone} className="mt-1" />
        )}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-[10.5px] text-slate-500">
          <Chip tone={style.tone} className="py-[2px] text-[10px] font-medium tracking-wide uppercase">
            {style.label}
          </Chip>
          <span>{actorName(entry.actor)}</span>
          {residentId && (
            <button
              type="button"
              onClick={() => setSelectedResident(residentId)}
              className="text-orange-200/80 underline-offset-2 hover:underline"
            >
              open thread
            </button>
          )}
        </div>
        <p className={cx("mt-1 leading-snug", big ? "text-[12.5px] text-slate-100" : "text-xs text-slate-300")}>
          {untrusted ? (
            <>
              <span className="font-mono text-[10px] text-orange-300/80">&lt;untrusted&gt; </span>
              {entry.summary}
              <span className="font-mono text-[10px] text-orange-300/80"> &lt;/untrusted&gt;</span>
            </>
          ) : (
            entry.summary
          )}
        </p>
      </div>
      <div className="flex shrink-0 flex-col items-end gap-0.5">
        <span className="font-mono text-[10px] text-slate-500">{hhmm(entry.sim_ts)}</span>
        <span className="font-mono text-[10px] text-slate-600">#{entry.seq}</span>
      </div>
    </div>
  );
}

export const EventRow = memo(EventRowImpl);
