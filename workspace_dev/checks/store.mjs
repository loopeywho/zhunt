// Run: node workspace_dev/checks/store.mjs   (STORE_DIR=/dir/with/old/store.js+api.js to test another copy)
// F10: refresh() must return the in-flight promise.  F11: after access is denied, pollers stop.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { mock } from 'node:test';

const here = path.dirname(fileURLToPath(import.meta.url));
const dir = process.env.STORE_DIR || path.join(here, '../../zhunt/workspace/js');

const calls = [];
let mode = 'ok'; // ok | slow | 401
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.location = { hash: '' };
globalThis.document = { hidden: false, addEventListener() {} };
globalThis.fetch = (url, opts) => {
  calls.push({ url: String(url), auth: !!opts?.headers?.Authorization });
  const respond = (status, body) => ({ status, ok: status < 400, headers: { get: () => 'application/json' }, text: async () => JSON.stringify(body), json: async () => body });
  if (mode === '401') return Promise.resolve(respond(401, {}));
  if (mode === 'slow') return new Promise((r) => setTimeout(() => r(respond(200, { providers: ['x'] })), 200));
  return Promise.resolve(respond(200, { providers: [] }));
};

const api = await import(pathToFileURL(path.join(dir, 'api.js')).href);
const { createStore } = await import(pathToFileURL(path.join(dir, 'store.js')).href);
let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };

// F10
{
  mode = 'slow';
  const store = createStore();
  store.subscribe('status', () => {}); // starts an in-flight refresh
  const ret = store.refresh('status');
  check('F10 refresh() while busy returns a promise', ret && typeof ret.then === 'function', String(ret));
  await ret;
  check('F10 awaiting it yields loaded data', store.get('status').data?.providers?.[0] === 'x', JSON.stringify(store.get('status')));
}

// F11
{
  mode = '401';
  mock.timers.enable({ apis: ['setTimeout'] });
  const store = createStore();
  let denied = 0;
  api.onUnauthorized(() => { denied += 1; store.stop?.(); });
  store.subscribe('status', () => {});
  store.subscribe('decisions', () => {});
  await Promise.resolve(); await Promise.resolve();
  for (let i = 0; i < 5; i += 1) { await new Promise((r) => setImmediate(r)); }
  const before = calls.length;
  for (let i = 0; i < 6; i += 1) { mock.timers.tick(5000); for (let j = 0; j < 5; j += 1) await new Promise((r) => setImmediate(r)); }
  check('F11 denial was reported', denied >= 1, String(denied));
  check('F11 no polling after a 401', calls.length === before, `${calls.length - before} extra requests`);
  mock.timers.reset();
}
process.exit(fail ? 1 : 0);
