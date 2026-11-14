"use client";

/* CityMap (PLAN §14): the consequences view. Dark Mapbox style with low-opacity 3D buildings,
 * feeder sections filled by state, households coloured by indoor temperature, cool spaces and
 * featured-resident pins. mapbox-gl is imported dynamically inside an effect, so it never runs
 * during SSR. The household GeoJSON is built once per static world; each map message only rewrites
 * feature properties and calls setData(). */

import "mapbox-gl/dist/mapbox-gl.css";
import { useEffect, useMemo, useRef, useState } from "react";
import type { FeatureCollection, Point } from "geojson";
import type { GeoJSONSource, Map as MapboxMap, MapMouseEvent, Popup } from "mapbox-gl";
import { useRunStore } from "@/store/useRunStore";
import { hhmm } from "@/lib/format";
import {
  CITY_CENTER,
  CITY_ZOOM,
  DARK_STYLE,
  LYR,
  MAPBOX_TOKEN,
  SRC,
  TEMP_STOPS,
  addMapIcons,
  applyMapBuffers,
  buildFeaturedFC,
  buildHouseholdsFC,
  buildSectionLabelsFC,
  buildSectionsFC,
  buildingsLayer,
  coolSpacesLayer,
  decodeFlags,
  emptyFC,
  facilitiesLayer,
  featuredLayer,
  householdsLayer,
  residentList,
  residentLngLat,
  sectionFillLayer,
  sectionInfos,
  sectionLabelLayer,
  sectionLineLayer,
  sectionSelectedLayer,
  sectionShieldLayer,
  sectionStateMap,
  worldBounds,
} from "@/lib/mapLayers";
import ResidentPanel from "./ResidentPanel";

type MapboxModule = typeof import("mapbox-gl").default;
type PopupRef = { current: Popup | null };

const CLICK_LAYERS = [LYR.featured, LYR.coolSpaces, LYR.facilities, LYR.households, LYR.sectionFill];

function esc(s: unknown): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("en-CA");
}

function setSourceData(map: MapboxMap | null, id: string, data: FeatureCollection): void {
  const src = map?.getSource(id) as GeoJSONSource | undefined;
  if (src) src.setData(data);
}

export interface CityMapProps {
  className?: string;
  /** Render the ResidentPanel as an overlay on the map (default true). */
  showResidentPanel?: boolean;
}

