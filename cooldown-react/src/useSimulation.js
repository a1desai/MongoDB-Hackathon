import {useEffect, useRef, useState} from 'react';
import {simulationApi} from './simulation-api.js';

const POLL_INTERVAL = 1500;
const VISIBLE_ENTRIES = 30;

export function useSimulation() {
  const [runId, setRunId] = useState(() => new URLSearchParams(window.location.search).get('run'));
  const [run, setRun] = useState(null);
  const [events, setEvents] = useState([]);
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
      const [state, ledger] = await Promise.allSettled([
        simulationApi.run(runId, signal),
        simulationApi.ledger(runId, cursor.current, signal),
      ]);
      if (signal.aborted) return;

      if (state.status === 'fulfilled') setRun(state.value);
      if (ledger.status === 'fulfilled' && ledger.value.entries.length) {
        cursor.current = ledger.value.last_seq;
        setEvents(previous => [...previous, ...ledger.value.entries].slice(-VISIBLE_ENTRIES));
      }

      const failure = [state, ledger].find(result => result.status === 'rejected');
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

  function selectRun(id, state) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('run', id);
    else url.searchParams.delete('run');
    window.history.replaceState(null, '', url);
    cursor.current = 0;
    setEvents([]);
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

  function toggle() {
    return command(async () => {
      if (!live) {
        const created = await simulationApi.start();
        selectRun(created.run_id, {...created, status: 'created'});
        return;
      }

      const state = await (running ? simulationApi.pause(runId) : simulationApi.resume(runId));
      setRun(previous => ({...previous, ...state}));
    });
  }

  function inject() {
    return command(() => simulationApi.inject(runId));
  }

  function reset() {
    return command(async () => {
      if (running) await simulationApi.pause(runId);
      selectRun(null, null);
    });
  }

  return {
    run, events, pending, running, canToggle, canInject: live && !pending,
    error: commandError || syncError || run?.error,
    consoleUrl: runId && simulationApi.consoleUrl(runId),
    toggle, inject, reset,
  };
}
