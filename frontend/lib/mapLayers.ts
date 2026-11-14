/* Mapbox layer and source definitions for CityMap and IntroMap (W10).
 * Pure data + small helpers: no React, no mapbox-gl runtime import (safe to import during SSR). */

import type { Feature, FeatureCollection, Geometry, Point } from "geojson";
import type { LayerSpecification, Map as MapboxMap } from "mapbox-gl";
import type { MapBuffers, Message, SectionState, StaticWorld } from "./types";

export const MAPBOX_TOKEN = process.env.NEXT_PUBLIC_MAPBOX_TOKEN || "";
export const DARK_STYLE = "mapbox://styles/mapbox/dark-v11";

/** Downtown Toronto (King & Bay) and the whole-city view used by CityMap. */
export const DOWNTOWN: [number, number] = [-79.3806, 43.6487];
export const CITY_CENTER: [number, number] = [-79.41, 43.705];
export const CITY_ZOOM = 10.2;

export const SRC = {
  sections: "cd-sections",
  sectionLabels: "cd-section-labels",
  households: "cd-households",
  facilities: "cd-facilities",
  featured: "cd-featured",
} as const;

export const LYR = {
  buildings: "cd-3d-buildings",
  sectionFill: "cd-section-fill",
  sectionLine: "cd-section-line",
  sectionSelected: "cd-section-selected",
  sectionShield: "cd-section-shield",
  sectionLabel: "cd-section-label",
  households: "cd-households",
  facilities: "cd-facilities",
  coolSpaces: "cd-cool-spaces",
  featured: "cd-featured",
} as const;

export const ICON = { shield: "cd-shield", cool: "cd-cool", pin: "cd-pin", pinUntrusted: "cd-pin-orange" } as const;

/** Flag bits in the map message (CONTRACTS.md encode_map). */
export const FLAG = { powerOff: 1, overridden: 2, precool: 4, setback: 8, vulnerable: 16 } as const;

/** Indoor-temperature colour ramp: 22 °C blue -> 30 °C red. */
export const TEMP_STOPS: [number, string][] = [
  [22, "#2563eb"],
  [24, "#06b6d4"],
  [26, "#facc15"],
  [28, "#f97316"],
  [30, "#dc2626"],
];

export function tempColor(t: number | null | undefined): string {
  if (t == null || !Number.isFinite(t)) return "#64748b";
  if (t <= TEMP_STOPS[0][0]) return TEMP_STOPS[0][1];
  for (let i = 1; i < TEMP_STOPS.length; i++) {
    if (t <= TEMP_STOPS[i][0]) return TEMP_STOPS[i][1];
  }
  return TEMP_STOPS[TEMP_STOPS.length - 1][1];
}

/* ------------------------------------------------------------------ layers */

/** 3D building extrusions from the Mapbox Streets composite source (present in dark-v11). */
export function buildingsLayer(opacity: number, color = "#1f2937"): LayerSpecification {
  return {
    id: LYR.buildings,
    type: "fill-extrusion",
    source: "composite",
    "source-layer": "building",
    filter: ["==", ["get", "extrude"], "true"],
    minzoom: 12,
    paint: {
      "fill-extrusion-color": [
        "interpolate",
        ["linear"],
        ["get", "height"],
        0,
        color,
        120,
        "#334155",
        300,
        "#475569",
      ],
      "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"], 12, 0, 13.5, ["get", "height"]],
      "fill-extrusion-base": ["interpolate", ["linear"], ["zoom"], 12, 0, 13.5, ["get", "min_height"]],
      "fill-extrusion-opacity": opacity,
    },
  } as LayerSpecification;
}

export const sectionFillLayer: LayerSpecification = {
  id: LYR.sectionFill,
  type: "fill",
  source: SRC.sections,
  paint: {
    "fill-color": [
      "case",
      ["boolean", ["get", "cut"], false],
      "#020304",
      ["boolean", ["get", "protected"], false],
      "#0ea5e9",
      "#3b82f6",
    ],
    "fill-opacity": [
      "case",
      ["boolean", ["get", "cut"], false],
      0.72,
      ["boolean", ["get", "protected"], false],
      0.04,
      0.07,
    ],
  },
} as LayerSpecification;

