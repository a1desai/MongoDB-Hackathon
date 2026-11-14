import React, {useEffect, useState} from 'react';
import {ArrowUpRight, ChevronDown, MapPin, X} from 'lucide-react';
import {actions, actorName, eventDescription, eventFeeders, eventTitle, formatMw, verdicts} from './activity-data.js';
import {simulationApi} from './simulation-api.js';
import PolicyReview, {PolicyList} from './PolicyReview.jsx';
import ActionTrace from './ActionTrace.jsx';

const filters = [
  ['all', 'All'],
  ['approvals', 'Approvals'],
  ['blocked', 'Blocked'],
  ['verified', 'Executed'],
  ['messages', 'Messages'],
  ['policies', 'Policies'],
];

function matches(entry, filter) {
  switch (filter) {
    case 'approvals': return entry.type.startsWith('approval_');
    case 'blocked': return entry.payload?.verdict === 'deny' || entry.type === 'execution_failed';
    case 'verified': return entry.type === 'execution_verified' || entry.type === 'execution_sent';
    case 'messages': return entry.type.startsWith('message_');
    default: return true;
  }
}

function Details({entry, card, plan, error, loading, feeders, onSections, trail, formatTime}) {
  const payload = entry.payload || {};
  const decision = card?.decision;
  const execution = card?.execution;
  const verification = payload.verification || (['verified', 'partial', 'failed', 'rolled_back'].includes(execution?.status)
    ? execution.verification : null);
  const steps = plan?.steps || payload.steps;
  const rationale = plan?.rationale || payload.rationale;
  const evidence = card?.evidence || payload.evidence || [];
  const why = decision?.rule_results?.filter(rule => rule.applies) || payload.failed_rules || [];
  const planEvidence = plan?.evidence_ids || payload.evidence_ids || [];
  const policySnapshot = decision?.policy_snapshot || payload.policy_snapshot || [];

  return <div className="event-details">
    {loading && <div role="status">Loading action details…</div>}
    {error && <div role="status">{error}</div>}
    {card && <ActionTrace card={card} verification={verification}/>}
    {rationale && <div><h3>Why this plan</h3><p>{rationale}</p></div>}
    {steps?.length > 0 && <div><h3>Planned sequence</h3><ol className="plan-steps">{steps.map((step, i) => <li key={step.step_id || i}>
      <div><strong>{actions[step.action_type] || step.action_type}</strong>
      {step.expected_mw > 0 && <span>{formatMw(step.expected_mw)} MW expected</span>}</div>
      {step.window && <small>{step.window.start}–{step.window.end} ET</small>}
      {onSections && step.selector?.sections?.length > 0 && <button className="event-link"
        onClick={() => onSections(step.selector.sections)}><MapPin size={13}/> {step.selector.sections.join(', ')}</button>}
    </li>)}</ol></div>}
    {card && <div className="action-facts">
      {card.expected_mw != null && <span>Expected relief <strong>{formatMw(card.expected_mw)} MW</strong></span>}
      {card.window && <span>Window <strong>{card.window.start}–{card.window.end} ET</strong></span>}
    </div>}
    {decision && <div className="action-facts">
      <span>Decision authority <strong>{actorName(decision.decided_by)}</strong></span>
      {card?.acting_for && <span>Acting for <strong>{actorName(card.acting_for)}</strong></span>}
      {card?.approval?.decided_by && <span>Human review <strong>{card.approval.status} by {actorName(card.approval.decided_by)}</strong></span>}
      <span>Policy version <strong>{decision.policy_version}</strong></span>
    </div>}
    {why.length > 0 && <div><h3>Policy checks {verdicts[decision?.verdict || payload.verdict] && `· ${verdicts[decision?.verdict || payload.verdict]}`}</h3>
      <ul className="policy-rules">{why.map(rule => <li key={rule.rule_id}>
        <strong>{rule.rule_id} · {rule.name}</strong> <span>{rule.passed ? 'Passed' : rule.effect === 'needs_approval' ? 'Review required' : 'Restricted'}</span>
        {rule.detail && <p>{rule.detail}</p>}
      </li>)}</ul></div>}
    {policySnapshot.length > 0 && <details className="ledger-record"><summary>Policy settings recorded with this decision</summary><PolicyList rules={policySnapshot}/></details>}
    {(evidence.length > 0 || planEvidence.length > 0) && <div><h3>Evidence used</h3><div className="agent-tags">
      {evidence.map(item => <span key={item.id}>{actorName(item.kind)} · {item.id}{item.trust === 'untrusted' ? ' · Untrusted' : ''}</span>)}
      {!evidence.length && planEvidence.map(id => <span key={id}>{id}</span>)}
    </div></div>}
    {verification && <div><h3>Execution read-back</h3><div className="action-facts">
      <span>Verified relief <strong>{formatMw(verification.delivered_mw)} MW</strong></span>
      <span>Applied <strong>{verification.applied}</strong></span>
      <span>Offline <strong>{verification.offline}</strong></span>
      <span>Failed <strong>{verification.failed}</strong></span>
      <span>Overridden <strong>{verification.overridden}</strong></span>
    </div></div>}
    {trail.length > 1 && <div><h3>Decision path</h3><ol className="decision-path">{trail.map(event => <li key={event.seq}>
      <span>{eventTitle(event, card?.action_type || event.payload?.action_type)}</span>
      <time dateTime={event.sim_ts}>{formatTime(event.sim_ts)}</time>
    </li>)}</ol></div>}
    {feeders.length > 0 && <div><h3>{entry.refs?.request_id ? 'Targeted simulated feeders' : 'Simulated feeders'}</h3>
      <button className="event-link" onClick={() => onSections(feeders)}>
        <MapPin size={14}/> Show {feeders.length === 1 ? feeders[0] : `${feeders.length} feeders`} on map
      </button>
      {feeders.length > 1 && <p>{feeders.join(', ')}</p>}</div>}
    <details className="ledger-record"><summary>Original ledger record</summary><p>{entry.summary}</p></details>
  </div>;
}

