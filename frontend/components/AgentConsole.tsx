"use client";

/* The primary element (PLAN §14): a live feed of ActionCards grouped by request_id, Planner
 * cards and ledger events, with tabs All / Needs approval / Denied / Messages / Auditor. */

import { useEffect, useMemo, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { actorName, hhmm, num } from "@/lib/format";
import type { Message } from "@/lib/types";
import { type Card, type ConsoleTab, type FeedItem, useRunStore } from "@/store/useRunStore";
import ActionCard from "./ActionCard";
import AuditorPanel from "./AuditorPanel";
import { EventRow, PlanCard } from "./FeedItems";
import { Chip, cx, Dot, Icon } from "./ui";

const PAGE = 120;

function ForecastStrip() {
  const forecast = useRunStore((s) => s.forecast);
  const mode = useRunStore((s) => s.run?.mode);
  if (!forecast) return null;
  const w = forecast.windows ?? [];
  return (
    <div className="flex items-center gap-2 border-b border-white/[0.06] px-4 py-2 text-[11.5px]">
      <Icon.Gauge size={13} className="shrink-0 text-cyan-300" />
      <span className="text-slate-400">Forecast {hhmm(forecast.simTs)}</span>
      {w.length === 0 ? (
        <span className="text-emerald-300/90">no shortfall in the next 6 h</span>
      ) : (
        <div className="flex min-w-0 flex-wrap gap-1">
          {w.slice(0, 3).map((x) => (
            <Chip key={`${x.start}-${x.end}`} tone="amber" mono>
              {x.start}–{x.end} · {num(x.peak_short_mw)} MW short
            </Chip>
          ))}
        </div>
      )}
      {mode === "baseline" && <Chip className="ml-auto">baseline · no agents</Chip>}
    </div>
  );
}

function itemContainsSeq(item: FeedItem, seq: number, cards: Record<string, Card>, planSeq: (id: string) => number | undefined) {
  if (item.kind === "event") return item.seq === seq;
  if (item.kind === "request") return cards[item.id]?.seqs.includes(seq) ?? false;
  return planSeq(item.id) === seq || item.order === seq;
}

function EmptyFeed({ tab }: { tab: ConsoleTab }) {
  const conn = useRunStore((s) => s.connection);
  const mode = useRunStore((s) => s.run?.mode);
  const text =
    tab === "approval"
      ? "Nothing is waiting for a human. Feeder outages and high-impact actions land here."
      : tab === "denied"
        ? "No denials yet. When the Policy Gate blocks an action, the failing rules show here."
        : mode === "baseline"
          ? "Baseline run: no agents. Status-quo operator actions show here as world events."
          : "Waiting for the first forecast. The Planner proposes as soon as a shortfall appears.";
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-8 py-16 text-center">
      <div className="relative flex h-12 w-12 items-center justify-center rounded-2xl border border-cyan-400/20 bg-cyan-400/5 text-cyan-300">
        <Icon.Shield size={20} />
        {conn === "open" && <span className="absolute -top-0.5 -right-0.5"><Dot tone="cyan" live /></span>}
      </div>
      <p className="max-w-xs text-[13px] leading-relaxed text-slate-400">{text}</p>
    </div>
  );
}

function threadName(threadId: string, names: Record<string, string>): string {
  if (names[threadId]) return names[threadId];
  if (threadId.startsWith("biz_")) {
    return threadId
      .slice(4)
      .split("_")
      .map((w) => w[0]?.toUpperCase() + w.slice(1))
      .join(" ");
  }
  return threadId;
}

