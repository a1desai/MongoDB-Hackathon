"use client";

/* Opens on a pending approval (PLAN §10.1, §14): blast radius, rule results, an
 * untrusted-influence warning, and Approve / Deny for the Supervisor identity only. */

import { useMemo, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { api, ApiError } from "@/lib/api";
import { actionLabel, actorName, hhmm, num, simMinutes } from "@/lib/format";
import { pendingApprovals, useRunStore } from "@/store/useRunStore";
import { RuleChips, VerdictChip } from "./ActionCard";
import { Button, Chip, cx, Icon, Label } from "./ui";

export default function ApprovalDrawer() {
  const { approvals, open, identity, cards, clockTs } = useRunStore(
    useShallow((s) => ({
      approvals: s.approvals,
      open: s.approvalDrawerOpen,
      identity: s.identity,
      cards: s.cardsByRequest,
      clockTs: s.clock?.sim_ts,
    })),
  );
  const setOpen = useRunStore((s) => s.setApprovalDrawerOpen);
  const setIdentity = useRunStore((s) => s.setIdentity);
  const toast = useRunStore((s) => s.toast);
  const upsertApprovals = useRunStore((s) => s.upsertApprovals);

  const pending = useMemo(() => pendingApprovals(approvals), [approvals]);
  const [idx, setIdx] = useState(0);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approved" | "denied" | null>(null);

  if (!open || pending.length === 0) return null;
  const i = Math.min(idx, pending.length - 1);
  const a = pending[i];
  const card = cards[a.request_id];
  const req = card?.request;
  const rules = a.rule_results?.length ? a.rule_results : (card?.decision?.rule_results ?? []);
  const br = a.blast_radius ?? card?.decision?.blast_radius ?? {};
  const untrusted = a.untrusted_influence || card?.untrusted;
  const r12 = rules.find((r) => r.rule_id === "R12");
  const isSupervisor = identity === "human:supervisor";
  const nowMin = simMinutes(clockTs);
  const expMin = simMinutes(a.expires_sim_ts);
  const left = nowMin !== null && expMin !== null ? Math.max(0, expMin - nowMin) : null;

  async function decide(decision: "approved" | "denied") {
    setBusy(decision);
    try {
      const res = await api.decideApproval(a._id, decision, identity, note);
      if (res && typeof res === "object" && "_id" in res) upsertApprovals([res]);
      else upsertApprovals([{ ...a, status: decision, decided_by: identity }]);
      toast("ok", `${decision === "approved" ? "Approved" : "Denied"}: ${a.summary ?? actionLabel(req?.action_type)}`);
      setNote("");
    } catch (e) {
      toast("error", `Decision failed: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  }

  return (
    <aside className="glass-strong animate-drawer-in pointer-events-auto absolute top-3 right-3 bottom-3 z-30 flex w-[400px] max-w-[calc(100%-24px)] flex-col overflow-hidden rounded-2xl">
      <header className="flex items-center gap-2 border-b border-white/[0.08] px-4 py-3">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg border border-violet-400/40 bg-violet-400/15 text-violet-200">
          <Icon.User size={14} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-white">Human approval required</div>
          <div className="text-[11px] text-slate-400">
            {pending.length > 1 ? `${i + 1} of ${pending.length} pending · ` : ""}clock held while pending
          </div>
        </div>
        {pending.length > 1 && (
          <div className="flex gap-1">
            <button type="button" className="rounded p-1 text-slate-400 hover:bg-white/10" onClick={() => setIdx((i - 1 + pending.length) % pending.length)} aria-label="Previous">
              <Icon.ChevronRight size={13} className="rotate-180" />
            </button>
            <button type="button" className="rounded p-1 text-slate-400 hover:bg-white/10" onClick={() => setIdx((i + 1) % pending.length)} aria-label="Next">
              <Icon.ChevronRight size={13} />
            </button>
          </div>
        )}
        <button type="button" onClick={() => setOpen(false)} className="rounded p-1 text-slate-400 transition hover:bg-white/10 hover:text-white" aria-label="Minimize">
          <Icon.X size={14} />
        </button>
      </header>

      <div className="scroll-thin min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
        <div>
          <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
            <span className="text-slate-200">{actorName(req?.proposed_by)}</span>
            {req?.acting_for && <span>for {actorName(req.acting_for)}</span>}
            <span className="text-slate-600">·</span>
            <span className={cx("font-semibold", req?.action_type === "feeder_outage" ? "text-rose-200" : "text-white")}>{actionLabel(req?.action_type)}</span>
            {card?.decision && <VerdictChip verdict={card.decision.verdict} />}
          </div>
          <p className="mt-2 text-[14px] leading-snug font-medium text-slate-50">{a.summary ?? req?.summary}</p>
          <div className="mt-1.5 flex items-center gap-3 font-mono text-[10.5px] text-slate-500">
            <span>{a._id}</span>
            <span>requested {hhmm(a.requested_sim_ts)}</span>
            {left !== null && (
              <span className={cx(left <= 3 ? "text-rose-300" : "text-slate-400")}>
                <Icon.Clock size={10} className="mr-1 inline" />
                expires in {left} sim-min
              </span>
            )}
          </div>
        </div>

        {untrusted && (
          <div className="flex gap-2.5 rounded-xl border border-orange-400/40 bg-orange-400/10 px-3 py-2.5 text-[12.5px] text-orange-100">
            <Icon.Alert size={16} className="mt-0.5 shrink-0 text-orange-300" />
            <div>
              <div className="font-semibold">Influenced by untrusted input</div>
              <div className="mt-0.5 text-orange-100/80">
                This proposal cites a message from a member of the public (R14). Check that it does not follow instructions hidden in that text.
              </div>
            </div>
          </div>
        )}

        <div>
          <Label className="mb-2">Blast radius</Label>
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-xl border border-white/[0.07] bg-black/25 px-3 py-2.5">
              <div className="font-mono text-lg font-semibold text-white tabular">{num(br.homes_real ?? 0)}</div>
              <div className="text-[10.5px] text-slate-500">real homes</div>
            </div>
            <div className="rounded-xl border border-white/[0.07] bg-black/25 px-3 py-2.5">
              <div className="font-mono text-lg font-semibold text-white tabular">{num(br.vulnerable_real ?? 0)}</div>
              <div className="flex items-center gap-1 text-[10.5px] text-slate-500">
                vulnerable
                {r12 && r12.applies !== false && (
                  <span className={r12.passed ? "text-emerald-300" : "text-rose-300"} title={r12.detail}>
                    {r12.passed ? "notified ✔" : "not notified"}
                  </span>
                )}
              </div>
            </div>
            <div className="rounded-xl border border-white/[0.07] bg-black/25 px-3 py-2.5">
              <div className="font-mono text-lg font-semibold text-cyan-200 tabular">{num(br.mw ?? 0, 1)}</div>
              <div className="text-[10.5px] text-slate-500">MW</div>
            </div>
          </div>
        </div>

        {rules.length > 0 && (
          <div>
            <Label className="mb-2">Policy Gate rule results</Label>
            <RuleChips rules={rules} />
          </div>
        )}

        {(req?.evidence?.length ?? 0) > 0 && (
          <div>
            <Label className="mb-2">Evidence</Label>
            <div className="flex flex-wrap gap-1">
              {req!.evidence!.map((e) => (
                <Chip key={e.id} mono tone={e.trust === "untrusted" ? "orange" : "neutral"} title={e.kind}>
                  {e.id}
                </Chip>
              ))}
            </div>
          </div>
        )}
      </div>

      <footer className="space-y-2.5 border-t border-white/[0.08] px-4 py-3">
        {!isSupervisor && (
          <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2 text-[12px] text-slate-300">
            <Icon.Shield size={13} className="text-violet-300" />
            Only the Supervisor can decide.
            <button type="button" onClick={() => setIdentity("human:supervisor")} className="ml-auto text-violet-200 underline-offset-2 hover:underline">
              Switch to A. Rahman
            </button>
          </div>
        )}
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={!isSupervisor}
          placeholder="Note for the ledger (optional)"
          className="h-8 w-full rounded-lg border border-white/10 bg-black/30 px-3 text-[12.5px] text-slate-100 placeholder:text-slate-500 focus:border-violet-400/50 focus:outline-none disabled:opacity-40"
        />
        <div className="flex gap-2">
          <Button variant="deny" className="h-10 flex-1" disabled={!isSupervisor || !!busy} onClick={() => decide("denied")}>
            <Icon.X size={14} />
            {busy === "denied" ? "Denying…" : "Deny"}
          </Button>
          <Button variant="approve" className="h-10 flex-[1.4]" disabled={!isSupervisor || !!busy} onClick={() => decide("approved")}>
            <Icon.Check size={14} />
            {busy === "approved" ? "Approving…" : "Approve"}
          </Button>
        </div>
        <div className="text-center font-mono text-[10px] text-slate-600">decision is written to the hash-chained ledger as {identity}</div>
      </footer>
    </aside>
  );
}
