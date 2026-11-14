import React, {useRef, useState} from 'react';
import {ArrowRight, CornerDownLeft, Terminal} from 'lucide-react';
import {simulationApi} from './simulation-api.js';
import PolicyReview from './PolicyReview.jsx';

const preset = {id: 'heatwave_2031', title: 'Toronto heatwave', date: '2031-06-24'};

export default function ScenarioPrompt({onStart, pending, runError, showPolicies}) {
  const [prompt, setPrompt] = useState('');
  const [scenario, setScenario] = useState(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(null);
  const form = useRef(null);
  const busy = generating || pending;

  async function startRun(id, title) {
    if (busy) return;
    setStarting(title);
    setError('');
    try {
      await onStart(id, title);
    } finally {
      setStarting(null);
    }
  }

  async function generate(event) {
    event.preventDefault();
    if (busy || prompt.trim().length < 12) return;
    setGenerating(true);
    setError('');
    setScenario(null);
    try {
      const result = await simulationApi.generateScenario(prompt.trim());
      setScenario(result.scenario);
    } catch (err) {
      setError(err.message);
    } finally {
      setGenerating(false);
    }
  }

  return <main className="scenario-start">
    <header className="terminal-header"><span><Terminal size={17}/> GridLock</span><span>~/scenarios</span></header>
    <section className="scenario-workbench" aria-labelledby={showPolicies ? undefined : 'scenario-heading'}>
      {showPolicies && <><a className="policy-back" href="./">← Scenario prompt</a><PolicyReview/></>}
      {!showPolicies && <>
      <div className="shell-command" aria-hidden="true"><span>operator@gridlock</span>:~$ scenario new</div>
      <h1 id="scenario-heading">What should the grid face?</h1>
      <p className="scenario-intro">Describe a power-grid scenario for Toronto.</p>
      <form ref={form} onSubmit={generate}>
        <label className="scenario-input"><span aria-hidden="true">&gt;</span>
          <textarea aria-label="Scenario prompt" value={prompt} onChange={event => {setPrompt(event.target.value); setScenario(null); setError('');}}
            disabled={busy} maxLength={4000} rows={4} autoFocus
            placeholder="A 42°C heatwave with a 600 MW generator loss at 18:15…"
            onKeyDown={event => {if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {event.preventDefault(); form.current.requestSubmit();}}}/>
        </label>
        <button className="terminal-primary" disabled={busy || prompt.trim().length < 12} type="submit">
          {generating ? 'Designing scenario…' : 'Generate scenario'} <CornerDownLeft size={15}/>
        </button>
      </form>
      {(error || runError) && <p className="scenario-error" role="alert">{error || runError}</p>}
      {starting && <section className="scenario-progress" aria-label="Run startup">
        <p role="status">Starting {starting}…</p>
        <progress aria-label="Loading the grid and initializing agents"/>
        <small>Loading the grid and initializing agents</small>
      </section>}
      {scenario && <section className="scenario-preview" aria-label="Generated scenario">
        <h2>{scenario.title}</h2><p>{scenario.summary}</p>
        <dl>
          <div><dt>Simulated time</dt><dd>{scenario.date} · {scenario.sim_start}–{scenario.sim_end} ET</dd></div>
          <div><dt>Peak demand</dt><dd>{scenario.target_peak_mw.toLocaleString()} MW</dd></div>
          <div><dt>Starting supply</dt><dd>{scenario.supply_mw.toLocaleString()} MW</dd></div>
          {scenario.contingencies.map((event, i) => <div key={i}><dt>{event.at} ET</dt><dd>{event.label} · {event.mw.toLocaleString()} MW</dd></div>)}
        </dl>
        <details><summary>Design rationale and assumptions</summary><p>{scenario.designer.reasoning}</p>
          <ul>{scenario.designer.assumptions.map((assumption, i) => <li key={i}>{assumption}</li>)}</ul>
          <p>Designer: {scenario.designer.source === 'fallback' ? 'Rule-based fallback' : 'LLM'}</p>
        </details>
        <button type="button" className="terminal-primary" disabled={busy} onClick={() => startRun(scenario.id, scenario.title)}>
          {pending ? 'Starting…' : 'Run scenario'} <ArrowRight size={15}/>
        </button>
      </section>}
      <div className="scenario-preset">
        <span className="preset-label">Built-in scenario</span>
        <button type="button" disabled={busy} onClick={() => startRun(preset.id, preset.title)}>
          <span><strong>{preset.title}</strong><small>{preset.date} · 350 MW generator trip at 17:40 ET</small></span><ArrowRight size={16}/>
        </button>
      </div>
      <a className="policy-back" href="?view=policies">Review policies</a>
      </>}
    </section>
  </main>;
}
