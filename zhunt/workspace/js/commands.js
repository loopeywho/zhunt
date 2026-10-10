// One registry of every action. The palette, the leader key, the status-bar hints and the help
// sheet are all generated from it, so they cannot drift apart.

import { KINDS } from './tree.js';
import { KIND_LABELS } from './tiles/index.js';
import { PRESETS, NAME_RE } from './layouts.js';

const DIRS = [
  ['left', 'h', 'ArrowLeft', 'H'],
  ['down', 'j', 'ArrowDown', 'J'],
  ['up', 'k', 'ArrowUp', 'K'],
  ['right', 'l', 'ArrowRight', 'L'],
];

export function buildCommands(app) {
  const { ws, layouts, themes, overlays, toast, store } = app;
  const fail = (err) => toast(err.message || String(err), true);
  const cmds = [];
  const add = (c) => cmds.push(c);

  // ---- tiles ----
  add({ id: 'split-right', group: 'Tile', title: 'Split right', leader: ['v'], run: () => ws.split('row') });
  add({ id: 'split-down', group: 'Tile', title: 'Split down', leader: ['s'], run: () => ws.split('col') });
  add({ id: 'close', group: 'Tile', title: 'Close tile', leader: ['x', 'q'], run: () => ws.closeFocused() });
  add({ id: 'zoom', group: 'Tile', title: 'Zoom tile (toggle fullscreen)', leader: ['z'], direct: 'Alt+Enter', run: () => ws.toggleZoom() });
  add({ id: 'input', group: 'Tile', title: 'Focus tile input (insert mode)', leader: ['i'], run: () => ws.focusPrimary() });
  for (const [dir, key, arrow, swapKey] of DIRS) {
    add({ id: 'focus-' + dir, group: 'Focus', title: 'Focus ' + dir, leader: [key, arrow], direct: 'Alt+' + key, run: () => ws.focusDir(dir) });
    add({ id: 'swap-' + dir, group: 'Tile', title: 'Swap with tile ' + dir, leader: [swapKey, 'Shift+' + arrow], direct: 'Alt+Shift+' + key, run: () => ws.swap(dir) });
  }
  add({ id: 'grow-w', group: 'Resize', title: 'Wider', leader: ['.', '>'], sticky: true, run: () => ws.resize('x', 0.04) });
  add({ id: 'shrink-w', group: 'Resize', title: 'Narrower', leader: [',', '<'], sticky: true, run: () => ws.resize('x', -0.04) });
  add({ id: 'grow-h', group: 'Resize', title: 'Taller', leader: ['=', '+'], sticky: true, run: () => ws.resize('y', 0.04) });
  add({ id: 'shrink-h', group: 'Resize', title: 'Shorter', leader: ['-', '_'], sticky: true, run: () => ws.resize('y', -0.04) });
  add({ id: 'equalize', group: 'Resize', title: 'Equalize all tiles', leader: ['e'], run: () => ws.equalize() });
  KINDS.forEach((kind, i) => {
    add({ id: 'kind-' + kind, group: 'Tile', title: 'Make this tile: ' + KIND_LABELS[kind], leader: [String(i + 1)], run: () => ws.setKind(kind) });
    add({ id: 'split-right-' + kind, group: 'Split right', title: KIND_LABELS[kind], run: () => ws.split('row', kind) });
    add({ id: 'split-down-' + kind, group: 'Split down', title: KIND_LABELS[kind], run: () => ws.split('col', kind) });
  });

  // ---- layouts ----
  add({
    id: 'layout-save', group: 'Layout', title: 'Save layout as...', leader: ['w'],
    run: async () => {
      const name = await overlays.prompt('save layout as', {
        initial: layouts.currentName || '', hint: 'lowercase letters, digits, - and _',
        validate: (n) => (n && !NAME_RE.test(n) ? 'use a-z 0-9 - _ , start with a letter or digit, max 40' : n === 'last' ? '"last" is reserved' : ''),
      });
      if (name) await layouts.save(name).catch(fail);
    },
  });
  add({
    id: 'layout-load', group: 'Layout', title: 'Load layout...', leader: ['o'],
    run: async () => {
      let names = []; let local = new Set();
      try { ({ names, localOnly: local } = await layouts.list()); } catch (err) { return fail(err); }
      const items = [
        ...names.map((n) => ({ title: n, group: 'saved', hint: local.has(n) ? 'this browser' : '', pick: () => layouts.load(n).catch(fail) })),
        ...Object.keys(PRESETS).map((n) => ({ title: n, group: 'preset', pick: () => { try { layouts.loadPreset(n); } catch (e) { fail(e); } } })),
      ];
      const picked = await overlays.palette(items, { placeholder: 'load layout' });
      picked?.pick();
    },
  });
  add({
    id: 'layout-delete', group: 'Layout', title: 'Delete saved layout...',
    run: async () => {
      let names = [];
      try { ({ names } = await layouts.list()); } catch (err) { return fail(err); }
      if (!names.length) return toast('no saved layouts');
      const picked = await overlays.palette(names.map((n) => ({ title: n })), { placeholder: 'delete which layout?' });
      if (!picked) return undefined;
      const sure = await overlays.palette([{ title: `yes, delete "${picked.title}"`, yes: true }, { title: 'cancel' }], { placeholder: 'confirm delete' });
      if (sure?.yes) await layouts.remove(picked.title).catch(fail);
      return undefined;
    },
  });

  // ---- themes ----
  add({ id: 'theme-next', group: 'Theme', title: 'Next theme', leader: ['t'], run: () => themes.next() });
  for (const t of themes.list) add({ id: 'theme-' + t.name, group: 'Theme', title: t.name + (t.builtin ? '' : '  (user)'), run: () => themes.apply(t.name) });

  // ---- misc ----
  add({ id: 'palette', group: 'App', title: 'Command palette', leader: [':'], direct: 'Ctrl+K', hidden: true, run: () => app.openPalette() });
  add({ id: 'help', group: 'App', title: 'Keyboard reference', leader: ['?'], direct: '?', run: () => app.openHelp() });
  add({ id: 'refresh', group: 'App', title: 'Refresh status and decisions now', leader: ['r'], run: () => { store.refresh('status'); store.refresh('decisions'); toast('refreshing'); } });
  return cmds;
}

export const leaderLabel = (c) => c.leader[0].replace('ArrowLeft', '<-').replace('ArrowDown', 'v').replace('ArrowUp', '^').replace('ArrowRight', '->');