export const sectionLineLayer: LayerSpecification = {
  id: LYR.sectionLine,
  type: "line",
  source: SRC.sections,
  paint: {
    "line-color": [
      "case",
      ["boolean", ["get", "cut"], false],
      "#ef4444",
      ["boolean", ["get", "protected"], false],
      "#38bdf8",
      "#64748b",
    ],
    "line-width": [
      "case",
      ["boolean", ["get", "cut"], false],
      2,
      ["boolean", ["get", "protected"], false],
      2.2,
      0.8,
    ],
    "line-opacity": ["case", ["boolean", ["get", "protected"], false], 0.95, 0.7],
  },
} as LayerSpecification;

export const sectionSelectedLayer: LayerSpecification = {
  id: LYR.sectionSelected,
  type: "line",
  source: SRC.sections,
  filter: ["==", ["get", "id"], "__none__"],
  paint: { "line-color": "#f8fafc", "line-width": 3.2, "line-opacity": 0.95 },
} as LayerSpecification;

export const sectionShieldLayer: LayerSpecification = {
  id: LYR.sectionShield,
  type: "symbol",
  source: SRC.sectionLabels,
  filter: ["==", ["get", "protected"], true],
  layout: {
    "icon-image": ICON.shield,
    "icon-size": ["interpolate", ["linear"], ["zoom"], 9, 0.55, 13, 0.9],
    "icon-allow-overlap": true,
    "icon-offset": [0, -18],
  },
} as LayerSpecification;

export const sectionLabelLayer: LayerSpecification = {
  id: LYR.sectionLabel,
  type: "symbol",
  source: SRC.sectionLabels,
  minzoom: 10.5,
  layout: {
    "text-field": ["get", "id"],
    "text-size": 11,
    "text-font": ["DIN Pro Medium", "Arial Unicode MS Regular"],
    "text-allow-overlap": false,
  },
  paint: {
    "text-color": ["case", ["boolean", ["get", "cut"], false], "#fca5a5", "#cbd5e1"],
    "text-halo-color": "#0b0f17",
    "text-halo-width": 1.2,
  },
} as LayerSpecification;

/** Households coloured by indoor temperature; ring when overridden; dimmed when power is off. */
export const householdsLayer: LayerSpecification = {
  id: LYR.households,
  type: "circle",
  source: SRC.households,
  paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 1.4, 11, 2.4, 13, 4, 15, 6],
    "circle-color": [
      "case",
      ["==", ["get", "t"], null],
      "#64748b",
      ["interpolate", ["linear"], ["get", "t"], ...TEMP_STOPS.flat()],
    ],
    "circle-opacity": ["case", ["boolean", ["get", "off"], false], 0.28, 0.88],
    "circle-stroke-width": ["case", ["boolean", ["get", "ov"], false], 1.4, 0],
    "circle-stroke-color": "#fbbf24",
    "circle-stroke-opacity": ["case", ["boolean", ["get", "off"], false], 0.4, 0.95],
    "circle-emissive-strength": 1,
  },
} as LayerSpecification;

/** Critical facilities and pumping stations: small neutral markers. */
export const facilitiesLayer: LayerSpecification = {
  id: LYR.facilities,
  type: "circle",
  source: SRC.facilities,
  filter: ["!=", ["get", "kind"], "cool_space"],
  paint: {
    "circle-radius": ["interpolate", ["linear"], ["zoom"], 9, 3, 13, 6],
    "circle-color": ["match", ["get", "kind"], "pumping", "#a78bfa", "#e2e8f0"],
    "circle-stroke-color": "#0b0f17",
    "circle-stroke-width": 1.5,
  },
} as LayerSpecification;

