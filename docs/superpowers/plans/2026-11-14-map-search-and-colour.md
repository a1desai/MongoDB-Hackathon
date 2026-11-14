# Map search and building colour implementation plan

**Goal:** Colour the existing 3D buildings and let users find Ontario cities, neighborhoods, and operating regions.

**Architecture:** Keep React, Vite, and Mapbox. Add a small search component with local suggestions plus debounced Mapbox Geocoding v6 results, restricted to Ontario. Selecting a city/area flies to street scale with a labeled marker; region results retain the region overview. Keep the current interface and add no dependencies.

**User requirements:** More colourful buildings; search specific cities or areas.

**Implementation choices:** Mint, coral, blue, lavender, and gold building colours by height. Search sits in the map toolbar. Local suggestions remain available if the geocoder fails. Clear, loading, empty, and failure states; arrow keys, Enter, and Escape; abort stale requests. Keep searched places separate from synthetic regional readings.

## Tasks

- [x] Write and run failing Node tests for local city/region matching, Ontario-only geocoder normalization, malformed coordinates, and duplicate results.
- [x] Implement `src/place-search.js` for local suggestions, remote result normalization, and result merging.
- [x] Implement `src/PlaceSearch.jsx` for search, request cancellation, keyboard navigation, and selection.
- [x] Integrate map navigation and a labeled marker in `src/main.jsx`; update extrusion colours. Respect the 2D/3D toggle and clear the marker on reset.
- [x] Add responsive search styles in `src/style.css`, preventing the dropdown from overlapping existing panels.
- [x] Run Node tests and production build. Verify city, region, neighborhood, keyboard, empty/error, cancellation, and mobile behavior in the running browser; inspect colourful 3D buildings.

## Review focus

- Older network responses must not replace suggestions for a newer query.
- Quebec and US results must not appear as Ontario places.
- Unknown areas must not inherit unrelated simulated regional readings.
- Mobile search results must remain usable above the region controls.
- Keyboard selection, Escape, clear, and pointer selection must work without accidentally panning the map.


Validation: 5 Node tests passed; production build passed. Browser checks passed for Toronto, Waterloo, The Annex, GTA, Waterloo County, keyboard selection, clearing/reset, error fallback, empty results, stale-request cancellation, and a 390px mobile viewport. Inspected colorful downtown Toronto buildings. The style detector reported only the existing Inter font choice; retained to preserve the current interface. Production build retains the existing large Mapbox bundle warning.
