"use client";

import { useRunStore } from "@/store/useRunStore";
import { cx, Icon } from "./ui";

export default function Toasts() {
  const toasts = useRunStore((s) => s.toasts);
  const dismiss = useRunStore((s) => s.dismissToast);
  if (!toasts.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-5 left-1/2 z-[60] flex -translate-x-1/2 flex-col items-center gap-2">
      {toasts.map((t) => (
        <button
          key={t.id}
          type="button"
          onClick={() => dismiss(t.id)}
          className={cx(
            "glass-strong animate-pop-in pointer-events-auto flex max-w-lg items-center gap-2 rounded-xl px-3.5 py-2.5 text-left text-[12.5px]",
            t.kind === "error" ? "border-rose-500/40 text-rose-100" : t.kind === "ok" ? "border-emerald-400/30 text-emerald-50" : "text-slate-100",
          )}
        >
          {t.kind === "error" ? (
            <Icon.Alert size={14} className="shrink-0 text-rose-300" />
          ) : t.kind === "ok" ? (
            <Icon.Check size={14} className="shrink-0 text-emerald-300" />
          ) : (
            <Icon.Bolt size={14} className="shrink-0 text-amber-300" />
          )}
          {t.text}
        </button>
      ))}
    </div>
  );
}
