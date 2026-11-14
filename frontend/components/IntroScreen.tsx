"use client";

/* Intro (PLAN §14): full-bleed IntroMap flyover behind the one-line pitch and two buttons
 * that POST /runs and route to /run/[id]. */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, API_URL, ApiError } from "@/lib/api";
import IntroMap from "./IntroMap";
import { Button, cx, Icon } from "./ui";

const PILLARS = [
  { icon: Icon.User, title: "Identity-bound", text: "Every action names its agent and the human it acts for." },
  { icon: Icon.Shield, title: "Policy-gated", text: "A deterministic Gate checks 16 rules before anything runs." },
  { icon: Icon.Chain, title: "On the record", text: "Hash-chained ledger in MongoDB Atlas, verifiable end to end." },
  { icon: Icon.Octagon, title: "Stoppable", text: "Halt the agents mid-run, roll back any execution." },
];

export default function IntroScreen() {
  const router = useRouter();
  const [starting, setStarting] = useState<"governed" | "baseline" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function start(mode: "governed" | "baseline") {
    setStarting(mode);
    setError(null);
    try {
      const r = await api.createRun(mode);
      if (!r?.run_id) throw new ApiError("The backend did not return a run_id", 500);
      router.push(`/run/${encodeURIComponent(r.run_id)}`);
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
      <div className="absolute inset-0">
        <IntroMap className="h-full w-full" />
      </div>

      {/* vignette for legibility */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_30%_50%,rgba(4,6,10,0.15)_0%,rgba(4,6,10,0.55)_55%,rgba(4,6,10,0.92)_100%)]" />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-1/2 bg-gradient-to-t from-[#04060a] via-[#04060a]/70 to-transparent" />
      <div className="pointer-events-none absolute inset-y-0 left-0 w-2/3 bg-gradient-to-r from-[#04060a]/85 via-[#04060a]/40 to-transparent" />

      {/* top bar */}
      <div className="animate-fade-in absolute inset-x-0 top-0 flex items-center gap-3 px-8 py-6">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-cyan-300 to-cyan-600 text-slate-950 shadow-[0_0_30px_-4px_rgba(34,211,238,0.8)]">
          <Icon.Thermo size={18} strokeWidth={2.2} />
        </div>
        <span className="text-[15px] font-semibold tracking-tight text-white">Cooldown</span>
        <span className="ml-auto hidden items-center gap-2 rounded-full border border-white/10 bg-black/40 px-3 py-1.5 text-[11px] text-slate-300 backdrop-blur-md sm:flex">
          <span className="h-1.5 w-1.5 rounded-full bg-cyan-300" />
          Agents You Can Audit · MongoDB Atlas
        </span>
      </div>

      {/* hero */}
      <div className="absolute inset-0 flex items-center px-8 md:px-16">
        <div className="max-w-2xl">
          <div className="animate-rise-in mb-5 inline-flex items-center gap-2 rounded-full border border-amber-300/25 bg-amber-300/10 px-3 py-1 text-[11.5px] text-amber-100 backdrop-blur-md [animation-delay:100ms]">
            <Icon.Thermo size={12} className="text-amber-300" />
            Toronto · June 24, 2031 · extreme heat · stress test (fictional)
          </div>
          <h1 className="animate-rise-in text-[44px] leading-[1.05] font-semibold tracking-[-0.02em] text-white md:text-[56px] [animation-delay:200ms]">
            Keep the city cool.
            <br />
            <span className="bg-gradient-to-r from-cyan-200 via-cyan-300 to-sky-400 bg-clip-text text-transparent">Prove every decision.</span>
          </h1>
          <p className="animate-rise-in mt-5 max-w-xl text-[16px] leading-relaxed text-slate-300 [animation-delay:320ms]">
            Governed AI agents keep Toronto&apos;s power on through an extreme heat wave with the least harm. LLMs propose; a
            deterministic Policy Gate decides; every step lands in a hash-chained ledger you can audit, halt, and roll back.
          </p>

          <div className="animate-rise-in mt-8 flex flex-wrap items-center gap-3 [animation-delay:440ms]">
            <Button variant="primary" size="lg" disabled={!!starting} onClick={() => start("governed")} className="rounded-xl">
              {starting === "governed" ? (
                <>
                  <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-900/30 border-t-slate-900" />
                  Starting…
                </>
              ) : (
                <>
                  <Icon.ShieldCheck size={17} />
                  Start heat wave (governed)
                </>
              )}
            </Button>
            <Button variant="ghost" size="lg" disabled={!!starting} onClick={() => start("baseline")} className="rounded-xl backdrop-blur-md">
              {starting === "baseline" ? "Starting…" : "Run baseline"}
            </Button>
          </div>

          {error && (
            <div className="animate-pop-in mt-4 flex max-w-xl items-start gap-2 rounded-xl border border-rose-500/40 bg-rose-950/60 px-3.5 py-2.5 text-[12.5px] text-rose-100 backdrop-blur-md">
              <Icon.Alert size={14} className="mt-0.5 shrink-0 text-rose-300" />
              <span className="font-mono text-[11.5px] leading-relaxed">{error}</span>
            </div>
          )}
        </div>
      </div>

      {/* pillars */}
      <div className="absolute inset-x-0 bottom-0 px-8 pb-8 md:px-16">
        <div className="grid max-w-5xl grid-cols-2 gap-3 lg:grid-cols-4">
          {PILLARS.map((p, i) => (
            <div
              key={p.title}
              className={cx("glass animate-rise-in rounded-xl px-4 py-3")}
              style={{ animationDelay: `${600 + i * 90}ms` }}
            >
              <div className="flex items-center gap-2 text-[12.5px] font-semibold text-slate-100">
                <p.icon size={14} className="text-cyan-300" />
                {p.title}
              </div>
              <p className="mt-1 text-[11.5px] leading-snug text-slate-400">{p.text}</p>
            </div>
          ))}
        </div>
        <p className="mt-4 text-[10.5px] text-slate-600">
          Synthetic population, fictional names and operators. No health or medical advice. Map © Mapbox © OpenStreetMap.
        </p>
      </div>
    </div>
  );
}
