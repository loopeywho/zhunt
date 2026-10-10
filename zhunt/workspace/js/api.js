// Daemon access. The master key lives in sessionStorage only (this tab), arrives via the URL
// fragment (never sent to a server) and is removed from the address bar right away.

const KEY_STORE = 'zhunt.key';

export function captureKeyFromFragment() {
  const hash = location.hash || '';
  if (hash.length > 1) {
    // Parsed by hand: URLSearchParams would turn '+' into a space.
    for (const part of hash.slice(1).split('&')) {
      const eq = part.indexOf('=');
      if (eq > 0 && part.slice(0, eq) === 'key') {
        let value = part.slice(eq + 1);
        try { value = decodeURIComponent(value); } catch { /* keep raw */ }
        if (value) { try { sessionStorage.setItem(KEY_STORE, value); } catch { /* storage blocked */ memoryKey = value; } }
      }
    }
    // Drop the fragment (and with it the key) from the address bar and history entry.
    history.replaceState(null, '', location.pathname + location.search);
  }
}

let memoryKey = null;

export function getKey() {
  try { return sessionStorage.getItem(KEY_STORE) || memoryKey; } catch { return memoryKey; }
}

export function clearKey() {
  memoryKey = null;
  try { sessionStorage.removeItem(KEY_STORE); } catch { /* ignore */ }
}

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

let unauthorizedHandler = () => {};
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

function detailOf(body, fallback) {
  if (!body) return fallback;
  if (typeof body === 'string') return body.slice(0, 400) || fallback;
  const d = body.detail ?? body.error?.message ?? body.error ?? body.message;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join('; ');
  if (d && typeof d === 'object') return d.message || JSON.stringify(d);
  return fallback;
}

async function readBody(res) {
  const text = await res.text();
  try { return JSON.parse(text); } catch { return text; }
}

async function request(path, { method = 'GET', json, headers = {}, signal, text = false } = {}) {
  const h = { Accept: text ? 'text/css,*/*' : 'application/json', ...headers };
  const key = getKey();
  if (key) h.Authorization = 'Bearer ' + key;
  if (json !== undefined) h['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(path, {
      method, headers: h, signal, cache: 'no-store', credentials: 'omit',
      body: json !== undefined ? JSON.stringify(json) : undefined,
    });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError(0, 'cannot reach the zhunt daemon (' + (err.message || 'network error') + ')');
  }
  if (res.status === 401) {
    unauthorizedHandler();
    throw new ApiError(401, 'invalid Zhunt master key');
  }
  if (!res.ok) {
    const body = await readBody(res);
    throw new ApiError(res.status, detailOf(body, 'HTTP ' + res.status), body);
  }
  if (text) return res.text();
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  status: (signal) => request('/zhunt/api/status', { signal }),
  decisions: ({ limit = 50, session } = {}, signal) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (session) q.set('session', session);
    return request('/zhunt/api/decisions?' + q, { signal });
  },
  compare: (messages, models, signal) => request('/zhunt/api/compare', { method: 'POST', json: { messages, models }, signal }),
  layouts: {
    list: () => request('/zhunt/api/layouts'),
    get: (name) => request('/zhunt/api/layouts/' + encodeURIComponent(name)),
    put: (name, doc) => request('/zhunt/api/layouts/' + encodeURIComponent(name), { method: 'PUT', json: doc }),
    remove: (name) => request('/zhunt/api/layouts/' + encodeURIComponent(name), { method: 'DELETE' }),
  },
  themes: {
    list: () => request('/zhunt/api/themes'),
    css: (name) => request('/zhunt/api/themes/' + encodeURIComponent(name), { text: true }),
  },
};

// Streaming chat. Calls onDelta(textChunk) as tokens arrive; resolves with {text, model}.
// Also copes with a plain JSON (non-SSE) reply.
export async function streamChat({ model, messages, session, signal, onDelta }) {
  const key = getKey();
  let res;
  try {
    res = await fetch('/v1/chat/completions', {
      method: 'POST', signal, cache: 'no-store', credentials: 'omit',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream, application/json',
        'x-session-id': session,
        ...(key ? { Authorization: 'Bearer ' + key } : {}),
      },
      body: JSON.stringify({ model, messages, stream: true }),
    });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError(0, 'cannot reach the zhunt daemon (' + (err.message || 'network error') + ')');
  }
  if (res.status === 401) { unauthorizedHandler(); throw new ApiError(401, 'invalid Zhunt master key'); }
  if (!res.ok) {
    const body = await readBody(res);
    throw new ApiError(res.status, detailOf(body, 'HTTP ' + res.status), body);
  }
  const type = res.headers.get('content-type') || '';
  if (!type.includes('text/event-stream')) {
    const body = await res.json();
    const text = body?.choices?.[0]?.message?.content ?? '';
    onDelta(text);
    return { text, model: body?.model || null };
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let text = '';
  let answeredBy = null;
  const handle = (line) => {
    if (!line.startsWith('data:')) return false;
    const data = line.slice(5).trim();
    if (!data) return false;
    if (data === '[DONE]') return true;
    let obj;
    try { obj = JSON.parse(data); } catch { return false; }
    if (obj.error) throw new ApiError(502, detailOf(obj, 'stream error'), obj);
    if (obj.model) answeredBy = obj.model;
    const delta = obj.choices?.[0]?.delta?.content;
    if (typeof delta === 'string' && delta) { text += delta; onDelta(delta); }
    return false;
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).replace(/\r$/, '');
      buf = buf.slice(nl + 1);
      if (handle(line)) { try { await reader.cancel(); } catch { /* ignore */ } return { text, model: answeredBy }; }
    }
  }
  if (buf) handle(buf.trim());
  return { text, model: answeredBy };
}
