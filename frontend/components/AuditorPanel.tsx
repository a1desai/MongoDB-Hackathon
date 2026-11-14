"use client";

/* Auditor tab (PLAN §14, P1): ask the Auditor agent a question; answers show a timeline whose
 * entries link to console cards by ledger seq. Verify ledger recomputes the hash chain. */

import { useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { actorName, hhmm, num, shortHash } from "@/lib/format";
import type { AuditAnswer, VerifyResult } from "@/lib/types";
import { useRunStore } from "@/store/useRunStore";
import { Button, Chip, cx, Icon, Label } from "./ui";

const SUGGESTIONS = [
  "Why was a section cut, and who approved it?",
  "Which actions were denied, and by which rules?",
  "Did any untrusted message influence a plan?",
  "What happened after the generator trip?",
];

export default function AuditorPanel() {
  const runId = useRunStore((s) => s.runId);
  const identity = useRunStore((s) => s.identity);
  const ledger = useRunStore((s) => s.ledgerBySeq);
  const lastSeq = useRunStore((s) => s.lastSeq);
  const focusSeq = useRunStore((s) => s.focusSeq);

  const [q, setQ] = useState("");
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState<(AuditAnswer & { question: string }) | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [showLedger, setShowLedger] = useState(true);

  const tail = useMemo(
    () =>
      Object.values(ledger)
        .sort((a, b) => b.seq - a.seq)
        .slice(0, 40),
    [ledger],
  );

  async function ask(question: string) {
    if (!runId || !question.trim()) return;
    setAsking(true);
    setErr(null);
    try {
      const a = await api.auditAsk(runId, question.trim(), identity);
      setAnswer({ ...a, question: question.trim() });
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : String(e));
    } finally {
      setAsking(false);
    }
  }

  async function runVerify() {
    if (!runId) return;
    setVerifying(true);
    try {
      setVerify(await api.verifyLedger(runId));
    } catch (e) {
      setVerify(null);
      setErr(`Verify failed: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setVerifying(false);
    }
  }

  return (
    <div className="space-y-4 p-4">
      {/* question */}
      <div>
        <div className="mb-2 flex items-center gap-2">
          <Icon.Search size={14} className="text-violet-300" />
          <span className="text-[13px] font-medium text-slate-100">Ask the Auditor</span>
          <span className="text-[11px] text-slate-500">as {actorName(identity)} · every claim cites a ledger seq</span>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void ask(q);
          }}
          className="flex gap-2"
        >
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Why was TOR-S11 cut at 17:58 and who approved it?"
            className="h-9 min-w-0 flex-1 rounded-lg border border-white/10 bg-black/30 px-3 text-[13px] text-slate-100 placeholder:text-slate-500 focus:border-violet-400/50 focus:outline-none"
          />
          <Button type="submit" variant="ghost" disabled={asking || !q.trim()} className="h-9">
            {asking ? "Asking…" : "Ask"}
          </Button>
        </form>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {SUGGESTIONS.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => {
                setQ(s);
                void ask(s);
              }}
              className="rounded-full border border-white/[0.08] bg-white/[0.03] px-2.5 py-1 text-[11px] text-slate-400 transition hover:border-violet-400/40 hover:text-violet-100"
            >
              {s}
            </button>
          ))}
        </div>
      </div>

      {err && <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">{err}</div>}

      {asking && (
        <div className="space-y-2">
          <div className="shimmer h-3 w-3/4 rounded" />
          <div className="shimmer h-3 w-2/3 rounded" />
          <div className="shimmer h-3 w-1/2 rounded" />
        </div>
      )}

      {answer && !asking && (
        <div className="glass animate-card-in rounded-xl p-3.5">
          <Label className="mb-1">Q · {answer.question}</Label>
          <p className="text-[13px] leading-relaxed whitespace-pre-wrap text-slate-100">{answer.answer}</p>
          {(answer.rules_cited?.length ?? 0) > 0 && (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {answer.rules_cited!.map((r) => (
                <Chip key={r} tone="cyan" mono>
                  {r}
                </Chip>
              ))}
            </div>
          )}
          {(answer.timeline?.length ?? 0) > 0 && (
            <ol className="mt-3 space-y-1 border-l border-white/10 pl-3">
              {answer.timeline!.map((t, i) => (
                <li key={`${t.seq}-${i}`} className="relative">
                  <span className="absolute top-[7px] -left-[15.5px] h-1.5 w-1.5 rounded-full bg-violet-300" />
                  <button
                    type="button"
                    onClick={() => focusSeq(t.seq)}
                    className="group flex w-full items-baseline gap-2 rounded px-1 py-0.5 text-left text-xs transition hover:bg-white/[0.04]"
                    title="Show this entry in the console"
                  >
                    <span className="font-mono text-[11px] text-violet-200 group-hover:underline">#{t.seq}</span>
                    <span className="font-mono text-[10px] text-slate-500">{hhmm(t.sim_ts)}</span>
                    <span className="shrink-0 text-slate-400">{actorName(t.actor)}</span>
                    <span className="min-w-0 text-slate-200">{t.summary}</span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}

      {/* verify */}
      <div className="glass rounded-xl p-3.5">
        <div className="flex items-center gap-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-cyan-400/25 bg-cyan-400/10 text-cyan-200">
            <Icon.Chain size={15} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="text-[13px] font-medium text-slate-100">Hash-chained ledger</div>
            <div className="text-[11px] text-slate-500">
              {num(lastSeq)} entries · sha256(prev_hash + entry) · append-only in Atlas
            </div>
          </div>
          <Button variant="ghost" onClick={runVerify} disabled={verifying || !runId}>
            <Icon.ShieldCheck size={13} />
            {verifying ? "Verifying…" : "Verify ledger"}
          </Button>
        </div>
        {verify && (
          <div
            className={cx(
              "animate-pop-in mt-3 flex items-center gap-2 rounded-lg border px-3 py-2 text-[12.5px]",
              verify.ok ? "border-emerald-400/30 bg-emerald-400/10 text-emerald-100" : "border-rose-500/40 bg-rose-500/10 text-rose-100",
            )}
          >
            {verify.ok ? <Icon.Check size={14} /> : <Icon.Alert size={14} />}
            {verify.ok
              ? `Chain verified: ${num(verify.checked ?? lastSeq)} entries, no tampering detected.`
              : `Chain broken at seq #${verify.first_broken_seq ?? "?"}: an entry was modified after it was written.`}
          </div>
        )}
      </div>

      {/* ledger tail */}
      <div>
        <button type="button" onClick={() => setShowLedger(!showLedger)} className="mb-2 flex items-center gap-1.5">
          {showLedger ? <Icon.ChevronDown size={12} className="text-slate-500" /> : <Icon.ChevronRight size={12} className="text-slate-500" />}
          <Label>Latest ledger entries</Label>
        </button>
        {showLedger && (
          <div className="overflow-hidden rounded-xl border border-white/[0.06]">
            {tail.length === 0 ? (
              <div className="px-3 py-6 text-center text-xs text-slate-500">The ledger is empty for this run.</div>
            ) : (
              tail.map((e) => (
                <button
                  key={e.seq}
                  type="button"
                  onClick={() => focusSeq(e.seq)}
                  className="grid w-full grid-cols-[44px_42px_1fr_auto] items-baseline gap-2 border-b border-white/[0.04] px-3 py-1.5 text-left text-[11px] transition last:border-0 hover:bg-white/[0.03]"
                >
                  <span className="font-mono text-cyan-200/80">#{e.seq}</span>
                  <span className="font-mono text-slate-500">{hhmm(e.sim_ts)}</span>
                  <span className="truncate text-slate-300">
                    <span className="mr-1.5 font-mono text-[10px] text-slate-500">{e.type}</span>
                    {e.summary}
                  </span>
                  <span className="font-mono text-[10px] text-slate-600" title={e.hash}>
                    {shortHash(e.hash, 8)}
                  </span>
                </button>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
}
