// Daemon access. The master key lives in sessionStorage only (this tab), arrives via the URL
// fragment (never sent to a server) and is removed from the address bar right away.

const KEY_STORE = 'zhunt.key';

// A key must be printable ASCII: anything else cannot go into an Authorization header (fetch would throw
// and the UI would wrongly report the daemon as unreachable).
const KEY_RE = /^[\x21-\x7e]+$/;

// Returns true when the fragment carried a key that was rejected as malformed.
export function captureKeyFromFragment() {
  const hash = location.hash || '';
  let invalid = false;
  if (hash.length > 1) {
    // Parsed by hand: URLSearchParams would turn '+' into a space.
    for (const part of hash.slice(1).split('&')) {
      const eq = part.indexOf('=');
      if (eq > 0 && part.slice(0, eq) === 'key') {
        let value = part.slice(eq + 1);
        try { value = decodeURIComponent(value); } catch { /* keep raw */ }
        if (value && !KEY_RE.test(value)) { invalid = true; clearKey(); }
        else if (value) { try { sessionStorage.setItem(KEY_STORE, value); } catch { /* storage blocked */ memoryKey = value; } }
      }
    }
    // Drop the fragment (and with it the key) from the address bar and history entry.
    history.replaceState(null, '', location.pathname + location.search);
  }
  return invalid;
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

async function request(path, { method = 'GET', json, headers = {}, signal, text = false, keepalive = false } = {}) {
  const h = { Accept: text ? 'text/css,*/*' : 'application/json', ...headers };
  const key = getKey();
  if (key) h.Authorization = 'Bearer ' + key;
  if (json !== undefined) h['Content-Type'] = 'application/json';
  let res;
  try {
    res = await fetch(path, {
      method, headers: h, signal, cache: 'no-store', credentials: 'omit', keepalive,
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
  plan: () => request('/zhunt/api/plan'),
  keys: {
    add: (platform, key) => request('/zhunt/api/keys', { method: 'POST', json: { platform, key } }),
    remove: (platform) => request('/zhunt/api/keys/' + encodeURIComponent(platform), { method: 'DELETE' }),
  },
  bots: {
    list: () => request('/zhunt/api/bots'),
    put: (handle, bot) => request('/zhunt/api/bots/' + encodeURIComponent(handle), { method: 'PUT', json: bot }),
    remove: (handle) => request('/zhunt/api/bots/' + encodeURIComponent(handle), { method: 'DELETE' }),
    avatar: (handle) => request('/zhunt/api/bots/' + encodeURIComponent(handle) + '/avatar'),
    setAvatar: (handle, image) => request('/zhunt/api/bots/' + encodeURIComponent(handle) + '/avatar', { method: 'PUT', json: { image } }),
    removeAvatar: (handle) => request('/zhunt/api/bots/' + encodeURIComponent(handle) + '/avatar', { method: 'DELETE' }),
  },
  images: {
    status: () => request('/zhunt/api/images/status'),
    connect: (account_id, token) => request('/zhunt/api/images/connect', { method: 'PUT', json: { account_id, token } }),
    disconnect: () => request('/zhunt/api/images/connect', { method: 'DELETE' }),
    generate: (prompt) => request('/zhunt/api/images', { method: 'POST', json: { prompt } }),
  },
  agent: {
    state: () => request('/zhunt/api/agent'),
    setFolders: (folders) => request('/zhunt/api/agent/folders', { method: 'PUT', json: { folders } }),
    start: (body) => request('/zhunt/api/agent/tasks', { method: 'POST', json: body }),
    task: (id, since = 0) => request('/zhunt/api/agent/tasks/' + encodeURIComponent(id) + '?since=' + since),
    decide: (id, call_id, approve) => request('/zhunt/api/agent/tasks/' + encodeURIComponent(id) + '/decide', { method: 'POST', json: { call_id, approve } }),
    stop: (id) => request('/zhunt/api/agent/tasks/' + encodeURIComponent(id) + '/stop', { method: 'POST' }),
    loop: (body) => request('/zhunt/api/agent/loops', { method: 'POST', json: body }),
    unloop: (id) => request('/zhunt/api/agent/loops/' + encodeURIComponent(id), { method: 'DELETE' }),
    briefs: () => request('/zhunt/api/agent/briefs'),
    briefSettings: (body) => request('/zhunt/api/agent/briefs/settings', { method: 'PUT', json: body }),
    runBrief: () => request('/zhunt/api/agent/briefs/run', { method: 'POST' }),
    actBrief: (b, i) => request('/zhunt/api/agent/briefs/' + encodeURIComponent(b) + '/items/' + encodeURIComponent(i) + '/act', { method: 'POST' }),
    dismissBrief: (b, i, skip = null) => request('/zhunt/api/agent/briefs/' + encodeURIComponent(b) + '/items/' + encodeURIComponent(i) + '/dismiss', { method: 'POST', json: { skip } }),
  },
  email: {
    status: () => request('/zhunt/api/email/status'),
    connect: (body) => request('/zhunt/api/email/connect', { method: 'PUT', json: body }),
    disconnect: () => request('/zhunt/api/email/connect', { method: 'DELETE' }),
  },
  wallpaper: {
    get: () => request('/zhunt/api/wallpaper'),
    put: (image) => request('/zhunt/api/wallpaper', { method: 'PUT', json: { image } }),
    remove: () => request('/zhunt/api/wallpaper', { method: 'DELETE' }),
  },
  catalog: {
    status: () => request('/zhunt/api/catalog'),
    update: () => request('/zhunt/api/catalog/update', { method: 'POST' }),
    setUpdates: (enabled) => request('/zhunt/api/catalog/updates', { method: 'PUT', json: { enabled } }),
  },
  setTraining: (allow) => request('/zhunt/api/policy/training', { method: 'PUT', json: { allow } }),
  decisions: ({ limit = 50, session } = {}, signal) => {
    const q = new URLSearchParams({ limit: String(limit) });
    if (session) q.set('session', session);
    return request('/zhunt/api/decisions?' + q, { signal });
  },
  compare: (messages, models, signal) => request('/zhunt/api/compare', { method: 'POST', json: { messages, models }, signal }),
  layouts: {
    list: () => request('/zhunt/api/layouts'),
    get: (name) => request('/zhunt/api/layouts/' + encodeURIComponent(name)),
    // keepalive lets the request outlive the page (used to flush the autosave on pagehide)
    put: (name, doc, { keepalive = false } = {}) => request('/zhunt/api/layouts/' + encodeURIComponent(name), { method: 'PUT', json: doc, keepalive }),
    remove: (name) => request('/zhunt/api/layouts/' + encodeURIComponent(name), { method: 'DELETE' }),
  },
  themes: {
    list: () => request('/zhunt/api/themes'),
    css: (name) => request('/zhunt/api/themes/' + encodeURIComponent(name), { text: true }),
  },
};

// Voice input: send a recording to the local daemon, which transcribes it with Groq Whisper.
export async function transcribe(blob) {
  const key = getKey();
  let res;
  try {
    res = await fetch('/zhunt/api/transcribe', {
      method: 'POST', body: blob, cache: 'no-store', credentials: 'omit',
      headers: { 'Content-Type': (blob.type || 'audio/webm').split(';')[0], ...(key ? { Authorization: 'Bearer ' + key } : {}) },
    });
  } catch (err) {
    throw new ApiError(0, 'cannot reach the zhunt daemon (' + (err.message || 'network error') + ')');
  }
  if (res.status === 401) { unauthorizedHandler(); throw new ApiError(401, 'invalid Zhunt master key'); }
  const body = await readBody(res);
  if (!res.ok) throw new ApiError(res.status, detailOf(body, 'HTTP ' + res.status), body);
  return (body && body.text) || '';
}

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
    return { text, model: body?.model || null, truncated: false };
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  let text = '';
  let answeredBy = null;
  let finished = false; // saw [DONE] or a finish_reason; a stream that just stops may have been cut off
  const handle = (line) => {
    if (!line.startsWith('data:')) return false;
    const data = line.slice(5).trim();
    if (!data) return false;
    if (data === '[DONE]') { finished = true; return true; }
    let obj;
    try { obj = JSON.parse(data); } catch { return false; }
    if (obj.error) throw new ApiError(502, detailOf(obj, 'stream error'), obj);
    if (obj.model) answeredBy = obj.model;
    if (obj.choices?.[0]?.finish_reason) finished = true;
    const delta = obj.choices?.[0]?.delta?.content;
    if (typeof delta === 'string' && delta) { text += delta; onDelta(delta); }
    return false;
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, nl).replace(/\r$/, '');
        buf = buf.slice(nl + 1);
        if (handle(line)) { try { await reader.cancel(); } catch { /* ignore */ } return { text, model: answeredBy, truncated: false }; }
      }
    }
    if (buf) handle(buf.trim());
  } catch (err) {
    // an {error} event (or an abort) mid-stream: release the connection instead of leaving it open
    try { await reader.cancel(); } catch { /* ignore */ }
    throw err;
  }
  return { text, model: answeredBy, truncated: !finished };
}
