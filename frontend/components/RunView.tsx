"use client";

/* /run/[id]: ControlBar on top; AgentConsole left (~40%, the primary element); CityMap right
 * (~60%, which overlays its own ResidentPanel) and the ApprovalDrawer; ComparisonCard modal at the end. Loads
 * the run, static world, ledger backlog and approvals over REST, then streams over WS. */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, API_URL, ApiError } from "@/lib/api";
import { connectRun } from "@/lib/ws";
import { useRunStore } from "@/store/useRunStore";
import AgentConsole from "./AgentConsole";
import ApprovalDrawer from "./ApprovalDrawer";
import CityMap from "./CityMap";
import ComparisonCard from "./ComparisonCard";
import ControlBar from "./ControlBar";
import Toasts from "./Toasts";
import { Button, Icon } from "./ui";

function BackendDown({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="glass-strong animate-pop-in absolute top-4 left-1/2 z-40 flex -translate-x-1/2 items-center gap-3 rounded-xl border-amber-300/30 px-4 py-2.5 text-[12.5px] text-amber-50">
      <Icon.Wifi size={15} className="text-amber-300" />
      <span>
        Backend unreachable at <span className="font-mono text-amber-200">{API_URL}</span>. Start it with{" "}
        <span className="font-mono text-amber-200">uvicorn app.main:app --port 8000</span>. Retrying…
      </span>
      <Button size="sm" variant="ghost" onClick={onRetry}>
        Retry now
      </Button>
    </div>
  );
}

function HaltBanner() {
  const halted = useRunStore((s) => s.halted);
  if (!halted) return null;
  return (
    <div className="animate-pop-in pointer-events-none absolute top-12 left-3 z-20 flex items-center gap-2.5 rounded-xl border border-rose-500/50 bg-rose-950/70 px-3.5 py-2 text-rose-50 shadow-[0_0_40px_-8px_rgba(244,63,94,0.7)] backdrop-blur-md">
      <Icon.Octagon size={16} className="text-rose-300" />
      <div className="leading-tight">
        <div className="text-[12.5px] font-semibold tracking-wide">AGENTS HALTED</div>
        <div className="text-[11px] text-rose-200/80">Executor refuses every action (R15). Physics keeps running.</div>
      </div>
    </div>
  );
}

function RunNotFound({ runId, message }: { runId: string; message: string }) {
  return (
    <div className="grid-bg flex h-full items-center justify-center p-6">
      <div className="glass-strong animate-pop-in max-w-md rounded-2xl p-6 text-center">
        <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-xl border border-rose-500/40 bg-rose-500/10 text-rose-300">
          <Icon.Alert size={18} />
        </div>
        <h1 className="text-[16px] font-semibold text-white">Run not found</h1>
        <p className="mt-1.5 text-[13px] text-slate-400">
          <span className="font-mono text-slate-300">{runId}</span> is not known to the backend. {message}
        </p>
        <Link href="/" className="mt-4 inline-block">
          <Button variant="primary">Start a new run</Button>
        </Link>
      </div>
    </div>
  );
}

export default function RunView({ runId }: { runId: string }) {
  const runError = useRunStore((s) => s.runError);
  const backendDown = useRunStore((s) => s.backendDown);
  const connection = useRunStore((s) => s.connection);
  const [nonce, setNonce] = useState(0);
  const loading = useRef(false);

  const load = useCallback(
    async (initial: boolean) => {
      // Polls skip while a load is in flight; the initial load always runs (StrictMode remounts).
      if (loading.current && !initial) return;
      loading.current = true;
      const s = useRunStore.getState();
      try {
        const run = await api.getRun(runId);
        s.setRun(run, null);
        s.setBackendDown(false);
        if (!useRunStore.getState().staticWorld) {
          api
            .worldStatic()
            .then((w) => useRunStore.getState().setStaticWorld(w))
            .catch(() => {
              /* the map shows its own placeholder */
            });
        }
        const [ledger, approvals] = await Promise.allSettled([
          api.ledger(runId, initial ? 0 : useRunStore.getState().lastSeq),
          api.approvals(runId),
        ]);
        if (ledger.status === "fulfilled") useRunStore.getState().ingestLedger(ledger.value);
        if (approvals.status === "fulfilled") useRunStore.getState().upsertApprovals(approvals.value);
        if (initial) {
          // Optional richer snapshot; ignore if the endpoint does not exist.
          api
            .requests(runId)
            .then((list) => useRunStore.getState().upsertRequests(list))
            .catch(() => {});
          if (run.status === "finished") {
            api
              .metrics(runId)
              .then((m) => useRunStore.getState().setMetrics(m))
              .catch(() => {});
          }
        }
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) s.setRun(null, e.message);
        else if (e instanceof ApiError && e.offline) s.setBackendDown(true);
        else s.toast("error", `Could not load the run: ${e instanceof Error ? e.message : String(e)}`);
      } finally {
        loading.current = false;
      }
    },
    [runId],
  );

  // Initial load + live stream.
  useEffect(() => {
    const s = useRunStore.getState();
    s.reset(runId);
    s.loadSavedIdentity();
    void load(true);
    const stop = connectRun({
      runId,
      onBatch: (msgs) => useRunStore.getState().ingest(msgs),
      onStatus: (st) => useRunStore.getState().setConnection(st),
      onOpen: (reconnect) => {
        if (reconnect) void load(false);
      },
    });
    return stop;
  }, [runId, load, nonce]);

  // REST polling while the socket is down, so the console still advances.
  useEffect(() => {
    if (connection === "open") return;
    const t = setInterval(() => void load(false), 3000);
    return () => clearInterval(t);
  }, [connection, load]);

  if (runError) return <RunNotFound runId={runId} message={runError} />;

  return (
    <div className="grid-bg flex h-full flex-col gap-3 p-3">
      <ControlBar />
      <main className="flex min-h-0 flex-1 gap-3">
        <div className="min-h-0 w-[40%] min-w-[420px] max-w-[680px]">
          <AgentConsole />
        </div>
        <div className="relative min-h-0 flex-1 overflow-hidden rounded-2xl border border-white/[0.08] bg-[#070a10]">
          {/* CityMap renders its own ResidentPanel overlay (W10). */}
          <div className="absolute inset-0">
            <CityMap />
          </div>
          <HaltBanner />
          <ApprovalDrawer />
        </div>
      </main>
      {backendDown && <BackendDown onRetry={() => setNonce((n) => n + 1)} />}
      <ComparisonCard />
      <Toasts />
    </div>
  );
}
