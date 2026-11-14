import React from 'react';
import {ArrowRight, CheckCircle2, CircleDot, ShieldCheck, Terminal} from 'lucide-react';
import {actions, formatMw, verdicts} from './activity-data.js';

const systems = {thermlink: 'ThermLink', chargeflow: 'ChargeFlow', powercell: 'PowerCell', textline: 'TextLine', gridops: 'GridOps'};
const settings = {
  offset_c: ['Temperature offset', '°C'], precool_offset_c: ['Temperature offset', '°C'], kw: ['Per device', 'kW'],
  duration_min: ['Duration', 'min'], mw: ['Load', 'MW'], requested_mw: ['Requested reduction', 'MW'],
  amount_cad: ['Offer per recipient', 'CAD'],
};

export default function ActionTrace({card, verification}) {
  const execution = card.execution;
  const gate = card.decision;
  const approved = card.approval?.status;
  const target = card.targets || {};
  const params = {...card.params, ...execution?.body};
  const fields = Object.entries(settings).filter(([key]) => params[key] != null);
  const steps = [
    ['Proposal', 'Recorded', CircleDot, 'done'],
    ['Policy gate', verdicts[gate?.verdict] || 'Pending', ShieldCheck,
      gate?.verdict === 'deny' ? 'blocked' : gate?.verdict === 'needs_approval' ? 'review' : gate ? 'done' : 'pending'],
  ];
  if (gate?.verdict === 'needs_approval' || approved) {
    steps.push(['Supervisor', approved || 'Pending', ShieldCheck, approved === 'approved' ? 'done' : approved === 'denied' || approved === 'expired' ? 'blocked' : 'review']);
  }
  steps.push(['Sandbox command', execution?.path ? systems[execution.system] || execution.system : gate?.verdict === 'deny' ? 'Blocked' : 'Awaiting dispatch',
    Terminal, execution?.path ? 'done' : gate?.verdict === 'deny' ? 'blocked' : 'pending']);
  steps.push(['Read-back', execution?.status === 'failed' ? 'Failed' : verification ? `${formatMw(verification.delivered_mw)} MW` : 'Pending',
    CheckCircle2, execution?.status === 'failed' ? 'blocked' : verification ? 'done' : 'pending']);

  return <section className="trace-card" aria-label="Exact action">
    <h3>{actions[card.action_type] || card.action_type}</h3>
    <p>{card.summary}</p>
    <div className="trace-stages" aria-label="Action lifecycle">{steps.map(([label,status,Icon,state], i) =>
      <React.Fragment key={label}>
        {i > 0 && <ArrowRight className="trace-arrow" size={13} aria-hidden="true"/>}
        <div data-state={state}><Icon size={15} aria-hidden="true"/><span>{label}</span><strong>{status}</strong></div>
      </React.Fragment>)}</div>
    <dl className="trace-facts">
      {target.section_id && <div><dt>Feeder</dt><dd>{target.section_id}</dd></div>}
      {target.business_id && <div><dt>Business</dt><dd>{target.business_id.replace(/^biz_/, '').replaceAll('_', ' ')}</dd></div>}
      {card.targets_count > 0 && <div><dt>Targeted</dt><dd>{card.targets_count.toLocaleString()}</dd></div>}
      {gate?.allowed_count != null && <div><dt>Eligible</dt><dd>{gate.allowed_count.toLocaleString()}</dd></div>}
      {gate?.denied_count > 0 && <div><dt>Excluded</dt><dd>{gate.denied_count.toLocaleString()}</dd></div>}
      {fields.map(([key,[name,unit]]) => <div key={key}><dt>{name}</dt><dd>{params[key]} {unit}</dd></div>)}
    </dl>
    {execution?.path && <details className="ledger-record"><summary>Command and device targets</summary>
      <p><code>POST {execution.path}</code></p>
      <pre aria-label="Recorded command parameters">{JSON.stringify(execution.body || {}, null, 2)}</pre>
    </details>}
    {card.messages?.length > 0 && <details className="ledger-record"><summary>Proposed message text</summary>
      {card.messages.map((message,i) => <blockquote key={i}>{message.body}</blockquote>)}
    </details>}
  </section>;
}
