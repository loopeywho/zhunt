// Team chat: several bots in one conversation. You @mention a bot; it answers through zhunt's
// free-model routing (on the model type it prefers), and if its reply @mentions a teammate,
// that bot picks up next. A hop limit stops bots from talking in circles.

import { h } from '../dom.js';
import { renderMarkdown } from '../md.js';
import { api, streamChat, ApiError } from '../api.js';
import { modelLabel, errorBox } from './util.js';

export const MAX_HOPS = 6; // bot turns per message you send
const HISTORY = 30; // transcript entries each bot sees
const MENTION = /@([a-z0-9][a-z0-9_-]{0,39})/g;
const ALIASES = [['zhunt-auto', 'auto'], ['zhunt-chat', 'chat'], ['zhunt-coding', 'coding'], ['zhunt-long-context', 'long context'], ['zhunt-reasoning', 'reasoning']];

export const STARTER_BOTS = [
  { handle: 'researcher', name: 'Researcher', alias: 'zhunt-reasoning', color: '#7aa2f7',
    instructions: 'Find the facts, options and trade-offs for the request. Be concrete and brief. When the material is ready, hand it to @writer.' },
  { handle: 'writer', name: 'Writer', alias: 'zhunt-chat', color: '#9ece6a',
    instructions: 'Turn the material into a clear, friendly answer for the user. When your draft is done, ask @critic to check it.' },
  { handle: 'critic', name: 'Critic', alias: 'zhunt-chat', color: '#f7768e',
    instructions: 'Check the latest draft for mistakes, gaps and unclear wording. Give short, specific fixes. Do not hand off unless a fix needs research.' },
];

// Teammates a message asks to act, in order, without repeats; never the author itself.
export function mentionsIn(text, handles, self = null) {
  const out = [];
  for (const m of String(text).matchAll(MENTION)) {
    const handle = m[1];
    if (handle !== self && handles.includes(handle) && !out.includes(handle)) out.push(handle);
  }
  return out;
}

export function systemPrompt(bot, bots) {
  const team = bots.map((b) => `@${b.handle} (${b.name})`).join(', ');
  return [
    `You are ${bot.name} (@${bot.handle}), one of several AI bots working with the user in a team chat.`,
    bot.instructions ? `Your instructions: ${bot.instructions}` : null,
    `Your teammates: ${team}.`,
    'To hand work to a teammate, write their @handle followed by exactly what you need from them. Mention a teammate only when you need them to act now. Keep replies focused; the user reads every message.',
  ].filter(Boolean).join('\n');
}

// The conversation as one bot sees it: its own turns as the assistant, everyone else's as
// labelled user turns; consecutive turns of one role merged; always starting with a user turn.
export function contextFor(bot, transcript, bots) {
  const msgs = [];
  for (const e of transcript.slice(-HISTORY)) {
    if (!e.text) continue;
    const role = e.who === bot.handle ? 'assistant' : 'user';
    const content = role === 'assistant' ? e.text : `[${e.who === 'you' ? 'User' : '@' + e.who}]: ${e.text}`;
    const last = msgs[msgs.length - 1];
    if (last && last.role === role) last.content += '\n\n' + content;
    else msgs.push({ role, content });
  }
  while (msgs.length && msgs[0].role !== 'user') msgs.shift();
  return [{ role: 'system', content: systemPrompt(bot, bots) }, ...msgs];
}

