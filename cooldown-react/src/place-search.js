import {cities, regions} from './data.js';

const normalize = value => value.trim().toLocaleLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
export const ONTARIO_BOUNDS = [-95.2, 41.6, -74.3, 56.9];

const cityPlaces = [...cities, ...regions.map(region => ({name: region.city, coordinates: region.center}))]
  .filter((city, index, all) => all.findIndex(other => other.name === city.name) === index)
  .map(city => ({id: `city-${city.name}`, name: city.name, center: city.coordinates,
    kind: 'City', subtitle: 'Ontario, Canada', zoom: 15.2}));
const regionPlaces = regions.map(region => ({id: `region-${region.id}`, name: region.name,
  center: region.center, kind: 'Region', subtitle: `${region.city} operating area`,
  regionId: region.id, aliases: region.id === 'gta' ? 'gta greater toronto area' : '',
  zoom: region.id === 'gta' ? 10.5 : 7.5}));

export function localPlaces(query) {
  const term = normalize(query);
  if (!term) return cityPlaces.slice(0, 6);
  return [...cityPlaces, ...regionPlaces]
    .filter(place => normalize(`${place.name} ${place.aliases || ''}`).includes(term))
    .sort((a, b) => {
      const score = place => normalize(place.name) === term ? 0 : normalize(place.name).startsWith(term) ? 1 : 2;
      return score(a) - score(b);
    }).slice(0, 6);
}

export function remotePlaces(data) {
  if (!Array.isArray(data?.features)) return [];
  return data.features.flatMap(feature => {
    const properties = feature.properties;
    const region = properties?.context?.region;
    const center = feature.geometry?.coordinates;
    if (!(region?.region_code === 'ON' || region?.region_code_full === 'CA-ON') ||
        !properties.name || feature.geometry?.type !== 'Point' ||
        !Array.isArray(center) || center.length !== 2 || !center.every(Number.isFinite) ||
        center[0] < ONTARIO_BOUNDS[0] || center[0] > ONTARIO_BOUNDS[2] ||
        center[1] < ONTARIO_BOUNDS[1] || center[1] > ONTARIO_BOUNDS[3]) return [];
    const kind = {place: 'City', locality: 'Area', neighborhood: 'Neighborhood', district: 'Area'}[properties.feature_type] || 'Area';
    const box = feature.bbox;
    const bounds = Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) &&
      box[0] >= -180 && box[2] <= 180 && box[1] >= -90 && box[3] <= 90 &&
      box[0] < box[2] && box[1] < box[3] ? box : undefined;
    return [{id: properties.mapbox_id || feature.id || `${properties.name}-${center.join(',')}`,
      name: properties.name, center, bounds, kind,
      zoom: kind === 'Neighborhood' ? 16 : kind === 'Area' ? 12 : 15.2,
      subtitle: properties.place_formatted || 'Ontario, Canada'}];
  });
}

export function mergePlaces(local, remote) {
  const results = [];
  for (const place of [...local, ...remote]) {
    if (!results.some(other => other.id === place.id ||
      (normalize(other.name) === normalize(place.name) &&
       Math.abs(other.center[0] - place.center[0]) < .3 &&
       Math.abs(other.center[1] - place.center[1]) < .3))) results.push(place);
  }
  return results.slice(0, 6);
}