function MessagesTab() {
  const threads = useRunStore((s) => s.threads);
  const residents = useRunStore((s) => s.staticWorld?.residents);
  const setSelectedResident = useRunStore((s) => s.setSelectedResident);
  const [open, setOpen] = useState<string | null>(null);

  const { names, residentByHh } = useMemo(() => {
    const names: Record<string, string> = {};
    const residentByHh: Record<string, string> = {};
    for (const r of residents ?? []) {
      const hh = r.household_id as string | undefined;
      const name = r.name as string | undefined;
      if (hh && name) names[hh] = name;
      if (hh && typeof r._id === "string") residentByHh[hh] = r._id;
    }
    return { names, residentByHh };
  }, [residents]);

  const list = useMemo(
    () =>
      Object.entries(threads)
        .map(([id, msgs]) => ({ id, msgs, last: msgs[msgs.length - 1] }))
        .sort((a, b) => (b.last?.sim_ts ?? "").localeCompare(a.last?.sim_ts ?? "")),
    [threads],
  );

  if (!list.length) {
    return (
      <div className="flex flex-col items-center gap-3 px-8 py-16 text-center text-[13px] text-slate-400">
        <Icon.Message size={20} className="text-slate-500" />
        No messages yet. Negotiator texts and resident or business replies appear here. Replies are stored as untrusted input.
      </div>
    );
  }

  return (
    <div className="space-y-2 p-3">
      {list.map(({ id, msgs, last }) => {
        const inbound = msgs.some((m) => m.direction === "in");
        const isOpen = open === id;
        return (
          <div key={id} className="glass animate-card-in overflow-hidden rounded-xl">
            <button
              type="button"
              onClick={() => setOpen(isOpen ? null : id)}
              className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left transition hover:bg-white/[0.03]"
            >
              <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-white/10 bg-white/[0.04] text-[11px] font-medium text-slate-300">
                {threadName(id, names).slice(0, 1)}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 text-[12.5px]">
                  <span className="font-medium text-slate-100">{threadName(id, names)}</span>
                  <span className="font-mono text-[10px] text-slate-500">{id}</span>
                  {inbound && (
                    <Chip tone="orange" className="py-[2px] text-[10px]">
                      untrusted replies
                    </Chip>
                  )}
                </div>
                <p className="truncate text-xs text-slate-400">{last?.body}</p>
              </div>
              <span className="shrink-0 font-mono text-[10px] text-slate-500">
                {hhmm(last?.sim_ts)} · {msgs.length}
              </span>
            </button>
            {isOpen && (
              <div className="animate-fade-in space-y-2 border-t border-white/[0.06] px-3 py-3">
                {msgs.map((m: Message, i) => (
                  <div key={m._id ?? i} className={cx("flex", m.direction === "in" ? "justify-start" : "justify-end")}>
                    <div
                      className={cx(
                        "max-w-[85%] rounded-xl px-3 py-2 text-[12.5px] leading-snug",
                        m.direction === "in"
                          ? "rounded-bl-sm border border-orange-400/30 bg-orange-400/[0.08] text-orange-50"
                          : "rounded-br-sm border border-cyan-400/20 bg-cyan-400/[0.08] text-slate-100",
                      )}
                    >
                      {m.direction === "in" && <div className="mb-0.5 font-mono text-[9.5px] tracking-wider text-orange-300/80 uppercase">untrusted</div>}
                      {m.body}
                      <div className="mt-1 font-mono text-[9.5px] text-slate-500">
                        {hhmm(m.sim_ts)} · {actorName(m.from)}
                        {m.decision && m.decision !== "none" ? ` · ${m.decision}` : ""}
                      </div>
                    </div>
                  </div>
                ))}
                {residentByHh[id] && (
                  <button
                    type="button"
                    onClick={() => setSelectedResident(residentByHh[id])}
                    className="text-[11px] text-cyan-300/80 hover:text-cyan-200"
                  >
                    Show resident on the map →
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function AgentConsole() {
  const { tab, feed, cards, plans, ledger, highlightSeq, approvals } = useRunStore(
    useShallow((s) => ({
      tab: s.tab,
      feed: s.feed,
      cards: s.cardsByRequest,
      plans: s.plans,
      ledger: s.ledgerBySeq,
      highlightSeq: s.highlightSeq,
      approvals: s.approvals,
    })),
  );
  const setTab = useRunStore((s) => s.setTab);
  const focusSeq = useRunStore((s) => s.focusSeq);
  const [limit, setLimit] = useState(PAGE);
  const scrollRef = useRef<HTMLDivElement>(null);

  const planSeq = useMemo(() => {
    const m: Record<string, number> = {};
    for (const e of Object.values(ledger)) {
      if (e.type === "plan_created") {
        const id = (e.payload?.plan_id as string | undefined) ?? undefined;
        if (id) m[id] = e.seq;
      }
    }
    return (id: string) => m[id];
  }, [ledger]);

  const sorted = useMemo(() => [...feed].sort((a, b) => b.order - a.order), [feed]);

  const items = useMemo(() => {
    if (tab === "approval") {
      return sorted.filter((f) => {
        if (f.kind !== "request") return false;
        const c = cards[f.id];
        return c && (c.request.status === "needs_approval" || c.approval?.status === "pending" || c.decision?.verdict === "needs_approval");
      });
    }
    if (tab === "denied") {
      return sorted.filter((f) => {
        if (f.kind !== "request") return false;
        const c = cards[f.id];
        return c && (c.request.status === "denied" || c.decision?.verdict === "deny" || (c.decision?.denied_count ?? 0) > 0);
      });
    }
    return sorted;
  }, [sorted, tab, cards]);

  const counts = useMemo(() => {
    let denied = 0;
    for (const c of Object.values(cards)) if (c.request.status === "denied" || c.decision?.verdict === "deny" || (c.decision?.denied_count ?? 0) > 0) denied++;
    const pending = Object.values(approvals).filter((a) => a.status === "pending").length;
    return { denied, pending };
  }, [cards, approvals]);

  const msgCount = useRunStore((s) => Object.keys(s.threads).length);

  // Scroll to and flash the item holding the focused ledger seq (Auditor timeline, seq chips).
  const highlightKey = useMemo(() => {
    if (highlightSeq === null) return null;
    return items.find((f) => itemContainsSeq(f, highlightSeq, cards, planSeq))?.key ?? null;
  }, [highlightSeq, items, cards, planSeq]);

  // Grow the rendered window (derived, not state) so a focused older item is mounted.
  const highlightIdx = highlightKey ? items.findIndex((f) => f.key === highlightKey) : -1;
  const shown = highlightIdx >= limit ? highlightIdx + 20 : limit;

  useEffect(() => {
    if (highlightSeq === null) return;
    if (highlightKey) {
      requestAnimationFrame(() => {
        const el = scrollRef.current?.querySelector(`[data-feed-key="${CSS.escape(highlightKey)}"]`);
        el?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }
    const t = setTimeout(() => focusSeq(null), 2400);
    return () => clearTimeout(t);
  }, [highlightSeq, highlightKey, focusSeq]);

  const tabs: { id: ConsoleTab; label: string; badge?: number; tone?: "violet" | "red" | "neutral" }[] = [
    { id: "all", label: "All" },
    { id: "approval", label: "Needs approval", badge: counts.pending, tone: "violet" },
    { id: "denied", label: "Denied", badge: counts.denied, tone: "red" },
    { id: "messages", label: "Messages", badge: msgCount, tone: "neutral" },
    { id: "auditor", label: "Auditor" },
  ];

  return (
    <section className="glass flex h-full min-h-0 flex-col overflow-hidden rounded-2xl">
      <div className="flex items-center gap-1 border-b border-white/[0.06] px-2 pt-2">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={cx(
              "relative flex items-center gap-1.5 rounded-t-lg px-3 pt-1.5 pb-2.5 text-[12.5px] font-medium transition",
              tab === t.id ? "text-white" : "text-slate-400 hover:text-slate-200",
            )}
          >
            {t.id === "auditor" && <Icon.Search size={12} />}
            {t.label}
            {t.badge ? (
              <span
                className={cx(
                  "rounded-full px-1.5 py-px font-mono text-[10px]",
                  t.tone === "violet" ? "bg-violet-400/20 text-violet-200" : t.tone === "red" ? "bg-rose-500/20 text-rose-200" : "bg-white/10 text-slate-300",
                )}
              >
                {t.badge}
              </span>
            ) : null}
            {tab === t.id && <span className="absolute inset-x-2 -bottom-px h-[2px] rounded-full bg-cyan-300 shadow-[0_0_12px_rgba(34,211,238,0.8)]" />}
          </button>
        ))}
      </div>

      {tab !== "auditor" && tab !== "messages" && <ForecastStrip />}

      <div ref={scrollRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {tab === "auditor" ? (
          <AuditorPanel />
        ) : tab === "messages" ? (
          <MessagesTab />
        ) : items.length === 0 ? (
          <EmptyFeed tab={tab} />
        ) : (
          <div className="space-y-2.5 p-3">
            {items.slice(0, shown).map((f) => {
              const hl = f.key === highlightKey;
              if (f.kind === "request") {
                const c = cards[f.id];
                return c ? (
                  <div key={f.key} data-feed-key={f.key}>
                    <ActionCard card={c} highlight={hl} />
                  </div>
                ) : null;
              }
              if (f.kind === "plan") {
                const p = plans[f.id];
                return p ? (
                  <div key={f.key} data-feed-key={f.key}>
                    <PlanCard plan={p} highlight={hl} />
                  </div>
                ) : null;
              }
              const e = ledger[f.seq];
              return e ? (
                <div key={f.key} data-feed-key={f.key}>
                  <EventRow entry={e} highlight={hl} />
                </div>
              ) : null;
            })}
            {items.length > shown && (
              <button
                type="button"
                onClick={() => setLimit(shown + PAGE)}
                className="w-full rounded-lg border border-white/[0.06] py-2 text-xs text-slate-400 transition hover:bg-white/[0.03] hover:text-slate-200"
              >
                Show {Math.min(PAGE, items.length - shown)} older items
              </button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
