// Shared, reference-counted pollers so several tiles watching the same data cost one request.
// Topics: 'status' (always polled while anyone listens), 'decisions' (polled only while a
// routing/logs tile is listening). Polling pauses while the tab is hidden.

import { api } from './api.js';

const TOPICS = {
  status: { interval: 5000, fetch: () => api.status() },
  decisions: { interval: 2000, fetch: () => api.decisions({ limit: 200 }) },
};

export function createStore() {
  const topics = {};
  let stopped = false; // set once the daemon has rejected our key: no more polling, no more subscribers
  for (const [name, def] of Object.entries(TOPICS)) {
    topics[name] = { def, subs: new Set(), data: null, error: null, at: 0, timer: null, inflight: null };
  }

  function emit(t) {
    for (const fn of [...t.subs]) {
      try { fn(snapshot(t)); } catch (err) { console.error(err); }
    }
  }
  const snapshot = (t) => ({ data: t.data, error: t.error, at: t.at });

  // Returns the in-flight promise when a fetch is already running, so callers can always await a result.
  function refresh(name) {
    const t = topics[name];
    if (stopped) return Promise.resolve();
    if (t.inflight) return t.inflight;
    t.inflight = (async () => {
      try {
        const res = await t.def.fetch();
        t.data = name === 'decisions' ? res.decisions || [] : res;
        t.error = null;
        t.at = Date.now();
      } catch (err) {
        if (err.status === 401) return;
        t.error = err;
      }
      if (!stopped) emit(t);
    })().finally(() => { t.inflight = null; });
    return t.inflight;
  }

  function schedule(name) {
    const t = topics[name];
    clearTimeout(t.timer);
    t.timer = null;
    if (stopped || !t.subs.size || document.hidden) return;
    t.timer = setTimeout(async () => { await refresh(name); schedule(name); }, t.def.interval);
  }

  document.addEventListener('visibilitychange', () => {
    for (const name of Object.keys(topics)) {
      if (!document.hidden && topics[name].subs.size) refresh(name);
      schedule(name);
    }
  });

  return {
    refresh,
    // Access was denied (401): stop every poller and forget every subscriber.
    stop() {
      stopped = true;
      for (const t of Object.values(topics)) { clearTimeout(t.timer); t.timer = null; t.subs.clear(); }
    },
    get: (name) => snapshot(topics[name]),
    // fn receives {data, error, at}; called once immediately if data exists.
    subscribe(name, fn) {
      const t = topics[name];
      if (stopped) return () => {};
      const first = t.subs.size === 0;
      t.subs.add(fn);
      if (t.data || t.error) fn(snapshot(t));
      if (first || !t.data) refresh(name).then(() => schedule(name));
      else schedule(name);
      return () => { t.subs.delete(fn); if (!t.subs.size) schedule(name); };
    },
  };
}
