# Cooldown · Ontario

React, Vite, and Mapbox GL JS map for the Cooldown heatwave simulation.

## Run

Requires Node.js 20.19+ or 22.12+. Follow the [backend setup](../README.md#run-it) to start FastAPI and initialize the simulation world in MongoDB.

```sh
cp .env.example .env.local
npm ci
npm run dev
```

Before starting Vite, set `VITE_MAPBOX_TOKEN` in `.env.local` to your public Mapbox token. The app uses `VITE_API_URL` for FastAPI (default `http://localhost:8000`) and `VITE_CONSOLE_URL` for links to the Next.js run console (default `http://localhost:3000`). Open the URL printed by Vite.

```sh
npm run build
npm run preview
```

For a hosted build, set the `VITE_*` variables before building and allow the site's origin in FastAPI's CORS settings. Restrict the Mapbox token to the origins that serve the map.

## Interface

- Light street map with translucent pastel operating zones, prominent coordinate grid and connecting routes.
- Ontario-wide overview includes Northwest, Far North, Northeast, Eastern Ontario, Central Ontario, Greater Toronto, Southwest and Niagara.
- Select a region to view its illustrative readings. “Explore city in 3D” zooms to buildings; province-scale views naturally do not show individual buildings.
- City dots zoom directly into the city. All Ontario resets the camera.
- Search cities, neighborhoods, and operating regions from the map toolbar. Local city/region suggestions work without a lookup; additional Ontario places use Mapbox Geocoding. Arrow keys browse suggestions, Enter explores, and Escape closes the list.
- 3D buildings use mint, coral, blue, lavender, and gold based on building height. Search a city to see the colours at street scale.
- Layers toggle zones, coordinate grid and connecting routes.
- Start, pause, and resume the governed Toronto simulation, inject a generator trip, and view recent MongoDB ledger entries. Reloading the page reopens the run in the URL.
- **Reset** pauses an active run and returns to the Ontario overview. Its ledger remains available in the run console.
- Open the Next.js run console for approvals, audit, halt, and rollback.

## Source

`src/main.jsx`: React components, map lifecycle, and layers.
`src/useSimulation.js` and `src/simulation-api.js`: run controls, clock, and ledger updates from FastAPI.
`src/data.js`: illustrative zone polygons, city coordinates, connecting lines and readings.
`src/style.css`: responsive interface styles.

`src/PlaceSearch.jsx` and `src/place-search.js`: accessible place search, Ontario filtering, and map search results. Run search logic checks with `node --test src/place-search.test.js`.

## Scope

Mapbox supplies the base geography and requires WebGL and internet access. The eight map regions, intercity routes, and regional readings are illustrative. The backend models a fictional Toronto heatwave, runs agents against mock control systems, and stores its audit ledger in MongoDB.
