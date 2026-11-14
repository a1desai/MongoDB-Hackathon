import test from 'node:test';
import assert from 'node:assert/strict';

import * as search from './place-search.js';

test('local search finds cities and operating regions without a network', () => {
  assert.equal(typeof search.localPlaces, 'function');
  assert.equal(search.localPlaces('  tOrOnTo  ')[0].name, 'Toronto');
  assert.equal(search.localPlaces('GTA')[0].regionId, 'gta');
  assert.equal(search.localPlaces('northwest')[0].kind, 'Region');
  assert.deepEqual(search.localPlaces('zzzzzz'), []);
});

test('empty queries offer useful Ontario cities', () => {
  assert.equal(typeof search.localPlaces, 'function');
  assert.ok(search.localPlaces('').some(place => place.name === 'Toronto'));
  assert.ok(search.localPlaces('').length <= 6);
});

const feature = (name, region, coordinates = [-79.4, 43.7]) => ({
  type: 'Feature', geometry: {type: 'Point', coordinates},
  properties: {mapbox_id: name, name, feature_type: 'neighborhood',
    place_formatted: 'Toronto, Ontario, Canada',
    context: {region: {region_code: region}}},
});

test('geocoder results keep only Ontario places with valid coordinates', () => {
  assert.equal(typeof search.remotePlaces, 'function');
  const places = search.remotePlaces({features: [
    feature('The Annex', 'ON'), feature('Montreal', 'QC'),
    feature('Broken', 'ON', [null, 43]), feature('Outside', 'ON', [0, 0]),
  ]});
  assert.deepEqual(places.map(place => place.name), ['The Annex']);
  assert.deepEqual(places[0].center, [-79.4, 43.7]);
  assert.equal(places[0].kind, 'Neighborhood');
  assert.deepEqual(search.remotePlaces({}), []);
});

test('duplicate city results keep the local match and distinct neighborhoods', () => {
  assert.equal(typeof search.mergePlaces, 'function');
  const local = [{id: 'city-toronto', name: 'Toronto', center: [-79.383, 43.653]}];
  const remote = [{id: 'remote-toronto', name: 'Toronto', center: [-79.4, 43.7]},
    {id: 'annex', name: 'The Annex', center: [-79.4, 43.67]}];
  assert.deepEqual(search.mergePlaces(local, remote).map(place => place.id), ['city-toronto', 'annex']);
});

test('broad areas retain their extent so the camera can show the whole area', () => {
  const county = feature('Waterloo County', 'ON');
  county.properties.feature_type = 'district';
  county.properties.context.region = {region_code_full: 'CA-ON'};
  county.bbox = [-80.9, 43.2, -80.1, 43.7];
  const [place] = search.remotePlaces({features: [county]});
  assert.deepEqual(place.bounds, county.bbox);
  assert.ok(place.zoom < 13);
});
