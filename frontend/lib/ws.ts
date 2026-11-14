/* WebSocket client for `/ws?run_id=` (PLAN §13). Reconnects with backoff, keeps the socket
 * alive with "ping", and batches messages per animation frame so a burst of ledger entries
 * becomes one store update. */

import { WS_URL } from "./api";
import type { WsMessage } from "./types";

export type SocketStatus = "connecting" | "open" | "reconnecting" | "closed";

export interface RunSocketOptions {
  runId: string;
  onBatch: (msgs: WsMessage[]) => void;
  onStatus?: (s: SocketStatus) => void;
  /** Called after every (re)connect, e.g. to backfill the ledger after a gap. */
  onOpen?: (reconnect: boolean) => void;
}

export function connectRun({ runId, onBatch, onStatus, onOpen }: RunSocketOptions): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let attempt = 0;
  let opened = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let queue: WsMessage[] = [];
  let frame: number | null = null;

  const flush = () => {
    frame = null;
    if (!queue.length) return;
    const batch = queue;
    queue = [];
    onBatch(batch);
  };

  const schedule = () => {
    if (frame !== null) return;
    if (typeof requestAnimationFrame === "function" && document.visibilityState === "visible") {
      frame = requestAnimationFrame(flush);
    } else {
      frame = setTimeout(flush, 50) as unknown as number;
    }
  };

  const open = () => {
    if (closed) return;
    onStatus?.(opened ? "reconnecting" : "connecting");
    try {
      ws = new WebSocket(`${WS_URL}?run_id=${encodeURIComponent(runId)}`);
    } catch {
      retry();
      return;
    }
    ws.onopen = () => {
      const reconnect = opened;
      opened = true;
      attempt = 0;
      onStatus?.("open");
      onOpen?.(reconnect);
      pingTimer = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
      }, 20000);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== "string") return;
      try {
        const msg = JSON.parse(ev.data) as WsMessage;
        if (msg && typeof msg.type === "string" && msg.type !== "pong") {
          queue.push(msg);
          schedule();
        }
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onclose = () => {
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = null;
      retry();
    };
    ws.onerror = () => {
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
    };
  };

  const retry = () => {
    if (closed || retryTimer) return;
    onStatus?.("reconnecting");
    const delay = Math.min(8000, 500 * 2 ** attempt++);
    retryTimer = setTimeout(() => {
      retryTimer = null;
      open();
    }, delay);
  };

  open();

  return () => {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (pingTimer) clearInterval(pingTimer);
    if (frame !== null) {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
      clearTimeout(frame as unknown as ReturnType<typeof setTimeout>);
    }
    flush();
    if (ws) {
      ws.onclose = null;
      ws.onerror = null;
      ws.onmessage = null;
      try {
        ws.close();
      } catch {
        /* ignore */
      }
    }
    onStatus?.("closed");
  };
}
