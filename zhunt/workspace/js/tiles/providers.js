// Providers tile: key status, per-model headroom, cooldown countdown, reliability, blocked reasons.
// It names the environment variable for a missing key but never asks for the key itself.

import { h, fmtDuration, splitModel, tablist } from '../dom.js';
import { tierChip, chip, headroomBar, errorBox, emptyState } from './util.js';

export function providersTile(svc, state, ctx) {
  let filter = ['all', 'usable', 'blocked'].includes(state.filter) ? state.filter : 'all';
  let snap = null;
  let tick = null;

  const tabs = tablist('Filter models', [
    { id: 'all', label: 'all' }, { id: 'usable', label: 'usable' }, { id: 'blocked', label: 'blocked' },
  ], filter, (id) => { filter = id; ctx.changed(); if (snap) paint(); });
  const policy = h('div', { class: 'policy dim' });
  const list = h('div', { class: 'prov-list' });
  list.dataset.keepScroll = '1';
  const el = h('div', { class: 'providers' }, policy, list);

  function cooldownLeft(m) {
    const base = m.cooldown_remaining || 0;
    return Math.max(0, base - (Date.now() - snap.at) / 1000);
  }

  function paint() {
    list.replaceChildren();
    if (snap.error && !snap.data) { list.append(errorBox('Cannot load provider status', snap.error.message)); policy.textContent = ''; return; }
    const s = snap.data;
    policy.replaceChildren(...[
      'policy: ', chip(s.policy?.allow_paid ? 'paid allowed' : 'free only', s.policy?.allow_paid ? 'warn' : ''),
      ' ', chip(s.policy?.allow_training ? 'training allowed' : 'no prompt-training', s.policy?.allow_training ? 'warn' : ''),
      snap.error ? h('span', { class: 'meta-err' }, '  stale: ' + snap.error.message) : null,
    ].filter(Boolean));
    let anyCooldown = false;
    let shown = 0;
    for (const p of s.providers || []) {
      const models = (p.models || []).filter((m) => (filter === 'all' ? true : filter === 'usable' ? m.allowed && !(m.cooldown_remaining > 0) : !m.allowed || m.cooldown_remaining > 0));
      if (!models.length && filter !== 'all') continue;
      shown += 1;
      const sec = h('section', { class: 'prov' + (p.key_set ? '' : ' nokey') },
        h('header', { class: 'prov-head' },
          h('h3', {}, p.name || p.platform),
          p.key_set ? chip('key set', 'ok') : chip('no key', 'err'),
          h('span', { class: 'dim prov-plat' }, p.platform)));
      if (!p.key_set) {
        sec.append(h('div', { class: 'cta' },
          h('p', {}, 'Not usable yet. Set ', h('code', {}, p.key_env || (p.platform.toUpperCase() + '_API_KEY')), ' in the environment where the zhunt daemon runs, then restart it.'),
          h('p', { class: 'dim' }, 'This page never asks for or sees provider keys.')));
      }
      const rows = h('div', { class: 'mrows', role: 'table', 'aria-label': (p.name || p.platform) + ' models' });
      rows.append(h('div', { class: 'mrow mhead', role: 'row' },
        ['model', 'tiers', 'headroom', 'cooldown', 'reliab.', 'status'].map((c) => h('span', { role: 'columnheader' }, c))));
      for (const m of models) {
        const left = cooldownLeft(m);
        if (left > 0) anyCooldown = true;
        const cd = h('span', { class: 'cd', role: 'cell' });
        if (left > 0) { cd.dataset.until = String(Date.now() + left * 1000); cd.textContent = fmtDuration(left); cd.classList.add('active'); } else cd.textContent = '-';
        const limits = m.limits ? Object.entries(m.limits).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => k + ' ' + v).join(' · ') : '';
        rows.append(h('div', { class: 'mrow' + (m.allowed ? '' : ' blocked'), role: 'row', title: limits ? 'limits: ' + limits : undefined },
          h('span', { class: 'mname', role: 'cell' }, splitModel(m.id).name, m.free ? null : chip('paid', 'warn'), m.may_train_on_prompts ? chip('trains', 'warn', 'May train on prompts') : null),
          h('span', { class: 'mtiers', role: 'cell' }, (m.tiers || []).map(tierChip)),
          h('span', { role: 'cell' }, headroomBar(m.headroom)),
          cd,
          h('span', { class: 'rel', role: 'cell' }, m.reliability === null || m.reliability === undefined ? '-' : Math.round(m.reliability * 100) + '%'),
          h('span', { role: 'cell' }, m.allowed ? (left > 0 ? chip('cooling', 'warn') : chip('ready', 'ok')) : chip(m.blocked_reason || 'blocked', 'err'))));
      }
      sec.append(rows);
      list.append(sec);
    }
    if (!shown) list.append(emptyState('Nothing matches this filter.'));
    clearInterval(tick);
    tick = anyCooldown ? setInterval(updateCountdowns, 1000) : null;
  }

  function updateCountdowns() {
    let live = false;
    for (const cd of list.querySelectorAll('.cd.active')) {
      const left = (Number(cd.dataset.until) - Date.now()) / 1000;
      if (left <= 0) { cd.textContent = 'ready'; cd.classList.remove('active'); } else { cd.textContent = fmtDuration(left); live = true; }
    }
    if (!live) { clearInterval(tick); tick = null; if (snap) svc.store.refresh('status'); }
  }

  const unsub = svc.store.subscribe('status', (s) => { snap = s; paint(); });

  return {
    el,
    header: h('div', { class: 'row' }, tabs.el),
    getState: () => ({ filter }),
    focusPrimary: () => el.closest('.pane')?.focus(),
    destroy() { unsub(); clearInterval(tick); },
  };
}
