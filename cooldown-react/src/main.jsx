import React, {useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import mapboxgl from 'mapbox-gl';
import {Layers,Activity,Map as MapIcon,MapPin,Play,Pause,Plus,Minus,LocateFixed,X,Zap,ChevronRight,RotateCcw,Box,Globe} from 'lucide-react';
import 'mapbox-gl/dist/mapbox-gl.css';
import './style.css';
import './terminal.css';
import {regions,cities,zoneFeatures} from './data.js';
import PlaceSearch from './PlaceSearch.jsx';
import AgentActivity from './AgentActivity.jsx';
import ScenarioPrompt, {BUILT_IN_SCENARIO} from './ScenarioPrompt.jsx';
import {useSimulation} from './useSimulation.js';
import {simulationApi} from './simulation-api.js';

const scenarioClock = new Intl.DateTimeFormat('en-CA', {
 timeZone: 'America/Toronto', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});
const localHour = new Intl.DateTimeFormat('en-GB', {timeZone: 'America/Toronto', hour: '2-digit', hourCycle: 'h23'});
const IESO_MAP = 'https://www.ieso.ca/localContent/zonal.map/index.html';
const ONTARIO_LINES = 'https://data.ontario.ca/dataset/utility-line';
const EMPTY_FEATURES = {type:'FeatureCollection', features:[]};
const INCIDENTS = [
 ['generator_trip','Generator trip','Bring forward the next scheduled trip, or remove 350 MW of supply.'],
 ['intertie_loss','Intertie loss','Interrupt imported supply. The default is 200 MW for 45 simulated minutes.'],
 ['injection_message','Untrusted resident message','Send a test message that attempts to override policy.'],
];

function runStatus(run) {
 if (!run) return 'Ready';
 if (run.status === 'finished') return 'Finished';
 if (run.status === 'failed') return 'Failed';
 if (run.live === false) return 'Offline';
 if (run.status === 'created') return 'Starting';
 if (run.status === 'paused') return 'Paused';
 if (run.paused_reason === 'approval') return 'Awaiting approval';
 if (run.paused_reason === 'planning') return 'Planning';
 return 'Running';
}

function App() {
 const simulation = useSimulation();
 const [openingPreset, setOpeningPreset] = useState(false);
 const startingTitle = openingPreset && !simulation.runId ? BUILT_IN_SCENARIO.title : null;

 async function start(id, title) {
  setOpeningPreset(id === BUILT_IN_SCENARIO.id);
  try {
   await simulation.start(id, 'governed', title);
  } finally {
   setOpeningPreset(false);
  }
 }

 return <>
  {!simulation.runId && <ScenarioPrompt pending={simulation.pending} runError={simulation.error}
   showPolicies={new URLSearchParams(window.location.search).get('view') === 'policies'}
   hidden={Boolean(startingTitle)} onStart={start}/>}
  {(startingTitle || simulation.runId) && <RunWorkspace simulation={simulation} startingTitle={startingTitle}/>}
 </>;
}

function RunWorkspace({simulation, startingTitle}) {
 const container=useRef(null),map=useRef(null), selectedRef=useRef(null),searchMarker=useRef(null);
 const [searchReset,setSearchReset]=useState(0);
 const [selected,setSelected]=useState(null),[tab,setTab]=useState('activity'),[layers,setLayers]=useState(false),[showZones,setShowZones]=useState(true),[showGrid,setShowGrid]=useState(false),[is3d,set3d]=useState(true),[error,setError]=useState('');
 const [observations, setObservations]=useState(null);
 const [observationError, setObservationError]=useState('');
 const [hydroLines, setHydroLines]=useState(null), [lineError, setLineError]=useState('');
 const [showLines, setShowLines]=useState(true), [showFeeders, setShowFeeders]=useState(true);
 const [feederMap, setFeederMap]=useState(EMPTY_FEATURES), [feederError, setFeederError]=useState('');
 const [feederId, setFeederId]=useState(null);
 const [activityFeeders, setActivityFeeders]=useState([]);
 const [mapLoaded, setMapLoaded]=useState(false);
 const [showIncidents, setShowIncidents]=useState(false);
 const pendingCamera = useRef(null);
 const {run, events, sections, running, pending, consoleUrl}=simulation;
 const loss=run?.contingency_lost_mw;
 const hour = run?.sim_ts ? `${localHour.format(new Date(run.sim_ts))}:00` : '12:00';
 const reportedDemand = selected && observations?.demand_mw[hour]?.[selected.name];
 const recordedTemperature = observations?.temperature_c[hour];
 const censusHouseholds = selected?.id === 'gta' ? observations?.toronto_census_households : null;
 const feeder = feederMap.features.find(feature => feature.properties.id === feederId);
 const feederState = sections.find(state => state.id === feederId);
 const activityOpen = tab === 'activity';
 const focusedFeeders = useMemo(() => feederMap.features.filter(feature =>
  feature.geometry && activityFeeders.includes(feature.properties.id)), [feederMap, activityFeeders]);
 selectedRef.current=setSelected;

 useLayoutEffect(() => {
  map.current?.resize();
 }, [activityOpen]);

 function refreshObservations(signal) {
  setObservationError('');
  return simulationApi.observations(signal).then(setObservations).catch(error => {
   if (!signal?.aborted) setObservationError(error.message);
  });
 }

 useEffect(() => {
  const controller = new AbortController();
  refreshObservations(controller.signal);
  simulationApi.hydroLines(controller.signal).then(setHydroLines).catch(error => {
   if (!controller.signal.aborted) setLineError(error.message);
  });
  simulationApi.world(controller.signal).then(world => setFeederMap(world.sections)).catch(error => {
   if (!controller.signal.aborted) setFeederError(error.message);
  });
  return () => controller.abort();
 }, []);

 useEffect(()=>{
  const token=import.meta.env.VITE_MAPBOX_TOKEN;
  if(!token){setError('Add your public Mapbox token to .env.local to load the map.');return;}
  mapboxgl.accessToken=token;
  let m;
  try{m=new mapboxgl.Map({container:container.current,style:'mapbox://styles/mapbox/dark-v11',center:[-79.383,43.69],zoom:10.2,pitch:15,attributionControl:true});map.current=m;}catch(e){setError('This browser could not start WebGL. Open the project in a browser with hardware acceleration enabled.');return;}
  m.on('error',()=>{if(!m.isStyleLoaded())setError('Map connection failed. Check your Mapbox token and its allowed URLs.');});
   m.once('style.load',()=>{
   setError('');
   m.addSource('regions',{type:'geojson',data:zoneFeatures()});
   m.addLayer({id:'zone-fill',type:'fill',source:'regions',paint:{'fill-color':['get','color'],'fill-opacity':.3}});
   m.addLayer({id:'zone-border',type:'line',source:'regions',paint:{'line-color':['get','color'],'line-width':2.5,'line-opacity':.95}});
   const grid={type:'FeatureCollection',features:[]};
   for(let x=-96;x<=-74;x+=.25)grid.features.push({type:'Feature',geometry:{type:'LineString',coordinates:[[x,41.5],[x,57]]}});
   for(let y=41.5;y<=57;y+=.2)grid.features.push({type:'Feature',geometry:{type:'LineString',coordinates:[[-96,y],[-74,y]]}});
   m.addSource('grid',{type:'geojson',data:grid});
   m.addLayer({id:'grid-lines',type:'line',source:'grid',paint:{'line-color':'#547482','line-width':['interpolate',['linear'],['zoom'],4,.7,8,1.1,13,.4],'line-opacity':['interpolate',['linear'],['zoom'],4,.2,7,.32,11,.08]}});
   m.addSource('hydro-lines',{type:'geojson',data:EMPTY_FEATURES});
   m.addLayer({id:'hydro-lines',type:'line',source:'hydro-lines',paint:{
    'line-color':['match',['get','kind'],'Submerged Hydro Line','#80b7ff','#6bcebb'],
    'line-width':['interpolate',['linear'],['zoom'],4,1,8,1.8,12,2.8], 'line-opacity':.85,
   }});
   m.addSource('feeders',{type:'geojson',data:EMPTY_FEATURES});
   m.addLayer({id:'feeder-fill',type:'fill',source:'feeders',minzoom:8,paint:{
    'fill-color':['match',['get','grid_status'],'cut','#c32b51','protected','#7741a2','#008476'],
    'fill-opacity':['case',['boolean',['get','focused'],false],.55,['==',['get','grid_status'],'cut'],.4,.15],
   }});
   m.addLayer({id:'feeder-outline',type:'line',source:'feeders',minzoom:8,paint:{
    'line-color':['case',['boolean',['get','focused'],false],'#9fd5ff',['match',['get','grid_status'],'cut','#f56b84','protected','#b994ed','#6bcebb']],
    'line-width':['case',['boolean',['get','focused'],false],4,1.5],
   }});
   m.addLayer({id:'feeder-id',type:'symbol',source:'feeders',minzoom:10,layout:{'text-field':['get','id'],'text-size':11},paint:{
    'text-color':'#e6f0ea','text-halo-color':'#0b100e','text-halo-width':1.5,
   }});
   const label=m.getStyle().layers.find(l=>l.type==='symbol'&&l.layout?.['text-field']);
   if(m.getSource('composite'))m.addLayer({id:'buildings-3d',type:'fill-extrusion',source:'composite','source-layer':'building',minzoom:12,filter:['==','extrude','true'],paint:{'fill-extrusion-color':['step',['coalesce',['get','height'],5],'#54cfad',12,'#ff987f',25,'#68b6f4',50,'#b491ef',100,'#f8ce63'],'fill-extrusion-height':['coalesce',['get','height'],5],'fill-extrusion-base':['coalesce',['get','min_height'],0],'fill-extrusion-opacity':.96}},label?.id);
   cities.forEach(c=>{const el=document.createElement('button');el.className='city-node';el.setAttribute('aria-label','Explore '+c.name);el.title=c.name;el.onclick=()=>{searchMarker.current?.remove();searchMarker.current=null;setSearchReset(value=>value+1);setFeederId(null);setActivityFeeders([]);const r=regions.find(r=>r.city===c.name);setSelected(r||null);m.flyTo({center:c.coordinates,zoom:15.2,pitch:52,duration:1800});};new mapboxgl.Marker({element:el}).setLngLat(c.coordinates).addTo(m);});
   m.on('click','zone-fill',e=>{
    if(m.queryRenderedFeatures(e.point,{layers:['feeder-fill']}).length)return;
    const r=regions.find(r=>r.id===e.features[0].properties.id);
    selectedRef.current(r);setFeederId(null);setActivityFeeders([]);
   });
   m.on('click','feeder-fill',e=>{setFeederId(e.features[0].properties.id);selectedRef.current(null);setActivityFeeders([]);});
   m.on('mouseenter','zone-fill',()=>m.getCanvas().style.cursor='pointer');m.on('mouseleave','zone-fill',()=>m.getCanvas().style.cursor='');
   pendingCamera.current?.(m);
   pendingCamera.current=null;
   setMapLoaded(true);
  });return()=>{searchMarker.current?.remove();m.remove();};
 },[]);
 useEffect(() => {
  if (mapLoaded) map.current.getSource('regions').setData(zoneFeatures(observations));
 }, [mapLoaded, observations]);

 useEffect(() => {
  if (mapLoaded) map.current.getSource('hydro-lines').setData(hydroLines || EMPTY_FEATURES);
 }, [mapLoaded, hydroLines]);

 useEffect(() => {
  if (!mapLoaded) return;
  const states = new Map(sections.map(state => [state.id, state]));
  map.current.getSource('feeders').setData({...feederMap, features: feederMap.features.map(feature => {
   const {id, protected: isProtected} = feature.properties;
   const state = states.get(id);
   const gridStatus = state?.cut ? 'cut' : (state?.protected ?? isProtected) ? 'protected' : 'normal';
   return {...feature, properties: {...feature.properties, grid_status:gridStatus, focused:id === feederId || activityFeeders.includes(id)}};
  })});
 }, [mapLoaded, feederMap, sections, feederId, activityFeeders]);

 useEffect(() => {
  if (!mapLoaded || !focusedFeeders.length) return;
  const bounds = new mapboxgl.LngLatBounds();
  for (const {geometry, properties} of focusedFeeders) {
   if (geometry.type === 'Polygon') {
    geometry.coordinates[0].forEach(position => bounds.extend(position));
   } else if (geometry.type === 'MultiPolygon') {
    geometry.coordinates.forEach(([outline]) => outline.forEach(position => bounds.extend(position)));
   } else if (properties.centroid) {
    bounds.extend(properties.centroid);
   }
  }
  setSelected(null);
  setFeederId(null);
  setShowFeeders(true);
  set3d(false);
  if (!bounds.isEmpty()) map.current.fitBounds(bounds, {padding:44, maxZoom:12, pitch:0, bearing:0, duration:750});
 }, [mapLoaded, focusedFeeders]);

 useEffect(()=>{if(!mapLoaded)return;[['zone-fill',showZones],['zone-border',showZones],['grid-lines',showGrid],
  ['hydro-lines',showLines],['feeder-fill',showFeeders],['feeder-outline',showFeeders],['feeder-id',showFeeders]]
  .forEach(([id,v])=>{map.current.setLayoutProperty(id,'visibility',v?'visible':'none')})},[mapLoaded,showZones,showGrid,showLines,showFeeders]);
 const clearSearchMarker=()=>{searchMarker.current?.remove();searchMarker.current=null;};
 function moveCamera(move) {
  if (mapLoaded) move(map.current);
  else pendingCamera.current = move;
 }

 const home=()=>{setSelected(null);setFeederId(null);setActivityFeeders([]);clearSearchMarker();setSearchReset(value=>value+1);moveCamera(m=>m.flyTo({center:[-84.4,48.3],zoom:5.1,pitch:is3d?35:0,bearing:-10,duration:1500}))};
 const focus=r=>{setSelected(r);setFeederId(null);setActivityFeeders([]);clearSearchMarker();setSearchReset(value=>value+1);moveCamera(m=>m.flyTo({center:r.center,zoom:r.id==='gta'?10.5:7.5,pitch:is3d?45:0,duration:1600}))};
 function openTab(next) {
  if (next !== 'activity') setActivityFeeders([]);
  setTab(next);
 }
 const explorePlace=place=>{
  clearSearchMarker();
  const region=regions.find(region=>region.id===place.regionId);
  setSelected(region||null);setFeederId(null);openTab('none');setLayers(false);
  const m=map.current;if(!m)return;
  if(!region){
   const label=document.createElement('div');label.className='search-pin';
   const dot=document.createElement('span');dot.className='search-pin-dot';
   const text=document.createElement('strong');text.textContent=place.name;
   label.append(dot,text);label.setAttribute('aria-label',place.name);
   searchMarker.current=new mapboxgl.Marker({element:label,anchor:'bottom'}).setLngLat(place.center).addTo(m);
  }
  if(place.kind==='Area'&&place.bounds){
   m.fitBounds(place.bounds,{padding:{top:150,bottom:130,left:65,right:65},maxZoom:15.2,pitch:is3d?45:0,bearing:-15,duration:1600});
  }else m.flyTo({center:place.center,zoom:place.zoom,pitch:is3d?55:0,bearing:-15,duration:1600});
 };
 async function injectIncident(event) {
  setShowIncidents(false);
  if (await simulation.inject(event)) {
   setTab('activity');
   moveCamera(m=>m.flyTo({center:[-79.383,43.69],zoom:10.2,pitch:15,duration:1300}));
  }
 }

 return <main className="app">
 <header>
  <a className="brand" href="./"><span aria-hidden="true">&gt;_</span>GridLock<span className="slash">/</span><b>run</b></a>
  <div className="header-center"><span className="live-dot"/>{startingTitle || run?.scenario_title || run?.scenario_id || simulation.runId}</div>
  <button className="reset" disabled={pending} onClick={simulation.reset}><RotateCcw size={15}/> New scenario</button>
 </header>
 <div className={activityOpen?'workspace activity-open':'workspace'}>
 {activityOpen&&<AgentActivity key={simulation.runId || 'idle'} simulation={simulation} formatTime={ts=>scenarioClock.format(new Date(ts))}
  onClose={()=>openTab('none')} onSections={setActivityFeeders} startingTitle={startingTitle}/>}
 <section className="canvas" aria-label="Ontario map"><div className="map" ref={container}/>{error&&<div className="map-error"><Globe size={28}/><h2>Connect your Ontario map</h2><p>{error}</p><code>VITE_MAPBOX_TOKEN=pk.…</code></div>}
 <div className="topbar">
  <div className="clock">
   <span className="live-dot"/>
   <b>{run?.sim_ts ? scenarioClock.format(new Date(run.sim_ts)) : 'Toronto'}</b>
   {run?.outdoor_c != null && <span>{run.outdoor_c}°C</span>}
   <span className="clock-divider"/>
   {run?.paused_reason === 'approval' && consoleUrl
    ? <button className="retry-data" onClick={() => setTab('activity')}>Awaiting approval</button>
    : <span>{startingTitle ? 'Starting run' : consoleUrl && !run ? 'Loading run' : runStatus(run)}</span>}
  </div>
  <PlaceSearch onSelect={explorePlace} onClear={clearSearchMarker} resetKey={searchReset}/>
  <button className="province-button" onClick={home}><Globe size={15}/> All Ontario</button>
 </div>
 <nav className="rail" aria-label="Map tools"><button className={tab==='map'?'active':''} aria-label="Map regions" onClick={()=>openTab(tab==='map'?'none':'map')}><MapIcon size={19}/></button><button className={tab==='activity'?'active':''} aria-label="Agent activity" onClick={()=>openTab(tab==='activity'?'none':'activity')}><Activity size={19}/>{events.length>0&&<i/>}</button><button className={layers?'active':''} aria-label="Map layers" onClick={()=>setLayers(!layers)}><Layers size={19}/></button></nav>
 {tab==='map'&&<aside className="panel regions">
  <div className="panel-title"><h1>Ontario electrical zones</h1><button aria-label="Close regions" onClick={()=>setTab('none')}><X size={16}/></button></div>
  {observationError && <p className="intro" role="status">{observationError} <button className="retry-data" onClick={() => refreshObservations()}>Retry</button></p>}
  {!observationError && observations && !observations.zones.features.length && <p className="intro">Zone boundaries unavailable.</p>}
  <div className="region-list">{regions.map(r=><button key={r.id} className={selected?.id===r.id?'region selected':'region'} onClick={()=>focus(r)}><i style={{background:r.color}}/><span>{r.name}</span><ChevronRight size={15}/></button>)}</div>
  <div className="panel-foot"><a href={IESO_MAP} target="_blank" rel="noreferrer">IESO Zonal Map</a></div>
 </aside>}
 {layers&&<div className="panel layer-menu">
  <strong>Map layers</strong>
  {[
   ['Electrical zones',showZones,setShowZones], ['Ontario hydro lines',showLines,setShowLines],
   ['Simulated Toronto feeders',showFeeders,setShowFeeders], ['Coordinate grid',showGrid,setShowGrid],
  ].map(([name,visible,toggle])=><label key={name}><input type="checkbox" checked={visible} onChange={e=>toggle(e.target.checked)}/>{name}</label>)}
  {showLines && <p className="layer-caption">Teal · Hydro line<br/>Blue · Submerged hydro line</p>}
  {showFeeders && <p className="layer-caption">Purple · Protected feeder<br/>Red · Feeder switched off<br/>Blue outline · Selected feeder</p>}
  {(lineError || feederError) && <p className="layer-caption" role="status">{[lineError, feederError].filter(Boolean).join(' ')}</p>}
  <a href={ONTARIO_LINES} target="_blank" rel="noreferrer">Ontario Utility Line data</a>
 </div>}
 {selected&&<aside className="region-detail panel">
  <div className="panel-title"><span className="region-badge" style={{background:selected.color}}>IESO ZONE</span><button onClick={()=>setSelected(null)} aria-label="Close region detail"><X size={16}/></button></div>
  <h2>{selected.name}</h2>
  <p>{observations?.date ?? '2025-06-24'} · {hour}–{String(Number(hour.slice(0, 2)) + 1).padStart(2, '0')}:00 ET</p>
  <div className="detail-stats"><div>
   <span>Reported hourly demand</span>
   <strong>{reportedDemand == null ? (observationError || observations ? 'Unavailable' : 'Loading…') : <>{reportedDemand.toLocaleString()} <small>MW</small></>}</strong>
   {reportedDemand != null && <a href={observations.demand_source} target="_blank" rel="noreferrer">IESO demand report</a>}
   {observationError && <p className="layer-caption" role="status">{observationError} <button className="retry-data" onClick={() => refreshObservations()}>Retry</button></p>}
  </div></div>
  {(recordedTemperature != null || censusHouseholds != null) && selected.id === 'gta' && <div className="detail-stats toronto-stats">
   {recordedTemperature != null && <div><span>{observations.weather_station}</span><strong>{recordedTemperature} <small>°C</small></strong><a href={observations.weather_source} target="_blank" rel="noreferrer">ECCC weather</a></div>}
   {censusHouseholds != null && <div><span>City of Toronto</span><strong>{censusHouseholds.toLocaleString()}</strong><a href={observations.census_source} target="_blank" rel="noreferrer">Households · 2021 profiles</a></div>}
  </div>}
  <button className="explore" onClick={()=>map.current?.flyTo({center:selected.center,zoom:14,pitch:55,duration:1900})}>Explore {selected.city} in 3D <ChevronRight size={15}/></button>
 </aside>}
 {feeder && <aside className="region-detail panel" aria-label="Simulated feeder">
  <div className="panel-title"><span className="region-badge feeder-badge">SIMULATED FEEDER</span><button onClick={()=>setFeederId(null)} aria-label="Close feeder detail"><X size={16}/></button></div>
  <h2>{feederId}</h2>
  <div className="detail-stats">
   <div><span>Breaker</span><strong>{feederState ? (feederState.cut ? 'Open' : 'Closed') : '—'}</strong></div>
   <div><span>{feederState?.cut ? 'Load if restored' : 'Feeder load'}</span><strong>{feederState?.demand_mw?.toLocaleString() ?? '—'} <small>MW</small></strong></div>
  </div>
  {feederState?.cut && <div className="status">{feederState.source === 'agent' ? 'Agent switched this feeder off' : 'Feeder switched off'}</div>}
  {feeder.properties.protected && <p>Protected feeder</p>}
 </aside>}
 {mapLoaded && showFeeders && focusedFeeders.length > 0 && <div className="event-map-focus" role="status"
  title={focusedFeeders.map(feature => feature.properties.id).join(', ')}>
  <MapPin size={14} aria-hidden="true"/>
  {focusedFeeders.length === 1 ? `${focusedFeeders[0].properties.id} highlighted` : `${focusedFeeders.length} feeders highlighted`}
 </div>}
 <div className="map-controls"><button aria-label="Zoom in" onClick={()=>map.current?.zoomIn()}><Plus size={18}/></button><button aria-label="Zoom out" onClick={()=>map.current?.zoomOut()}><Minus size={18}/></button><button aria-label="Recenter Ontario" onClick={home}><LocateFixed size={18}/></button><button className={is3d?'active':''} aria-label="Toggle 3D" onClick={()=>{set3d(!is3d);map.current?.easeTo({pitch:is3d?0:50,duration:700})}}><Box size={18}/></button></div>
 {simulation.error && <p className="simulation-error" role="alert">{simulation.error}</p>}
 <div className="bottom-dock">
  {simulation.live && <button className="play" disabled={!simulation.canToggle} onClick={simulation.toggle}>
   {running ? <Pause size={17}/> : <Play size={17}/>} {pending ? 'Working…' : running ? 'Pause' : 'Resume'}
  </button>}
  <div className="dock-divider"/>
  <span className="dock-stat"><b>{run?.demand_mw?.toLocaleString() ?? '—'}</b> simulated MW demand</span>
  <span className="dock-stat"><b>{loss?.toLocaleString() ?? '—'}</b> MW lost</span>
  {simulation.live && <div className="incident-controls" onKeyDown={event => {if (event.key === 'Escape') setShowIncidents(false);}}>
   <button className="inject" disabled={!simulation.canInject} aria-expanded={showIncidents} onClick={()=>setShowIncidents(!showIncidents)}><Zap size={15}/> Inject event</button>
   {showIncidents && <section className="incident-menu" aria-label="Available events">
    {INCIDENTS.filter(([event]) => (run?.injectable_events || ['generator_trip','injection_message']).includes(event)).map(([event,title,description]) =>
     <button key={event} disabled={!simulation.canInject} onClick={()=>injectIncident(event)}><strong>{title}</strong><small>{description}</small></button>)}
   </section>}
  </div>}
 </div>
 <div className="map-note">© 2004–2022 Independent Electricity System Operator · <a href="https://www.ieso.ca/Terms-of-Use" target="_blank" rel="noreferrer">Terms of Use</a></div>
 </section></div></main>
}
createRoot(document.getElementById('root')).render(<App/>);