export function teamTile(svc) {
  const teamId = Math.random().toString(36).slice(2, 8);
  let bots = [];
  let transcript = []; // {who: 'you' | handle, text, model?}
  let queue = [];
  let hops = 0;
  let busy = false;
  let controller = null;
  let destroyed = false;

  const chips = h('div', { class: 'team-bots', role: 'toolbar', 'aria-label': 'Bots' });
  const msgs = h('div', { class: 'msgs team-msgs', role: 'log', 'aria-label': 'Team conversation', tabindex: '0' });
  msgs.dataset.keepScroll = '1';
  const input = h('textarea', { class: 'composer-input', rows: '1', 'aria-label': 'Message to the team', spellcheck: 'false',
    placeholder: 'Message the team: @researcher, @writer… (no @ continues with the last bot)' });
  const sendBtn = h('button', { class: 'btn primary', type: 'submit' }, 'send');
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); if (busy) stop(); else send(); } }, input, sendBtn);
  const editor = h('div', { class: 'team-editor', hidden: true });
  const el = h('div', { class: 'team' }, chips, msgs, editor, form);

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); if (!busy) send(); }
    else if (e.key === 'Escape') { e.preventDefault(); if (busy) stop(); else el.closest('.pane')?.focus(); }
  });
  input.addEventListener('input', () => { input.rows = Math.min(6, Math.max(1, input.value.split('\n').length)); });

  async function load() {
    try {
      bots = (await api.bots.list()).bots || [];
      if (!bots.length) {
        for (const b of STARTER_BOTS) await api.bots.put(b.handle, b);
        bots = (await api.bots.list()).bots || [];
      }
    } catch (err) {
      if (err.status !== 401) msgs.replaceChildren(errorBox('Cannot load bots', err.message));
      return;
    }
    paintChips();
    if (!transcript.length) paintEmpty();
  }

  function paintChips() {
    chips.replaceChildren(
      ...bots.map((b) => {
        const chip = h('button', { class: 'team-chip', type: 'button', title: `${b.name}: ${b.instructions.slice(0, 120)}`, onclick: () => mention(b.handle) }, '@' + b.handle);
        chip.style.setProperty('--bot', b.color);
        const edit = h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Edit ' + b.name, onclick: () => openEditor(b) }, '✎');
        return h('span', { class: 'team-chip-wrap' }, chip, edit);
      }),
      h('button', { class: 'act', type: 'button', onclick: () => openEditor(null) }, '+ bot'));
  }

  function paintEmpty() {
    msgs.replaceChildren(h('div', { class: 'empty chat-empty' },
      h('p', { class: 'chat-greet' }, 'Your AI team'),
      h('p', { class: 'empty-detail' }, 'Each bot has its own job and runs on whichever free model suits it. Mention one with @, and they hand work to each other: try "@researcher compare three free note-taking apps".'),
      h('div', { class: 'suggest' }, ['@researcher compare three free note-taking apps', '@writer write a friendly out-of-office reply'].map((t) =>
        h('button', { class: 'suggest-btn', type: 'button', onclick: () => { input.value = t; input.focus(); } }, t)))));
  }

  function mention(handle) {
    input.value = (input.value && !input.value.endsWith(' ') ? input.value + ' ' : input.value) + '@' + handle + ' ';
    input.focus();
  }

  function addBubble(who) {
    msgs.querySelector('.chat-empty')?.remove();
    const bot = bots.find((b) => b.handle === who);
    const text = h('div', { class: 'msg-text' });
    const meta = h('div', { class: 'msg-meta' });
    const node = h('article', { class: 'msg ' + (who === 'you' ? 'user' : 'assistant team-bot') },
      h('div', { class: 'msg-role' }, who === 'you' ? 'you' : `${bot?.name || who} @${who}`), text, meta);
    if (bot) node.style.setProperty('--bot', bot.color);
    msgs.append(node);
    msgs.scrollTop = msgs.scrollHeight;
    return { node, text, meta };
  }

  function send() {
    const content = input.value.trim();
    if (!content || busy || !bots.length) return;
    input.value = '';
    input.rows = 1;
    transcript.push({ who: 'you', text: content });
    addBubble('you').text.textContent = content;
    const asked = mentionsIn(content, bots.map((b) => b.handle));
    // no @mention: carry on with whoever spoke last, else the researcher (or the first bot)
    const last = [...transcript].reverse().find((e) => e.who !== 'you' && bots.some((b) => b.handle === e.who));
    const fallback = last ? last.who : (bots.find((b) => b.handle === 'researcher') || bots[0]).handle;
    queue = asked.length ? asked : [fallback];
    hops = 0;
    run();
  }

  async function run() {
    busy = true;
    sendBtn.textContent = 'stop';
    sendBtn.classList.add('danger');
    while (queue.length && !destroyed) {
      if (hops >= MAX_HOPS) {
        msgs.append(h('p', { class: 'dim team-note' }, `Paused after ${MAX_HOPS} bot turns so they do not talk in circles. Reply to carry on.`));
        queue = [];
        break;
      }
      const handle = queue.shift();
      const bot = bots.find((b) => b.handle === handle);
      if (!bot) continue;
      hops += 1;
      const ok = await turn(bot);
      if (!ok) { queue = []; break; }
    }
    busy = false;
    controller = null;
    sendBtn.textContent = 'send';
    sendBtn.classList.remove('danger');
  }

  // One bot's reply; true when it finished (so hand-offs in it may run).
  async function turn(bot) {
    const bubble = addBubble(bot.handle);
    bubble.node.classList.add('pending');
    bubble.text.textContent = 'thinking...';
    controller = new AbortController();
    let acc = '';
    try {
      const res = await streamChat({
        model: bot.alias, messages: contextFor(bot, transcript, bots), session: `team-${teamId}-${bot.handle}`, signal: controller.signal,
        onDelta: (d) => { acc += d; bubble.node.classList.remove('pending'); renderMarkdown(acc, bubble.text); msgs.scrollTop = msgs.scrollHeight; },
      });
      acc = res.text || '(no reply)';
      bubble.node.classList.remove('pending');
      renderMarkdown(acc, bubble.text);
      transcript.push({ who: bot.handle, text: acc, model: res.model });
      bubble.meta.replaceChildren(...(res.model ? [h('span', { class: 'meta-label' }, 'on '), modelLabel(res.model)] : []));
      for (const next of mentionsIn(acc, bots.map((b) => b.handle), bot.handle)) if (!queue.includes(next)) queue.push(next);
      return true;
    } catch (err) {
      bubble.node.classList.remove('pending');
      if (err.name === 'AbortError') {
        if (acc) transcript.push({ who: bot.handle, text: acc });
        bubble.meta.textContent = 'stopped';
      } else {
        bubble.node.classList.add('failed');
        bubble.text.replaceChildren(errorBox(err instanceof ApiError && err.status === 503 ? 'No free model available right now' : 'This bot could not answer', err.message));
      }
      return false;
    }
  }

  function stop() { queue = []; controller?.abort(); }

  function openEditor(bot) {
    const isNew = !bot;
    const name = h('input', { class: 'connect-input', value: bot?.name || '', maxlength: '40', 'aria-label': 'Bot name' });
    const handle = h('input', { class: 'connect-input', value: bot?.handle || '', maxlength: '40', 'aria-label': 'Handle', disabled: !isNew, placeholder: 'e.g. translator' });
    const instructions = h('textarea', { class: 'connect-input team-instructions', rows: '5', maxlength: '4000', 'aria-label': 'Instructions' });
    instructions.value = bot?.instructions || '';
    const alias = h('select', { class: 'connect-input', 'aria-label': 'Model type' }, ALIASES.map(([v, l]) => h('option', { value: v, selected: v === (bot?.alias || 'zhunt-auto') }, l)));
    const color = h('input', { type: 'color', value: bot?.color || '#7aa2f7', 'aria-label': 'Colour' });
    const note = h('p', { class: 'quiet connect-note', role: 'status' });
    const field = (label, control) => h('label', { class: 'team-field' }, h('span', {}, label), control);
    const save = h('button', { class: 'act primary', type: 'submit' }, isNew ? 'Create bot' : 'Save');
    const remove = isNew ? null : h('button', { class: 'act ghost', type: 'button', onclick: async () => { await api.bots.remove(bot.handle); close(); load(); } }, 'Delete');
    const cancel = h('button', { class: 'act', type: 'button', onclick: () => close() }, 'Cancel');
    const formEl = h('form', { class: 'team-form' },
      h('h3', { class: 'connect-h' }, isNew ? 'New bot' : 'Edit @' + bot.handle),
      field('Name', name), field('Handle (@)', handle), field('Instructions: what this bot does', instructions),
      h('div', { class: 'team-row' }, field('Model type', alias), field('Colour', color)),
      h('div', { class: 'acts' }, save, cancel, remove), note);
    formEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      const id = (isNew ? handle.value : bot.handle).trim().toLowerCase();
      try {
        await api.bots.put(id, { name: name.value, instructions: instructions.value, alias: alias.value, color: color.value });
        close();
        await load();
      } catch (err) { note.textContent = err.message || String(err); }
    });
    editor.replaceChildren(formEl);
    editor.hidden = false;
    msgs.hidden = true;
    form.hidden = true;
    (isNew ? handle : name).focus();
    function close() { editor.hidden = true; editor.replaceChildren(); msgs.hidden = false; form.hidden = false; }
  }

  load();

  return {
    el,
    getState: () => ({}),
    focusPrimary: () => input.focus(),
    destroy() { destroyed = true; stop(); },
  };
}
