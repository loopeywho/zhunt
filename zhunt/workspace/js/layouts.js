// Layout persistence. A layout document is plain JSON, versioned, and hand-editable:
//
//   { "version": 1, "kind": "zhunt-layout", "saved_at": "...", "focus": 0, "mode": "floating",
//     "tree": { "type": "split", "dir": "row", "ratio": 0.6,
//               "a": { "type": "tile", "kind": "chat", "state": {"alias": "zhunt-auto"} },
//               "b": { "type": "tile", "kind": "routing", "state": {},
//                      "win": { "x": 0.74, "y": 0.02, "w": 0.25, "h": 0.47, "min": false } } } }
//
// "mode" is "focus", "floating" or "tiled" (missing = tiled, as written before floating windows existed);
// "win" is the window's floating rectangle as fractions of the desktop.
//
// Named layouts go to the daemon (/zhunt/api/layouts/<name>); if it is unreachable they are kept in
// localStorage instead. The working layout is autosaved as the layout called "last" (and mirrored
// to localStorage) and restored on the next start.

import { api, ApiError } from './api.js';
import * as T from './tree.js';

export const LAYOUT_VERSION = 1;
export const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/;
const LAST = 'last';
const LS_LAST = 'zhunt.layout.last';
const LS_NAMED = 'zhunt.layouts.named';

export const PRESETS = {
  // One agent task with free-model capacity and usage visible from the first launch.
  desktop: () => T.split('row', T.tile('panels'), T.tile('agent'), 0.28),
  'single-chat': () => T.tile('chat', { alias: 'zhunt-auto' }),
  'chat-routing': () => T.split('row', T.tile('chat', { alias: 'zhunt-auto' }), T.tile('routing'), 0.62),
  'chat-compare-providers': () => T.split('row',
    T.tile('chat', { alias: 'zhunt-auto' }),
    T.split('col', T.tile('compare'), T.tile('providers'), 0.5), 0.4),
};
export const DEFAULT_PRESET = 'desktop';
// The desktop starts focused; specialist presets open tiled.
const presetMode = (name) => name === 'desktop' ? 'focus' : 'tiled';

function lsGet(key) { try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch { return null; } }
function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); return true; } catch { return false; } }

// Validate an untrusted document and turn it into {tree, focus}. Throws Error with a readable reason.
export function parseDoc(doc) {
  if (!doc || typeof doc !== 'object' || doc.kind !== 'zhunt-layout' || typeof doc.version !== 'number') {
    throw new Error('not a zhunt layout file');
  }
  if (doc.version > LAYOUT_VERSION) throw new Error(`layout version ${doc.version} is newer than this workspace understands (${LAYOUT_VERSION})`);
  // future migrations for version < LAYOUT_VERSION go here
  const tree = T.sanitize(doc.tree);
  if (!tree) throw new Error('layout contains no usable tiles');
  return { tree, focus: Number.isInteger(doc.focus) ? doc.focus : 0, mode: ['focus', 'floating', 'tiled'].includes(doc.mode) ? doc.mode : 'tiled' };
}

