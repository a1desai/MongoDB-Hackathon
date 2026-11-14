# GridLock · Ontario

React, Vite, and Mapbox GL JS map for the GridLock heatwave simulation.

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

- Start from an empty scenario prompt or choose the built-in Toronto heatwave. Custom scenarios show their assumptions before you run them.
- Light street map with the ten published IESO electrical zones and a coordinate grid.
- Select a zone to view the reported demand for June 24, 2025, initially for 12:00–13:00 Toronto time. During a run, the report follows the simulation hour. The Toronto panel also shows recorded weather and the household estimates summed from the city's 2021 neighbourhood profiles.
- Use the zone panel’s 3D button to see buildings at street scale.
- City dots zoom directly into the city. All Ontario resets the camera.
- Search cities, neighborhoods, and operating regions from the map toolbar. Local city/region suggestions work without a lookup; additional Ontario places use Mapbox Geocoding. Arrow keys browse suggestions, Enter explores, and Escape closes the list.
- 3D buildings use mint, coral, blue, lavender, and gold based on building height. Search a city to see the colours at street scale.
- Layers toggle electrical zones, Ontario hydro lines, the coordinate grid, and simulated Toronto feeders. Select a feeder to inspect its simulated breaker and load.
- Start, pause, and resume the governed Toronto simulation, inject a generator trip, and view recent MongoDB ledger entries. Reloading the page reopens the run in the URL.
- **Reset** pauses an active run and returns to the Ontario overview. Its ledger remains available in the run console.
- Expand a ledger entry for the decision and execution flow, sandbox command parameters, targeted feeders, and verified effects. Use **Inject event** during a run to trigger a generator trip, a temporary intertie loss, or an untrusted resident message.
- Open the Next.js run console for audit, halt, and rollback.

## Source

`src/main.jsx`: React components, map lifecycle, and layers.
`src/useSimulation.js` and `src/simulation-api.js`: run controls, clock, and ledger updates from FastAPI.
`src/data.js`: IESO zone colours and map camera targets. FastAPI serves the zone boundaries and observations at `/world/observations`.
`src/style.css`: responsive interface styles.

`src/PlaceSearch.jsx` and `src/place-search.js`: accessible place search, Ontario filtering, and map search results. Run search logic checks with `node --test src/place-search.test.js`.

## Scope

Mapbox supplies the base geography and requires WebGL and internet access. The City of Toronto census count covers municipal Toronto. The IESO demand reading covers the wider electrical zone. The backend models a fictional Toronto heatwave with synthetic households, feeder sections, and equipment, and stores the simulation's audit ledger in MongoDB.