export const coolSpacesLayer: LayerSpecification = {
  id: LYR.coolSpaces,
  type: "symbol",
  source: SRC.facilities,
  filter: ["==", ["get", "kind"], "cool_space"],
  layout: {
    "icon-image": ICON.cool,
    // ["zoom"] must be the input of a top-level interpolate/step, so the case goes inside the stops.
    "icon-size": [
      "interpolate",
      ["linear"],
      ["zoom"],
      9,
      ["case", ["boolean", ["get", "designated"], false], 0.5, 0.3],
      13,
      ["case", ["boolean", ["get", "designated"], false], 0.85, 0.55],
    ],
    "icon-allow-overlap": true,
    "symbol-sort-key": ["case", ["boolean", ["get", "designated"], false], 1, 2],
  },
  paint: { "icon-opacity": ["case", ["boolean", ["get", "designated"], false], 1, 0.6] },
} as LayerSpecification;

export const featuredLayer: LayerSpecification = {
  id: LYR.featured,
  type: "symbol",
  source: SRC.featured,
  layout: {
    "icon-image": ["case", ["boolean", ["get", "untrusted"], false], ICON.pinUntrusted, ICON.pin],
    "icon-size": ["case", ["boolean", ["get", "selected"], false], 1.05, 0.75],
    "icon-anchor": "bottom",
    "icon-allow-overlap": true,
    "text-field": ["step", ["zoom"], "", 12, ["get", "name"]],
    "text-size": 11,
    "text-offset": [0, 0.4],
    "text-anchor": "top",
    "text-optional": true,
    "text-font": ["DIN Pro Medium", "Arial Unicode MS Regular"],
  },
  paint: { "text-color": "#e2e8f0", "text-halo-color": "#0b0f17", "text-halo-width": 1.2 },
} as LayerSpecification;

/* ------------------------------------------------------------------ icons */

type Painter = (ctx: CanvasRenderingContext2D, s: number) => void;

function makeIcon(size: number, paint: Painter): { width: number; height: number; data: Uint8Array } | null {
  if (typeof document === "undefined") return null;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d");
  if (!ctx) return null;
  paint(ctx, size);
  const img = ctx.getImageData(0, 0, size, size);
  return { width: size, height: size, data: new Uint8Array(img.data.buffer) };
}

const shieldPainter: Painter = (c, s) => {
  c.beginPath();
  c.moveTo(s * 0.5, s * 0.06);
  c.lineTo(s * 0.88, s * 0.2);
  c.quadraticCurveTo(s * 0.86, s * 0.7, s * 0.5, s * 0.95);
  c.quadraticCurveTo(s * 0.14, s * 0.7, s * 0.12, s * 0.2);
  c.closePath();
  c.fillStyle = "#0c4a6e";
  c.fill();
  c.lineWidth = s * 0.07;
  c.strokeStyle = "#38bdf8";
  c.stroke();
  c.beginPath();
  c.moveTo(s * 0.32, s * 0.5);
  c.lineTo(s * 0.46, s * 0.64);
  c.lineTo(s * 0.7, s * 0.36);
  c.lineWidth = s * 0.08;
  c.strokeStyle = "#e0f2fe";
  c.lineCap = "round";
  c.lineJoin = "round";
  c.stroke();
};

const coolPainter: Painter = (c, s) => {
  const r = s / 2;
  c.beginPath();
  c.arc(r, r, r * 0.86, 0, Math.PI * 2);
  c.fillStyle = "#083344";
  c.fill();
  c.lineWidth = s * 0.08;
  c.strokeStyle = "#22d3ee";
  c.stroke();
  // Snowflake: three crossing strokes.
  c.strokeStyle = "#cffafe";
  c.lineWidth = s * 0.07;
  c.lineCap = "round";
  for (let k = 0; k < 3; k++) {
    const a = (k * Math.PI) / 3;
    const dx = Math.cos(a) * r * 0.5;
    const dy = Math.sin(a) * r * 0.5;
    c.beginPath();
    c.moveTo(r - dx, r - dy);
    c.lineTo(r + dx, r + dy);
    c.stroke();
  }
};

