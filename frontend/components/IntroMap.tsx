"use client";

/* IntroMap (PLAN §14 "/" intro): a cinematic, non-interactive backdrop. Dark Mapbox style with
 * 3D building extrusions, a flyTo from the whole city into downtown Toronto, then a slow orbit.
 * mapbox-gl loads only in the browser. Without a token it renders an animated gradient instead. */

import "mapbox-gl/dist/mapbox-gl.css";
import { useEffect, useRef, useState } from "react";
import type { Map as MapboxMap } from "mapbox-gl";
import { DARK_STYLE, DOWNTOWN, MAPBOX_TOKEN, buildingsLayer, LYR } from "@/lib/mapLayers";

export interface IntroMapProps {
  className?: string;
  /** Degrees per second for the orbit after the fly-in (default 2.2). */
  orbitSpeed?: number;
}

export default function IntroMap({ className = "", orbitSpeed = 2.2 }: IntroMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!MAPBOX_TOKEN || !containerRef.current) return;
    let cancelled = false;
    let map: MapboxMap | null = null;
    let raf = 0;
    let flyTimer: ReturnType<typeof setTimeout> | null = null;
    const reduceMotion = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

    import("mapbox-gl")
      .then((mod) => {
        const mapboxgl = mod.default;
        if (cancelled || !containerRef.current) return;
        mapboxgl.accessToken = MAPBOX_TOKEN;
        map = new mapboxgl.Map({
          container: containerRef.current,
          style: DARK_STYLE,
          center: [-79.39, 43.7],
          zoom: 10.4,
          pitch: 20,
          bearing: -30,
          interactive: false,
          attributionControl: false,
          antialias: true,
        });
        map.addControl(new mapboxgl.AttributionControl({ compact: true }), "bottom-right");

        map.on("error", (e) => {
          const status = (e as { error?: { status?: number } }).error?.status;
          if (status === 401 || status === 403) setFailed(true);
        });

        map.on("load", () => {
          if (!map || cancelled) return;
          try {
            map.setFog({
              range: [0.8, 9],
              color: "#1a1020",
              "high-color": "#3b1d12",
              "horizon-blend": 0.12,
              "space-color": "#07070b",
              "star-intensity": 0.05,
            });
          } catch {
            /* fog unsupported: fine */
          }
          if (map.getSource("composite")) {
            const firstSymbol = map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
            map.addLayer(buildingsLayer(0.9), firstSymbol);
            // Heat-wave glow: taller towers pick up a warm tint.
            map.setPaintProperty(LYR.buildings, "fill-extrusion-color", [
              "interpolate",
              ["linear"],
              ["get", "height"],
              0,
              "#1c1f2b",
              60,
              "#3a2a2a",
              150,
              "#9a3412",
              300,
              "#f97316",
            ]);
          }
          setLoaded(true);

          if (reduceMotion) {
            map.jumpTo({ center: DOWNTOWN, zoom: 15, pitch: 60, bearing: -20 });
            return;
          }
          flyTimer = setTimeout(() => {
            if (!map || cancelled) return;
            map.flyTo({ center: DOWNTOWN, zoom: 15.2, pitch: 62, bearing: -20, duration: 9000, curve: 1.3, essential: true });
            map.once("moveend", () => {
              if (!map || cancelled) return;
              let last = performance.now();
              const spin = (now: number) => {
                if (!map || cancelled) return;
                const dt = (now - last) / 1000;
                last = now;
                map.setBearing((map.getBearing() + orbitSpeed * dt) % 360);
                raf = requestAnimationFrame(spin);
              };
              raf = requestAnimationFrame(spin);
            });
          }, 600);
        });
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
      if (raf) cancelAnimationFrame(raf);
      if (flyTimer) clearTimeout(flyTimer);
      map?.remove();
    };
  }, [orbitSpeed]);

  const showFallback = !MAPBOX_TOKEN || failed;

  return (
    <div className={`absolute inset-0 overflow-hidden bg-[#07070b] ${className}`} aria-hidden="true">
      {!showFallback && (
        <div
          ref={containerRef}
          className={`absolute inset-0 transition-opacity duration-[1500ms] ${loaded ? "opacity-100" : "opacity-0"}`}
        />
      )}
      {showFallback && <FallbackBackdrop missingToken={!MAPBOX_TOKEN} />}
      {/* vignette so overlaid text stays readable */}
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_center,transparent_35%,rgba(7,7,11,0.85)_100%)]" />
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-1/3 bg-gradient-to-t from-[#07070b] to-transparent" />
    </div>
  );
}

function FallbackBackdrop({ missingToken }: { missingToken: boolean }) {
  return (
    <>
      <style>{`
        @keyframes cd-heat-drift { 0% { transform: translate3d(-4%, -2%, 0) scale(1.05); } 50% { transform: translate3d(3%, 2%, 0) scale(1.12); } 100% { transform: translate3d(-4%, -2%, 0) scale(1.05); } }
        @media (prefers-reduced-motion: reduce) { .cd-heat { animation: none !important; } }
      `}</style>
      <div
        className="cd-heat absolute -inset-[10%]"
        style={{
          background:
            "radial-gradient(circle at 30% 60%, rgba(249,115,22,0.35), transparent 45%), radial-gradient(circle at 70% 35%, rgba(220,38,38,0.25), transparent 40%), radial-gradient(circle at 50% 80%, rgba(37,99,235,0.18), transparent 50%), #07070b",
          animation: "cd-heat-drift 24s ease-in-out infinite",
        }}
      />
      <div
        className="absolute inset-0 opacity-[0.12]"
        style={{
          backgroundImage:
            "linear-gradient(rgba(148,163,184,0.5) 1px, transparent 1px), linear-gradient(90deg, rgba(148,163,184,0.5) 1px, transparent 1px)",
          backgroundSize: "48px 48px",
        }}
      />
      {missingToken && (
        <div className="absolute bottom-3 left-3 z-10 rounded bg-black/60 px-2 py-1 text-[10.5px] text-slate-400">
          Map preview off: NEXT_PUBLIC_MAPBOX_TOKEN is not set
        </div>
      )}
    </>
  );
}
