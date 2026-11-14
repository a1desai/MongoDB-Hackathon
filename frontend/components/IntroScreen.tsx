"use client";

/* Prompt-first scenario studio. A Scenario Designer agent turns free text into validated grid
 * parameters in Atlas; the user sees those parameters before starting a governed/baseline run. */

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { api, API_URL, ApiError } from "@/lib/api";
import type { GeneratedScenario } from "@/lib/types";
import IntroMap from "./IntroMap";
import { Button, Chip, cx, Icon } from "./ui";

const PILLARS = [
  { icon: Icon.Sparkle, title: "Prompt-designed", text: "Agents infer weather, demand, supply, disruptions and assumptions." },
  { icon: Icon.Shield, title: "Policy-gated", text: "The same 16 deterministic rules check every resulting action." },
  { icon: Icon.Chain, title: "Reproducible", text: "The prompt and generated scenario version are stored in MongoDB Atlas." },
  { icon: Icon.Octagon, title: "Stoppable", text: "Approve, deny, halt and roll back agent actions during the run." },
];

function n(value: number | undefined, digits = 0): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

function temperatureRange(scenario: GeneratedScenario): string {
  const values = Object.values(scenario.weather?.profile_c ?? {});
  if (!values.length) return "—";
  const low = Math.min(...values);
  const high = Math.max(...values);
  return low === high ? n(low, 1) : `${n(low, 1)} to ${n(high, 1)}`;
}

function Metric({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="rounded-xl border border-white/[0.08] bg-black/25 px-3 py-2.5">
      <div className="text-[9.5px] font-medium tracking-[0.13em] text-slate-500 uppercase">{label}</div>
      <div className="mt-1 font-mono text-[17px] font-semibold text-white tabular">
        {value}
        {unit && <span className="ml-1 text-[10px] font-normal text-slate-500">{unit}</span>}
      </div>
    </div>
  );
}