function pinPainter(fill: string): Painter {
  return (c, s) => {
    const cx = s / 2;
    const r = s * 0.3;
    c.beginPath();
    c.arc(cx, r + s * 0.06, r, Math.PI, 0);
    c.quadraticCurveTo(cx + r, r + s * 0.4, cx, s * 0.96);
    c.quadraticCurveTo(cx - r, r + s * 0.4, cx - r, r + s * 0.06);
    c.closePath();
    c.fillStyle = fill;
    c.fill();
    c.lineWidth = s * 0.06;
    c.strokeStyle = "#0b0f17";
    c.stroke();
    c.beginPath();
    c.arc(cx, r + s * 0.06, r * 0.42, 0, Math.PI * 2);
    c.fillStyle = "#0b0f17";
    c.fill();
  };
}

/** Registers the canvas-drawn icons (no external sprite needed). Safe to call again after a style reload. */
export function addMapIcons(map: MapboxMap): void {
  const icons: [string, number, Painter][] = [
    [ICON.shield, 40, shieldPainter],
    [ICON.cool, 36, coolPainter],
    [ICON.pin, 40, pinPainter("#f8fafc")],
    [ICON.pinUntrusted, 40, pinPainter("#fb923c")],
  ];
  for (const [name, size, paint] of icons) {
    if (map.hasImage(name)) continue;
    const img = makeIcon(size, paint);
    if (img) map.addImage(name, img, { pixelRatio: 2 });
  }
}

/* ------------------------------------------------------------------ data helpers */

const EMPTY_FC: FeatureCollection = { type: "FeatureCollection", features: [] };
export function emptyFC(): FeatureCollection {
  return { ...EMPTY_FC, features: [] };
}

/** Section id from a feature, tolerating {id}, {_id} or feature.id. */
export function sectionIdOf(f: Feature): string {
  const p = (f.properties || {}) as Record<string, unknown>;
  return String(p.id ?? p._id ?? f.id ?? "");
}

export interface SectionInfo {
  id: string;
  households: number;
  vulnerable: number;
  income_quintile: number | null;
  protected: boolean;
  protected_reasons: string[];
  centroid: [number, number] | null;
}

function ringCentroid(g: Geometry | null | undefined): [number, number] | null {
  if (!g) return null;
  let ring: number[][] | undefined;
  if (g.type === "Polygon") ring = g.coordinates[0];
  else if (g.type === "MultiPolygon") {
    let best = -1;
    for (const poly of g.coordinates) {
      if (poly[0] && poly[0].length > best) {
        best = poly[0].length;
        ring = poly[0];
      }
    }
  }
  if (!ring || ring.length === 0) return null;
  let x = 0;
  let y = 0;
  for (const p of ring) {
    x += p[0];
    y += p[1];
  }
  return [x / ring.length, y / ring.length];
}

/** Static per-section info, from the /world/static sections FeatureCollection. */
export function sectionInfos(sw: StaticWorld | null | undefined): Map<string, SectionInfo> {
  const out = new Map<string, SectionInfo>();
  for (const f of sw?.sections?.features ?? []) {
    const p = (f.properties || {}) as Record<string, unknown>;
    const id = sectionIdOf(f);
    if (!id) continue;
    const c = p.centroid as Point | [number, number] | undefined;
    let centroid: [number, number] | null = null;
    if (Array.isArray(c) && c.length >= 2) centroid = [Number(c[0]), Number(c[1])];
    else if (c && typeof c === "object" && Array.isArray((c as Point).coordinates))
      centroid = [(c as Point).coordinates[0], (c as Point).coordinates[1]];
    out.set(id, {
      id,
      households: Number(p.households ?? 0),
      vulnerable: Number(p.vulnerable ?? 0),
      income_quintile: p.income_quintile == null ? null : Number(p.income_quintile),
      protected: Boolean(p.protected),
      protected_reasons: Array.isArray(p.protected_reasons) ? (p.protected_reasons as unknown[]).map(String) : [],
      centroid: centroid ?? ringCentroid(f.geometry),
    });
  }
  return out;
}

/** Normalises the store's `sections` slice (array or map) into id -> state. */
export function sectionStateMap(sections: unknown): Map<string, SectionState> {
  const out = new Map<string, SectionState>();
  if (!sections) return out;
  if (Array.isArray(sections)) {
    for (const s of sections as SectionState[]) if (s && s.id) out.set(s.id, s);
  } else if (sections instanceof Map) {
    for (const [k, v] of sections as Map<string, SectionState>) out.set(k, { ...v, id: v?.id ?? k });
  } else if (typeof sections === "object") {
    for (const [k, v] of Object.entries(sections as Record<string, SectionState>)) {
      if (v && typeof v === "object") out.set(k, { ...v, id: v.id ?? k });
    }
  }
  return out;
}

