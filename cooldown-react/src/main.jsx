import React, {useEffect,useRef,useState} from 'react';
import {createRoot} from 'react-dom/client';
import mapboxgl from 'mapbox-gl';
import {Layers,Activity,Map,Play,Pause,Plus,Minus,LocateFixed,X,Zap,ShieldCheck,ChevronRight,RotateCcw,Box,Globe} from 'lucide-react';
import 'mapbox-gl/dist/mapbox-gl.css';
import './style.css';
import {regions,cities,regionGeoJSON,connections} from './data';
import PlaceSearch from './PlaceSearch.jsx';
import {useSimulation} from './useSimulation.js';

const scenarioClock = new Intl.DateTimeFormat('en-CA', {
 timeZone: 'America/Toronto', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
});

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

function App(){
 const container=useRef(null),map=useRef(null), selectedRef=useRef(null),searchMarker=useRef(null);
 const [searchReset,setSearchReset]=useState(0);
 const [selected,setSelected]=useState(null),[tab,setTab]=useState('map'),[layers,setLayers]=useState(false),[showZones,setShowZones]=useState(true),[showGrid,setShowGrid]=useState(true),[showRoutes,setShowRoutes]=useState(true),[is3d,set3d]=useState(true),[error,setError]=useState('');
 const simulation=useSimulation();
 const {run, events, running, pending, consoleUrl}=simulation;
 const loss=run?.contingency_lost_mw;
 selectedRef.current=setSelected;
 useEffect(()=>{
  const token=import.meta.env.VITE_MAPBOX_TOKEN;
  if(!token){setError('Add your public Mapbox token to .env.local to load the map.');return;}
  mapboxgl.accessToken=token;
  let m;
  try{m=new mapboxgl.Map({container:container.current,style:'mapbox://styles/mapbox/streets-v12',center:[-84.4,48.3],zoom:5.1,pitch:35,bearing:-10,attributionControl:true});map.current=m;}catch(e){setError('This browser could not start WebGL. Open the project in a browser with hardware acceleration enabled.');return;}
  m.on('error',()=>{if(!m.isStyleLoaded())setError('Map connection failed. Check your Mapbox token and its allowed URLs.');});
  m.on('load',()=>{
   setError('');
   m.addSource('regions',{type:'geojson',data:regionGeoJSON});
   m.addLayer({id:'zone-fill',type:'fill',source:'regions',paint:{'fill-color':['get','color'],'fill-opacity':.3}});
   m.addLayer({id:'zone-border',type:'line',source:'regions',paint:{'line-color':['get','color'],'line-width':2.5,'line-opacity':.95}});
   const grid={type:'FeatureCollection',features:[]};
   for(let x=-96;x<=-74;x+=.25)grid.features.push({type:'Feature',geometry:{type:'LineString',coordinates:[[x,41.5],[x,57]]}});
   for(let y=41.5;y<=57;y+=.2)grid.features.push({type:'Feature',geometry:{type:'LineString',coordinates:[[-96,y],[-74,y]]}});
   m.addSource('grid',{type:'geojson',data:grid});
   m.addLayer({id:'grid-lines',type:'line',source:'grid',paint:{'line-color':'#547482','line-width':['interpolate',['linear'],['zoom'],4,.7,8,1.1,13,.4],'line-opacity':['interpolate',['linear'],['zoom'],4,.2,7,.32,11,.08]}});
   m.addSource('routes',{type:'geojson',data:connections});
   m.addLayer({id:'route-lines',type:'line',source:'routes',layout:{'line-cap':'round','line-join':'round'},paint:{'line-color':['get','color'],'line-width':3,'line-opacity':.8}});
   const label=m.getStyle().layers.find(l=>l.type==='symbol'&&l.layout?.['text-field']);
   if(m.getSource('composite'))m.addLayer({id:'buildings-3d',type:'fill-extrusion',source:'composite','source-layer':'building',minzoom:12,filter:['==','extrude','true'],paint:{'fill-extrusion-color':['step',['coalesce',['get','height'],5],'#54cfad',12,'#ff987f',25,'#68b6f4',50,'#b491ef',100,'#f8ce63'],'fill-extrusion-height':['coalesce',['get','height'],5],'fill-extrusion-base':['coalesce',['get','min_height'],0],'fill-extrusion-opacity':.96}},label?.id);
   cities.forEach(c=>{const el=document.createElement('button');el.className='city-node';el.setAttribute('aria-label','Explore '+c.name);el.title=c.name;el.onclick=()=>{searchMarker.current?.remove();searchMarker.current=null;setSearchReset(value=>value+1);const r=regions.find(r=>r.city===c.name);setSelected(r||null);m.flyTo({center:c.coordinates,zoom:15.2,pitch:52,duration:1800});};new mapboxgl.Marker({element:el}).setLngLat(c.coordinates).addTo(m);});
   m.on('click','zone-fill',e=>{const r=regions.find(r=>r.id===e.features[0].properties.id);selectedRef.current(r);});
   m.on('mouseenter','zone-fill',()=>m.getCanvas().style.cursor='pointer');m.on('mouseleave','zone-fill',()=>m.getCanvas().style.cursor='');
  });return()=>{searchMarker.current?.remove();m.remove();};
 },[]);
 useEffect(()=>{const m=map.current;if(!m?.isStyleLoaded())return;[['zone-fill',showZones],['zone-border',showZones],['grid-lines',showGrid],['route-lines',showRoutes]].forEach(([id,v])=>{if(m.getLayer(id))m.setLayoutProperty(id,'visibility',v?'visible':'none')})},[showZones,showGrid,showRoutes]);
 const clearSearchMarker=()=>{searchMarker.current?.remove();searchMarker.current=null;};
 const home=()=>{setSelected(null);clearSearchMarker();setSearchReset(value=>value+1);map.current?.flyTo({center:[-84.4,48.3],zoom:5.1,pitch:is3d?35:0,bearing:-10,duration:1500})};
 const focus=r=>{setSelected(r);clearSearchMarker();setSearchReset(value=>value+1);map.current?.flyTo({center:r.center,zoom:r.id==='gta'?10.5:7.5,pitch:is3d?45:0,duration:1600})};
 const explorePlace=place=>{
  clearSearchMarker();
  const region=regions.find(region=>region.id===place.regionId);
  setSelected(region||null);setTab('none');setLayers(false);
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
 async function trip() {
  if (await simulation.inject()) setTab('activity');
 }

 async function reset() {
  if (await simulation.reset()) home();
 }

 let toggleLabel = 'Start simulation';
 if (running) toggleLabel = 'Pause';
 if (run?.status === 'paused' && run.live !== false) toggleLabel = 'Resume';

 return <main className="app">
 <header>
  <a className="brand" href="./"><span><Zap size={19}/></span>cooldown<span className="slash">/</span><b>Ontario</b></a>
  <div className="header-center"><span className="live-dot"/>Toronto heatwave simulation</div>
  <button className="reset" disabled={pending} onClick={reset}><RotateCcw size={15}/> Reset</button>
 </header>
 <section className="canvas"><div className="map" ref={container}/>{error&&<div className="map-error"><Globe size={28}/><h2>Connect your Ontario map</h2><p>{error}</p><code>VITE_MAPBOX_TOKEN=pk.…</code></div>}
 <div className="topbar">
  <div className="clock">
   <span className="live-dot"/>
   <b>{run?.sim_ts ? scenarioClock.format(new Date(run.sim_ts)) : 'Toronto'}</b>
   {run?.outdoor_c != null && <span>{run.outdoor_c}°C</span>}
   <span className="clock-divider"/>
   {run?.paused_reason === 'approval' && consoleUrl
    ? <a href={consoleUrl} target="_blank" rel="noopener noreferrer">Awaiting approval</a>
    : <span>{consoleUrl && !run ? 'Loading run' : runStatus(run)}</span>}
  </div>
  <PlaceSearch onSelect={explorePlace} onClear={clearSearchMarker} resetKey={searchReset}/>
  <button className="province-button" onClick={home}><Globe size={15}/> All Ontario</button>
 </div>
 <nav className="rail" aria-label="Map tools"><button className={tab==='map'?'active':''} aria-label="Map regions" onClick={()=>setTab(tab==='map'?'none':'map')}><Map size={19}/></button><button className={tab==='activity'?'active':''} aria-label="Agent activity" onClick={()=>setTab(tab==='activity'?'none':'activity')}><Activity size={19}/>{events.length>0&&<i/>}</button><button className={layers?'active':''} aria-label="Map layers" onClick={()=>setLayers(!layers)}><Layers size={19}/></button></nav>
 {tab==='map'&&<aside className="panel regions"><div className="panel-title"><div><span className="kicker">NETWORK</span><h1>Across Ontario</h1></div><button aria-label="Close regions" onClick={()=>setTab('none')}><X size={16}/></button></div><p className="intro">One province. Eight connected regions.</p><div className="region-list">{regions.map(r=><button key={r.id} className={selected?.id===r.id?'region selected':'region'} onClick={()=>focus(r)}><i style={{background:r.color}}/><span>{r.name}<small>{r.city}</small></span><ChevronRight size={15}/></button>)}</div><div className="panel-foot"><ShieldCheck size={14}/> Governed agents · synthetic data</div></aside>}
 {tab==='activity'&&<aside className="panel activity">
  <div className="panel-title"><h1>Run activity</h1><button aria-label="Close activity" onClick={()=>setTab('none')}><X size={16}/></button></div>
  {events.length ? [...events].reverse().map(entry=><div className="event" key={entry.seq}>
   <i/><div><strong>{entry.summary}</strong><p>{entry.actor}</p><small>{scenarioClock.format(new Date(entry.sim_ts))}</small></div>
  </div>) : <p className="intro empty">{consoleUrl ? 'Waiting for ledger activity.' : 'Start the Toronto simulation to see ledger activity.'}</p>}
  {consoleUrl && <div className="panel-foot"><a href={consoleUrl} target="_blank" rel="noopener noreferrer">Open run console</a></div>}
 </aside>}
 {layers&&<div className="panel layer-menu"><strong>Map layers</strong>{[['Coverage zones',showZones,setShowZones],['Coordinate grid',showGrid,setShowGrid],['Network connections',showRoutes,setShowRoutes]].map(([n,v,f])=><label key={n}><input type="checkbox" checked={v} onChange={e=>f(e.target.checked)}/>{n}</label>)}</div>}
 {selected&&<aside className="region-detail panel"><div className="panel-title"><span className="region-badge" style={{background:selected.color}}>REGION</span><button onClick={()=>setSelected(null)} aria-label="Close region detail"><X size={16}/></button></div><h2>{selected.name}</h2><p>{selected.city} operating area</p><div className="detail-stats"><div><span>Demand</span><strong>{selected.demand.toLocaleString()} <small>MW</small></strong></div><div><span>Households</span><strong>{(selected.homes/1000).toFixed(1)}<small>k</small></strong></div></div><button className="explore" onClick={()=>map.current?.flyTo({center:selected.center,zoom:14,pitch:55,duration:1900})}>Explore city in 3D <ChevronRight size={15}/></button><small className="illustrative">Illustrative coverage and readings</small></aside>}
 <div className="map-controls"><button aria-label="Zoom in" onClick={()=>map.current?.zoomIn()}><Plus size={18}/></button><button aria-label="Zoom out" onClick={()=>map.current?.zoomOut()}><Minus size={18}/></button><button aria-label="Recenter Ontario" onClick={home}><LocateFixed size={18}/></button><button className={is3d?'active':''} aria-label="Toggle 3D" onClick={()=>{set3d(!is3d);map.current?.easeTo({pitch:is3d?0:50,duration:700})}}><Box size={18}/></button></div>
 {simulation.error && <p className="simulation-error" role="alert">{simulation.error}</p>}
 <div className="bottom-dock">
  <button className="play" disabled={!simulation.canToggle} onClick={simulation.toggle}>
   {running ? <Pause size={17}/> : <Play size={17}/>} {pending ? 'Working…' : toggleLabel}
  </button>
  <div className="dock-divider"/>
  <span className="dock-stat"><b>{run?.demand_mw?.toLocaleString() ?? '—'}</b> MW demand</span>
  <span className="dock-stat"><b>{loss?.toLocaleString() ?? '—'}</b> MW lost</span>
  <button className="inject" disabled={!simulation.canInject} onClick={trip}><Zap size={15}/> Inject event</button>
 </div>
 <div className="map-note">Illustrative operating zones · fictional grid scenario</div>
 </section></main>
}
createRoot(document.getElementById('root')).render(<App/>);