export default function CityMap({ className = "", showResidentPanel = true }: CityMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MapboxMap | null>(null);
  const popupRef = useRef<Popup | null>(null);
  const householdsRef = useRef<FeatureCollection<Point> | null>(null);
  const fittedRef = useRef(false);
  const [ready, setReady] = useState(false);
  const [mapError, setMapError] = useState<string | null>(null);

  const staticWorld = useRunStore((s) => s.staticWorld);
  const mapBuffers = useRunStore((s) => s.mapBuffers);
  const sections = useRunStore((s) => s.sections);
  const threads = useRunStore((s) => s.threads);
  const selectedResidentId = useRunStore((s) => s.selectedResidentId);
  const selectedSectionId = useRunStore((s) => s.selectedSectionId);

  const residents = useMemo(() => residentList(staticWorld), [staticWorld]);
  const sectionStates = useMemo(() => sectionStateMap(sections), [sections]);
  const cutCount = useMemo(() => [...sectionStates.values()].filter((s) => s.cut).length, [sectionStates]);

  /* ---------------------------------------------------------------- map lifecycle */
  useEffect(() => {
    if (!MAPBOX_TOKEN || !containerRef.current) return;
    let cancelled = false;
    let map: MapboxMap | null = null;

    import("mapbox-gl")
      .then((mod) => {
        const mapboxgl = mod.default;
        if (cancelled || !containerRef.current) return;
        mapboxgl.accessToken = MAPBOX_TOKEN;
        map = new mapboxgl.Map({
          container: containerRef.current,
          style: DARK_STYLE,
          center: CITY_CENTER,
          zoom: CITY_ZOOM,
          pitch: 38,
          bearing: -17,
          antialias: true,
        });
        mapRef.current = map;
        map.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), "top-right");

        map.on("error", (e) => {
          const err = (e as { error?: { status?: number; message?: string } }).error;
          if (err?.status === 401 || err?.status === 403) setMapError("Mapbox rejected the token (check NEXT_PUBLIC_MAPBOX_TOKEN).");
          else if (err?.message) console.warn("mapbox:", err.message);
        });

        map.on("load", () => {
          if (!map || cancelled) return;
          setupLayers(map);
          bindInteractions(map, mapboxgl, popupRef);
          setReady(true);
        });
      })
      .catch((err) => {
        console.error("mapbox-gl failed to load", err);
        if (!cancelled) setMapError("The map library failed to load.");
      });

    return () => {
      cancelled = true;
      popupRef.current?.remove();
      popupRef.current = null;
      map?.remove();
      mapRef.current = null;
      householdsRef.current = null;
      fittedRef.current = false;
      setReady(false);
    };
  }, []);

  /* ---------------------------------------------------------------- static data */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !staticWorld) return;
    const fc = buildHouseholdsFC(staticWorld);
    applyMapBuffers(fc, useRunStore.getState().mapBuffers);
    householdsRef.current = fc;
    setSourceData(map, SRC.households, fc);
    setSourceData(map, SRC.facilities, staticWorld.facilities ?? emptyFC());
    if (!fittedRef.current) {
      const b = worldBounds(staticWorld);
      if (b) map.fitBounds(b, { padding: 40, pitch: 38, bearing: -17, duration: 1200 });
      fittedRef.current = true;
    }
  }, [ready, staticWorld]);

  /* ---------------------------------------------------------------- live households */
  useEffect(() => {
    const map = mapRef.current;
    const fc = householdsRef.current;
    if (!ready || !map || !fc || !mapBuffers) return;
    if (applyMapBuffers(fc, mapBuffers)) setSourceData(map, SRC.households, fc);
  }, [ready, mapBuffers]);

  /* ---------------------------------------------------------------- sections */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !staticWorld) return;
    setSourceData(map, SRC.sections, buildSectionsFC(staticWorld, sectionStates));
    setSourceData(map, SRC.sectionLabels, buildSectionLabelsFC(sectionInfos(staticWorld), sectionStates));
  }, [ready, staticWorld, sectionStates]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !map.getLayer(LYR.sectionSelected)) return;
    map.setFilter(LYR.sectionSelected, ["==", ["get", "id"], selectedSectionId ?? "__none__"]);
  }, [ready, selectedSectionId]);

  /* ---------------------------------------------------------------- featured residents */
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !staticWorld) return;
    setSourceData(map, SRC.featured, buildFeaturedFC(residents, staticWorld, threads, selectedResidentId));
  }, [ready, staticWorld, residents, threads, selectedResidentId]);

  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !selectedResidentId) return;
    const r = residents.find((x) => x.id === selectedResidentId);
    const ll = r ? residentLngLat(r, staticWorld) : null;
    if (ll) map.easeTo({ center: ll, zoom: Math.max(map.getZoom(), 13), duration: 900 });
  }, [ready, selectedResidentId, residents, staticWorld]);

  /* ---------------------------------------------------------------- render */
  if (!MAPBOX_TOKEN) {
    return (
      <div className={`relative h-full w-full overflow-hidden bg-[#0b0f17] ${className}`}>
        <MapPlaceholder
          title="Map unavailable"
          body="Set NEXT_PUBLIC_MAPBOX_TOKEN in the repo .env (frontend/.env.local links to it) and restart the dev server. The run, console and approvals keep working without the map."
        />
        {showResidentPanel && <ResidentPanel className="absolute right-3 top-3 bottom-3 z-20" />}
      </div>
    );
  }

  return (
    <div className={`relative h-full w-full overflow-hidden bg-[#0b0f17] ${className}`}>
      <div ref={containerRef} className="absolute inset-0" />
      <style>{POPUP_CSS}</style>

      {!staticWorld && !mapError && (
        <div className="pointer-events-none absolute left-3 top-3 z-10 rounded-md bg-black/60 px-3 py-1.5 text-xs text-slate-300">
          Loading city…
        </div>
      )}
      {staticWorld && (
        <div className="pointer-events-none absolute left-3 top-3 z-10 flex gap-2 text-[11px]">
          <span className="rounded-md bg-black/60 px-2 py-1 text-slate-300">
            {mapBuffers ? `Homes @ ${hhmm(mapBuffers.simTs)}` : "Waiting for first map update"}
          </span>
          {cutCount > 0 && (
            <span className="rounded-md bg-red-950/80 px-2 py-1 font-semibold text-red-200">
              {cutCount} section{cutCount === 1 ? "" : "s"} cut
            </span>
          )}
        </div>
      )}
      {mapError && <MapPlaceholder title="Map error" body={mapError} />}

      <Legend />
      {showResidentPanel && <ResidentPanel className="absolute right-3 top-14 bottom-3 z-20" />}
    </div>
  );
}