/** Sections FeatureCollection with live state merged into properties (24 features: cheap to rebuild). */
export function buildSectionsFC(sw: StaticWorld | null | undefined, states: Map<string, SectionState>): FeatureCollection {
  const feats: Feature[] = [];
  for (const f of sw?.sections?.features ?? []) {
    const id = sectionIdOf(f);
    const p = (f.properties || {}) as Record<string, unknown>;
    const st = states.get(id);
    feats.push({
      type: "Feature",
      id: feats.length,
      geometry: f.geometry,
      properties: {
        id,
        households: p.households ?? 0,
        vulnerable: p.vulnerable ?? 0,
        income_quintile: p.income_quintile ?? null,
        protected: Boolean(st?.protected ?? p.protected),
        cut: Boolean(st?.cut),
        cut_minutes_today: Number(st?.cut_minutes_today ?? 0),
      },
    });
  }
  return { type: "FeatureCollection", features: feats };
}

export function buildSectionLabelsFC(infos: Map<string, SectionInfo>, states: Map<string, SectionState>): FeatureCollection {
  const feats: Feature[] = [];
  for (const s of infos.values()) {
    if (!s.centroid) continue;
    const st = states.get(s.id);
    feats.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: s.centroid },
      properties: { id: s.id, protected: Boolean(st?.protected ?? s.protected), cut: Boolean(st?.cut) },
    });
  }
  return { type: "FeatureCollection", features: feats };
}

/** Household point features in fixed order (index == household idx). Built once per static world. */
export function buildHouseholdsFC(sw: StaticWorld | null | undefined): FeatureCollection<Point> {
  const hh = sw?.households;
  const n = hh?.lon?.length ?? 0;
  const features: Feature<Point>[] = new Array(n);
  for (let i = 0; i < n; i++) {
    features[i] = {
      type: "Feature",
      id: i,
      geometry: { type: "Point", coordinates: [hh!.lon[i], hh!.lat[i]] },
      properties: { i, t: null, off: false, ov: false, vul: Boolean(hh!.vulnerable?.[i]) },
    };
  }
  return { type: "FeatureCollection", features };
}

/** Writes the latest map buffers into the household features in place. Returns true if anything was applied. */
export function applyMapBuffers(fc: FeatureCollection<Point>, buf: MapBuffers | null | undefined): boolean {
  if (!buf || !buf.temps || buf.temps.length === 0) return false;
  const n = Math.min(fc.features.length, buf.temps.length);
  const flags = buf.flags && buf.flags.length >= n ? buf.flags : null;
  for (let i = 0; i < n; i++) {
    const p = fc.features[i].properties as Record<string, unknown>;
    p.t = buf.temps[i] / 4;
    if (flags) {
      const f = flags[i];
      p.off = (f & FLAG.powerOff) !== 0;
      p.ov = (f & FLAG.overridden) !== 0;
      p.pc = (f & FLAG.precool) !== 0;
      p.sb = (f & FLAG.setback) !== 0;
    }
  }
  return true;
}

export interface ResidentLite {
  id: string;
  name: string;
  household_id: string | null;
  idx: number | null;
  injection_actor: boolean;
  raw: Record<string, unknown>;
}

/** Featured residents joined with their household index (households.featured: {res_id: idx}). */
export function residentList(sw: StaticWorld | null | undefined): ResidentLite[] {
  const featured = sw?.households?.featured ?? {};
  const ids = sw?.households?.ids ?? [];
  const out: ResidentLite[] = [];
  const seen = new Set<string>();
  for (const r of sw?.residents ?? []) {
    const id = String(r._id ?? r.id ?? "");
    if (!id) continue;
    seen.add(id);
    const hhId = r.household_id ? String(r.household_id) : null;
    let idx: number | null = featured[id] ?? null;
    if (idx == null && hhId) {
      const j = ids.indexOf(hhId);
      if (j >= 0) idx = j;
    }
    out.push({ id, name: String(r.name ?? id), household_id: hhId ?? (idx != null ? ids[idx] ?? null : null), idx, injection_actor: Boolean(r.injection_actor), raw: r });
  }
  for (const [id, idx] of Object.entries(featured)) {
    if (seen.has(id)) continue;
    out.push({ id, name: id, household_id: ids[idx] ?? null, idx, injection_actor: false, raw: {} });
  }
  return out;
}

