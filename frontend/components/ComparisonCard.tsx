"use client";

/* End-of-run modal (PLAN §14, P1): one small table, governed vs baseline. Numbers only. */

import { useEffect, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { api, ApiError } from "@/lib/api";
import { isRecord, num } from "@/lib/format";
import { useRunStore } from "@/store/useRunStore";
import { Button, cx, Icon } from "./ui";

type Summary = Record<string, unknown>;

/** Pull a number out of a metrics summary, trying several key spellings (sum dicts). */
function pick(s: Summary | null | undefined, keys: string[]): number | null {
  if (!s) return null;
  for (const k of keys) {
    const v = s[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (isRecord(v)) {
      if (typeof v.all === "number") return v.all;
      if (typeof v.total === "number") return v.total;
      const nums = Object.values(v).filter((x): x is number => typeof x === "number");
      if (nums.length) return nums.reduce((a, b) => a + b, 0);
    }
  }
  return null;
}

function quintileGap(s: Summary | null | undefined): number | null {
  const direct = pick(s, ["quintile_outage_gap_min", "quintile_gap_min", "income_quintile_gap_min", "outage_quintile_gap", "equity_gap_min"]);
  if (direct !== null) return direct;
  const q = s?.outage_minutes_by_quintile ?? s?.outage_min_by_quintile;
  if (isRecord(q) || Array.isArray(q)) {
    const vals = Object.values(q).filter((x): x is number => typeof x === "number");
    if (vals.length) return Math.max(...vals) - Math.min(...vals);
  }
  return null;
}

const ROWS: { label: string; get: (s: Summary | null) => number | null; unit?: string; digits?: number; higherIsBetter?: boolean }[] = [
  { label: "Uncontrolled trips", get: (s) => pick(s, ["uncontrolled_trips"]) },
  { label: "Outage home-minutes", get: (s) => pick(s, ["outage_home_minutes", "outage_home_min"]) },
  { label: "Home-minutes over T_safe", get: (s) => pick(s, ["home_minutes_over_t_safe", "home_min_over_t_safe", "over_t_safe_home_minutes"]) },
  {
    label: "· vulnerable homes",
    get: (s) => pick(s, ["home_minutes_over_t_safe_vulnerable", "vulnerable_home_minutes_over_t_safe", "vulnerable_over_t_safe"]),
  },
  { label: "Income-quintile outage gap", get: quintileGap, unit: "min" },
  { label: "Peak shortfall", get: (s) => pick(s, ["peak_shortfall_mw"]), unit: "MW", digits: 1 },
  { label: "Voluntary MW delivered", get: (s) => pick(s, ["voluntary_mw_total", "voluntary_mw_delivered", "voluntary_mw"]), unit: "MW", digits: 1, higherIsBetter: true },
  { label: "Rebound peak", get: (s) => pick(s, ["rebound_peak_mw"]), unit: "MW" },
];

interface Row {
  label: string;
  governed: number | null;
  baseline: number | null;
  higherIsBetter?: boolean;
  unit?: string;
  digits?: number;
}

interface Cmp {
  governed: Summary | null;
  baseline: Summary | null;
  rows?: Row[];
}

const toNum = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function splitCompare(c: Record<string, unknown> | null): Cmp {
  if (!c) return { governed: null, baseline: null };
  const g = c.governed ?? c.run ?? c.current;
  const b = c.baseline;
  const out: Cmp = {
    governed: isRecord(g) ? (isRecord(g.summary) ? g.summary : g) : null,
    baseline: isRecord(b) ? (isRecord(b.summary) ? b.summary : b) : null,
  };
  // GET /runs/{id}/compare returns ready-made rows: {key, label, governed, baseline, better}.
  if (Array.isArray(c.rows)) {
    out.rows = (c.rows as Record<string, unknown>[]).filter(isRecord).map((r) => {
      const label = String(r.label ?? r.key ?? "");
      return {
        label,
        governed: toNum(r.governed),
        baseline: toNum(r.baseline),
        higherIsBetter: r.better === "higher",
        digits: /MW/.test(label) ? 1 : 0,
      };
    });
  }
  return out;
}

export default function ComparisonCard() {
  const { open, runId, run, metrics } = useRunStore(
    useShallow((s) => ({ open: s.comparisonOpen, runId: s.runId, run: s.run, metrics: s.metrics })),
  );
  const setOpen = useRunStore((s) => s.setComparisonOpen);
  const toast = useRunStore((s) => s.toast);
  const [cmp, setCmp] = useState<Cmp | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    if (!open || !runId) return;
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const c = await api.compare(runId, run?.baseline_run_id);
        if (!cancelled) setCmp(splitCompare(c));
      } catch {
        try {
          const m = await api.metrics(runId);
          if (!cancelled) setCmp({ governed: isRecord(m?.summary) ? (m.summary as Summary) : m, baseline: null });
        } catch {
          if (!cancelled) setCmp(null);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [open, runId, run?.baseline_run_id]);

  if (!open) return null;

  const isBaseline = run?.mode === "baseline";
  const mine = cmp?.governed ?? metrics ?? null;
  const governed = isBaseline ? (cmp?.baseline ?? null) : mine;
  const baseline = isBaseline ? mine : (cmp?.baseline ?? null);
  // Read straight from the two summaries (robust to dict-valued metrics and key variants).
  const rows: Row[] = ROWS.map((r) => ({
    label: r.label,
    governed: r.get(governed),
    baseline: r.get(baseline),
    higherIsBetter: r.higherIsBetter,
    unit: r.unit,
    digits: r.digits,
  }));

  async function startBaseline() {
    setStarting(true);
    try {
      const r = await api.createRun("baseline", run?.seed ?? 42, "human:operator", run?.scenario_id);
      window.open(`/run/${r.run_id}`, "_blank");
    } catch (e) {
      toast("error", `Could not start a baseline run: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="animate-fade-in fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm" onClick={() => setOpen(false)}>
      <div className="glass-strong animate-pop-in w-full max-w-xl rounded-2xl p-6" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-start gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl border border-cyan-400/30 bg-cyan-400/10 text-cyan-200">
            <Icon.ShieldCheck size={17} />
          </div>
          <div className="flex-1">
            <h2 className="text-[17px] font-semibold tracking-tight text-white">Governed agents vs status quo</h2>
            <p className="text-[12px] text-slate-400">Same seed, same weather, same contingency. Impacts at real scale.</p>
          </div>
          <button type="button" onClick={() => setOpen(false)} className="rounded p-1 text-slate-400 hover:bg-white/10 hover:text-white" aria-label="Close">
            <Icon.X size={15} />
          </button>
        </div>

        <table className="mt-5 w-full text-[13px]">
          <thead>
            <tr className="text-[10px] tracking-[0.14em] text-slate-500 uppercase">
              <th className="pb-2 text-left font-medium">Metric</th>
              <th className="pb-2 text-right font-medium text-cyan-300/90">Governed</th>
              <th className="pb-2 text-right font-medium">Baseline</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const g = row.governed;
              const b = row.baseline;
              const better = g !== null && b !== null && g !== b ? (row.higherIsBetter ? g > b : g < b) : null;
              return (
                <tr key={row.label} className="border-t border-white/[0.06]">
                  <td className={cx("py-2.5", row.label.startsWith("·") || /^Vulnerable/.test(row.label) ? "pl-3 text-slate-400" : "text-slate-200")}>{row.label}</td>
                  <td className={cx("py-2.5 text-right font-mono tabular", better === true ? "text-emerald-300" : better === false ? "text-rose-300" : "text-white")}>
                    {loading && g === null ? <span className="shimmer inline-block h-3 w-12 rounded" /> : g === null ? "—" : num(g, row.digits ?? 0)}
                    {g !== null && row.unit ? <span className="ml-1 text-[10px] text-slate-500">{row.unit}</span> : null}
                  </td>
                  <td className="py-2.5 text-right font-mono text-slate-400 tabular">
                    {loading && b === null ? <span className="shimmer inline-block h-3 w-12 rounded" /> : b === null ? "—" : num(b, row.digits ?? 0)}
                    {b !== null && row.unit ? <span className="ml-1 text-[10px] text-slate-600">{row.unit}</span> : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        <div className="mt-5 flex items-center gap-2">
          {!baseline && !isBaseline && (
            <Button variant="ghost" onClick={startBaseline} disabled={starting}>
              {starting ? "Starting…" : "Run the baseline for comparison"}
            </Button>
          )}
          <span className="ml-auto text-[11px] text-slate-500">LLMs propose · the Policy Gate decides · every step is on the record</span>
        </div>
      </div>
    </div>
  );
}