export function createLayouts({ workspace, toast }) {
  let currentName = null;
  let saveTimer = null;
  let apiDownUntil = 0;
  let dirty = false; // an autosave is owed to the daemon
  let changeSeq = 0; // bumped on every change, so a save that finishes late cannot clear a newer change
  const listeners = new Set();

  const makeDoc = () => {
    const s = workspace.serialize();
    return { version: LAYOUT_VERSION, kind: 'zhunt-layout', saved_at: new Date().toISOString(), focus: s.focus, mode: s.mode, tree: s.tree };
  };

  function apply({ tree, focus, mode }, name) {
    workspace.setRoot(tree, focus, mode);
    currentName = name;
    for (const fn of listeners) fn(currentName);
  }

  const isOffline = (err) => err instanceof ApiError && (err.status === 0 || err.status >= 500);

  // Save the working layout to the daemon. While the daemon is marked down we wait out the window and retry,
  // so a transient outage does not leave the last change unsaved until the next edit.
  async function flushAutosave() {
    if (!dirty) return;
    const wait = apiDownUntil - Date.now();
    if (wait > 0) { clearTimeout(saveTimer); saveTimer = setTimeout(flushAutosave, wait + 50); return; }
    const mine = changeSeq;
    try {
      await api.layouts.put(LAST, makeDoc());
      if (changeSeq === mine) dirty = false;
    } catch (err) {
      if (err.status === 401) return;
      apiDownUntil = Date.now() + 30000;
      if (isOffline(err)) { clearTimeout(saveTimer); saveTimer = setTimeout(flushAutosave, 30050); }
    }
  }

  // Closing the tab inside the 700 ms debounce would lose the change on the daemon: send it with keepalive.
  const flushOnLeave = () => {
    if (!dirty) return;
    clearTimeout(saveTimer);
    try { api.layouts.put(LAST, makeDoc(), { keepalive: true }).catch(() => {}); } catch { /* page is going away */ }
  };
  window.addEventListener('pagehide', flushOnLeave);

  const mgr = {
    get currentName() { return currentName; },
    onName(fn) { listeners.add(fn); },

    async list() {
      const local = Object.keys(lsGet(LS_NAMED) || {});
      let remote = [];
      let apiOk = true;
      try { remote = (await api.layouts.list()).layouts || []; } catch (err) { if (err.status === 401) throw err; apiOk = false; }
      const names = [...new Set([...remote, ...local])].filter((n) => n !== LAST).sort();
      return { names, apiOk, localOnly: new Set(local.filter((n) => !remote.includes(n))) };
    },

    async save(name) {
      if (!NAME_RE.test(name)) throw new Error('names are lowercase letters, digits, - and _ (max 40, must start with a letter or digit)');
      if (name === LAST) throw new Error('"last" is reserved for the automatic working layout');
      const doc = makeDoc();
      try {
        await api.layouts.put(name, doc);
        toast?.(`saved layout "${name}"`);
      } catch (err) {
        if (!isOffline(err) && err.status !== 404) throw err;
        const named = lsGet(LS_NAMED) || {};
        named[name] = doc;
        if (!lsSet(LS_NAMED, named)) throw new Error('could not save: daemon unreachable and browser storage is unavailable');
        toast?.(`saved layout "${name}" in this browser (daemon layouts API unavailable)`);
      }
      currentName = name;
      for (const fn of listeners) fn(currentName);
    },

    async load(name) {
      let doc = null;
      try { doc = await api.layouts.get(name); } catch (err) {
        if (err.status === 401) throw err;
        doc = (lsGet(LS_NAMED) || {})[name] || null;
        if (!doc) throw (err.status === 404 ? new Error(`no layout named "${name}"`) : err);
      }
      apply(parseDoc(doc), name);
      toast?.(`loaded layout "${name}"`);
    },

    async remove(name) {
      let removed = false;
      const named = lsGet(LS_NAMED) || {};
      if (name in named) { delete named[name]; lsSet(LS_NAMED, named); removed = true; }
      try { await api.layouts.remove(name); removed = true; } catch (err) {
        if (err.status === 401) throw err;
        if (err.status !== 404 && !removed) throw err;
      }
      if (!removed) throw new Error(`no layout named "${name}"`);
      toast?.(`deleted layout "${name}"`);
    },

    loadPreset(name) {
      const make = PRESETS[name];
      if (!make) throw new Error('unknown preset ' + name);
      apply({ tree: make(), focus: 0, mode: presetMode(name) }, null);
      toast?.(`preset ${name}`);
    },

    // Debounced; called on every structural or state change.
    queueAutosave() {
      lsSet(LS_LAST, makeDoc()); // cheap and synchronous: survives a reload right after a change
      dirty = true;
      changeSeq += 1;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(flushAutosave, 700);
    },

    // Last working layout: the newer of the daemon copy and this browser's copy (by saved_at), then the
    // default preset. A daemon copy can be stale (daemon was down, or the tab closed before the save landed).
    async restore() {
      const candidates = [];
      try { candidates.push(['daemon', await api.layouts.get(LAST)]); } catch (err) { if (err.status === 401) throw err; }
      candidates.push(['browser', lsGet(LS_LAST)]);
      const stamp = (doc) => { const t = Date.parse(doc && doc.saved_at); return Number.isFinite(t) ? t : -1; };
      candidates.sort((x, y) => stamp(y[1]) - stamp(x[1])); // stable: ties (incl. no stamps) keep daemon first
      for (const [, doc] of candidates) {
        if (!doc) continue;
        try { apply(parseDoc(doc), null); return true; } catch (err) { console.warn('layout restore skipped:', err.message); }
      }
      apply({ tree: PRESETS[DEFAULT_PRESET](), focus: 1, mode: 'focus' }, null); // focus the chat window
      return false;
    },
  };
  return mgr;
}
