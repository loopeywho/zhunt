// Logs tile: raw decision table with a text filter.

import { h, fmtMs, fmtNum, fmtClock, uid, tablist } from '../dom.js';
import { liveToggle, emptyState, errorBox } from './util.js';

const COLS = [
  ['time', (d) => fmtClock(d.ts)],
  ['session', (d) => d.session],
  ['alias', (d) => (d.requested_alias || '').replace('zhunt-', '')],
  ['tier', (d) => d.tier],
  ['model', (d) => d.model],
  ['att', (d) => d.attempt],
  ['ok', (d) => (d.success ? 'yes' : 'no')],
  ['deg', (d) => (d.degraded ? 'yes' : '')],
  ['failure', (d) => d.failure || ''],
  ['in', (d) => fmtNum(d.input_tokens)],
  ['out', (d) => fmtNum(d.output_tokens)],
  ['latency', (d) => fmtMs(d.latency_ms)],
];

export function logsTile(svc, state, ctx) {
  let paused = !!state.paused;
  let query = typeof state.query === 'string' ? state.query : '';
  let mode = ['all', 'failed', 'degraded'].includes(state.mode) ? state.mode : 'all';
  let last = null;

  const filterId = uid('flt');
  const filter = h('input', {
    id: filterId, class: 'ctl grow', type: 'search', 'aria-label': 'Filter log rows', placeholder: 'filter: model, tier, session, failure...',
    value: query, spellcheck: 'false',
  });
  filter.addEventListener('input', () => { query = filter.value; ctx.changed(); if (last) paint(last); });
  filter.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); if (filter.value) { filter.value = ''; query = ''; if (last) paint(last); } else el.closest('.pane')?.focus(); } });
  const tabs = tablist('Row filter', [{ id: 'all', label: 'all' }, { id: 'failed', label: 'failed' }, { id: 'degraded', label: 'degraded' }], mode,
    (id) => { mode = id; ctx.changed(); if (last) paint(last); });
  const count = h('span', { class: 'dim' });
  const header = h('div', { class: 'row' }, count, liveToggle(paused, (p) => { paused = p; ctx.changed(); if (!p && last) paint(last); }));

  const tbody = h('tbody');
  const table = h('table', { class: 'logtable', 'aria-label': 'Routing decisions' },
    h('thead', {}, h('tr', {}, COLS.map(([c]) => h('th', { scope: 'col' }, c)))), tbody);
  const wrap = h('div', { class: 'table-wrap' }, table);
  wrap.dataset.keepScroll = '1';
  wrap.tabIndex = 0;
  wrap.setAttribute('role', 'region');
  wrap.setAttribute('aria-label', 'Decision log');
  const msg = h('div', { class: 'log-msg' });
  const el = h('div', { class: 'logs' }, h('div', { class: 'logs-bar' }, filter, tabs.el), msg, wrap);

  function paint(snap) {
    tbody.replaceChildren();
    msg.replaceChildren();
    if (snap.error && !snap.data) { msg.append(errorBox('Cannot load decisions', snap.error.message)); count.textContent = 'offline'; return; }
    const q = query.trim().toLowerCase();
    const rows = (snap.data || []).filter((d) => {
      if (mode === 'failed' && d.success) return false;
      if (mode === 'degraded' && !d.degraded) return false;
      if (!q) return true;
      return COLS.some(([, f]) => String(f(d)).toLowerCase().includes(q));
    });
    count.textContent = rows.length + ' / ' + (snap.data || []).length;
    if (!rows.length) { msg.append(emptyState((snap.data || []).length ? 'No rows match the filter.' : 'No decisions recorded yet.')); return; }
    const frag = document.createDocumentFragment();
    for (const d of rows) {
      frag.append(h('tr', { class: d.success ? '' : 'failed' }, COLS.map(([name, f]) => h('td', { class: 'c-' + name }, String(f(d))))));
    }
    tbody.append(frag);
  }

  const unsub = svc.store.subscribe('decisions', (snap) => { last = snap; if (!paused) paint(snap); });

  return {
    el, header,
    getState: () => ({ paused, query, mode }),
    focusPrimary: () => filter.focus(),
    destroy: unsub,
  };
}
