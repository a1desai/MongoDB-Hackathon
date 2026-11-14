/* Small UI primitives: chips, buttons and inline icons (no icon library needed). */

import type { ButtonHTMLAttributes, ReactNode, SVGProps } from "react";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

// ------------------------------------------------------------------ chips

export type Tone = "neutral" | "cyan" | "orange" | "red" | "green" | "violet" | "amber";

const TONE: Record<Tone, string> = {
  neutral: "border-white/10 bg-white/[0.04] text-slate-300",
  cyan: "border-cyan-400/30 bg-cyan-400/10 text-cyan-200",
  orange: "border-orange-400/40 bg-orange-400/10 text-orange-200",
  red: "border-rose-500/40 bg-rose-500/10 text-rose-200",
  green: "border-emerald-400/35 bg-emerald-400/10 text-emerald-200",
  violet: "border-violet-400/40 bg-violet-400/10 text-violet-200",
  amber: "border-amber-300/35 bg-amber-300/10 text-amber-100",
};

export function Chip({
  tone = "neutral",
  mono = false,
  className,
  children,
  title,
  onClick,
  active,
}: {
  tone?: Tone;
  mono?: boolean;
  className?: string;
  children: ReactNode;
  title?: string;
  onClick?: () => void;
  active?: boolean;
}) {
  const cls = cx(
    "inline-flex items-center gap-1 rounded-md border px-1.5 py-[3px] text-[11px] leading-none whitespace-nowrap",
    mono && "font-mono tracking-tight",
    TONE[tone],
    onClick && "cursor-pointer transition hover:brightness-125 focus-visible:outline focus-visible:outline-1 focus-visible:outline-cyan-300",
    active && "ring-1 ring-white/40",
    className,
  );
  if (onClick) {
    return (
      <button type="button" title={title} onClick={onClick} className={cls}>
        {children}
      </button>
    );
  }
  return (
    <span title={title} className={cls}>
      {children}
    </span>
  );
}

export function Dot({ tone = "cyan", live = false, className }: { tone?: Tone; live?: boolean; className?: string }) {
  const color: Record<Tone, string> = {
    neutral: "bg-slate-400 text-slate-400",
    cyan: "bg-cyan-300 text-cyan-300",
    orange: "bg-orange-400 text-orange-400",
    red: "bg-rose-500 text-rose-500",
    green: "bg-emerald-400 text-emerald-400",
    violet: "bg-violet-400 text-violet-400",
    amber: "bg-amber-300 text-amber-300",
  };
  return <span className={cx("inline-block h-1.5 w-1.5 shrink-0 rounded-full", color[tone], live && "live-dot", className)} />;
}

// ------------------------------------------------------------------ buttons

type BtnVariant = "primary" | "ghost" | "danger" | "approve" | "deny" | "subtle";

const BTN: Record<BtnVariant, string> = {
  primary:
    "bg-[#8fe4b5] text-[#102017] hover:bg-[#b0f3cc]",
  ghost: "border border-white/12 bg-white/[0.04] text-slate-200 hover:bg-white/[0.08] hover:border-white/20",
  subtle: "text-slate-300 hover:bg-white/[0.06] hover:text-white",
  danger:
    "bg-rose-600 text-white hover:bg-rose-500 shadow-[0_0_0_1px_rgba(244,63,94,0.6),0_8px_30px_-8px_rgba(244,63,94,0.7)]",
  approve: "bg-emerald-400 text-slate-950 hover:bg-emerald-300 shadow-[0_8px_30px_-10px_rgba(52,211,153,0.8)]",
  deny: "border border-rose-500/50 bg-rose-500/10 text-rose-100 hover:bg-rose-500/20",
};

