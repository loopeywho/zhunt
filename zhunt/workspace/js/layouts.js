// Layout persistence. A layout document is plain JSON, versioned, and hand-editable:
//
//   { "version": 1, "kind": "zhunt-layout", "saved_at": "...", "focus": 0,
//     "tree": { "type": "split", "dir": "row", "ratio": 0.6,
//               "a": { "type": "tile", "kind": "chat", "state": {"alias": "zhunt-auto"} },
//               "b": { "type": "tile", "kind": "routing", "state": {} } } }
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
  'single-chat': () => T.tile('chat', { alias: 'zhunt-auto' }),
  'chat-routing': () => T.split('row', T.tile('chat', { alias: 'zhunt-auto' }), T.tile('routing'), 0.62),
  'chat-compare-providers': () => T.split('row',
    T.tile('chat', { alias: 'zhunt-auto' }),
    T.split('col', T.tile('compare'), T.tile('providers'), 0.5), 0.4),
};
export const DEFAULT_PRESET = 'chat-routing';

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
  return { tree, focus: Number.isInteger(doc.focus) ? doc.focus : 0 };
}

export function createLayouts({ workspace, toast }) {
  let currentName = null;
  let saveTimer = null;
  let apiDownUntil = 0;
  const listeners = new Set();

  const makeDoc = () => {
    const s = workspace.serialize();
    return { version: LAYOUT_VERSION, kind: 'zhunt-layout', saved_at: new Date().toISOString(), focus: s.focus, tree: s.tree };
  };

  function apply({ tree, focus }, name) {
    workspace.setRoot(tree, focus);
    currentName = name;
    for (const fn of listeners) fn(currentName);
  }

  const isOffline = (err) => err instanceof ApiError && (err.status === 0 || err.status >= 500);

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
      apply({ tree: make(), focus: 0 }, null);
      toast?.(`preset ${name}`);
    },

    // Debounced; called on every structural or state change.
    queueAutosave() {
      lsSet(LS_LAST, makeDoc()); // cheap and synchronous: survives a reload right after a change
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async () => {
        const doc = makeDoc();
        if (Date.now() < apiDownUntil) return;
        try { await api.layouts.put(LAST, doc); } catch (err) {
          if (err.status !== 401) apiDownUntil = Date.now() + 30000;
        }
      }, 700);
    },

    // Last working layout: daemon copy first, then this browser's copy, then the default preset.
    async restore() {
      const candidates = [];
      try { candidates.push(['daemon', await api.layouts.get(LAST)]); } catch (err) { if (err.status === 401) throw err; }
      candidates.push(['browser', lsGet(LS_LAST)]);
      for (const [, doc] of candidates) {
        if (!doc) continue;
        try { apply(parseDoc(doc), null); return true; } catch (err) { console.warn('layout restore skipped:', err.message); }
      }
      apply({ tree: PRESETS[DEFAULT_PRESET](), focus: 0 }, null);
      return false;
    },
  };
  return mgr;
}