function setupLayers(map: MapboxMap) {
  addMapIcons(map);
  if (map.getSource("composite") && !map.getLayer(LYR.buildings)) {
    // Put buildings under the style's label layers so road names stay readable.
    const firstSymbol = map.getStyle()?.layers?.find((l) => l.type === "symbol")?.id;
    map.addLayer(buildingsLayer(0.28), firstSymbol);
  }
  const sources = [SRC.sections, SRC.sectionLabels, SRC.households, SRC.facilities, SRC.featured];
  for (const id of sources) {
    if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: emptyFC() });
  }
  for (const layer of [
    sectionFillLayer,
    householdsLayer,
    sectionLineLayer,
    sectionSelectedLayer,
    facilitiesLayer,
    coolSpacesLayer,
    sectionShieldLayer,
    sectionLabelLayer,
    featuredLayer,
  ]) {
    if (!map.getLayer(layer.id)) map.addLayer(layer);
  }
}

function bindInteractions(map: MapboxMap, gl: MapboxModule, popupRef: PopupRef) {
  map.on("mousemove", (e: MapMouseEvent) => {
    const layers = CLICK_LAYERS.filter((l) => l !== LYR.sectionFill && map.getLayer(l));
    const hit = layers.length ? map.queryRenderedFeatures(e.point, { layers }) : [];
    map.getCanvas().style.cursor = hit.length ? "pointer" : "";
  });

  map.on("click", (e: MapMouseEvent) => {
    const layers = CLICK_LAYERS.filter((l) => map.getLayer(l));
    if (!layers.length) return;
    const hits = map.queryRenderedFeatures(e.point, { layers });
    const pick = (layer: string) => hits.find((h) => h.layer?.id === layer);
    const store = useRunStore.getState();

    const pin = pick(LYR.featured);
    if (pin) {
      const rid = String(pin.properties?.rid ?? "");
      if (rid) store.setSelectedResident(rid);
      popupRef.current?.remove();
      return;
    }
    const fac = pick(LYR.coolSpaces) ?? pick(LYR.facilities);
    if (fac) {
      const p = fac.properties ?? {};
      let hours = "";
      try {
        const h = typeof p.hours === "string" ? JSON.parse(p.hours) : p.hours;
        if (h?.open && h?.close) hours = `${h.open}–${h.close}`;
      } catch {
        /* hours not JSON */
      }
      const kind = p.kind === "cool_space" ? (p.designated ? "Designated cool space (extended hours)" : "Cool space") : p.kind === "pumping" ? "Pumping station" : "Critical facility";
      showPopup(map, gl, popupRef, e.lngLat, `<div class="cd-pop-title">${esc(p.name ?? "Facility")}</div><div class="cd-pop-sub">${esc(kind)}${hours ? ` · ${esc(hours)}` : ""}</div>${p.section_id ? `<div class="cd-pop-row"><span>Section</span><b>${esc(p.section_id)}</b></div>` : ""}`);
      return;
    }
    const home = pick(LYR.households);
    const sec = pick(LYR.sectionFill);
    if (sec) {
      const id = String(sec.properties?.id ?? "");
      if (id) store.setSelectedSection(id);
    }
    if (home) {
      const i = Number(home.properties?.i ?? -1);
      showPopup(map, gl, popupRef, e.lngLat, homeHtml(i));
      return;
    }
    if (sec) {
      showPopup(map, gl, popupRef, e.lngLat, sectionHtml(String(sec.properties?.id ?? "")));
      return;
    }
    popupRef.current?.remove();
  });
}

function showPopup(map: MapboxMap, gl: MapboxModule, popupRef: PopupRef, lngLat: { lng: number; lat: number }, html: string) {
  popupRef.current?.remove();
  popupRef.current = new gl.Popup({ closeButton: true, closeOnClick: false, maxWidth: "260px", className: "cd-popup" })
    .setLngLat(lngLat)
    .setHTML(html)
    .addTo(map);
}

function sectionHtml(id: string): string {
  const s = useRunStore.getState();
  const info = sectionInfos(s.staticWorld).get(id);
  const st = sectionStateMap(s.sections).get(id);
  const scale = Number(s.run?.scale ?? 1) || 1;
  const homes = info?.households ?? 0;
  const vul = info?.vulnerable ?? 0;
  const real = (n: number) => (scale > 1 ? `${fmtInt(n * scale)} <small>(${fmtInt(n)} sim)</small>` : fmtInt(n));
  const prot = Boolean(st?.protected ?? info?.protected);
  const state = st?.cut ? '<span class="cd-chip cd-chip-cut">CUT</span>' : prot ? '<span class="cd-chip cd-chip-prot">PROTECTED</span>' : '<span class="cd-chip">energised</span>';
  const reasons = prot && info?.protected_reasons?.length ? `<div class="cd-pop-note">${info.protected_reasons.map(esc).join("<br/>")}</div>` : "";
  return `<div class="cd-pop-title">${esc(id)} ${state}</div>
    <div class="cd-pop-row"><span>Homes</span><b>${real(homes)}</b></div>
    <div class="cd-pop-row"><span>Vulnerable homes</span><b>${real(vul)}</b></div>
    <div class="cd-pop-row"><span>Minutes cut today</span><b>${fmtInt(Number(st?.cut_minutes_today ?? 0))}</b></div>
    <div class="cd-pop-row"><span>Income quintile</span><b>${info?.income_quintile ?? "–"}</b></div>${reasons}`;
}

