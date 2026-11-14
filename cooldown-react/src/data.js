// Colours and camera targets for the zones published by IESO.
export const regions = [
  {id: 'northwest', name: 'Northwest', city: 'Thunder Bay', center: [-89.25, 48.38], color: '#aa8be4'},
  {id: 'northeast', name: 'Northeast', city: 'Sudbury', center: [-80.99, 46.49], color: '#87c7cc'},
  {id: 'ottawa', name: 'Ottawa', city: 'Ottawa', center: [-75.70, 45.42], color: '#f0b88c'},
  {id: 'east', name: 'East', city: 'Peterborough', center: [-78.32, 44.30], color: '#b7a1ed'},
  {id: 'gta', name: 'Toronto', city: 'Toronto', center: [-79.383, 43.653], color: '#76b4f2'},
  {id: 'essa', name: 'Essa', city: 'Barrie', center: [-79.69, 44.39], color: '#f3b6c9'},
  {id: 'bruce', name: 'Bruce', city: 'Kincardine', center: [-81.64, 44.18], color: '#f3a78d'},
  {id: 'southwest', name: 'Southwest', city: 'Kitchener', center: [-80.49, 43.45], color: '#91d6ae'},
  {id: 'niagara', name: 'Niagara', city: 'St. Catharines', center: [-79.24, 43.16], color: '#f0d079'},
  {id: 'west', name: 'West', city: 'Windsor', center: [-83.03, 42.31], color: '#89c8b0'},
];

const cityMarkers = [
  {name: 'Toronto', coordinates: [-79.383, 43.653]},
  {name: 'Ottawa', coordinates: [-75.70, 45.42]},
  {name: 'Hamilton', coordinates: [-79.87, 43.25]},
  {name: 'London', coordinates: [-81.25, 42.98]},
  {name: 'Windsor', coordinates: [-83.03, 42.31]},
  {name: 'Sudbury', coordinates: [-80.99, 46.49]},
  {name: 'Timmins', coordinates: [-81.33, 48.48]},
  {name: 'Kenora', coordinates: [-94.49, 49.77]},
  {name: 'Moosonee', coordinates: [-80.65, 51.27]},
];

export const cities = [...cityMarkers, ...regions
  .filter(region => !cityMarkers.some(city => city.name === region.city))
  .map(region => ({name: region.city, coordinates: region.center}))];

export function zoneFeatures(observations) {
  const features = (observations?.zones?.features ?? []).flatMap(feature => {
    const region = regions.find(region => region.name === feature.properties.name);
    if (!region) return [];
    return [{...feature, properties: {...feature.properties, id: region.id, color: region.color}}];
  });
  return {type: 'FeatureCollection', features};
}
