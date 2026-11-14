"use client";

/* The hero of the console: one card per action_request, built from the ledger. Header
 * (agent, acting-for, rung, action, targets), evidence chips, the Gate decision with per-rule
 * chips, the execution read-back, the Trace link and Roll back (Supervisor only). */

import { memo, useState } from "react";
import { api, ApiError, traceUrl } from "@/lib/api";
import { actionLabel, actorName, hhmm, num, RUNG_OF } from "@/lib/format";
import type { RuleResult, Verdict } from "@/lib/types";
import { type Card, useRunStore } from "@/store/useRunStore";
import { Button, Chip, cx, Dot, Icon, type Tone } from "./ui";

const STATUS: Record<string, { label: string; tone: Tone; pulse?: boolean }> = {
  proposed: { label: "Proposed", tone: "neutral", pulse: true },
  needs_approval: { label: "Needs approval", tone: "violet", pulse: true },
  approved: { label: "Approved", tone: "cyan" },
  denied: { label: "Denied", tone: "red" },
  executing: { label: "Executing", tone: "cyan", pulse: true },
  verified: { label: "Verified", tone: "green" },
  partial: { label: "Partial", tone: "amber" },
  failed: { label: "Failed", tone: "red" },
  rolled_back: { label: "Rolled back", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

const VERDICT: Record<Verdict, { label: string; tone: Tone }> = {
  allow: { label: "ALLOW", tone: "cyan" },
  partial_allow: { label: "PARTIAL ALLOW", tone: "amber" },
  deny: { label: "DENY", tone: "red" },
  needs_approval: { label: "NEEDS APPROVAL", tone: "violet" },
};

export function StatusPill({ status }: { status?: string }) {
  const s = STATUS[status ?? "proposed"] ?? { label: status ?? "—", tone: "neutral" as Tone };
  return (
    <Chip tone={s.tone} className="gap-1.5 font-medium">
      <Dot tone={s.tone} live={s.pulse} />
      {s.label}
    </Chip>
  );
}

export function VerdictChip({ verdict }: { verdict: Verdict }) {
  const v = VERDICT[verdict] ?? { label: verdict, tone: "neutral" as Tone };
  return (
    <Chip tone={v.tone} mono className="px-2 py-1 font-semibold tracking-wider">
      {verdict === "deny" ? <Icon.X size={11} /> : verdict === "needs_approval" ? <Icon.User size={11} /> : <Icon.ShieldCheck size={11} />}
      {v.label}
    </Chip>
  );
}

function ruleTone(r: RuleResult): Tone {
  if (!r.applies) return "neutral";
  if (r.passed) return "green";
  return r.effect === "needs_approval" ? "violet" : "red";
}

function deniedCount(r: RuleResult): number | undefined {
  const c = r.counts ?? {};
  return c.denied ?? c.blocked ?? c.failed ?? undefined;
}

export function RuleChips({ rules, compact = false }: { rules: RuleResult[]; compact?: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const applicable = rules.filter((r) => r.applies !== false);
  const na = rules.length - applicable.length;
  const sel = rules.find((r) => r.rule_id === open);
  return (
    <div>
      <div className="flex flex-wrap items-center gap-1">
        {applicable.map((r) => {
          const tone = ruleTone(r);
          const dc = deniedCount(r);
          return (
            <Chip
              key={r.rule_id}
              tone={tone}
              mono
              active={open === r.rule_id}
              onClick={() => setOpen(open === r.rule_id ? null : r.rule_id)}
              title={`${r.rule_id} ${r.name ?? ""}: ${r.detail ?? ""}`}
              className={cx(r.passed && "opacity-80")}
            >
              {r.passed ? <Icon.Check size={10} /> : r.effect === "needs_approval" ? <Icon.User size={10} /> : <Icon.X size={10} />}
              {r.rule_id}
              {!compact && r.name ? <span className="opacity-60">{r.name}</span> : null}
              {!r.passed && dc ? <span className="opacity-80">×{num(dc)}</span> : null}
            </Chip>
          );
        })}
        {na > 0 && <span className="font-mono text-[10px] text-slate-500">+{na} n/a</span>}
      </div>
      {sel && (
        <div className="animate-fade-in mt-2 rounded-lg border border-white/10 bg-black/30 p-2.5 text-xs">
          <div className="flex items-center gap-2">
            <span className="font-mono text-slate-400">{sel.rule_id}</span>
            <span className="font-medium text-slate-100">{sel.name}</span>
            <span className="ml-auto">
              <Chip tone={ruleTone(sel)}>{sel.passed ? "pass" : sel.effect === "needs_approval" ? "needs approval" : "fail"}</Chip>
            </span>
          </div>
          {sel.detail && <p className="mt-1.5 leading-relaxed text-slate-300">{sel.detail}</p>}
          {sel.counts && Object.keys(sel.counts).length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {Object.entries(sel.counts).map(([k, v]) => (
                <span key={k} className="rounded bg-white/[0.05] px-1.5 py-0.5 font-mono text-[11px] text-slate-300">
                  {k}: <span className="text-white">{num(v)}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ExecutionRow({ card }: { card: Card }) {
  const exe = card.execution;
  if (card.held) {
    return (
      <div className="flex items-center gap-2 text-xs text-rose-200">
        <Icon.Octagon size={13} />
        Held by kill switch <span className="font-mono text-rose-300/80">R15</span> — will run on resume
      </div>
    );
  }
  if (!exe) {
    const st = card.request.status;
    if (st === "approved") {
      return (
        <div className="flex items-center gap-2 text-xs text-slate-400">
          <Dot tone="cyan" live /> Queued for the Executor
        </div>
      );
    }
    return null;
  }
  if (exe.refused) {
    return (
      <div className="flex items-center gap-2 text-xs text-rose-200">
        <Icon.Octagon size={13} /> Executor refused: {exe.detail}
      </div>
    );
  }
  const v = exe.verification;
  const total = v ? (v.applied ?? 0) + (v.pending ?? 0) + (v.offline ?? 0) + (v.overridden ?? 0) + (v.failed ?? 0) : 0;
  const pct = (n?: number) => (total ? Math.round((100 * (n ?? 0)) / total) : 0);
  const st = exe.status ?? "sent";
  const final =
    st === "verified" ? (
      <span className="flex items-center gap-1 text-emerald-300">
        <Icon.Check size={12} /> verified
      </span>
    ) : st === "partial" ? (
      <span className="flex items-center gap-1 text-amber-200">
        <Icon.Alert size={12} /> partial
      </span>
    ) : st === "failed" ? (
      <span className="flex items-center gap-1 text-rose-300">
        <Icon.X size={12} /> failed
      </span>
    ) : st === "rolled_back" ? (
      <span className="flex items-center gap-1 text-slate-300">
        <Icon.Undo size={12} /> rolled back
      </span>
    ) : (
      <span className="flex items-center gap-1.5 text-cyan-200/80">
        <Dot tone="cyan" live /> awaiting read-back
      </span>
    );
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
      <span className="flex items-center gap-1.5 text-slate-300">
        <span className="font-mono text-[10px] text-slate-500">{exe.system ?? card.request.system}</span>
        sent
        {exe.attempts && exe.attempts > 1 ? <span className="font-mono text-[10px] text-amber-200/80">×{exe.attempts}</span> : null}
        <Icon.ChevronRight size={11} className="text-slate-600" />
        {final}
      </span>
      {total > 0 && (
        <span className="font-mono text-[11px] text-slate-400">
          <span className="text-slate-200">{pct(v?.applied)}%</span> applied
          {v?.offline ? <> · {pct(v.offline)}% offline</> : null}
          {v?.overridden ? <> · {num(v.overridden)} overridden</> : null}
          {v?.pending ? <> · {num(v.pending)} pending</> : null}
        </span>
      )}
      {typeof v?.delivered_mw === "number" && (
        <span className="ml-auto font-mono text-[12px] font-medium text-cyan-200">
          {num(v.delivered_mw, 1)} <span className="text-cyan-200/60">MW</span>
        </span>
      )}
    </div>
  );
}

function ActionCardImpl({ card, highlight }: { card: Card; highlight?: boolean }) {
  const identity = useRunStore((s) => s.identity);
  const focusSeq = useRunStore((s) => s.focusSeq);
  const toast = useRunStore((s) => s.toast);
  const setApprovalDrawerOpen = useRunStore((s) => s.setApprovalDrawerOpen);
  const [busy, setBusy] = useState(false);

  const r = card.request;
  const d = card.decision;
  const exe = card.execution;
  const rung = r.rung ?? (r.action_type ? RUNG_OF[r.action_type] : undefined);
  const isSupervisor = identity === "human:supervisor";
  const status = r.status ?? "proposed";
  const isOutage = r.action_type === "feeder_outage";
  const canRollback =
    !!exe?._id &&
    r.action_type !== "undo" &&
    !card.rollbackRequested &&
    exe.undo !== null &&
    ["sent", "verified", "partial"].includes(exe.status ?? "") &&
    status !== "rolled_back";
  const trace = traceUrl(r);
  const pendingApproval = card.approval?.status === "pending";

  async function rollback() {
    if (!exe?._id) return;
    setBusy(true);
    try {
      await api.rollback(exe._id, identity, `Rollback of ${r._id} from the console`);
      toast("ok", `Rollback requested for ${actionLabel(r.action_type)}. It goes through the Gate (R16).`);
    } catch (e) {
      toast("error", `Rollback failed: ${e instanceof ApiError ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  const accent = card.untrusted
    ? "before:bg-orange-400"
    : status === "denied" || status === "failed"
      ? "before:bg-rose-500"
      : pendingApproval || status === "needs_approval"
        ? "before:bg-violet-400"
        : status === "verified"
          ? "before:bg-emerald-400"
          : isOutage
            ? "before:bg-rose-400/70"
            : "before:bg-cyan-400/70";

  return (
    <article
      id={`card-${r._id}`}
      data-seqs={card.seqs.join(",")}
      className={cx(
        "glass animate-card-in relative overflow-hidden rounded-xl p-3.5 pl-4",
        "before:absolute before:inset-y-0 before:left-0 before:w-[3px] before:content-['']",
        accent,
        pendingApproval && "approval-glow",
        highlight && "seq-flash",
      )}
    >
      {/* header */}
      <header className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-slate-400">
            <span className="font-medium text-slate-200">{actorName(r.proposed_by)}</span>
            {r.acting_for && (
              <span>
                for <span className="text-slate-300">{actorName(r.acting_for)}</span>
              </span>
            )}
            <span className="text-slate-600">·</span>
            {rung ? (
              <Chip tone={rung >= 7 ? "red" : "cyan"} mono title="Rung on the least-harm ladder">
                R{rung}
              </Chip>
            ) : null}
            <span className={cx("font-semibold", isOutage ? "text-rose-200" : "text-white")}>{actionLabel(r.action_type)}</span>
            {r.undo_of && <Chip mono>undo {r.undo_of}</Chip>}
          </div>
          <p className="mt-1.5 text-[13px] leading-snug text-slate-100">{r.summary ?? r._id}</p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <StatusPill status={status} />
          <span className="font-mono text-[10px] text-slate-500">{hhmm(r.created_sim_ts ?? r.sim_ts)}</span>
        </div>
      </header>

      {/* targets + expected */}
      {(r.targets_count || r.expected_mw || r.window) && (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-slate-400">
          {r.targets_count ? <span>{num(r.targets_count)} targets</span> : null}
          {r.targets?.section_id ? <span className="text-slate-300">{r.targets.section_id}</span> : null}
          {r.expected_mw ? <span>~{num(r.expected_mw, 1)} MW expected</span> : null}
          {r.window ? (
            <span>
              {r.window.start}–{r.window.end}
            </span>
          ) : null}
          {card.messagesOut > 0 && <span>{num(card.messagesOut)} msg sent</span>}
        </div>
      )}

      {/* evidence */}
      {(r.evidence?.length || card.untrusted) && (
        <div className="mt-2.5 flex flex-wrap items-center gap-1">
          {card.untrusted && (
            <Chip tone="orange" className="font-medium">
              <Icon.Alert size={11} /> influenced by untrusted input
            </Chip>
          )}
          {r.evidence?.slice(0, 8).map((e) => (
            <Chip
              key={e.id}
              mono
              tone={e.trust === "untrusted" ? "orange" : "neutral"}
              title={`${e.kind ?? "evidence"} · ${e.trust ?? "system"}`}
            >
              {e.trust === "untrusted" && <Dot tone="orange" />}
              {e.id.length > 18 ? `${e.id.slice(0, 16)}…` : e.id}
            </Chip>
          ))}
          {(r.evidence?.length ?? 0) > 8 && <span className="font-mono text-[10px] text-slate-500">+{(r.evidence?.length ?? 0) - 8}</span>}
        </div>
      )}

      {/* decision */}
      {d ? (
        <div className="mt-3 border-t border-white/[0.06] pt-2.5">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <VerdictChip verdict={d.verdict} />
            {(d.allowed_count || d.denied_count) ? (
              <span className="font-mono text-[11px] text-slate-300">
                <span className="text-cyan-200">{num(d.allowed_count ?? 0)}</span> allowed ·{" "}
                <span className={d.denied_count ? "text-rose-300" : ""}>{num(d.denied_count ?? 0)}</span> denied
              </span>
            ) : null}
            {d.denied_reasons &&
              Object.entries(d.denied_reasons).map(([rid, n]) => (
                <Chip key={rid} tone="red" mono>
                  {rid} ×{num(n)}
                </Chip>
              ))}
            <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-slate-500" title="Decided by the deterministic Policy Gate">
              <Icon.Shield size={11} /> gate · {d.policy_version ?? "policy"}
            </span>
          </div>
          {d.rule_results && d.rule_results.length > 0 && <RuleChips rules={d.rule_results} compact />}
          {d.policy_snapshot && d.policy_snapshot.length > 0 && (
            <details className="mt-2 text-xs text-emerald-200">
              <summary className="cursor-pointer">Policy settings recorded with this decision</summary>
              <ul className="mt-2 space-y-2 text-slate-300">
                {d.policy_snapshot.map(rule => <li key={rule._id}>
                  <strong>{rule._id} · {rule.name.replaceAll("_", " ").toLowerCase()}</strong>
                  <p className="mt-1">{rule.description}</p>
                  {Object.keys(rule.params).length > 0 && <pre className="mt-1 whitespace-pre-wrap break-words">{JSON.stringify(rule.params, null, 2)}</pre>}
                </li>)}
              </ul>
            </details>
          )}
          {pendingApproval && (
            <button
              type="button"
              onClick={() => setApprovalDrawerOpen(true)}
              className="mt-2.5 flex w-full items-center gap-2 rounded-lg border border-violet-400/40 bg-violet-400/10 px-3 py-2 text-left text-xs text-violet-100 transition hover:bg-violet-400/20"
            >
              <Icon.User size={13} />
              Waiting for a Supervisor decision
              <span className="ml-auto flex items-center gap-1 text-violet-200">
                Review <Icon.ChevronRight size={12} />
              </span>
            </button>
          )}
          {card.approval && card.approval.status !== "pending" && (
            <div className="mt-2 flex items-center gap-2 text-xs text-slate-400">
              {card.approval.status === "approved" ? <Icon.Check size={12} className="text-emerald-300" /> : <Icon.X size={12} className="text-rose-300" />}
              {card.approval.status === "expired" ? "Approval expired (auto-denied)" : `${card.approval.status === "approved" ? "Approved" : "Denied"} by ${actorName(card.approval.decided_by)}`}
              {card.approval.note ? <span className="truncate text-slate-500">“{card.approval.note}”</span> : null}
            </div>
          )}
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-2 border-t border-white/[0.06] pt-2.5 text-xs text-slate-500">
          <Icon.Shield size={12} />
          <span className="shimmer h-2 w-24 rounded" /> awaiting the Policy Gate
        </div>
      )}

      {/* execution */}
      {(exe || card.held || status === "approved") && (
        <div className="mt-2.5 border-t border-white/[0.06] pt-2.5">
          <ExecutionRow card={card} />
        </div>
      )}

      {/* footer */}
      <footer className="mt-2.5 flex items-center gap-2">
        <div className="flex min-w-0 flex-1 flex-wrap gap-1">
          {card.seqs.slice(0, 8).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => focusSeq(s)}
              className="rounded px-1 font-mono text-[10px] text-slate-500 transition hover:bg-white/[0.06] hover:text-cyan-200"
              title="Ledger sequence number"
            >
              #{s}
            </button>
          ))}
          {card.seqs.length > 8 && <span className="font-mono text-[10px] text-slate-600">…</span>}
        </div>
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
          <span className="px-1.5 text-[11px] text-slate-600" title="No LLM call behind this action (deterministic path)">
            no trace
          </span>
        )}
        {canRollback && (
          <Button
            size="sm"
            variant="ghost"
            disabled={!isSupervisor || busy}
            onClick={rollback}
            title={isSupervisor ? "Create a gated undo request from the stored undo command" : "Switch to Supervisor A. Rahman to roll back"}
          >
            <Icon.Undo size={12} />
            {busy ? "Rolling back…" : "Roll back"}
          </Button>
        )}
        {card.rollbackRequested && (
          <Chip tone="neutral">
            <Icon.Undo size={11} /> rollback requested
          </Chip>
        )}
      </footer>
    </article>
  );
}

export const ActionCard = memo(ActionCardImpl);
export default ActionCard;