export default function IntroScreen() {
  const router = useRouter();
  const [prompt, setPrompt] = useState("");
  const [scenario, setScenario] = useState<GeneratedScenario | null>(null);
  const [generating, setGenerating] = useState(false);
  const [starting, setStarting] = useState<"governed" | "baseline" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canGenerate = prompt.trim().length >= 12 && !generating && !starting;
  const sourceTone = scenario?.designer?.source === "llm" ? "violet" : scenario?.designer?.source === "cache" ? "cyan" : "amber";
  const disruptions = useMemo(() => scenario?.contingencies ?? [], [scenario]);

  async function generate() {
    if (!canGenerate) return;
    setGenerating(true);
    setScenario(null);
    setError(null);
    try {
      const result = await api.generateScenario(prompt.trim());
      if (!result?.scenario?._id) throw new ApiError("The backend did not return a generated scenario", 500);
      setScenario(result.scenario);
    } catch (e) {
      setError(
        e instanceof ApiError && e.offline
          ? `Backend unreachable at ${API_URL}. Start it with: cd backend && .venv/bin/uvicorn app.main:app --port 8000`
          : `Could not design the scenario: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setGenerating(false);
    }
  }

  async function start(mode: "governed" | "baseline") {
    if (!scenario) return;
    setStarting(mode);
    setError(null);
    try {
      const result = await api.createRun(mode, 42, "human:operator", scenario._id);
      if (!result?.run_id) throw new ApiError("The backend did not return a run_id", 500);
      router.push(`/run/${encodeURIComponent(result.run_id)}`);
    } catch (e) {
      setError(
        e instanceof ApiError && e.offline
          ? `Backend unreachable at ${API_URL}. Start it with: cd backend && .venv/bin/uvicorn app.main:app --port 8000`
          : `Could not start the run: ${e instanceof Error ? e.message : String(e)}`,
      );
      setStarting(null);
    }
  }

  return (
    <div className="relative h-full w-full overflow-hidden bg-background">
      <div className="absolute inset-0"><IntroMap className="h-full w-full" /></div>
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_30%_45%,rgba(4,6,10,0.12)_0%,rgba(4,6,10,0.62)_58%,rgba(4,6,10,0.94)_100%)]" />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-[#04060a] via-[#04060a]/75 to-transparent" />
      <div className="pointer-events-none absolute inset-y-0 left-0 w-2/3 bg-gradient-to-r from-[#04060a]/90 via-[#04060a]/45 to-transparent" />

      <div className="animate-fade-in absolute inset-x-0 top-0 z-10 flex items-center gap-3 px-8 py-6">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-300 to-cyan-600 text-slate-950 shadow-[0_0_30px_-4px_rgba(34,211,238,0.8)]">
          <Icon.Thermo size={18} strokeWidth={2.2} />
        </div>
        <span className="text-[15px] font-semibold tracking-tight text-white">Cooldown</span>
        <span className="ml-auto hidden items-center gap-2 rounded-full border border-white/10 bg-black/40 px-3 py-1.5 text-[11px] text-slate-300 backdrop-blur-md sm:flex">
          <span className="h-1.5 w-1.5 rounded-full bg-violet-300" />
          Scenario Studio · governed agents on MongoDB Atlas
        </span>
      </div>

      <div className="absolute inset-0 flex items-center px-8 pt-14 pb-36 md:px-14">
        <div className="mx-auto grid w-full max-w-[1380px] items-center gap-10 lg:grid-cols-[minmax(380px,0.9fr)_minmax(520px,1.1fr)]">
          <section className="max-w-xl">
            <div className="animate-rise-in mb-5 inline-flex items-center gap-2 rounded-full border border-violet-300/25 bg-violet-300/10 px-3 py-1 text-[11.5px] text-violet-100 backdrop-blur-md [animation-delay:100ms]">
              <Icon.Sparkle size={12} className="text-violet-300" />
              Describe any grid stress test in plain language
            </div>
            <h1 className="animate-rise-in text-[42px] leading-[1.05] font-semibold tracking-[-0.025em] text-white md:text-[54px] [animation-delay:180ms]">
              Imagine the crisis.
              <br />
              <span className="bg-gradient-to-r from-cyan-200 via-cyan-300 to-violet-400 bg-clip-text text-transparent">Let agents build the test.</span>
            </h1>
            <p className="animate-rise-in mt-5 max-w-lg text-[15.5px] leading-relaxed text-slate-300 [animation-delay:280ms]">
              Type a scenario. A Scenario Designer agent turns it into weather, demand, supply and disruption parameters. Then governed agents run it—and prove every decision.
            </p>
            <div className="animate-rise-in mt-7 grid grid-cols-2 gap-2 [animation-delay:360ms]">
              {PILLARS.map((pillar) => (
                <div key={pillar.title} className="glass rounded-xl px-3.5 py-3">
                  <div className="flex items-center gap-2 text-[12px] font-semibold text-slate-100">
                    <pillar.icon size={13} className="text-cyan-300" />
                    {pillar.title}
                  </div>
                  <p className="mt-1 text-[11px] leading-snug text-slate-400">{pillar.text}</p>
                </div>
              ))}
            </div>
          </section>

          <section className="glass-strong animate-rise-in max-h-[calc(100vh-150px)] overflow-y-auto rounded-2xl p-5 [animation-delay:220ms]">
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-violet-400/35 bg-violet-400/10 text-violet-200">
                <Icon.Sparkle size={17} />
              </div>
              <div>
                <h2 className="text-[15px] font-semibold text-white">What should happen to the grid?</h2>
                <p className="mt-0.5 text-[11.5px] text-slate-400">Mention anything you care about; the agent will infer missing simulation parameters.</p>
              </div>
            </div>

            <textarea
              value={prompt}
              onChange={(event) => {
                setPrompt(event.target.value);
                setScenario(null);
                setError(null);
              }}
              rows={5}
              maxLength={4000}
              placeholder="For example: A week-long heat dome peaks at 43 C. At 18:10 a transmission corridor loses 650 MW while EV charging surges. Protect hospitals and avoid outages in low-income neighbourhoods."
              className="mt-4 w-full resize-none rounded-xl border border-white/10 bg-black/35 px-3.5 py-3 text-[13.5px] leading-relaxed text-slate-100 outline-none placeholder:text-slate-600 focus:border-violet-400/50 focus:ring-1 focus:ring-violet-400/20"
              aria-label="Custom grid scenario prompt"
            />
            <div className="mt-2 flex items-center gap-3">
              <span className={cx("text-[10.5px]", prompt.length > 3800 ? "text-amber-300" : "text-slate-600")}>{prompt.length.toLocaleString()} / 4,000</span>
              <Button variant="primary" size="lg" className="ml-auto h-10 rounded-xl px-5" disabled={!canGenerate} onClick={() => void generate()}>
                {generating ? (
                  <><span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-900/30 border-t-slate-900" />Designing scenario…</>
                ) : (
                  <><Icon.Sparkle size={15} />Have agents design it</>
                )}
              </Button>
            </div>

            {error && (
              <div className="animate-pop-in mt-3 flex items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-950/50 px-3.5 py-2.5 text-[12px] text-rose-100">
                <Icon.Alert size={14} className="mt-0.5 shrink-0 text-rose-300" />
                <span className="font-mono text-[11px] leading-relaxed">{error}</span>
              </div>
            )}

            {scenario && (
              <div className="animate-card-in mt-4 space-y-3 border-t border-white/[0.08] pt-4">
                <div className="flex items-start gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-[15px] font-semibold text-white">{scenario.title}</h3>
                      <Chip tone={sourceTone} mono>{scenario.designer?.source ?? "agent"}</Chip>
                    </div>
                    {scenario.summary && <p className="mt-1 text-[12px] leading-relaxed text-slate-400">{scenario.summary}</p>}
                  </div>
                  <Chip tone="green"><Icon.Check size={10} />validated</Chip>
                </div>

                <div className="grid grid-cols-4 gap-2">
                  <Metric label="Temp range" value={temperatureRange(scenario)} unit="°C" />
                  <Metric label="Demand peak" value={n(scenario.target_peak_mw)} unit="MW" />
                  <Metric label="Start supply" value={n(scenario.supply_mw)} unit="MW" />
                  <Metric label="Safe indoor" value={n(scenario.t_safe_c, 1)} unit="°C" />
                </div>

                {disruptions.length > 0 && (
                  <div className="rounded-xl border border-rose-500/20 bg-rose-500/[0.06] px-3 py-2.5">
                    <div className="mb-1.5 text-[9.5px] font-medium tracking-[0.13em] text-rose-300/80 uppercase">Designed disruptions</div>
                    {disruptions.map((event, index) => (
                      <div key={`${event.at}-${index}`} className="flex items-center gap-2 text-[11.5px] text-slate-200">
                        <Icon.Bolt size={11} className="text-rose-300" />
                        <span className="font-mono text-rose-100">{event.at}</span>
                        <span>{event.label ?? event.type}</span>
                        <span className="ml-auto font-mono text-slate-300">−{n(event.mw)} MW</span>
                      </div>
                    ))}
                  </div>
                )}

                {scenario.designer?.reasoning && (
                  <details className="group rounded-xl border border-white/[0.07] bg-black/20 px-3 py-2.5">
                    <summary className="cursor-pointer text-[11.5px] font-medium text-violet-200">Why the agent chose these parameters</summary>
                    <p className="mt-2 text-[11.5px] leading-relaxed text-slate-400">{scenario.designer.reasoning}</p>
                    {(scenario.designer.assumptions?.length ?? 0) > 0 && (
                      <ul className="mt-2 space-y-1 text-[11px] text-slate-500">
                        {scenario.designer.assumptions!.map((assumption) => <li key={assumption}>· {assumption}</li>)}
                      </ul>
                    )}
                  </details>
                )}

                <div className="flex flex-wrap items-center gap-2 pt-1">
                  <Button variant="primary" size="lg" className="h-11 rounded-xl" disabled={!!starting} onClick={() => void start("governed")}>
                    {starting === "governed" ? "Starting…" : <><Icon.ShieldCheck size={16} />Run with governed agents</>}
                  </Button>
                  <Button variant="ghost" size="lg" className="h-11 rounded-xl" disabled={!!starting} onClick={() => void start("baseline")}>
                    {starting === "baseline" ? "Starting…" : "Run baseline"}
                  </Button>
                  <span className="ml-auto font-mono text-[9.5px] text-slate-600">{scenario._id}</span>
                </div>
              </div>
            )}
          </section>
        </div>
      </div>

      <div className="absolute inset-x-0 bottom-0 px-8 pb-6 md:px-14">
        <p className="text-[10.5px] text-slate-600">
          Scenario parameters and population are simulated. No real infrastructure is controlled. Prompt and generated version are stored for reproducibility. Map © Mapbox © OpenStreetMap.
        </p>
      </div>
    </div>
  );
}
