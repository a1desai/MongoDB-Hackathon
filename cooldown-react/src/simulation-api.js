const API_URL = (import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(/\/$/, '');
const CONSOLE_URL = (import.meta.env.VITE_CONSOLE_URL || 'http://localhost:3000').replace(/\/$/, '');

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
    throw new Error(typeof error?.detail === 'string' ? error.detail : `Simulation API returned HTTP ${response.status}.`);
  }
  return response.json();
}

export const simulationApi = {
  start: () => request('/runs', {
    body: {scenario_id: 'heatwave_2031', mode: 'governed'},
    timeout: 60000,
  }),
  run: (id, signal) => request(runPath(id), {signal}),
  ledger: (id, afterSeq, signal) => request(`${runPath(id)}/ledger?after_seq=${afterSeq}`, {signal}),
  pause: id => request(`${runPath(id)}/pause`, {body: {}}),
  resume: id => request(`${runPath(id)}/resume`, {body: {}}),
  inject: id => request(`${runPath(id)}/inject`, {body: {event: 'generator_trip'}}),
  consoleUrl: id => `${CONSOLE_URL}/run/${encodeURIComponent(id)}`,
};
