// Desktop chrome around the windows: the top bar (clock, readiness, Floating/Tiled switch, new
// window, theme menu) and, in floating mode or on a narrow screen, a dock listing every window.
// The wallpaper is the theme's --wallpaper, or a sky that follows the local time when a theme has none.

import { h } from './dom.js';
import { KINDS } from './tree.js';
import { KIND_LABELS } from './tiles/index.js';

export function skyFor(hour) {
  if (hour >= 5 && hour < 8) return 'dawn';
  if (hour >= 8 && hour < 17) return 'day';
  if (hour >= 17 && hour < 20) return 'dusk';
  return 'night';
}

const SHORT = { chat: 'Chat', foryou: 'For you', panels: 'Panels', compare: 'Compare', routing: 'Routing', providers: 'Providers', logs: 'Logs' };

export function createDesktop({ appEl, topbar, dock, ws, store, themes, overlays, toast, openPalette }) {
  const clock = h('span', { class: 'top-clock' });
  const pulse = h('span', { class: 'top-pulse', role: 'status' });
  const modeBtns = ['floating', 'tiled'].map((m) => h('button', {
    class: 'seg-btn', type: 'button', role: 'radio', 'aria-checked': 'false', dataset: { mode: m },
    title: m === 'floating' ? 'Floating windows you can drag and overlap (Leader g)' : 'Tiled windows that fill the screen (Leader g)',
    onclick: () => { ws.setMode(m); refresh(); },
  }, m === 'floating' ? 'Floating' : 'Tiled'));
  const seg = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Window style' }, modeBtns);
  const btn = (label, title, onclick) => h('button', { class: 'top-btn', type: 'button', title, onclick }, label);
  topbar.replaceChildren(
    h('span', { class: 'top-mark' }, 'zhunt'), clock, h('span', { class: 'top-spacer' }), pulse, seg,
    btn('+ Window', 'Open a new window (Leader n)', () => newWindowMenu()),
    btn('Theme', 'Choose a theme (Leader t)', () => themeMenu()),
    btn('⌘K', 'Command palette (Ctrl+K)', () => openPalette()));

  function tick() {
    const d = new Date();
    appEl.dataset.sky = skyFor(d.getHours());
    clock.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  tick();
  setInterval(tick, 30000);

  store.subscribe('status', (snap) => {
    if (!snap.data) {
      pulse.textContent = snap.error ? 'offline' : '';
      pulse.className = 'top-pulse' + (snap.error ? ' bad' : '');
      return;
    }
    const models = (snap.data.providers || []).flatMap((p) => p.models || []);
    const usable = models.filter((m) => m.allowed && !(m.cooldown_remaining > 0)).length;
    pulse.textContent = `${usable} free model${usable === 1 ? '' : 's'} ready`;
    pulse.className = 'top-pulse' + (snap.error ? ' bad' : usable ? ' ok' : ' warn');
  });

  async function newWindowMenu() {
    const picked = await overlays.palette(KINDS.map((k) => ({ title: KIND_LABELS[k], group: 'window', kind: k })), { placeholder: 'open which window?' });
    if (picked) ws.split('row', picked.kind);
  }

  async function themeMenu() {
    await themes.refresh();
    const current = themes.current;
    const items = themes.list.map((t) => ({ title: t.name, group: t.builtin ? 'theme' : 'your theme', hint: t.name === current ? 'current' : '', name: t.name }));
    const picked = await overlays.palette(items, { placeholder: 'choose a theme' });
    if (picked) { await themes.apply(picked.name); toast(`theme ${picked.name}`); }
  }

  function refresh() {
    const mode = ws.mode;
    for (const b of modeBtns) b.setAttribute('aria-checked', String(b.dataset.mode === mode));
    // floating: the dock lists and restores windows; on a narrow screen (one window at a time, either
    // mode) it is the only touch-friendly way to switch windows
    const showDock = mode === 'floating' || ws.narrow;
    dock.hidden = !showDock;
    appEl.classList.toggle('has-dock', showDock); // windows stay clear of the dock
    if (!showDock) return;
    dock.replaceChildren(...ws.windows().map((w, i) => h('button', {
      class: 'dock-btn' + (w.min ? ' is-min' : '') + (w.focused && !w.min ? ' is-focused' : ''), type: 'button',
      title: (w.min ? 'Restore ' : 'Show ') + KIND_LABELS[w.kind] + ` (Alt+${i + 1})`,
      onclick: () => { ws.setFocus(w.id, { dom: true }); refresh(); },
    }, SHORT[w.kind] || w.kind)),
    h('button', { class: 'dock-btn dock-add', type: 'button', 'aria-label': 'Open a new window', onclick: () => newWindowMenu() }, '+'));
  }

  return { refresh, newWindowMenu, themeMenu };
}