export function Button({
  variant = "ghost",
  size = "md",
  className,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: BtnVariant; size?: "sm" | "md" | "lg" }) {
  const sz = size === "sm" ? "h-7 px-2.5 text-xs" : size === "lg" ? "h-12 px-6 text-[15px]" : "h-8 px-3 text-[13px]";
  return (
    <button
      type="button"
      {...rest}
      className={cx(
        "inline-flex items-center justify-center gap-1.5 rounded-sm font-medium whitespace-nowrap transition duration-150 select-none",
        "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-300",
        "disabled:cursor-not-allowed disabled:opacity-40 disabled:shadow-none",
        sz,
        BTN[variant],
        className,
      )}
    >
      {children}
    </button>
  );
}

// ------------------------------------------------------------------ icons

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 14, ...p }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.8,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    ...p,
  };
}

export const Icon = {
  Shield: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6L12 3Z" />
    </svg>
  ),
  ShieldCheck: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6L12 3Z" />
      <path d="m8.8 12 2.2 2.2 4.4-4.4" />
    </svg>
  ),
  Bolt: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />
    </svg>
  ),
  Play: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M7 4.5v15l12-7.5-12-7.5Z" fill="currentColor" stroke="none" />
    </svg>
  ),
  Pause: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M8 5v14M16 5v14" strokeWidth={2.6} />
    </svg>
  ),
  Octagon: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M8.2 2.5h7.6l5.7 5.7v7.6l-5.7 5.7H8.2l-5.7-5.7V8.2l5.7-5.7Z" />
      <path d="M9 12h6" strokeWidth={2.4} />
    </svg>
  ),
  Thermo: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z" />
    </svg>
  ),
  Check: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="m5 12.5 4.5 4.5L19 7.5" strokeWidth={2.2} />
    </svg>
  ),
  X: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M6 6l12 12M18 6 6 18" strokeWidth={2.2} />
    </svg>
  ),
  Alert: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M12 3 2.5 20h19L12 3Z" />
      <path d="M12 10v4.5M12 17.5v.01" strokeWidth={2.2} />
    </svg>
  ),
  Clock: (p: IconProps) => (
    <svg {...base(p)}>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </svg>
  ),
  External: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
    </svg>
  ),
  Undo: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M9 14 4 9l5-5" />
      <path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" />
    </svg>
  ),
  User: (p: IconProps) => (
    <svg {...base(p)}>
      <circle cx="12" cy="8" r="3.6" />
      <path d="M4.5 20c1.2-3.6 4-5.4 7.5-5.4s6.3 1.8 7.5 5.4" />
    </svg>
  ),
  Sparkle: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" />
    </svg>
  ),
  Search: (p: IconProps) => (
    <svg {...base(p)}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4.5 4.5" />
    </svg>
  ),
  Chain: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </svg>
  ),
  Message: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M4 5h16v11H9l-5 4V5Z" />
    </svg>
  ),
  Gauge: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M4 17a8 8 0 1 1 16 0" />
      <path d="m12 17 4-6" />
    </svg>
  ),
  Plan: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M8 6h12M8 12h12M8 18h12" />
      <circle cx="4" cy="6" r="1" fill="currentColor" />
      <circle cx="4" cy="12" r="1" fill="currentColor" />
      <circle cx="4" cy="18" r="1" fill="currentColor" />
    </svg>
  ),
  Inbox: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M3 13h5l1.5 3h5L16 13h5" />
      <path d="M5 5h14l2 8v6H3v-6l2-8Z" />
    </svg>
  ),
  ChevronRight: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="m9 6 6 6-6 6" />
    </svg>
  ),
  ChevronDown: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="m6 9 6 6 6-6" />
    </svg>
  ),
  Wifi: (p: IconProps) => (
    <svg {...base(p)}>
      <path d="M2.5 9a14 14 0 0 1 19 0M6 12.5a9 9 0 0 1 12 0M9.5 16a4 4 0 0 1 5 0" />
      <circle cx="12" cy="19.2" r="0.6" fill="currentColor" />
    </svg>
  ),
};

/** Section label used in cards and drawers. */
export function Label({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("text-[10px] font-medium tracking-[0.14em] text-slate-500 uppercase", className)}>{children}</div>;
}
