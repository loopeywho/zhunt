import { captureKeyFromFragment, getKey, clearKey, onUnauthorized } from './api.js';
import { showAuthScreen } from './auth.js';
import { createStore } from './store.js';
import { createWorkspace } from './tiling.js';
import { createTileFactory } from './tiles/index.js';
import { createLayouts } from './layouts.js';
import { createThemes } from './themes.js';
import { createOverlays } from './palette.js';
import { createStatusbar } from './statusbar.js';
import { createKeys } from './keys.js';
import { buildCommands, leaderLabel } from './commands.js';
import { leaves } from './tree.js';

let authShown = false; // module scope: the fatal-error handler at the bottom must know whether the auth screen is up
let store = null;

async function boot() {
  const badKey = captureKeyFromFragment(); // key -> sessionStorage, fragment removed from the address bar
  // `zhunt open` pointed at an already-open tab only changes the fragment: pick the new key up.
  window.addEventListener('hashchange', () => {
    if (location.hash.includes('key=')) { captureKeyFromFragment(); location.reload(); }
  });

  const denied = (reason) => {
    if (authShown) return;
    authShown = true;
    clearKey();
    store?.stop(); // no more unauthenticated status/decisions polling behind the auth screen
    showAuthScreen(reason);
  };
  onUnauthorized(() => denied('rejected'));
  if (!getKey()) { denied(badKey ? 'invalid' : 'missing'); return; }

  const stage = document.getElementById('stage');
  const bar = document.getElementById('statusbar');
  const overlayRoot = document.getElementById('overlay-root');

  store = createStore();
  const statusbar = createStatusbar(bar, { store });
  const toast = (text, isError) => statusbar.toast(text, isError);
  const overlays = createOverlays(overlayRoot);

  // Verify the key before building anything (a 401 here shows the auth screen).
  await store.refresh('status');
  if (authShown) return;

  const themes = createThemes({ toast, onChange: (name) => statusbar.setTheme(name) });
  statusbar.setTheme(themes.current);
  themes.refresh().then(() => themes.apply(themes.current, { persist: false }));

  let layouts = null;
  const updateWhere = () => {
    const all = leaves(ws.root);
    const i = all.findIndex((n) => n.id === ws.focusedId);
    const node = all[i];
    statusbar.setWhere(`[${i + 1}/${all.length} ${node ? node.kind : ''}]${ws.zoomId ? ' zoom' : ''} ${layouts?.currentName ? 'layout:' + layouts.currentName : 'layout:unsaved'}`);
  };
  const ws = createWorkspace(stage, {
    makeTile: createTileFactory({ store, toast }),
    onChange: () => { layouts?.queueAutosave(); },
    onFocus: updateWhere,
    toast,
  });
  layouts = createLayouts({ workspace: ws, toast });
  layouts.onName(updateWhere);

  const app = { ws, layouts, themes, overlays, toast, store, openPalette: () => openPalette(), openHelp: () => openHelp() };
  const getCommands = () => buildCommands(app);

  function openPalette() {
    const items = getCommands().filter((c) => !c.hidden).map((c) => ({
      title: c.title,
      group: c.group,
      keys: c.leader ? 'Leader ' + leaderLabel(c) : c.direct || '',
      cmd: c,
    }));
    overlays.palette(items, { placeholder: 'run a command' }).then((picked) => {
      if (!picked) return;
      Promise.resolve().then(() => picked.cmd.run()).catch((err) => toast(err.message || String(err), true));
    });
  }

  function openHelp() {
    const groups = new Map();
    for (const c of getCommands()) {
      if (c.id.startsWith('split-right-') || c.id.startsWith('split-down-') || c.id.startsWith('theme-') && c.id !== 'theme-next') continue;
      const keys = [c.leader ? 'Leader ' + c.leader.slice(0, 2).join(' / ') : null, c.direct].filter(Boolean).join('   ');
      if (!keys) continue;
      if (!groups.has(c.group)) groups.set(c.group, []);
      groups.get(c.group).push([keys, c.title]);
    }
    const rows = [...groups].map(([title, rowsOf]) => ({ title, rows: rowsOf }));
    rows.unshift({
      title: 'Basics',
      rows: [
        ['Ctrl+Space  (or Ctrl+\\)', 'Leader key: press, then one key from the groups below. Esc cancels.'],
        ['Ctrl+K  (or Cmd+K)', 'Command palette: every action, searchable'],
        ['Esc', 'Leave a text field; focus returns to the tile (normal mode)'],
        ['h j k l / arrows', 'Normal mode (tile focused, not typing): move focus. H J K L swaps. i or Enter types. z zooms.'],
        ['Drag a divider', 'Resize. Double-click resets it. With a divider focused, arrows resize too.'],
      ],
    });
    overlays.help(rows);
  }

  createKeys({ ws, getCommands, overlays, openPalette, openHelp, statusbar, toast });

  await layouts.restore();
  if (authShown) return;
  updateWhere();
  ws.focusPrimary();
}

boot().catch((err) => {
  console.error(err);
  if (authShown) return; // the auth screen is already the explanation
  const msg = document.createElement('pre');
  msg.className = 'fatal';
  msg.textContent = 'zhunt workspace failed to start: ' + (err && err.message ? err.message : String(err));
  document.body.append(msg);
});
