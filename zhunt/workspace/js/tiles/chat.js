// Chat tile: streams from POST /v1/chat/completions, then asks /decisions which model answered.

import { h, uid, fmtMs, fmtNum } from '../dom.js';
import { renderMarkdown } from '../md.js';
import { api, streamChat, ApiError } from '../api.js';
import { tierChip, modelLabel, errorBox, chip } from './util.js';

export const ALIASES = [
  ['zhunt-auto', 'auto'],
  ['zhunt-chat', 'chat'],
  ['zhunt-coding', 'coding'],
  ['zhunt-long-context', 'long-context'],
  ['zhunt-reasoning', 'reasoning'],
];

const MAX_HISTORY = 24;

export function chatTile(svc, state, ctx) {
  const session = 'ui-' + Math.random().toString(36).slice(2, 8);
  let alias = ALIASES.some(([v]) => v === state.alias) ? state.alias : 'zhunt-auto';
  const history = []; // {role, content} sent to the model
  let busy = false;
  let controller = null;
  let lastSeenTs = 0;
  let destroyed = false;

  const selectId = uid('alias');
  const select = h('select', { id: selectId, class: 'ctl', 'aria-label': 'Model alias', title: 'Which Zhunt alias to request' },
    ALIASES.map(([v, label]) => h('option', { value: v, selected: v === alias }, label)));
  select.addEventListener('change', () => { alias = select.value; ctx.changed(); });
  const clearBtn = h('button', { class: 'pane-btn wide', type: 'button', title: 'Clear conversation', onclick: reset }, 'clear');
  const header = h('div', { class: 'row' }, select, clearBtn);

  const msgs = h('div', { class: 'msgs', role: 'log', 'aria-label': 'Conversation', 'aria-live': 'off', tabindex: '0' });
  msgs.dataset.keepScroll = '1';
  const live = h('div', { class: 'sr-only', role: 'status' });
  const input = h('textarea', {
    class: 'composer-input', rows: '1', 'aria-label': 'Message', spellcheck: 'false',
    placeholder: 'message  (Enter sends, Shift+Enter newline, Esc leaves)',
  });
  const sendBtn = h('button', { class: 'btn primary', type: 'submit' }, 'send');
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); if (busy) stop(); else send(); } }, input, sendBtn);
  const el = h('div', { class: 'chat' }, msgs, form, live);

  function paintEmpty() {
    if (msgs.childElementCount) return;
    msgs.append(h('div', { class: 'empty chat-empty' },
      h('p', { class: 'empty-title' }, 'Ask anything.'),
      h('p', { class: 'empty-detail' }, 'zhunt picks the best available free model and fails over if it runs dry. After each reply the routing line shows which model actually answered.')));
  }
  paintEmpty();

  function autosize() {
    const lines = Math.min(8, Math.max(1, input.value.split('\n').length));
    input.rows = lines;
  }
  input.addEventListener('input', autosize);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!busy) send(); }
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

  function reset() {
    if (busy) stop();
    history.length = 0;
    msgs.replaceChildren();
    paintEmpty();
    input.focus();
  }

  function stop() { controller?.abort(); }

  async function send() {
    const content = input.value.trim();
    if (!content || busy) return;
    input.value = '';
    autosize();
    const user = addMsg('user');
    user.text.textContent = content;
    history.push({ role: 'user', content });
    const reply = addMsg('assistant');
    reply.node.classList.add('pending');
    reply.text.textContent = 'routing...';
    setBusy(true);
    controller = new AbortController();
    const prevTs = lastSeenTs;
    let acc = '';
    let raf = 0;
    const paint = () => { raf = 0; const stick = nearBottom(); renderMarkdown(acc, reply.text); if (stick) scrollDown(); };
    try {
      const res = await streamChat({
        model: alias, messages: history.slice(-MAX_HISTORY), session, signal: controller.signal,
        onDelta: (d) => { acc += d; reply.node.classList.remove('pending'); if (!raf) raf = requestAnimationFrame(paint); },
      });
      if (raf) cancelAnimationFrame(raf);
      acc = res.text;
      if (!acc) acc = '(empty reply)';
      reply.node.classList.remove('pending');
      renderMarkdown(acc, reply.text);
      history.push({ role: 'assistant', content: res.text });
      live.textContent = 'Reply complete';
      svc.store.refresh('decisions');
      await attachDecision(reply.meta, prevTs, res.model);
    } catch (err) {
      if (raf) cancelAnimationFrame(raf);
      reply.node.classList.remove('pending');
      if (err.name === 'AbortError') {
        if (acc) { renderMarkdown(acc, reply.text); history.push({ role: 'assistant', content: acc }); } else { history.pop(); reply.node.remove(); restoreInput(content); }
        reply.meta.textContent = 'stopped';
      } else if (err instanceof ApiError && err.status === 401) {
        history.pop(); reply.node.remove(); restoreInput(content);
      } else {
        history.pop();
        restoreInput(content);
        if (acc) { renderMarkdown(acc, reply.text); } else { reply.text.replaceChildren(); }
        reply.node.classList.add('failed');
        reply.text.append(describeError(err));
        live.textContent = 'Request failed';
        svc.store.refresh('decisions');
        attachDecision(reply.meta, prevTs, null, true);
      }
    } finally {
      setBusy(false);
      controller = null;
      if (!destroyed) scrollDown();
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
          return;
        }
      } catch (err) {
        if (err.status === 401) return;
        if (attempt === 4) { renderMeta(metaEl, null, [], streamModel, errorCase); return; }
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

  return {
    el,
    header,
    getState: () => ({ alias }),
    focusPrimary: () => input.focus(),
    destroy() { destroyed = true; controller?.abort(); },
  };
}