/** Location of a resident: their household point, else the resident doc's own location. */
export function residentLngLat(r: ResidentLite, sw: StaticWorld | null | undefined): [number, number] | null {
  const hh = sw?.households;
  if (r.idx != null && hh && hh.lon[r.idx] != null) return [hh.lon[r.idx], hh.lat[r.idx]];
  const loc = r.raw.location as Point | undefined;
  if (loc && Array.isArray(loc.coordinates)) return [loc.coordinates[0], loc.coordinates[1]];
  return null;
}

/** Normalises the store's `threads` slice into a flat list for one thread key. */
export function threadMessages(threads: unknown, keys: (string | null | undefined)[]): Message[] {
  if (!threads || typeof threads !== "object") return [];
  const wanted = keys.filter((k): k is string => Boolean(k));
  const out: Message[] = [];
  const seen = new Set<string>();
  const push = (list: unknown) => {
    if (!Array.isArray(list)) return;
    for (const m of list as Message[]) {
      if (!m || seen.has(m._id)) continue;
      seen.add(m._id);
      out.push(m);
    }
  };
  if (threads instanceof Map) {
    for (const k of wanted) push((threads as Map<string, unknown>).get(k));
  } else {
    const rec = threads as Record<string, unknown>;
    for (const k of wanted) push(rec[k]);
  }
  out.sort((a, b) => String(a.sim_ts ?? "").localeCompare(String(b.sim_ts ?? "")));
  return out;
}

/** Thread-has-untrusted-inbound check, used to colour featured pins orange. */
export function threadHasUntrusted(threads: unknown, keys: (string | null | undefined)[]): boolean {
  return threadMessages(threads, keys).some((m) => m.trust === "untrusted" && m.direction !== "out");
}

export function buildFeaturedFC(
  residents: ResidentLite[],
  sw: StaticWorld | null | undefined,
  threads: unknown,
  selectedId: string | null | undefined,
): FeatureCollection {
  const feats: Feature[] = [];
  for (const r of residents) {
    const ll = residentLngLat(r, sw);
    if (!ll) continue;
    feats.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: ll },
      properties: {
        rid: r.id,
        name: r.name,
        selected: r.id === selectedId,
        untrusted: threadHasUntrusted(threads, [r.household_id, r.id]),
      },
    });
  }
  return { type: "FeatureCollection", features: feats };
}

/** Bounding box of the households (fallback: the sections), for fitBounds. */
export function worldBounds(sw: StaticWorld | null | undefined): [[number, number], [number, number]] | null {
  const lon = sw?.households?.lon ?? [];
  const lat = sw?.households?.lat ?? [];
  if (lon.length === 0) return null;
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  for (let i = 0; i < lon.length; i++) {
    if (lon[i] < w) w = lon[i];
    if (lon[i] > e) e = lon[i];
    if (lat[i] < s) s = lat[i];
    if (lat[i] > n) n = lat[i];
  }
  if (!Number.isFinite(w)) return null;
  return [
    [w, s],
    [e, n],
  ];
}

/** Reads `flags` for one household into named booleans. */
export function decodeFlags(f: number | undefined): { powerOff: boolean; overridden: boolean; precool: boolean; setback: boolean; vulnerable: boolean } {
  const v = f ?? 0;
  return {
    powerOff: (v & FLAG.powerOff) !== 0,
    overridden: (v & FLAG.overridden) !== 0,
    precool: (v & FLAG.precool) !== 0,
    setback: (v & FLAG.setback) !== 0,
    vulnerable: (v & FLAG.vulnerable) !== 0,
  };
}
