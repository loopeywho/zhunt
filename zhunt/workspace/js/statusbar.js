// Slim bottom bar: mode, focused tile, layout name, leader hints, quota summary, theme, connection.

import { h } from './dom.js';
import { leaderLabel } from './commands.js';

export function createStatusbar(root, { store }) {
  const mode = h('span', { class: 'sb-mode normal' }, 'NORMAL');
  const where = h('span', { class: 'sb-where' });
  const hints = h('span', { class: 'sb-hints' });
  const toastEl = h('span', { class: 'sb-toast', role: 'status', 'aria-live': 'polite' });
  const quota = h('span', { class: 'sb-quota' });
  const themeEl = h('span', { class: 'sb-theme' });
  const conn = h('span', { class: 'sb-conn', title: 'daemon connection' });
  root.replaceChildren(mode, where, h('span', { class: 'sb-mid' }, hints, toastEl), quota, themeEl, conn);

  let toastTimer = null;
  let leaderHints = null;
  const idleHints = () => [
    h('span', { class: 'hint' }, h('kbd', {}, 'Ctrl+Space'), ' leader'),
    h('span', { class: 'hint' }, h('kbd', {}, 'Ctrl+K'), ' palette'),
    h('span', { class: 'hint' }, h('kbd', {}, 'Alt+hjkl'), ' focus'),
    h('span', { class: 'hint' }, h('kbd', {}, '?'), ' help'),
  ];
  function paintHints() {
    hints.replaceChildren();
    if (leaderHints) {
      const seen = new Set();
      for (const c of leaderHints) {
        if (seen.has(c.id) || c.id.startsWith('split-right-') || c.id.startsWith('split-down-')) continue;
        if (c.id.startsWith('focus-') || c.id.startsWith('swap-') || c.id.startsWith('kind-')) continue; // grouped below
        seen.add(c.id);
        hints.append(h('span', { class: 'hint' }, h('kbd', {}, leaderLabel(c)), ' ' + c.title.toLowerCase().replace(/ \(.*\)/, '').replace('...', '')));
      }
      hints.append(h('span', { class: 'hint' }, h('kbd', {}, 'hjkl'), ' focus'), h('span', { class: 'hint' }, h('kbd', {}, 'HJKL'), ' swap'),
        h('span', { class: 'hint' }, h('kbd', {}, '1-7'), ' tile type'), h('span', { class: 'hint' }, h('kbd', {}, 'Esc'), ' cancel'));
    } else {
      hints.append(...idleHints());
    }
  }
  paintHints();

  store.subscribe('status', (snap) => {
    conn.classList.toggle('bad', !!snap.error && !snap.data);
    conn.classList.toggle('ok', !snap.error);
    conn.textContent = snap.error ? 'offline' : 'online';
    conn.title = snap.error ? snap.error.message : 'connected to the zhunt daemon';
    if (!snap.data) { quota.textContent = ''; return; }
    const models = (snap.data.providers || []).flatMap((p) => p.models || []).filter((m) => m.free || snap.data.policy?.allow_paid);
    const usable = models.filter((m) => m.allowed && !(m.cooldown_remaining > 0)).length;
    const keys = (snap.data.providers || []).filter((p) => p.key_set).length;
    quota.textContent = `${usable}/${models.length} usable  ${keys}/${(snap.data.providers || []).length} keys`;
  });

  // Omarchy-style key pop-up: while the leader is armed, every next key, grouped.
  const whichkey = h('div', { class: 'whichkey', hidden: true, 'aria-hidden': 'true' });
  document.body.append(whichkey);
  function paintWhichkey(cmds) {
    const groups = new Map();
    const add = (group, keys, title) => { if (!groups.has(group)) groups.set(group, []); groups.get(group).push([keys, title]); };
    for (const c of cmds) {
      if (/^(split-right-|split-down-|focus-|swap-|kind-|theme-(?!menu))/.test(c.id)) continue;
      add(c.group, c.leader.slice(0, 2).map((k) => k.replace('ArrowLeft', '<-').replace('ArrowRight', '->')).join(' '), c.title.replace(/ \(.*\)/, '').replace('...', ''));
    }
    add('Focus', 'h j k l', 'Move between windows');
    add('Tile', 'H J K L', 'Swap tiles (floating: move window)');
    add('Tile', '1-' + cmds.filter((c) => c.id.startsWith('kind-')).length, 'Change this window\'s type');
    whichkey.replaceChildren(
      h('div', { class: 'wk-title' }, 'Ctrl+Space then...', h('span', { class: 'wk-esc' }, 'Esc closes')),
      h('div', { class: 'wk-grid' }, [...groups].map(([group, rows]) => h('section', { class: 'wk-group' },
        h('h4', {}, group),
        rows.map(([keys, title]) => h('div', { class: 'wk-row' }, h('kbd', {}, keys), h('span', {}, title)))))));
  }

  return {
    setMode(m) {
      mode.className = 'sb-mode ' + m;
      mode.textContent = m.toUpperCase();
    },
    setLeader(on, cmds) {
      leaderHints = on ? cmds : null;
      if (on) paintWhichkey(cmds);
      whichkey.hidden = !on;
      root.classList.toggle('leader', on);
      if (on) { hints.hidden = false; toastEl.textContent = ''; }
      paintHints();
    },
    setWhere(text) { where.textContent = text; },
    setTheme(name) { themeEl.textContent = name; },
    toast(text, isError = false) {
      toastEl.textContent = text;
      toastEl.classList.toggle('err', isError);
      hints.hidden = true;
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => { toastEl.textContent = ''; hints.hidden = false; }, isError ? 5000 : 2500);
    },
  };
}
