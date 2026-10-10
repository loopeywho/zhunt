// Run: node workspace_dev/checks/layouts.mjs   (JS_DIR=/other/copy/of/js to test another copy)
// F9: restore() takes the newer of daemon/browser copies; pagehide flushes with keepalive; autosave retries
// after the 30 s daemon-down window.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { mock } from 'node:test';

const here = path.dirname(fileURLToPath(import.meta.url));
const dir = process.env.JS_DIR || path.join(here, '../../zhunt/workspace/js');

const ls = new Map();
globalThis.localStorage = { getItem: (k) => ls.get(k) ?? null, setItem: (k, v) => ls.set(k, v), removeItem: (k) => ls.delete(k) };
globalThis.sessionStorage = { getItem: () => 'k', setItem() {}, removeItem() {} };
const listeners = {};
globalThis.window = { addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); } };
globalThis.document = { hidden: false, addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); } };
globalThis.addEventListener = globalThis.window.addEventListener;

const puts = [];
let daemonDoc = null;
let daemonDown = false;
globalThis.fetch = async (url, opts = {}) => {
  const res = (status, body) => ({ status, ok: status < 400, headers: { get: () => 'application/json' }, text: async () => JSON.stringify(body), json: async () => body });
  if (daemonDown) throw new TypeError('network down');
  if ((opts.method || 'GET') === 'PUT') { puts.push({ url: String(url), keepalive: !!opts.keepalive, doc: JSON.parse(opts.body) }); daemonDoc = JSON.parse(opts.body); return res(200, {}); }
  if (String(url).endsWith('/layouts/last')) return daemonDoc ? res(200, daemonDoc) : res(404, { detail: 'nope' });
  return res(200, {});
};

const { createLayouts } = await import(pathToFileURL(path.join(dir, 'layouts.js')).href);
const T = await import(pathToFileURL(path.join(dir, 'tree.js')).href);

const mkDoc = (kind, saved_at) => ({ version: 1, kind: 'zhunt-layout', saved_at, focus: 0, tree: { type: 'tile', kind, state: {} } });
const stubWs = () => {
  const ws = { root: null, setRoot(tree) { ws.root = tree; }, serialize: () => ({ focus: 0, tree: ws.root || T.tile('chat', {}) }) };
  return ws;
};
let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };

// restore: browser copy newer than daemon copy
{
  daemonDoc = mkDoc('routing', '2026-01-01T10:00:00.000Z');
  ls.set('zhunt.layout.last', JSON.stringify(mkDoc('logs', '2026-01-01T10:05:00.000Z')));
  const ws = stubWs();
  await createLayouts({ workspace: ws, toast() {} }).restore();
  check('F9 newer browser copy beats older daemon copy', ws.root?.kind === 'logs', JSON.stringify(ws.root));
}
// restore: daemon copy newer
{
  daemonDoc = mkDoc('routing', '2026-01-01T11:00:00.000Z');
  ls.set('zhunt.layout.last', JSON.stringify(mkDoc('logs', '2026-01-01T10:05:00.000Z')));
  const ws = stubWs();
  await createLayouts({ workspace: ws, toast() {} }).restore();
  check('F9 newer daemon copy beats older browser copy', ws.root?.kind === 'routing', JSON.stringify(ws.root));
}
// restore: daemon copy without a stamp -> the stamped browser copy wins; and with no stamps the daemon wins
{
  daemonDoc = mkDoc('routing', undefined);
  ls.set('zhunt.layout.last', JSON.stringify(mkDoc('logs', '2026-01-01T10:05:00.000Z')));
  let ws = stubWs();
  await createLayouts({ workspace: ws, toast() {} }).restore();
  check('F9 unstamped daemon copy loses to stamped browser copy', ws.root?.kind === 'logs', JSON.stringify(ws.root));
  ls.set('zhunt.layout.last', JSON.stringify(mkDoc('logs', undefined)));
  ws = stubWs();
  await createLayouts({ workspace: ws, toast() {} }).restore();
  check('F9 no stamps anywhere: daemon copy is kept as before', ws.root?.kind === 'routing', JSON.stringify(ws.root));
}
// restore: daemon down -> browser copy
{
  daemonDown = true;
  const ws = stubWs();
  await createLayouts({ workspace: ws, toast() {} }).restore();
  check('F9 daemon unreachable: browser copy is used', ws.root?.kind === 'logs', JSON.stringify(ws.root));
  daemonDown = false;
}

// pagehide flush
{
  mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const lay = createLayouts({ workspace: stubWs(), toast() {} });
  puts.length = 0;
  lay.queueAutosave();
  for (const fn of listeners.pagehide || []) fn({});
  await new Promise((r) => setImmediate(r));
  check('F9 pagehide flushes the pending autosave with keepalive', puts.length === 1 && puts[0].keepalive === true, JSON.stringify(puts.map((p) => p.keepalive)));
  mock.timers.reset();
}

// retry after the 30 s window
{
  mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  const lay = createLayouts({ workspace: stubWs(), toast() {} });
  puts.length = 0;
  daemonDown = true;
  lay.queueAutosave();
  mock.timers.tick(800);
  for (let j = 0; j < 5; j += 1) await new Promise((r) => setImmediate(r)); // first save fails -> daemon marked down
  daemonDown = false;
  mock.timers.tick(31000);
  for (let j = 0; j < 5; j += 1) await new Promise((r) => setImmediate(r));
  check('F9 autosave retried after the daemon-down window without another change', puts.length >= 1, String(puts.length));
  mock.timers.reset();
}
process.exit(fail ? 1 : 0);
