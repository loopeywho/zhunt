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
  for (const [name, def] of Object.entries(TOPICS)) {
    topics[name] = { def, subs: new Set(), data: null, error: null, at: 0, timer: null, busy: false };
  }

  function emit(t) {
    for (const fn of [...t.subs]) {
      try { fn(snapshot(t)); } catch (err) { console.error(err); }
    }
  }
  const snapshot = (t) => ({ data: t.data, error: t.error, at: t.at });

  async function refresh(name) {
    const t = topics[name];
    if (t.busy) return;
    t.busy = true;
    try {
      const res = await t.def.fetch();
      t.data = name === 'decisions' ? res.decisions || [] : res;
      t.error = null;
      t.at = Date.now();
    } catch (err) {
      if (err.status === 401) { t.busy = false; return; }
      t.error = err;
    } finally {
      t.busy = false;
    }
    emit(t);
  }

  function schedule(name) {
    const t = topics[name];
    clearTimeout(t.timer);
    t.timer = null;
    if (!t.subs.size || document.hidden) return;
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
    get: (name) => snapshot(topics[name]),
    // fn receives {data, error, at}; called once immediately if data exists.
    subscribe(name, fn) {
      const t = topics[name];
      const first = t.subs.size === 0;
      t.subs.add(fn);
      if (t.data || t.error) fn(snapshot(t));
      if (first || !t.data) refresh(name).then(() => schedule(name));
      else schedule(name);
      return () => { t.subs.delete(fn); if (!t.subs.size) schedule(name); };
    },
  };
}
