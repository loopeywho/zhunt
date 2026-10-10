// Routing tile: live "why this model" view of /decisions, newest request first.

import { h, fmtMs, fmtNum, fmtClock } from '../dom.js';
import { tierChip, modelLabel, chip, emptyState, errorBox, liveToggle } from './util.js';

// Failover attempts of one request arrive as separate decisions (attempt 1, 2, ...).
// Chronologically group them back into requests.
export function groupRequests(decisionsNewestFirst) {
  const groups = [];
  for (const d of [...decisionsNewestFirst].reverse()) {
    const g = groups[groups.length - 1];
    if (g && !g.done && g.session === d.session && d.attempt > g.last.attempt) {
      g.attempts.push(d);
    } else {
      groups.push({ session: d.session, attempts: [d], last: d, done: false });
    }
    const cur = groups[groups.length - 1];
    cur.last = d;
    cur.done = !!d.success;
  }
  return groups.reverse();
}

function whyText(g) {
  const final = g.attempts.find((a) => a.success);
  const first = g.attempts[0];
  const parts = [];
  parts.push(first.requested_alias === 'zhunt-auto'
    ? `classified as ${first.tier}`
    : `${first.requested_alias} pins the ${first.tier} tier`);
  const failed = g.attempts.filter((a) => !a.success);
  if (final) {
    parts.push(final.degraded ? `tier exhausted, so a lower tier served it (degraded)` : `top healthy candidate: ${final.model}`);
    if (failed.length) parts.push(`${failed.length} earlier attempt${failed.length > 1 ? 's' : ''} failed first`);
  } else {
    parts.push('every attempt failed, nothing was served');
  }
  return parts.join('; ');
}

export function routingTile(svc, state, ctx) {
  let paused = !!state.paused;
  let last = null;
  const list = h('div', { class: 'rq-list' });
  const countEl = h('span', { class: 'dim' });
  const header = h('div', { class: 'row' }, countEl, liveToggle(paused, (p) => { paused = p; ctx.changed(); if (!p && last) paint(last); }));
  const el = h('div', { class: 'routing' }, list);
  list.dataset.keepScroll = '1';

  function paint(snap) {
    list.replaceChildren();
    if (snap.error && !snap.data) { list.append(errorBox('Cannot load decisions', snap.error.message)); countEl.textContent = 'offline'; return; }
    const groups = groupRequests(snap.data || []);
    countEl.textContent = groups.length + ' request' + (groups.length === 1 ? '' : 's');
    if (!groups.length) { list.append(emptyState('No requests yet.', 'Send something from a chat tile; each routing decision shows up here.')); return; }
    groups.slice(0, 60).forEach((g, i) => {
      const final = g.attempts.find((a) => a.success);
      const shown = final || g.last;
      const stateCls = final ? (final.degraded ? 'degraded' : 'ok') : 'failed';
      const row = h('article', { class: 'rq ' + stateCls + (i === 0 ? ' latest' : '') },
        h('div', { class: 'rq-head' },
          h('time', { class: 'rq-time' }, fmtClock(shown.ts)),
          h('span', { class: 'rq-alias' }, shown.requested_alias.replace('zhunt-', '')),
          h('span', { class: 'arrow', 'aria-hidden': 'true' }, '>'),
          tierChip(shown.tier),
          final ? modelLabel(final.model) : chip('no model served', 'err'),
          final?.degraded ? chip('degraded', 'warn', 'The preferred tier was exhausted; served from a lower tier') : null,
          h('span', { class: 'rq-spacer' }),
          h('span', { class: 'rq-stat' }, fmtMs(shown.latency_ms)),
          h('span', { class: 'rq-stat dim' }, `${fmtNum(shown.input_tokens)}/${fmtNum(shown.output_tokens)} tok`),
        ),
        h('p', { class: 'rq-why' }, whyText(g)),
        g.attempts.length > 1 || !final ? h('ol', { class: 'rq-attempts' }, g.attempts.map((a) => h('li', { class: a.success ? 'ok' : 'failed' },
          h('span', { class: 'att-n' }, 'attempt ' + a.attempt),
          modelLabel(a.model),
          a.success ? chip('ok', 'ok') : chip(a.failure || 'failed', 'err'),
          h('span', { class: 'dim' }, fmtMs(a.latency_ms))))) : null,
        h('div', { class: 'rq-sess dim' }, 'session ' + g.session));
      list.append(row);
    });
  }

  const unsub = svc.store.subscribe('decisions', (snap) => { last = snap; if (!paused) paint(snap); });

  return {
    el, header,
    getState: () => ({ paused }),
    focusPrimary: () => el.closest('.pane')?.focus(),
    destroy: unsub,
  };
}