function homeHtml(i: number): string {
  const s = useRunStore.getState();
  const id = s.staticWorld?.households?.ids?.[i] ?? `#${i}`;
  const t = s.mapBuffers?.temps?.[i];
  const f = decodeFlags(s.mapBuffers?.flags?.[i]);
  const vul = Boolean(s.staticWorld?.households?.vulnerable?.[i]) || f.vulnerable;
  const tags = [
    f.powerOff ? "power off" : "power on",
    f.overridden ? "overridden" : "",
    f.precool ? "pre-cooling" : "",
    f.setback ? "setback" : "",
    vul ? "vulnerable (synthetic flag)" : "",
  ].filter(Boolean);
  return `<div class="cd-pop-title">Home ${esc(id)}</div>
    <div class="cd-pop-row"><span>Indoor</span><b>${t == null ? "–" : `${(t / 4).toFixed(1)} °C`}</b></div>
    <div class="cd-pop-sub">${esc(tags.join(" · "))}</div>`;
}

function MapPlaceholder({ title, body }: { title: string; body: string }) {
  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-[radial-gradient(ellipse_at_center,#13203a_0%,#0b0f17_70%)] p-6">
      <div className="max-w-sm rounded-lg border border-slate-700/70 bg-slate-900/80 p-5 text-center shadow-xl">
        <div className="mb-1 text-sm font-semibold text-slate-100">{title}</div>
        <p className="text-xs leading-relaxed text-slate-400">{body}</p>
      </div>
    </div>
  );
}

function Legend() {
  const gradient = `linear-gradient(90deg, ${TEMP_STOPS.map(([, c]) => c).join(", ")})`;
  return (
    <div className="pointer-events-none absolute bottom-3 left-3 z-10 w-56 rounded-md border border-slate-700/60 bg-black/70 p-2.5 text-[10.5px] text-slate-300 backdrop-blur-sm">
      <div className="mb-1 font-semibold text-slate-200">Indoor temperature</div>
      <div className="h-2 rounded-sm" style={{ background: gradient }} />
      <div className="mt-0.5 flex justify-between text-slate-400">
        <span>22 °C</span>
        <span>26 °C</span>
        <span>30 °C</span>
      </div>
      <div className="mt-2 grid grid-cols-2 gap-x-2 gap-y-1">
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2.5 rounded-full border-[1.5px] border-amber-400 bg-cyan-500" /> overridden
        </span>
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2.5 rounded-full bg-cyan-500/30" /> power off
        </span>
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-3.5 rounded-sm border border-red-500 bg-black" /> section cut
        </span>
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-3.5 rounded-sm border-2 border-sky-400" /> protected
        </span>
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2.5 rounded-full border-2 border-cyan-400 bg-cyan-950" /> cool space
        </span>
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-2.5 w-2 rounded-t-full bg-slate-100" /> resident
        </span>
      </div>
    </div>
  );
}

const POPUP_CSS = `
.cd-popup .mapboxgl-popup-content{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:10px 12px;font:12px/1.4 ui-sans-serif,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.cd-popup .mapboxgl-popup-tip{border-top-color:#0f172a;border-bottom-color:#0f172a}
.cd-popup .mapboxgl-popup-close-button{color:#94a3b8;font-size:16px;padding:0 6px}
.cd-pop-title{font-weight:600;font-size:13px;margin-bottom:4px;display:flex;align-items:center;gap:6px}
.cd-pop-sub{color:#94a3b8;font-size:11px;margin-top:2px}
.cd-pop-row{display:flex;justify-content:space-between;gap:12px;padding:1px 0}
.cd-pop-row span{color:#94a3b8}
.cd-pop-row small{color:#64748b;font-weight:400}
.cd-pop-note{margin-top:6px;color:#7dd3fc;font-size:11px}
.cd-chip{font-size:9.5px;font-weight:700;letter-spacing:.04em;padding:1px 6px;border-radius:9999px;background:#1e293b;color:#cbd5e1}
.cd-chip-cut{background:#450a0a;color:#fecaca}
.cd-chip-prot{background:#082f49;color:#7dd3fc}
`;
