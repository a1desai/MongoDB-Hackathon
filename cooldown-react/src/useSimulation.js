import {useEffect, useRef, useState} from 'react';
import {feederEvents} from './activity-data.js';
import {simulationApi} from './simulation-api.js';

const POLL_INTERVAL = 1500;
const VISIBLE_ENTRIES = 60;
const ACTION_EVENTS = new Set([
  'plan_created', 'replan_triggered', 'request_proposed', 'decision', 'approval_requested',
  'approval_decided', 'execution_sent', 'execution_verified', 'execution_failed', 'contingency',
  'message_in', 'message_out', 'halt', 'resume',
]);

export function useSimulation() {
  const [runId, setRunId] = useState(() => new URLSearchParams(window.location.search).get('run'));
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
  const [approvals, setApprovals] = useState([]);
  const [sections, setSections] = useState([]);
  const [pending, setPending] = useState(false);
  const [commandError, setCommandError] = useState('');
  const [syncError, setSyncError] = useState('');
  const cursor = useRef(0);

  useEffect(() => {
    if (!runId || pending) return;
    const controller = new AbortController();
    const {signal} = controller;
    let timer;

    async function refresh() {
      const [state, ledger, pendingApprovals] = await Promise.allSettled([
        simulationApi.run(runId, signal),
        simulationApi.ledger(runId, cursor.current, signal),
        simulationApi.approvals(runId, signal),
      ]);
      if (signal.aborted) return;

      if (state.status === 'fulfilled') setRun(state.value);
      if (ledger.status === 'fulfilled' && ledger.value.entries.length) {
        cursor.current = ledger.value.last_seq;
        const newEvents = ledger.value.entries.filter(entry => ACTION_EVENTS.has(entry.type) ||
          Object.hasOwn(feederEvents, entry.payload?.kind) || ['run_started', 'run_finished'].includes(entry.payload?.kind));
        setEvents(previous => [...previous, ...newEvents].slice(-VISIBLE_ENTRIES));
      }
      if (pendingApprovals.status === 'fulfilled') setApprovals(pendingApprovals.value.approvals);

      const failure = [state, ledger, pendingApprovals].find(result => result.status === 'rejected');
      setSyncError(failure?.reason.message || '');
      timer = setTimeout(refresh, POLL_INTERVAL);
    }

    refresh();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [runId, pending]);

  const live = run?.live !== false && ['created', 'running', 'paused'].includes(run?.status);
  const running = live && run.status !== 'paused';
  const canToggle = !pending && (!runId || run != null);

  useEffect(() => {
    if (!runId || !live) return;
    let socket, retry, heartbeat;
    let retryDelay = 1000;

    function connect() {
      socket = simulationApi.runSocket(runId);
      socket.onopen = () => {
        retryDelay = 1000;
        heartbeat = setInterval(() => socket.send('ping'), 20000);
      };
      socket.onmessage = ({data}) => {
        const message = JSON.parse(data);
        if (message.type === 'sections') setSections(message.states);
      };
      socket.onclose = () => {
        clearInterval(heartbeat);
        setSections([]);
        retry = setTimeout(connect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 8000);
      };
    }

    connect();
    return () => {
      clearTimeout(retry);
      clearInterval(heartbeat);
      socket.onclose = null;
      socket.onmessage = null;
      socket.close();
    };
  }, [runId, live]);

  function selectRun(id, state) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('run', id);
    else url.searchParams.delete('run');
    window.history.replaceState(null, '', url);
    cursor.current = 0;
    setEvents([]);
    setApprovals([]);
    setSections([]);
    setSyncError('');
    setRunId(id);
    setRun(state);
  }

  async function command(action) {
    setPending(true);
    setCommandError('');
    try {
      await action();
      return true;
    } catch (error) {
      setCommandError(error.message);
      return false;
    } finally {
      setPending(false);
    }
  }

  function start(scenarioId, mode = 'governed', title) {
    return command(async () => {
      const created = await simulationApi.start(scenarioId, mode);
      selectRun(created.run_id, {...created, scenario_id: scenarioId, scenario_title: title, status: 'created'});
    });
  }

  function toggle() {
    return command(async () => {
      const state = await (running ? simulationApi.pause(runId) : simulationApi.resume(runId));
      setRun(previous => ({...previous, ...state}));
    });
  }

  function inject(event) {
    return command(() => simulationApi.inject(runId, event));
  }

  function reset() {
    return command(async () => {
      if (running) await simulationApi.pause(runId);
      selectRun(null, null);
    });
  }

  function decideApproval(id, decision) {
    return command(async () => {
      await simulationApi.decideApproval(id, decision);
      setApprovals(previous => previous.filter(approval => approval._id !== id));
    });
  }

  return {
    run, runId, events, approvals, sections, live, pending, running, canToggle, canInject: live && !pending,
    error: commandError || syncError || run?.error,
    consoleUrl: runId && simulationApi.consoleUrl(runId),
    start, toggle, inject, reset, decideApproval,
  };
}
