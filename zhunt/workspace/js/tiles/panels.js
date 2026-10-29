// Panels window (after Hark Pro's panels): small dashboards the user can reorder or hide. Which
// panels show, and in what order, is tile state, so it is saved with the layout.

import { h, fmtMs, fmtNum, splitModel } from '../dom.js';
import { groupRequests } from './routing.js';
import { tierChip, modelLabel, headroomBar, chip, swapIfChanged } from './util.js';

export const PANELS = {
  capacity: 'Free capacity',
  today: 'Recent usage today',
  last: 'Last answer',
  live: 'Live routing',
};
const DEFAULT_ORDER = Object.keys(PANELS);
const shortModel = (id) => splitModel(id).name.split('/').pop();

export function panelsTile(svc, state, ctx) {
  let order = Array.isArray(state.order) ? state.order.filter((k) => PANELS[k]) : [];
  for (const k of DEFAULT_ORDER) if (!order.includes(k)) order.push(k);
  const hidden = new Set(Array.isArray(state.hidden) ? state.hidden.filter((k) => PANELS[k]) : []);
  let customizing = false;
  let status = svc.store.get('status');
  let decisions = svc.store.get('decisions');

  const list = h('div', { class: 'panel-list' });
  list.dataset.keepScroll = '1';
  const addRow = h('div', { class: 'panel-add' });
  const el = h('div', { class: 'panels' }, list, addRow);
  const customizeBtn = h('button', { class: 'pane-btn wide', type: 'button', 'aria-pressed': 'false', title: 'Reorder or hide panels', onclick: toggle }, 'customize');

  function toggle() {
    customizing = !customizing;
    customizeBtn.setAttribute('aria-pressed', String(customizing));
    customizeBtn.textContent = customizing ? 'done' : 'customize';
    el.classList.toggle('customizing', customizing);
    paint();
  }

  function paint() {
    const shown = order.filter((k) => !hidden.has(k));
    swapIfChanged(list, shown.map((k, i) => panelEl(k, i, shown.length)));
    swapIfChanged(addRow, customizing && hidden.size ? [h('span', { class: 'quiet' }, 'Add: '),
      ...[...hidden].map((k) => h('button', { class: 'act', type: 'button', onclick: () => { hidden.delete(k); ctx.changed(); paint(); } }, '+ ' + PANELS[k]))] : []);
  }

  function panelEl(kind, index, count) {
    const tools = customizing ? h('div', { class: 'panel-tools' },
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Move ' + PANELS[kind] + ' up', disabled: index === 0, onclick: () => move(kind, -1) }, '↑'),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Move ' + PANELS[kind] + ' down', disabled: index === count - 1, onclick: () => move(kind, 1) }, '↓'),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Hide ' + PANELS[kind], onclick: () => { hidden.add(kind); ctx.changed(); paint(); } }, '×')) : null;
    return h('section', { class: 'panel', 'data-panel': kind },
      h('header', { class: 'panel-head' }, h('h3', {}, PANELS[kind]), tools),
      h('div', { class: 'panel-body' }, body(kind)));
  }

  function move(kind, delta) {
    const shown = order.filter((k) => !hidden.has(k));
    const i = shown.indexOf(kind);
    const j = i + delta;
    if (j < 0 || j >= shown.length) return;
    [shown[i], shown[j]] = [shown[j], shown[i]];
    order = [...shown, ...order.filter((k) => hidden.has(k))];
    ctx.changed();
    paint();
  }

  function body(kind) {
    const s = status.data;
    const ds = decisions.data || [];
    if (kind === 'capacity') {
      if (!s) return h('p', { class: 'quiet' }, status.error ? 'daemon unreachable' : 'loading...');
      const models = (s.providers || []).flatMap((p) => p.models || []).filter((m) => m.free);
      const usable = models.filter((m) => m.allowed && !(m.cooldown_remaining > 0));
      const known = usable.map((m) => m.headroom).filter((x) => x !== null && x !== undefined);
      const avg = known.length ? known.reduce((a, b) => a + b, 0) / known.length : null;
      const keys = (s.providers || []).filter((p) => p.key_set).length;
      return [
        h('p', { class: 'big' }, String(usable.length), h('span', { class: 'big-of' }, ` / ${models.length} models ready`)),
        h('div', { class: 'meter' }, h('span', { class: 'quiet' }, 'estimated quota left'), headroomBar(avg)),
        h('p', { class: 'quiet' }, known.length ? `${known.length} of ${usable.length} ready free models report quota estimates` : 'Remaining quota unknown'),
        h('button', { class: 'act', type: 'button', onclick: () => svc.openWindow('providers') }, 'View free models & limits'),
        h('p', { class: 'quiet' }, `${keys} of ${(s.providers || []).length} providers have a key · ${s.policy?.allow_paid ? 'premium on' : 'free only'}`),
        s.spend && s.policy?.allow_paid ? h('p', { class: 'quiet' }, `premium this month: $${s.spend.spent.toFixed(2)} of $${s.spend.cap.toFixed(2)}`) : null,
      ];
    }
    if (kind === 'today') {
      const midnight = new Date(); midnight.setHours(0, 0, 0, 0);
      const today = ds.filter((d) => d.ts * 1000 >= midnight.getTime());
      const tokIn = today.reduce((a, d) => a + (d.input_tokens || 0), 0);
      const tokOut = today.reduce((a, d) => a + (d.output_tokens || 0), 0);
      const stat = (label, value) => h('div', {}, h('dt', {}, label), h('dd', {}, value));
      return h('dl', { class: 'stats' },
        stat('requests', String(groupRequests(today).length)),
        stat('failovers', String(today.filter((d) => !d.success).length)),
        stat('tokens in', fmtNum(tokIn)),
        stat('tokens out', fmtNum(tokOut)));
    }
    if (kind === 'last') {
      const d = ds.find((x) => x.success);
      if (!d) return h('p', { class: 'quiet' }, 'No answers yet. Ask something.');
      return [
        h('p', { class: 'last-model' }, modelLabel(d.model)),
        h('p', {}, tierChip(d.tier), d.degraded ? chip('degraded', 'warn') : null, h('span', { class: 'quiet' }, ` ${fmtMs(d.latency_ms)} · attempt ${d.attempt}`)),
      ];
    }
    // live: the last few requests, a small window onto the router at work
    const groups = groupRequests(ds).slice(0, 4);
    if (!groups.length) return h('p', { class: 'quiet' }, 'Requests will appear here as zhunt routes them.');
    return [
      h('ul', { class: 'live' }, groups.map((g) => {
        const final = g.attempts.find((a) => a.success);
        return h('li', { class: final ? '' : 'failed' },
          h('span', { class: 'live-model' }, shortModel((final || g.last).model)),
          g.attempts.length > 1 ? chip(`${g.attempts.length} tries`, final ? 'warn' : 'err') : null,
          h('span', { class: 'quiet' }, final ? fmtMs(final.latency_ms) : 'failed'));
      })),
      h('button', { class: 'act', type: 'button', onclick: () => svc.openWindow('routing') }, 'Why these models?'),
    ];
  }

  const unsubS = svc.store.subscribe('status', (snap) => { status = snap; paint(); });
  const unsubD = svc.store.subscribe('decisions', (snap) => { decisions = snap; paint(); });
  paint();

  return {
    el,
    header: h('div', { class: 'row' }, customizeBtn),
    getState: () => ({ order, hidden: [...hidden] }),
    focusPrimary: () => el.closest('.pane')?.focus(),
    destroy() { unsubS(); unsubD(); },
  };
}
