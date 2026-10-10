// Run: node workspace_dev/checks/api.mjs   (API_JS=/path/to/other/api.js to test another copy)
// F15: malformed keys are rejected on capture.  F16: SSE error cancels the reader; unfinished stream is flagged.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const file = process.env.API_JS || path.join(here, '../../zhunt/workspace/js/api.js');

const store = new Map();
globalThis.sessionStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) };
globalThis.location = { hash: '', pathname: '/workspace/', search: '' };
globalThis.history = { replaceState() {} };
let cancelled = 0;
let lastHeaders = null;

// fetch like the browser's: a header value with a char > 0xFF throws TypeError
function fakeFetch(sse) {
  return async (url, opts) => {
    lastHeaders = opts?.headers || {};
    for (const v of Object.values(lastHeaders)) if ([...String(v)].some((c) => c.charCodeAt(0) > 255)) throw new TypeError('Failed to execute fetch: non ISO-8859-1 header value');
    const chunks = sse.map((s) => new TextEncoder().encode(s));
    let i = 0;
    return {
      status: 200, ok: true,
      headers: { get: () => 'text/event-stream' },
      body: { getReader: () => ({
        read: async () => (i < chunks.length ? { done: false, value: chunks[i++] } : { done: true }),
        cancel: async () => { cancelled += 1; },
      }) },
    };
  };
}

const api = await import(pathToFileURL(file).href);
let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };
const ev = (o) => 'data: ' + (typeof o === 'string' ? o : JSON.stringify(o)) + '\n\n';
const delta = (t) => ev({ model: 'm', choices: [{ delta: { content: t } }] });

// F15
location.hash = '#key=caf%C3%A9';
api.captureKeyFromFragment();
check('F15 a non-ASCII key is not stored', !api.getKey(), String(api.getKey()));
store.clear();
location.hash = '#key=' + encodeURIComponent('has space');
api.captureKeyFromFragment();
check('F15 a key with a space is not stored', !api.getKey(), String(api.getKey()));
store.clear();
location.hash = '#key=' + encodeURIComponent('abc+/=_-123');
api.captureKeyFromFragment();
check('F15 a normal key (including + / =) is kept', api.getKey() === 'abc+/=_-123', String(api.getKey()));

// F16: error event cancels the reader
globalThis.fetch = fakeFetch([delta('a'), ev({ error: { message: 'boom' } }), delta('b')]);
cancelled = 0;
let err = null;
try { await api.streamChat({ model: 'x', messages: [], session: 's', onDelta() {} }); } catch (e) { err = e; }
check('F16 {error} event rejects', err && /boom/.test(err.message), String(err));
check('F16 {error} event cancels the reader', cancelled >= 1, String(cancelled));

// F16: stream that just stops is flagged, a finished one is not
globalThis.fetch = fakeFetch([delta('hello')]);
let res = await api.streamChat({ model: 'x', messages: [], session: 's', onDelta() {} });
check('F16 stream without [DONE]/finish_reason is flagged truncated', res.truncated === true, JSON.stringify(res));
globalThis.fetch = fakeFetch([delta('hello'), ev('[DONE]')]);
res = await api.streamChat({ model: 'x', messages: [], session: 's', onDelta() {} });
check('F16 stream with [DONE] is complete', res.text === 'hello' && !res.truncated, JSON.stringify(res));
globalThis.fetch = fakeFetch([delta('hello'), ev({ choices: [{ delta: {}, finish_reason: 'stop' }] })]);
res = await api.streamChat({ model: 'x', messages: [], session: 's', onDelta() {} });
check('F16 stream with finish_reason is complete', res.text === 'hello' && !res.truncated, JSON.stringify(res));

process.exit(fail ? 1 : 0);
