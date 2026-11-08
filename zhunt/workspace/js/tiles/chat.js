// Chat tile: streams from POST /v1/chat/completions, then asks /decisions which model answered.

import { h, uid, fmtMs, fmtNum } from '../dom.js';
import { renderMarkdown } from '../md.js';
import { api, streamChat, transcribe, ApiError } from '../api.js';
import { tierChip, modelLabel, errorBox, chip } from './util.js';
import { createHistory, newId } from './chat-history.js';

export const ALIASES = [
  ['zhunt-auto', 'auto'],
  ['zhunt-chat', 'chat'],
  ['zhunt-coding', 'coding'],
  ['zhunt-long-context', 'long-context'],
  ['zhunt-reasoning', 'reasoning'],
];

const MAX_HISTORY = 24;
const ID16 = /^[a-f0-9]{16}$/;
const SUGGESTIONS = ['Explain this error message', 'Write a regex for UK postcodes', 'Plan a 3-day trip to Penang', 'Summarise a long article'];

export function chatTile(svc, state, ctx) {
  const session = 'ui-' + Math.random().toString(36).slice(2, 8);
  let alias = ALIASES.some(([v]) => v === state.alias) ? state.alias : 'zhunt-auto';
  const history = []; // {role, content, model?} (model is only kept for saving; it is not sent)
  // Saved chats: a chat is saved as it goes unless this window is "private". The id is made on the
  // first message; revs holds the server's revision per chat so a stale second window gets a 409.
  let chatId = ID16.test(state.chat) ? state.chat : null;
  let project = ID16.test(state.project) ? state.project : null;
  let priv = state.private === true;
  let projects = [];
  const revs = new Map();
  const conflicts = new Set();
  let saveChain = Promise.resolve();
  const warned = new Set(); // chats whose save failure has already been explained
  let scheduledLen = 0; // messages in the last save this window scheduled
  let openToken = 0;
  let busy = false;
  let controller = null;
  let lastSeenTs = 0;
  let destroyed = false;
  let generation = 0; // bumped by clear/destroy: a send from an older generation must not touch history or DOM

  const selectId = uid('alias');
  const select = h('select', { id: selectId, class: 'ctl', 'aria-label': 'Model alias', title: 'Which Zhunt alias to request' },
    ALIASES.map(([v, label]) => h('option', { value: v, selected: v === alias }, label)));
  select.addEventListener('change', () => { alias = select.value; ctx.changed(); });
  const projectSelect = h('select', { class: 'ctl', 'aria-label': 'Project for this chat', title: 'Project: its instructions are sent with every message' });
  projectSelect.addEventListener('change', () => { project = projectSelect.value || null; ctx.changed(); if (history.length) save(); });
  const historyBtn = h('button', { class: 'pane-btn wide', type: 'button', title: 'Saved chats and projects', onclick: () => panel.toggle() }, 'history');
  const newBtn = h('button', { class: 'pane-btn wide', type: 'button', title: 'Start a new chat (the current one stays in History)', onclick: startNew }, 'new');
  const saveBtn = h('button', { class: 'pane-btn wide', type: 'button', onclick: toggleSaving });
  const header = h('div', { class: 'row chat-header' }, select, projectSelect, historyBtn, newBtn, saveBtn);
  const panel = createHistory(svc, {
    current: () => chatId,
    open: (id) => openChat(id),
    deleted: (id) => { revs.delete(id); if (id === chatId) startNew(); },
    projects: (list) => { projects = list; paintProjects(); },
  });

  const msgs = h('div', { class: 'msgs', role: 'log', 'aria-label': 'Conversation', 'aria-live': 'off', tabindex: '0' });
  msgs.dataset.keepScroll = '1';
  const live = h('div', { class: 'sr-only', role: 'status' });
  const input = h('textarea', {
    class: 'composer-input', rows: '1', 'aria-label': 'Message', spellcheck: 'false',
    placeholder: 'Ask zhunt anything  (Enter sends, Shift+Enter new line)',
  });
  const sendBtn = h('button', { class: 'btn primary', type: 'submit' }, 'send');
  const micBtn = canRecord() ? h('button', {
    class: 'btn mic', type: 'button', 'aria-label': 'Speak your message', title: 'Speak (free Groq Whisper)', 'aria-pressed': 'false',
    onclick: () => (recorder ? recorder.stop() : record()),
  }, 'mic') : null;
  let recorder = null;
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); if (busy) stop(); else send(); } }, input, micBtn, sendBtn);

  function canRecord() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }

  // Record until the button is pressed again (or 60 s), then put the transcript in the box.
  async function record() {
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch {
      svc.toast('microphone not available (permission denied or blocked)', true);
      return;
    }
    const chunks = [];
    recorder = new MediaRecorder(stream);
    const limit = setTimeout(() => recorder?.stop(), 60000);
    recorder.addEventListener('dataavailable', (e) => { if (e.data.size) chunks.push(e.data); });
    recorder.addEventListener('stop', async () => {
      clearTimeout(limit);
      stream.getTracks().forEach((t) => t.stop());
      const type = recorder.mimeType;
      recorder = null;
      micBtn.textContent = '...';
      micBtn.setAttribute('aria-pressed', 'false');
      micBtn.classList.remove('recording');
      try {
        const text = await transcribe(new Blob(chunks, { type }));
        if (text) { input.value = (input.value ? input.value + ' ' : '') + text; autosize(); input.focus(); }
        else svc.toast('no speech heard');
      } catch (err) {
        svc.toast(err.message || String(err), true);
      } finally {
        micBtn.textContent = 'mic';
      }
    });
    recorder.start();
    micBtn.textContent = 'stop';
    micBtn.setAttribute('aria-pressed', 'true');
    micBtn.classList.add('recording');
  }
  const el = h('div', { class: 'chat' }, msgs, form, live, panel.el);

  function paintEmpty() {
    if (msgs.childElementCount) return;
    const hour = new Date().getHours();
    const greeting = hour >= 5 && hour < 12 ? 'Good morning.' : hour >= 12 && hour < 18 ? 'Good afternoon.' : hour >= 18 ? 'Good evening.' : 'Hello.';
    msgs.append(h('div', { class: 'empty chat-empty' },
      h('p', { class: 'chat-greet' }, greeting),
      h('p', { class: 'empty-detail' }, 'Ask anything. zhunt picks the best free model available and switches if one runs dry; the line under each reply says which model answered.'),
      h('div', { class: 'suggest', 'aria-label': 'Suggestions' }, SUGGESTIONS.map((text) =>
        h('button', { class: 'suggest-btn', type: 'button', onclick: () => { input.value = text; autosize(); input.focus(); } }, text)))));
  }
  paintEmpty();

  function autosize() {
    const lines = Math.min(8, Math.max(1, input.value.split('\n').length));
    input.rows = lines;
  }
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); if (!busy) send(); }
    else if (e.key === 'Escape') {
      e.preventDefault();
      if (busy) stop(); else el.closest('.pane')?.focus();
    }
  });

  function scrollDown() { msgs.scrollTop = msgs.scrollHeight; }
  const nearBottom = () => msgs.scrollHeight - msgs.scrollTop - msgs.clientHeight < 80;

  function addMsg(role) {
    msgs.querySelector('.chat-empty')?.remove();
    const text = h('div', { class: 'msg-text' });
    const meta = h('div', { class: 'msg-meta' });
    const node = h('article', { class: 'msg ' + role }, h('div', { class: 'msg-role' }, role === 'user' ? 'you' : 'zhunt'), text, meta);
    msgs.append(node);
    scrollDown();
    return { node, text, meta };
  }

  function reset(focus = true) {
    generation += 1;
    if (busy) { stop(); setBusy(false); controller = null; }
    history.length = 0;
    msgs.replaceChildren();
    paintEmpty();
    if (focus) input.focus();
  }

  function stop() { controller?.abort(); }

  function paintSaving() {
    saveBtn.textContent = priv ? 'private' : 'saved';
    saveBtn.setAttribute('aria-pressed', String(priv));
    saveBtn.title = priv ? 'This window is not saving chats. Click to save again.' : 'Chats are saved on this computer as you go. Click to stop saving.';
  }
  function paintProjects() {
    if (project && projects.length && !projects.some((p) => p.id === project)) { project = null; ctx.changed(); } // deleted elsewhere
    projectSelect.replaceChildren(h('option', { value: '' }, 'no project'), ...projects.map((p) => h('option', { value: p.id }, p.name)));
    projectSelect.value = project || '';
  }
  paintSaving();
  paintProjects();
  api.projects.list().then((r) => { projects = r.projects; paintProjects(); }, () => {});

  function toggleSaving() {
    if (!priv) {
      if (chatId && revs.has(chatId) && !confirm('Stop saving this chat? The copy already saved stays in History until you delete it there.')) return;
      priv = true;
    } else {
      priv = false;
      if (history.length) save();
    }
    paintSaving();
    ctx.changed();
  }

  function startNew() {
    reset();
    chatId = null;
    scheduledLen = 0;
    openToken += 1;
    ctx.changed();
  }

  // Save the conversation so far. Saves run one after another, keyed by chat id, so a reply that
  // finishes right before the user switches chats is still written to the chat it belongs to.
  function save() {
    if (priv || !history.length || history[history.length - 1].role !== 'assistant') return;
    if (!chatId) { chatId = newId(); ctx.changed(); }
    const id = chatId;
    if (conflicts.has(id)) return;
    scheduledLen = history.length;
    const messages = history.map((m) => (m.model ? { role: m.role, content: m.content, model: m.model } : { role: m.role, content: m.content }));
    const doc = { project, alias, messages };
    saveChain = saveChain.then(async () => {
      if (conflicts.has(id)) return;
      try {
        const res = await api.chats.put(id, { ...doc, rev: revs.get(id) ?? null });
        revs.set(id, res.rev);
      } catch (err) {
        if (err.status === 409 && revs.has(id)) {
          conflicts.add(id);
          svc.toast('This chat was changed in another window, so this window has stopped saving it. Reopen it from History.', true);
        } else if (err.status === 413) {
          conflicts.add(id); // too long to save: stop retrying it
          svc.toast('This chat is too long to save any more. Start a new chat to keep saving.', true);
        } else if (err.status === 404 && revs.has(id)) {
          conflicts.add(id);
          svc.toast('This chat was deleted in another window, so it is no longer being saved.', true);
        } else if (!warned.has(id)) {
          warned.add(id);
          svc.toast('Could not save this chat: ' + (err.message || err), true);
        }
      }
    });
  }

  async function openChat(id) {
    const mine = ++openToken;
    const restoring = !history.length && !msgs.querySelector('.msg'); // opening the window, not the user's click
    let chat;
    try { chat = await api.chats.get(id); } catch (err) {
      if (mine !== openToken) return;
      if (err.status === 404) { if (id === chatId) { chatId = null; ctx.changed(); } svc.toast('That chat is no longer saved.', true); } else svc.toast(err.message || String(err), true);
      return;
    }
    if (mine !== openToken || destroyed) return;
    reset(!restoring);
    chatId = chat.id;
    revs.set(chat.id, chat.rev);
    conflicts.delete(chat.id);
    project = ID16.test(chat.project) ? chat.project : null;
    if (ALIASES.some(([v]) => v === chat.alias)) { alias = chat.alias; select.value = alias; }
    history.push(...chat.messages.map((m) => ({ role: m.role, content: m.content, ...(m.model ? { model: m.model } : {}) })));
    scheduledLen = history.length; // what was just loaded is already saved
    paintProjects();
    renderHistory();
    ctx.changed();
  }

  // Saved text is untrusted: user turns are set as text, assistant turns go through the markdown renderer.
  function renderHistory() {
    msgs.replaceChildren();
    for (const m of history) {
      const node = addMsg(m.role);
      if (m.role === 'user') node.text.textContent = m.content;
      else {
        renderMarkdown(m.content, node.text);
        if (m.model) node.meta.append(h('div', { class: 'meta-line' }, h('span', { class: 'meta-label' }, 'answered by'), modelLabel(m.model)));
      }
    }
    if (!history.length) paintEmpty();
    scrollDown();
  }

  async function send() {
    const content = input.value.trim();
    if (!content || busy) return;
    input.value = '';
    autosize();
    const mine = generation;
    const stale = () => mine !== generation;
    let released = false;
    const user = addMsg('user');
    user.text.textContent = content;
    history.push({ role: 'user', content });
    const reply = addMsg('assistant');
    reply.node.classList.add('pending');
    reply.text.textContent = 'routing...';
    setBusy(true);
    const ctrl = new AbortController();
    controller = ctrl;
    const prevTs = lastSeenTs;
    let acc = '';
    let raf = 0;
    const paint = () => { raf = 0; if (stale()) return; const stick = nearBottom(); renderMarkdown(acc, reply.text); if (stick) scrollDown(); };
    try {
      // the window must start on a user turn (some providers reject a leading assistant message)
      const outgoing = history.slice(-MAX_HISTORY).map(({ role, content: text }) => ({ role, content: text }));
      while (outgoing.length && outgoing[0].role !== 'user') outgoing.shift();
      if (project) { try { projects = (await api.projects.list()).projects; } catch { /* keep the copy we have */ } }
      const rules = projects.find((p) => p.id === project)?.instructions;
      if (rules) outgoing.unshift({ role: 'system', content: rules }); // after the trim above, which would drop it
      const res = await streamChat({
        model: alias, messages: outgoing, session, signal: ctrl.signal,
        onDelta: (d) => { if (stale()) return; acc += d; reply.node.classList.remove('pending'); if (!raf) raf = requestAnimationFrame(paint); },
      });
      if (raf) cancelAnimationFrame(raf);
      if (stale()) return;
      acc = res.text;
      if (!acc) acc = '(empty reply)';
      reply.node.classList.remove('pending');
      renderMarkdown(acc, reply.text);
      const entry = { role: 'assistant', content: res.text, ...(res.model ? { model: res.model } : {}) };
      history.push(entry);
      save();
      if (res.truncated && res.text) reply.text.append(h('p', { class: 'dim' }, '(the stream ended without a finish signal: this reply may be cut short)'));
      live.textContent = res.truncated ? 'Reply may be incomplete' : 'Reply complete';
      // free the composer now: the routing-decision lookup below can take a couple of seconds
      released = true;
      setBusy(false);
      controller = null;
      svc.store.refresh('decisions');
      const answeredBy = await attachDecision(reply.meta, prevTs, res.model);
      // The stream only names the alias ("zhunt-auto"); the routing decision names the model that answered.
      // Record it in the saved chat too (only if this chat is still the one on screen).
      if (answeredBy && answeredBy !== entry.model && history.includes(entry)) { entry.model = answeredBy; save(); }
    } catch (err) {
      if (raf) cancelAnimationFrame(raf);
      if (stale()) return; // cleared or closed while in flight: the new conversation is none of our business
      reply.node.classList.remove('pending');
      if (err.name === 'AbortError') {
        if (acc) { renderMarkdown(acc, reply.text); history.push({ role: 'assistant', content: acc }); save(); } else { history.pop(); user.node.remove(); reply.node.remove(); restoreInput(content); paintEmpty(); }
        reply.meta.textContent = 'stopped';
      } else if (err instanceof ApiError && err.status === 401) {
        history.pop(); user.node.remove(); reply.node.remove(); restoreInput(content); paintEmpty();
      } else {
        history.pop();
        if (!acc) user.node.remove();
        restoreInput(content);
        if (acc) { renderMarkdown(acc, reply.text); } else { reply.text.replaceChildren(); }
        reply.node.classList.add('failed');
        reply.text.append(describeError(err));
        live.textContent = 'Request failed';
        svc.store.refresh('decisions');
        attachDecision(reply.meta, prevTs, null, true);
      }
    } finally {
      if (!released && !stale()) { setBusy(false); controller = null; }
      if (!destroyed && !stale()) scrollDown();
    }
  }

  function restoreInput(content) {
    if (!input.value) { input.value = content; autosize(); }
  }

  function describeError(err) {
    if (err instanceof ApiError && err.status === 503) {
      return errorBox('No model available',
        'Every candidate for this request is exhausted, in cooldown or blocked by policy. ' + (err.message ? '(' + err.message + ') ' : '') +
        'Check the providers tile for missing keys and cooldowns, or retry in a moment.');
    }
    if (err instanceof ApiError && err.status === 0) {
      return errorBox('Daemon unreachable', err.message + '. Is `zhunt serve` still running?');
    }
    if (err instanceof ApiError && err.status === 429) {
      return errorBox('Rate limited', err.message);
    }
    return errorBox('Request failed' + (err.status ? ' (HTTP ' + err.status + ')' : ''), err.message || String(err));
  }

  function setBusy(b) {
    busy = b;
    sendBtn.textContent = b ? 'stop' : 'send';
    sendBtn.classList.toggle('danger', b);
    el.setAttribute('aria-busy', String(b));
  }

  // Look the answer up in /decisions: which model, which tier, any failover that happened first.
  async function attachDecision(metaEl, prevTs, streamModel, errorCase = false) {
    metaEl.textContent = 'looking up routing decision...';
    for (let attempt = 0; attempt < 5; attempt += 1) {
      if (destroyed) return;
      try {
        const { decisions } = await api.decisions({ limit: 20, session });
        const fresh = (decisions || []).filter((d) => d.ts > prevTs);
        const final = fresh.find((d) => d.success);
        if (final || (errorCase && fresh.length) || attempt === 4) {
          if (fresh.length) lastSeenTs = Math.max(lastSeenTs, ...fresh.map((d) => d.ts));
          renderMeta(metaEl, final, fresh.filter((d) => !d.success).reverse(), streamModel, errorCase);
          return final ? final.model : null;
        }
      } catch (err) {
        if (err.status === 401) return;
        if (attempt === 4) { renderMeta(metaEl, null, [], streamModel, errorCase); return null; }
      }
      await new Promise((r) => setTimeout(r, 250 + attempt * 150));
    }
  }

  function renderMeta(metaEl, final, failed, streamModel, errorCase) {
    metaEl.replaceChildren();
    const bits = [];
    if (final) {
      bits.push(h('span', { class: 'meta-label' }, 'answered by'), modelLabel(final.model), tierChip(final.tier));
      if (final.degraded) bits.push(chip('degraded', 'warn', 'The preferred tier was exhausted; served from a lower tier'));
      bits.push(h('span', { class: 'meta-dim' },
        `attempt ${final.attempt} · ${fmtMs(final.latency_ms)} · ${fmtNum(final.input_tokens)} in / ${fmtNum(final.output_tokens)} out`));
    } else if (streamModel && !errorCase) {
      bits.push(h('span', { class: 'meta-label' }, 'answered by'), modelLabel(streamModel), h('span', { class: 'meta-dim' }, '(decision not found)'));
    } else if (!errorCase) {
      bits.push(h('span', { class: 'meta-dim' }, 'routing decision not available'));
    }
    if (bits.length) metaEl.append(h('div', { class: 'meta-line' }, bits));
    if (failed.length) {
      metaEl.append(h('div', { class: 'meta-line failover' },
        h('span', { class: 'meta-label' }, errorCase ? 'tried' : 'failed over from'),
        failed.map((d) => h('span', { class: 'tried' }, modelLabel(d.model), h('span', { class: 'meta-err' }, ' ' + (d.failure || 'failed'))))));
    }
  }

  if (chatId && !priv) openChat(chatId);

  return {
    el,
    header,
    getState: () => ({ alias, project, chat: priv ? null : chatId, private: priv }),
    focusPrimary: () => input.focus(),
    destroy() { if (history.length !== scheduledLen) save(); destroyed = true; generation += 1; controller?.abort(); recorder?.stop(); },
  };
}
