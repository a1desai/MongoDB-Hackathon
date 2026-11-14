"use client";

/* ResidentPanel (PLAN §14): a featured resident's persona, live home state and SMS thread.
 * Opens when `selectedResidentId` is set (pin click on CityMap, or anywhere else in the UI).
 * Inbound replies are untrusted simulated text: shown as plain text with an orange "untrusted" mark. */

import { useEffect, useMemo, useRef, type ReactNode } from "react";
import { useRunStore } from "@/store/useRunStore";
import { api } from "@/lib/api";
import { actorName, hhmm } from "@/lib/format";
import { decodeFlags, residentList, tempColor, threadMessages } from "@/lib/mapLayers";
import type { Message } from "@/lib/types";

export interface ResidentPanelProps {
  className?: string;
}

function str(v: unknown): string | null {
  if (v == null) return null;
  const s = String(v).trim();
  return s ? s : null;
}

function isInbound(m: Message): boolean {
  if (m.direction) return m.direction === "in";
  return (m.from ?? "").startsWith("sim:");
}

export default function ResidentPanel({ className = "" }: ResidentPanelProps) {
  const selectedResidentId = useRunStore((s) => s.selectedResidentId);
  const setSelectedResident = useRunStore((s) => s.setSelectedResident);
  const staticWorld = useRunStore((s) => s.staticWorld);
  const mapBuffers = useRunStore((s) => s.mapBuffers);
  const threads = useRunStore((s) => s.threads);
  const runId = useRunStore((s) => s.runId);
  const setThread = useRunStore((s) => s.setThread);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const resident = useMemo(() => {
    if (!selectedResidentId) return null;
    return residentList(staticWorld).find((r) => r.id === selectedResidentId) ?? null;
  }, [staticWorld, selectedResidentId]);

  const hhId = resident?.household_id ?? null;
  const messages = useMemo(
    () => (selectedResidentId ? threadMessages(threads, [hhId, selectedResidentId]) : []),
    [threads, hhId, selectedResidentId],
  );

  // Backfill the thread from REST once per selection (the WS only carries new messages).
  useEffect(() => {
    if (!selectedResidentId || !runId) return;
    let cancelled = false;
    api
      .residentThread(selectedResidentId, runId)
      .then((list) => {
        if (cancelled || !list.length) return;
        const key = list[0]?.thread_id || hhId || selectedResidentId;
        setThread(key, list);
      })
      .catch(() => {
        /* endpoint optional; live messages still arrive over the WebSocket */
      });
    return () => {
      cancelled = true;
    };
  }, [selectedResidentId, runId, hhId, setThread]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, selectedResidentId]);

  useEffect(() => {
    if (!selectedResidentId) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSelectedResident(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedResidentId, setSelectedResident]);

  if (!selectedResidentId) return null;

  const raw = resident?.raw ?? {};
  const idx = resident?.idx ?? null;
  const temp = idx != null && mapBuffers?.temps && idx < mapBuffers.temps.length ? mapBuffers.temps[idx] / 4 : null;
  const flags = decodeFlags(idx != null ? mapBuffers?.flags?.[idx] : undefined);
  const vulnerable = Boolean(raw.vulnerable) || (idx != null && Boolean(staticWorld?.households?.vulnerable?.[idx])) || flags.vulnerable;
  const attitudes = Array.isArray(raw.attitudes) ? (raw.attitudes as unknown[]).map(String) : [];
  const facts: [string, string | null][] = [
    ["Age", str(raw.age_bracket)],
    ["Household", str(raw.household)],
    ["Job", str(raw.job)],
    ["Neighbourhood", str(raw.neighbourhood)],
    ["Section", str(raw.section_id)],
    ["Home", hhId],
  ];
  const programs = [
    raw.enrolled ? "CoolShift member" : null,
    raw.has_ev ? "EV (ChargeFlow)" : null,
    raw.has_battery ? "Battery (PowerCell)" : null,
  ].filter(Boolean) as string[];
  const hasUntrusted = messages.some((m) => isInbound(m) && m.trust === "untrusted");

  return (
    <aside
      className={`flex w-[340px] max-w-[calc(100%-1.5rem)] flex-col overflow-hidden rounded-lg border border-slate-700/70 bg-slate-950/92 text-slate-200 shadow-2xl backdrop-blur ${className}`}
      aria-label="Resident details"
    >
      {/* header */}
      <div className="flex items-start gap-3 border-b border-slate-800 px-4 py-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-800 text-sm font-semibold text-slate-100">
          {(resident?.name ?? selectedResidentId)
            .split(/\s+/)
            .map((w) => w[0])
            .slice(0, 2)
            .join("")}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-semibold text-slate-50">{resident?.name ?? selectedResidentId}</div>
          <div className="text-[11px] text-slate-400">Featured resident · fictional</div>
        </div>
        <button
          type="button"
          onClick={() => setSelectedResident(null)}
          className="rounded px-1.5 text-lg leading-none text-slate-400 hover:bg-slate-800 hover:text-slate-100"
          aria-label="Close resident panel"
        >
          ×
        </button>
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
        {!resident && (
          <p className="px-4 py-3 text-xs text-slate-400">
            No profile loaded for {selectedResidentId}. The static world may still be loading.
          </p>
        )}

        {/* home state */}
        <section className="border-b border-slate-800 px-4 py-3">
          <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-wider text-slate-500">Home now</div>
          <div className="flex items-center gap-3">
            <div className="flex items-baseline gap-1">
              <span className="text-2xl font-semibold tabular-nums" style={{ color: tempColor(temp) }}>
                {temp == null ? "–" : temp.toFixed(1)}
              </span>
              <span className="text-xs text-slate-400">°C indoor</span>
            </div>
            <div className="flex flex-wrap gap-1">
              <StateChip on={!flags.powerOff || temp == null} onLabel="power on" offLabel="power off" offTone="red" />
              {flags.precool && <Chip tone="blue">pre-cooling</Chip>}
              {flags.setback && <Chip tone="amber">setback</Chip>}
              {flags.overridden && <Chip tone="amber">overridden</Chip>}
              {vulnerable && <Chip tone="slate">vulnerable (synthetic)</Chip>}
            </div>
          </div>
          {programs.length > 0 && <div className="mt-2 text-[11px] text-slate-400">{programs.join(" · ")}</div>}
        </section>

        {/* persona */}
        {resident && (
          <section className="border-b border-slate-800 px-4 py-3">
            <div className="mb-2 text-[10.5px] font-semibold uppercase tracking-wider text-slate-500">Persona</div>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
              {facts
                .filter(([, v]) => v)
                .map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="text-slate-500">{k}</dt>
                    <dd className="text-slate-200">{v}</dd>
                  </div>
                ))}
            </dl>
            {attitudes.length > 0 && (
              <ul className="mt-2 space-y-0.5 text-[11.5px] text-slate-300">
                {attitudes.map((a) => (
                  <li key={a}>· {a}</li>
                ))}
              </ul>
            )}
          </section>
        )}

        {/* thread */}
        <section className="flex min-h-[160px] flex-1 flex-col px-4 py-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-[10.5px] font-semibold uppercase tracking-wider text-slate-500">TextLine thread</span>
            {hasUntrusted && (
              <span className="rounded-full border border-orange-500/60 bg-orange-500/10 px-2 py-0.5 text-[10px] font-medium text-orange-300">
                contains untrusted replies
              </span>
            )}
          </div>
          <div ref={scrollRef} className="flex flex-1 flex-col gap-2 overflow-y-auto pr-1">
            {messages.length === 0 && <p className="text-xs text-slate-500">No messages yet.</p>}
            {messages.map((m) => {
              const inbound = isInbound(m);
              const untrusted = m.trust === "untrusted";
              return (
                <div key={m._id} className={`flex flex-col ${inbound ? "items-start" : "items-end"}`}>
                  <div
                    className={`max-w-[88%] whitespace-pre-wrap break-words rounded-lg px-3 py-2 text-[12px] leading-snug ${
                      inbound
                        ? untrusted
                          ? "border border-orange-500/70 bg-orange-950/40 text-orange-50"
                          : "bg-slate-800 text-slate-100"
                        : "bg-sky-900/60 text-sky-50"
                    }`}
                  >
                    {m.body}
                  </div>
                  <div className="mt-0.5 flex items-center gap-1.5 text-[10px] text-slate-500">
                    <span>{inbound ? resident?.name ?? actorName(m.from) : actorName(m.from)}</span>
                    {m.sim_ts && <span>· {hhmm(m.sim_ts)}</span>}
                    {inbound && untrusted && (
                      <span className="rounded bg-orange-500/20 px-1 font-semibold uppercase tracking-wide text-orange-300">untrusted</span>
                    )}
                    {m.decision && <span>· {m.decision}</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      </div>
    </aside>
  );
}

const TONES = {
  blue: "border-sky-500/50 bg-sky-500/10 text-sky-200",
  amber: "border-amber-500/50 bg-amber-500/10 text-amber-200",
  red: "border-red-500/60 bg-red-500/15 text-red-200",
  green: "border-emerald-500/50 bg-emerald-500/10 text-emerald-200",
  slate: "border-slate-600 bg-slate-800/60 text-slate-300",
} as const;

function Chip({ tone, children }: { tone: keyof typeof TONES; children: ReactNode }) {
  return <span className={`rounded-full border px-2 py-0.5 text-[10.5px] ${TONES[tone]}`}>{children}</span>;
}

function StateChip({ on, onLabel, offLabel, offTone }: { on: boolean; onLabel: string; offLabel: string; offTone: keyof typeof TONES }) {
  return <Chip tone={on ? "green" : offTone}>{on ? onLabel : offLabel}</Chip>;
}
