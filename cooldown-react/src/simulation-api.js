const API_URL = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');
const CONSOLE_URL = (import.meta.env.VITE_CONSOLE_URL || 'http://localhost:3000').replace(/\/$/, '');
const WS_URL = `${API_URL.replace(/^http/, 'ws')}/ws`;

function runPath(id) {
  return `/runs/${encodeURIComponent(id)}`;
}

async function request(path, {body, signal, timeout = 10000} = {}) {
  const deadline = AbortSignal.timeout(timeout);
  let response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: signal ? AbortSignal.any([signal, deadline]) : deadline,
      cache: 'no-store',
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new Error(deadline.aborted ? 'The simulation API timed out.' : `Cannot reach the simulation API at ${API_URL}.`);
  }

  if (!response.ok) {
    const error = await response.json().catch(() => null);
    const failure = new Error(typeof error?.detail === 'string' ? error.detail : `Simulation API returned HTTP ${response.status}.`);
    failure.status = response.status;
    throw failure;
  }
  return response.json();
}

async function publicData(filename, path, signal) {
  const local = await fetch(`${import.meta.env.BASE_URL}data/${filename}`, {signal}).catch(error => {
    if (signal?.aborted) throw error;
  });
  if (local?.ok && local.headers.get('content-type')?.includes('json')) return local.json();
  return request(path, {signal});
}

export const simulationApi = {
  policies: async signal => {
    try {
      return await request('/policies', {signal});
    } catch (error) {
      if (error.status !== 404) throw error;
      const response = await fetch(`${import.meta.env.BASE_URL}data/policies.json`, {signal});
      if (!response.ok || !response.headers.get('content-type')?.includes('json')) {
        throw new Error('The connected backend does not expose the policy catalog. Restart FastAPI with the current code.');
      }
      return {...await response.json(), bundled: true};
    }
  },
  observations: signal => publicData('observations.json', '/world/observations', signal),
  hydroLines: signal => publicData('hydro-lines.geojson', '/world/hydro-lines', signal),
  world: signal => request('/world/static', {signal}),
  runSocket: id => new WebSocket(`${WS_URL}?run_id=${encodeURIComponent(id)}`),
  generateScenario: prompt => request('/scenarios/generate', {body: {prompt}, timeout: 120000}),
  start: (scenarioId, mode) => request('/runs', {
    body: {scenario_id: scenarioId, mode, with_baseline: mode === 'governed', defer_baseline: true},
    timeout: 60000,
  }),
  run: (id, signal) => request(runPath(id), {signal}),
  ledger: (id, afterSeq, signal) => request(`${runPath(id)}/ledger?after_seq=${afterSeq}`, {signal}),
  action: (id, actionId, signal, executionId) => request(`${runPath(id)}/requests/${encodeURIComponent(actionId)}${executionId ? `?execution_id=${encodeURIComponent(executionId)}` : ''}`, {signal}),
  plans: (id, signal) => request(`${runPath(id)}/plans`, {signal}),
  approvals: (id, signal) => request(`${runPath(id)}/approvals?status=pending`, {signal}),
  decideApproval: (id, decision) => request(`/approvals/${encodeURIComponent(id)}/decide`, {
    body: {decision, by: 'human:supervisor'},
  }),
  pause: id => request(`${runPath(id)}/pause`, {body: {}}),
  resume: id => request(`${runPath(id)}/resume`, {body: {}}),
  inject: (id, event) => request(`${runPath(id)}/inject`, {body: {event}}),
  consoleUrl: id => `${CONSOLE_URL}/run/${encodeURIComponent(id)}`,
};
