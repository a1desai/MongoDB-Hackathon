"use client";

/* Top bar (PLAN §14): sim clock + outdoor °C, demand vs supply as two numbers with a
 * shortfall badge (no chart), speed, Pause, Inject, HALT AGENTS, identity switcher. */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { api, ApiError } from "@/lib/api";
import { hhmm, IDENTITIES, num } from "@/lib/format";
import { pendingApprovals, useRunStore } from "@/store/useRunStore";
import { Button, Chip, cx, Dot, Icon } from "./ui";

const SPEEDS = [1, 2, 4, 8];

function Metric({ label, value, unit, tone = "text-white", sub }: { label: string; value: string; unit?: string; tone?: string; sub?: React.ReactNode }) {
  return (
    <div className="flex flex-col justify-center leading-none">
      <span className="text-[9.5px] font-medium tracking-[0.14em] text-slate-500 uppercase">{label}</span>
      <span className={cx("mt-1 font-mono text-[17px] font-semibold tabular", tone)}>
        {value}
        {unit && <span className="ml-0.5 text-[11px] font-normal text-slate-500">{unit}</span>}
      </span>
      {sub}
    </div>
  );
}

function IdentitySwitcher() {
  const identity = useRunStore((s) => s.identity);
  const setIdentity = useRunStore((s) => s.setIdentity);
  return (
    <div className="flex items-center rounded-lg border border-white/10 bg-black/30 p-0.5" role="radiogroup" aria-label="Acting as">
      {IDENTITIES.map((idn) => {
        const active = idn.id === identity;
        return (
          <button
            key={idn.id}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setIdentity(idn.id)}
            title={`${idn.label}: ${idn.role}`}
            className={cx(
              "flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11.5px] transition",
              active
                ? idn.id === "human:supervisor"
                  ? "bg-violet-400/20 text-violet-100 shadow-[inset_0_0_0_1px_rgba(167,139,250,0.45)]"
                  : idn.id === "human:auditor"
                    ? "bg-cyan-400/15 text-cyan-100 shadow-[inset_0_0_0_1px_rgba(34,211,238,0.35)]"
                    : "bg-white/10 text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.15)]"
                : "text-slate-400 hover:text-slate-200",
            )}
          >
            <Icon.User size={11} />
            <span className="font-medium">{idn.label}</span>
            {idn.id !== "human:operator" && <span className="hidden opacity-70 2xl:inline">{idn.name}</span>}
          </button>
        );
      })}
    </div>
  );
}

function InjectMenu({ disabled, runId }: { disabled: boolean; runId: string }) {
  const [open, setOpen] = useState(false);
  const identity = useRunStore((s) => s.identity);
  const toast = useRunStore((s) => s.toast);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  async function inject(event: "generator_trip" | "injection_message") {
    setOpen(false);
    try {
      await api.inject(runId, event, identity);
      toast("info", event === "generator_trip" ? "Injected the scenario's primary supply-loss event" : "Injected: untrusted message from a resident");
    } catch (e) {
      toast("error", `Inject failed: ${e instanceof ApiError ? e.message : String(e)}`);
    }
  }

  return (
    <div className="relative" ref={ref}>
      <Button variant="ghost" disabled={disabled} onClick={() => setOpen(!open)} title="Operator: inject a scenario event">
        <Icon.Bolt size={13} className="text-amber-300" />
        Inject
        <Icon.ChevronDown size={11} className="opacity-60" />
      </Button>
      {open && (
        <div className="glass-strong animate-pop-in absolute top-10 right-0 z-50 w-64 overflow-hidden rounded-xl p-1">
          <button type="button" onClick={() => inject("generator_trip")} className="flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left hover:bg-white/[0.06]">
            <Icon.Bolt size={14} className="mt-0.5 text-rose-300" />
            <span>
              <span className="block text-[12.5px] font-medium text-slate-100">Primary supply loss</span>
              <span className="block text-[11px] text-slate-400">Inject the first disruption inferred for this scenario</span>
            </span>
          </button>
          <button type="button" onClick={() => inject("injection_message")} className="flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left hover:bg-white/[0.06]">
            <Icon.Message size={14} className="mt-0.5 text-orange-300" />
            <span>
              <span className="block text-[12.5px] font-medium text-slate-100">Prompt-injection reply</span>
              <span className="block text-[11px] text-slate-400">A resident SMS posing as a supervisor</span>
            </span>
          </button>
        </div>
      )}
    </div>
  );
}

