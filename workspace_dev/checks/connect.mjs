// Run: node workspace_dev/checks/connect.mjs
// The Connect window's first-run flow, driven against workspace_dev/mock_server.py with a small DOM stub:
// no keys -> a plain "working with no keys" line; one "paste any key" box that detects the provider or asks;
// Groq recommended first, the rest under "More free capacity".
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(here, '../..');

// --- minimal DOM: enough for dom.js h(), swapIfChanged() and form submit handlers ---
class Node_ {}
class Text_ extends Node_ {
  constructor(s) { super(); this.data = String(s); }
  get textContent() { return this.data; }
  get outerHTML() { return this.data; }
}
class El extends Node_ {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.value = ''; this.className = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(v) { if (v) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  append(...kids) { for (const k of kids) this.children.push(k); }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  contains(n) { return n === this || this.children.some((c) => c instanceof El && c.contains(n)); }
  focus() { document.activeElement = this; }
  closest() { return null; }
  get textContent() { return this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this.children = [new Text_(v)]; }
  get outerHTML() {
    const a = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
    return `<${this.tagName} class="${this.className}"${a}>${this.children.map((c) => c.outerHTML).join('')}</${this.tagName}>`;
  }
  all(pred, out = []) { if (pred(this)) out.push(this); for (const c of this.children) if (c instanceof El) c.all(pred, out); return out; }
  querySelectorAll(sel) { return this.all((e) => e.tagName === sel.toUpperCase()); }
  querySelector() { return null; }
  dispatch(type) { for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, target: this }); }
}
globalThis.window = new EventTarget();
globalThis.Node = Node_;
globalThis.document = { createElement: (t) => new El(t), createTextNode: (s) => new Text_(s), activeElement: null };
globalThis.requestAnimationFrame = (fn) => fn();
globalThis.sessionStorage = { getItem: (k) => (k === 'zhunt.key' ? 'mockkey' : null), setItem() {}, removeItem() {} };
globalThis.location = { hash: '' };

// --- the mock daemon ---
const port = 18000 + Math.floor(Math.random() * 1000);
const base = `http://127.0.0.1:${port}`;
const server = spawn('python3', [path.join(root, 'workspace_dev/mock_server.py'), '--port', String(port)], { stdio: ['ignore', 'pipe', 'inherit'] });
await new Promise((resolve) => server.stdout.on('data', (d) => { if (String(d).includes('mock zhunt daemon')) resolve(); }));
const nodeFetch = globalThis.fetch;
const sent = [];
globalThis.fetch = (url, opts = {}) => { if (opts.body) sent.push(opts.body); return nodeFetch(base + url, opts); };
await nodeFetch(base + '/mock/control', { method: 'POST', body: JSON.stringify({ no_keys: true }) });

const { api } = await import(pathToFileURL(path.join(root, 'zhunt/workspace/js/api.js')).href);
const { connectTile } = await import(pathToFileURL(path.join(root, 'zhunt/workspace/js/tiles/connect.js')).href);

let snap = { data: await api.status(), error: null };
const subs = [];
const toasts = [];
const svc = {
  toast: (t) => toasts.push(t),
  store: {
    get: () => snap,
    subscribe: (_, fn) => { subs.push(fn); return () => {}; },
    refresh: async () => { snap = { data: await api.status(), error: null }; subs.forEach((fn) => fn(snap)); },
  },
};
const tile = connectTile(svc);
const settle = () => new Promise((r) => setTimeout(r, 150));
await settle();

let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };
const text = () => tile.el.textContent;
const anyForm = () => tile.el.all((e) => e.tagName === 'FORM' && e.attrs['data-for'] === 'any')[0];
const parts = () => {
  const f = anyForm();
  return { f, input: f.all((e) => e.tagName === 'INPUT')[0], pick: f.all((e) => e.tagName === 'SELECT')[0], note: f.all((e) => e.className.includes('connect-note'))[0] };
};
const headings = () => tile.el.all((e) => e.tagName === 'H3').map((e) => e.textContent);
const submit = async (value) => { const p = parts(); p.input.value = value; p.f.dispatch('submit'); await settle(); return parts(); };

try {
  check('fresh install says plainly that it works with no keys',
    text().includes('Working with no keys (slow, shared limits). Add a free Groq key for speed.'), text().slice(0, 300));
  check('one "paste any key" box comes before the providers', !!anyForm() && headings()[0] === 'Recommended', headings().join(' | '));
  const buttons = tile.el.all((e) => e.tagName === 'BUTTON').map((b) => b.textContent);
  check('Groq is recommended first with a one-minute key', buttons.includes('Get a free Groq key (about 1 minute)'), buttons.join(' | '));
  check('the other providers sit under "More free capacity"', headings()[1].startsWith('More free capacity'), headings().join(' | '));
  const rows = tile.el.all((e) => e.tagName === 'ARTICLE').map((a) => a.attrs['data-platform']);
  check('Groq is the first provider row', rows.filter((platform) => platform !== 'auto')[0] === 'groq', rows.join());

  const typed = parts().input;
  typed.value = 'half-typed';
  await svc.store.refresh();
  check('a status refresh keeps what is being pasted', parts().input === typed && typed.value === 'half-typed');

  let p = await submit('mystery-key-1234567890');
  check('an unknown key asks which provider instead of guessing', !p.pick.hidden && /cannot tell which provider/.test(p.note.textContent), p.note.textContent);
  check('the unknown key is kept in the box for the second try', p.input.value === 'mystery-key-1234567890');
  p.pick.value = 'cerebras';
  p.f.dispatch('submit');
  await settle();
  p = parts();
  check('choosing the provider saves it', toasts.at(-1) === 'Cerebras connected' && p.input.value === '' && p.pick.hidden, toasts.join());
  check('the request with a chosen provider names it', JSON.parse(sent.at(-1)).platform === 'cerebras');

  p = await submit('mystery-key-1234567890');
  p.input.value = 'gsk_live1234567890abcdef';
  p.input.dispatch('input');
  check('editing the key hides the provider picker again', p.pick.hidden && p.note.textContent === '');
  p.f.dispatch('submit');
  await settle();
  check('a Groq key is detected from its prefix', toasts.at(-1) === 'Groq connected', toasts.join());
  check('the detection request sends no provider', !('platform' in JSON.parse(sent.at(-1))), sent.at(-1));
  check('the no-keys line goes once a key is set', !text().includes('Working with no keys'));
  const toastText = toasts.join(' ');
  check('no key value is ever shown', !toastText.includes('gsk_live') && !toastText.includes('mystery-key') && !text().includes('gsk_live'));
} finally {
  tile.destroy();
  server.kill();
}

if (fail) { console.log(fail + ' failed'); process.exit(1); }
console.log('PASS all connect checks');
