import React, {useEffect, useId, useMemo, useRef, useState} from 'react';
import {Search, MapPin, X, ArrowUpRight} from 'lucide-react';
import {localPlaces, remotePlaces, mergePlaces, ONTARIO_BOUNDS} from './place-search.js';

export default function PlaceSearch({onSelect, onClear, resetKey}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [remote, setRemote] = useState([]);
  const [status, setStatus] = useState('idle');
  const [active, setActive] = useState(0);
  const input = useRef(null), root = useRef(null);
  const listId = useId();
  const local = useMemo(() => localPlaces(query), [query]);
  const results = useMemo(() => mergePlaces(local, remote), [local, remote]);

  useEffect(() => {
    setQuery(''); setRemote([]); setOpen(false); setStatus('idle'); setActive(0);
  }, [resetKey]);

  useEffect(() => {
    function outside(event) { if (!root.current?.contains(event.target)) setOpen(false); }
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, []);

  useEffect(() => {
    const term = query.trim(), token = import.meta.env.VITE_MAPBOX_TOKEN;
    if (!open || term.length < 2) return;
    if (!token) { setStatus('error'); return; }
    const controller = new AbortController();
    let current = true, timeout;
    setStatus('loading');
    const debounce = setTimeout(async () => {
      const params = new URLSearchParams({q: term, access_token: token, country: 'ca',
        bbox: ONTARIO_BOUNDS.join(','), types: 'place,locality,neighborhood,district',
        autocomplete: 'true', language: 'en', limit: '10'});
      timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const response = await fetch(`https://api.mapbox.com/search/geocode/v6/forward?${params}`, {signal: controller.signal});
        if (!response.ok) throw new Error('Search unavailable');
        const data = await response.json();
        if (current) {setRemote(remotePlaces(data)); setStatus('ready');}
      } catch {
        if (current) {setRemote([]); setStatus('error');}
      } finally { clearTimeout(timeout); }
    }, 350);
    return () => { current = false; clearTimeout(debounce); clearTimeout(timeout); controller.abort(); };
  }, [query, open]);

  function select(place) {
    setQuery(place.name); setOpen(false); setRemote([]); setActive(0);
    onSelect(place);
    input.current?.focus();
  }

  function clear() {
    setQuery(''); setRemote([]); setStatus('idle'); setActive(0); setOpen(true);
    onClear(); input.current?.focus();
  }

  function keyDown(event) {
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); setOpen(true);
      setActive(index => results.length ? (index + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length : 0);
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      if (open && results[active]) select(results[active]);
      else setOpen(true);
    }
  }

  return <div className="place-search" ref={root} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }}>
    <div className="search-field">
      <Search size={17} aria-hidden="true"/>
      <input ref={input} type="text" role="combobox" aria-label="Search cities or areas"
        placeholder="Search cities or areas" autoComplete="off" spellCheck="false" maxLength={160}
        aria-expanded={open} aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={open && results[active] ? `${listId}-${active}` : undefined}
        value={query} onFocus={() => setOpen(true)} onKeyDown={keyDown}
        onChange={event => {setQuery(event.target.value); setRemote([]); setStatus('idle'); setActive(0); setOpen(true);}}/>
      {query && <button className="search-clear" aria-label="Clear search" onClick={clear}><X size={15}/></button>}
    </div>
    {open && <div className="search-dropdown">
      <div className="search-heading">{query.trim() ? 'Places in Ontario' : 'Explore a city'}</div>
      <ul id={listId} role="listbox" aria-label="Ontario places">
        {results.map((place, index) => <li key={place.id} id={`${listId}-${index}`}
          role="option" aria-selected={index === active} className={index === active ? 'search-option active' : 'search-option'}
          onPointerMove={() => setActive(index)} onPointerDown={event => event.preventDefault()} onClick={() => select(place)}>
          <MapPin size={17} aria-hidden="true"/><span><strong>{place.name}</strong><small>{place.kind} · {place.subtitle}</small></span><ArrowUpRight size={15} aria-hidden="true"/>
        </li>)}
      </ul>
      <p className="search-feedback" role="status" aria-live="polite">
        {status === 'loading' ? 'Searching Ontario…' : status === 'error' ?
          'Area lookup is unavailable. Try a listed city or region, or search again.' :
          !results.length ? 'No Ontario places found. Try a nearby city or another name.' :
          '↑ ↓ to browse · Enter to explore'}
      </p>
    </div>}
  </div>;
}