export default function AgentActivity({simulation, formatTime, onClose, onSections, startingTitle}) {
  const {run, runId, events, approvals, consoleUrl, live, pending, decideApproval} = simulation;
  const [filter, setFilter] = useState(() => new URLSearchParams(window.location.search).get('view') === 'policies' ? 'policies' : 'all');
  const [expanded, setExpanded] = useState(null);
  const [detail, setDetail] = useState(null);

  const open = events.find(entry => entry.seq === expanded);
  const requestId = open?.refs?.request_id;
  const planVersion = open?.refs?.plan_version;
  const executionId = open?.refs?.execution_id;
  const detailKey = open ? `${runId}:${open.seq}` : null;

  useEffect(() => {
    if (!detailKey || !runId || !(requestId || planVersion != null)) return;
    const controller = new AbortController();
    const {signal} = controller;
    const card = requestId ? simulationApi.action(runId, requestId, signal, executionId) : Promise.resolve(null);
    const plans = planVersion != null ? simulationApi.plans(runId, signal) : Promise.resolve(null);
    Promise.allSettled([card, plans]).then(([action, planList]) => {
      if (signal.aborted) return;
      setDetail({
        key: detailKey,
        card: action.value,
        plan: planList.value?.plans.find(plan => plan.version === planVersion),
        error: action.reason?.message || planList.reason?.message,
      });
    });
    return () => controller.abort();
  }, [detailKey, runId, requestId, planVersion, executionId]);

  const requests = new Map(events.filter(entry => entry.type === 'request_proposed')
    .map(entry => [entry.refs?.request_id, entry.payload]));
  const visible = filter === 'policies' ? [] : events.filter(entry => matches(entry, filter)).reverse();
  const currentDetail = detail?.key === detailKey ? detail : null;
  const trail = currentDetail?.card?.decision_path || (requestId ? events.filter(entry => entry.refs?.request_id === requestId) : []);
  const feeders = eventFeeders(open, requests.get(requestId), currentDetail?.card, currentDetail?.plan);
  const sectionKey = feeders.join(',');

  useEffect(() => {
    onSections(sectionKey ? sectionKey.split(',') : []);
  }, [detailKey, sectionKey, onSections]);

  return <aside className="activity-workspace" aria-label="Agent activity">
    <div className="activity-header">
      <div className="activity-title"><div><small>Toronto grid simulation</small><h1>Agent activity</h1></div>
        <button aria-label="Close activity" onClick={onClose}><X size={19}/></button></div>
      {run && <div className="run-balance" aria-label="Simulated power balance">
        {[['Demand', run.demand_mw], ['Supply', run.supply_mw], ['Shortfall', run.shortfall_mw]].map(([label, value]) =>
          <div key={label}><span>{label}</span><strong>{formatMw(value)} <small>MW</small></strong></div>)}
      </div>}
      <nav className="activity-filters" aria-label="Filter events">
        {filters.map(([value, label]) => <button key={value} aria-pressed={filter === value}
          onClick={() => {setFilter(value); setExpanded(null);}}>{label}{value === 'approvals' && approvals.length > 0 && <span>{approvals.length}</span>}</button>)}
      </nav>
    </div>
    <div className="activity-scroll">
      {filter === 'policies' && <PolicyReview/>}
      {live && (filter === 'all' || filter === 'approvals') && approvals.map(approval =>
        <article className="pending-approval" key={approval._id}>
          <strong>Supervisor approval needed</strong><p>{approval.summary}</p>
          <div><button disabled={pending} onClick={() => decideApproval(approval._id, 'approved')}>Approve</button>
          <button disabled={pending} onClick={() => decideApproval(approval._id, 'denied')}>Deny</button></div>
        </article>)}
      {visible.map(entry => {
        const payload = entry.payload || {};
        const action = payload.action_type || requests.get(entry.refs?.request_id)?.action_type;
        const isOpen = expanded === entry.seq;
        return <article className="activity-event" key={entry.seq} data-stage={entry.type} data-verdict={payload.verdict}>
          <button className="event-toggle" aria-expanded={isOpen} onClick={() => setExpanded(isOpen ? null : entry.seq)}>
            <span className="event-indicator"/>
            <span className="event-text"><span className="event-meta">{actorName(entry.actor)}</span>
            <strong>{eventTitle(entry, action)}</strong><span className="event-summary">{action && entry.type !== 'request_proposed' && <b>{actions[action]} · </b>}{eventDescription(entry)}</span></span>
            <span className="event-tail"><time dateTime={entry.sim_ts}>{formatTime(entry.sim_ts)}</time><ChevronDown size={16}/></span>
          </button>
          {isOpen && <Details entry={entry} feeders={feeders} onSections={onSections} card={currentDetail?.card}
            plan={currentDetail?.plan} error={currentDetail?.error} trail={trail} formatTime={formatTime}
            loading={Boolean((requestId || planVersion != null) && !currentDetail)}/>}
        </article>;
      })}
      {startingTitle && <section className="scenario-progress activity-startup" aria-label="Run startup">
        <p role="status">Starting {startingTitle}…</p>
        <progress aria-label="Loading the grid and initializing agents"/>
        <small>Loading the grid and initializing agents</small>
      </section>}
      {!startingTitle && filter !== 'policies' && !visible.length && <p className="activity-empty">{runId ? (events.length ? 'No events match this filter.' : 'Waiting for the first ledger event.') : 'Start a simulation to see how agents respond to a power shortfall.'}</p>}
    </div>
    {consoleUrl && <footer className="activity-footer"><a href={consoleUrl} target="_blank" rel="noopener noreferrer">Open full run console <ArrowUpRight size={13}/></a></footer>}
  </aside>;
}
