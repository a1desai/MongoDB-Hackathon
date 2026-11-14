# Cooldown · Ontario

Standalone React + Vite + Mapbox GL JS source. No Sites dependency or hosting configuration.

## Run

Requires Node.js 20.19+ or 22.12+.

```sh
npm install
npm run dev
```

Before starting the app, copy `.env.example` to `.env.local` and set `VITE_MAPBOX_TOKEN` to your public Mapbox token. `.env.local` is ignored by Git and is not included in a fresh clone. Open the URL printed by Vite. Vite intentionally exposes this public token in the browser. Restrict the token's allowed URLs to your local and deployment domains through Mapbox.

```sh
npm run build
npm run preview
```

Deploy the resulting `dist/` folder to your preferred static host. Set `VITE_MAPBOX_TOKEN` before building in CI.

## Interface

- Light street map with translucent pastel operating zones, prominent coordinate grid and connecting routes.
- Ontario-wide overview includes Northwest, Far North, Northeast, Eastern Ontario, Central Ontario, Greater Toronto, Southwest and Niagara.
- Select a region to view its illustrative readings. “Explore city in 3D” zooms to buildings; province-scale views naturally do not show individual buildings.
- City dots zoom directly into the city. All Ontario resets the camera.
- Search cities, neighborhoods, and operating regions from the map toolbar. Local city/region suggestions work without a lookup; additional Ontario places use Mapbox Geocoding. Arrow keys browse suggestions, Enter explores, and Escape closes the list.
- 3D buildings use mint, coral, blue, lavender, and gold based on building height. Search a city to see the colours at street scale.
- Layers toggle zones, coordinate grid and connecting routes.
- Start/pause the simulated clock, inject a generator-loss event, inspect local activity, and reset.

## Source

`src/main.jsx`: React components, map lifecycle, layers and browser-local scenario state.
`src/data.js`: illustrative zone polygons, city coordinates, connecting lines and readings.
`src/style.css`: responsive interface styles.

`src/PlaceSearch.jsx` and `src/place-search.js`: accessible place search, Ontario filtering, and map search results. Run search logic checks with `node --test src/place-search.test.js`.

## Scope

Mapbox loads real base geography. Coverage polygons are coarse illustrative operating areas, not official province, municipal or utility boundaries. Routes are fictional intercity connections, not power-line infrastructure. Readings, household counts and activity are synthetic. The Far North polygon is illustrative coverage rather than a precise Ontario outline. The reference influenced the light map and colored polygon treatment; its assets/code are not included.

This is a frontend prototype. There is no MongoDB backend, agent execution, actual electricity control, server-enforced policy gate or persistent audit storage. Connect real API responses and authenticated actions before presenting those as implemented. WebGL and an internet connection are required for the real map.