export default function ControlBar() {
  const { runId, run, clock, halted, identity, connection, approvals, metrics } = useRunStore(
    useShallow((s) => ({
      runId: s.runId,
      run: s.run,
      clock: s.clock,
      halted: s.halted,
      identity: s.identity,
      connection: s.connection,
      approvals: s.approvals,
      metrics: s.metrics,
    })),
  );
  const toast = useRunStore((s) => s.toast);
  const setHalted = useRunStore((s) => s.setHalted);
  const setApprovalDrawerOpen = useRunStore((s) => s.setApprovalDrawerOpen);
  const setComparisonOpen = useRunStore((s) => s.setComparisonOpen);
  const [speed, setSpeed] = useState<number | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const pending = pendingApprovals(approvals).length;
  const status = clock?.status ?? run?.status ?? "created";
  const paused = status === "paused";
  const finished = status === "finished" || status === "failed";
  const shortfall = clock?.shortfall_mw ?? 0;
  // shortfall_mw is clipped at 0, so the margin comes from supply - demand; null until the first tick.
  const margin =
    typeof clock?.supply_mw === "number" && typeof clock?.demand_mw === "number"
      ? Math.max(0, clock.supply_mw - clock.demand_mw)
      : null;
  const isSupervisor = identity === "human:supervisor";
  const canOperate = identity !== "human:auditor";
  const curSpeed = speed ?? clock?.speed ?? run?.speed ?? 1;
  const governed = run?.mode !== "baseline";

  async function act(key: string, fn: () => Promise<unknown>, ok?: string) {
    setBusy(key);
    try {
      await fn();
      if (ok) toast("ok", ok);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (!runId) return null;

  return (
    <header className={cx("glass relative z-20 flex h-16 shrink-0 items-center gap-4 rounded-2xl px-4", halted && "halt-sweep border-rose-500/40")}>
      {/* brand + run */}
      <Link href="/" className="flex items-center gap-2.5 pr-1" title="Back to start">
        <div className="relative flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-cyan-300 to-cyan-600 text-slate-950 shadow-[0_0_24px_-4px_rgba(34,211,238,0.7)]">
          <Icon.Thermo size={16} strokeWidth={2.2} />
        </div>
        <div className="hidden leading-tight xl:block">
          <div className="text-[13px] font-semibold tracking-tight text-white">Cooldown</div>
          <div className="font-mono text-[10px] text-slate-500">{runId.length > 18 ? `${runId.slice(0, 18)}…` : runId}</div>
        </div>
      </Link>

      <div className="h-8 w-px bg-white/10" />

      {/* clock */}
      <div className="flex items-center gap-3">
        <div className="flex flex-col leading-none">
          <span className="text-[9.5px] font-medium tracking-[0.14em] text-slate-500 uppercase">Sim time</span>
          <span className="mt-1 font-mono text-[22px] font-semibold text-white tabular">{hhmm(clock?.sim_ts ?? run?.sim_ts)}</span>
        </div>
        <Metric label="Outdoor" value={num(clock?.outdoor_c, 1)} unit="°C" tone="text-amber-200" />
      </div>

      <div className="h-8 w-px bg-white/10" />

      {/* grid numbers */}
      <div className="flex items-center gap-4">
        <Metric label="Demand" value={num(clock?.demand_mw)} unit="MW" />
        <Metric label="Supply" value={num(clock?.supply_mw)} unit="MW" tone="text-slate-300" />
        <div
          className={cx(
            "flex flex-col items-start rounded-lg border px-2.5 py-1 leading-none transition-colors",
            shortfall > 0 ? "border-rose-500/50 bg-rose-500/15 shadow-[0_0_24px_-6px_rgba(244,63,94,0.6)]" : "border-emerald-400/30 bg-emerald-400/10",
          )}
        >
          <span className={cx("text-[9.5px] font-medium tracking-[0.14em] uppercase", shortfall > 0 ? "text-rose-300" : "text-emerald-300")}>
            {shortfall > 0 ? "Shortfall" : "Margin"}
          </span>
          <span className={cx("mt-1 font-mono text-[15px] font-semibold tabular", shortfall > 0 ? "text-rose-100" : "text-emerald-100")}>
            {shortfall > 0 ? num(shortfall) : num(margin)}
            <span className="ml-0.5 text-[10px] font-normal opacity-70">MW</span>
          </span>
        </div>
      </div>

      {/* status chips */}
      <div className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden">
        {connection !== "open" ? (
          <Chip tone="amber">
            <Icon.Wifi size={11} /> {connection === "reconnecting" ? "reconnecting…" : connection === "connecting" ? "connecting…" : "offline"}
          </Chip>
        ) : finished ? (
          <Chip tone="neutral">finished</Chip>
        ) : paused ? (
          <Chip tone="amber">
            <Icon.Pause size={10} /> {clock?.paused_reason ? `held: ${clock.paused_reason.replace(/_/g, " ")}` : "paused"}
          </Chip>
        ) : (
          <Chip tone="green">
            <Dot tone="green" live /> live
          </Chip>
        )}
        {run?.mode && (
          <Chip tone={governed ? "cyan" : "neutral"} className="hidden 2xl:inline-flex">
            {governed ? <Icon.ShieldCheck size={11} /> : null}
            {governed ? "governed" : "baseline"}
          </Chip>
        )}
        {pending > 0 && (
          <button
            type="button"
            onClick={() => setApprovalDrawerOpen(true)}
            className="approval-glow flex shrink-0 items-center gap-1.5 rounded-md bg-violet-400/15 px-2 py-1 text-[11px] font-medium whitespace-nowrap text-violet-100"
            title="Open the approval drawer"
          >
            <Dot tone="violet" live /> {pending} pending
          </button>
        )}
        {(metrics || finished) && (
          <Button size="sm" variant="ghost" className="shrink-0" onClick={() => setComparisonOpen(true)}>
            Results
          </Button>
        )}
      </div>

      {/* run controls */}
      <div className="flex items-center gap-2">
        <div className="flex items-center rounded-lg border border-white/10 bg-black/30 p-0.5">
          {SPEEDS.map((x) => (
            <button
              key={x}
              type="button"
              disabled={!canOperate || finished}
              onClick={() => {
                setSpeed(x);
                void act("speed", () => api.setSpeed(runId, x));
              }}
              className={cx(
                "rounded-md px-2 py-1 font-mono text-[11px] transition disabled:opacity-40",
                curSpeed === x ? "bg-white/12 text-white" : "text-slate-400 hover:text-slate-200",
              )}
            >
              {x}×
            </button>
          ))}
        </div>
        <Button
          variant="ghost"
          disabled={!canOperate || finished || busy === "pause"}
          onClick={() => act("pause", () => (paused ? api.resume(runId, identity) : api.pause(runId, identity)))}
          title={paused ? "Resume the sim clock" : "Pause the sim clock"}
          className="w-[84px]"
        >
          {paused ? <Icon.Play size={12} /> : <Icon.Pause size={12} />}
          {paused ? "Resume" : "Pause"}
        </Button>
        <InjectMenu disabled={!canOperate || finished} runId={runId} />
        {governed &&
          (halted ? (
            <Button
              variant="approve"
              disabled={!isSupervisor || busy === "halt"}
              title={isSupervisor ? "Resume the agents" : "Switch to Supervisor A. Rahman"}
              onClick={() =>
                act(
                  "halt",
                  async () => {
                    await api.resumeAgents(runId, identity);
                    setHalted(false);
                  },
                  "Agents resumed",
                )
              }
            >
              <Icon.Play size={12} />
              RESUME AGENTS
            </Button>
          ) : (
            <Button
              variant="danger"
              disabled={!isSupervisor || busy === "halt"}
              title={isSupervisor ? "Kill switch: the Executor refuses every action (R15). Physics keeps running." : "Switch to Supervisor A. Rahman to halt"}
              onClick={() =>
                act(
                  "halt",
                  async () => {
                    await api.halt(runId, identity, "Supervisor pressed HALT AGENTS in the console");
                    setHalted(true);
                  },
                  "Agents halted. The Executor will refuse every action (R15).",
                )
              }
              className="tracking-wide"
            >
              <Icon.Octagon size={14} />
              HALT AGENTS
            </Button>
          ))}
      </div>

      <div className="h-8 w-px bg-white/10" />
      <IdentitySwitcher />
    </header>
  );
}
