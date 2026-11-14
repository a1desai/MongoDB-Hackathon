# MongoDB-Hackathon

Cooldown is an Ontario-wide heatwave simulation frontend built with React, Vite, and Mapbox GL JS. The map includes colourful 3D buildings, operating regions, and city/neighborhood search.

## Run the frontend

Requires Node.js 20.19+ or 22.12+.

```sh
cd cooldown-react
npm ci
```

Copy `.env.example` to `.env.local` and set `VITE_MAPBOX_TOKEN` to your public Mapbox token. The local environment file is ignored by Git.

```sh
npm run dev
```

Open the local URL printed by Vite. See [the frontend README](cooldown-react/README.md) for controls, configuration, and prototype scope.

## Verify

```sh
cd cooldown-react
node --test src/place-search.test.js
npm run build
```

Mapbox provides the base geography. Simulation readings and operating zones are illustrative; this repository currently contains a frontend prototype.
