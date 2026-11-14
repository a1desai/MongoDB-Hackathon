export const actions = {
  precool: 'Pre-cool homes', battery_discharge: 'Dispatch batteries', ev_pause: 'Pause EV charging',
  thermostat_setback: 'Adjust thermostats', thermostat_restore: 'Restore thermostat settings',
  business_curtail_request: 'Reduce business load', send_sms: 'Send resident messages',
  feeder_outage: 'Switch off a feeder', feeder_restore: 'Restore a feeder', undo: 'Reverse an action',
};

export const verdicts = {allow: 'Auto-authorized', partial_allow: 'Eligible targets auto-authorized', deny: 'Blocked', needs_approval: 'Needs supervisor approval'};

export const feederEvents = {
  uncontrolled_trip: 'Feeder tripped',
  trip_restore: 'Feeder restored',
  world_cut_released: 'Power restored after a trip',
  baseline_outage: 'Rolling outage started',
  baseline_restore: 'Feeder re-energised',
  baseline_restore_all: 'Baseline restoration',
};

function feederIds(...ids) {
  return [...new Set(ids.flat().filter(id => typeof id === 'string' && id.length))];
}

export function eventFeeders(entry, proposal, card, plan) {
  if (!entry) return [];
  const payload = entry.payload || {};
  const recorded = feederIds(payload.section_id, payload.section_ids, payload.sections);
  if (recorded.length) return recorded;
  if (entry.refs?.request_id) {
    return feederIds(proposal?.section_id, card?.targets?.section_id, card?.target_sections);
  }
  if (entry.type === 'plan_created') {
    return feederIds((plan?.steps || payload.steps || []).flatMap(step => step.selector?.sections || []));
  }
  return [];
}

export function eventTitle(entry, actionType) {
  const action = actions[actionType];
  const payload = entry.payload || {};
  switch (entry.type) {
    case 'replan_triggered': return 'Grid conditions changed';
    case 'plan_created': return 'New plan to balance the grid';
    case 'request_proposed': return action || 'Agent proposed an action';
    case 'decision': return verdicts[payload.verdict] || 'Policy check completed';
    case 'approval_requested': return 'Supervisor approval requested';
    case 'approval_decided': return 'Supervisor reviewed the request';
    case 'execution_sent': return 'Command sent';
    case 'execution_verified': return 'Action verified';
    case 'execution_failed': return 'Execution failed';
    case 'contingency': return {generator_trip: 'Generator tripped', intertie_loss: 'Intertie supply interrupted', contingency_restored: 'Supply restored'}[payload.kind] || 'Grid contingency';
    case 'message_in': return 'Resident or business replied';
    case 'message_out': return 'Message sent';
    default: return feederEvents[payload.kind] || {
      run_started: 'Simulation started', run_finished: 'Simulation finished',
    }[payload.kind] || 'Simulation update';
  }
}

export function eventDescription(entry) {
  const payload = entry.payload || {};
  if (entry.type === 'plan_created' && Number.isFinite(payload.expected_shortfall_mw)) {
    const count = payload.steps?.length ?? 0;
    return `${count} proposed ${count === 1 ? 'step' : 'steps'} for a ${formatMw(payload.expected_shortfall_mw)} MW shortfall.`;
  }
  if (entry.type === 'contingency' && payload.supply_mw != null && payload.mw != null) {
    return `${formatMw(payload.mw)} MW of supply lost${payload.duration_min ? ` for ${payload.duration_min} minutes` : ''}. ${formatMw(payload.supply_mw)} MW available.`;
  }
  if (entry.type === 'decision') {
    const reasons = (payload.failed_rules || []).map(rule => rule.name?.toLowerCase().replaceAll('_', ' ')).filter(Boolean);
    if (reasons.length) return `${verdicts[payload.verdict] || 'Reviewed'} · ${reasons.join(', ')}`;
    if (payload.verdict === 'allow') return 'The action passed the applicable policy checks.';
  }
  if (entry.type === 'execution_verified' && payload.verification) {
    return `${payload.verification.applied.toLocaleString()} targets confirmed by read-back · ${formatMw(payload.verification.delivered_mw)} MW verified in the simulation.`;
  }
  if (entry.type === 'execution_sent') return 'Executor sent a command and is waiting for confirmation.';
  return entry.summary;
}

export function actorName(actor) {
  const name = actor?.split(':').at(-1).replaceAll('_', ' ');
  return name ? name.charAt(0).toUpperCase() + name.slice(1) : 'Simulation';
}

export const formatMw = value => value == null ? '—' : value.toLocaleString(undefined, {maximumFractionDigits: 1});
