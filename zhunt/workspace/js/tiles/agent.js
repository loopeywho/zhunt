// Agent window: give zhunt a task and watch it work (thinking, tools, results), approve anything
// that changes files, and get the answer. Slash commands: /goal /loop /web /files /stop /clear /help.

import { h } from '../dom.js';
import { renderMarkdown } from '../md.js';
import { api } from '../api.js';
import { modelLabel, chip, errorBox } from './util.js';

export const COMMANDS = [
  ['/goal', '<outcome>', 'keep working until the outcome is reached (more steps)'],
  ['/loop', '<30m|2h|1d> <task>', 'repeat a task on a timer while zhunt runs'],
  ['/loops', '', 'list running loops'],
  ['/unloop', '<id>', 'stop a loop'],
  ['/web', '<task>', 'only use web research tools'],
  ['/files', '<task>', 'only use the shared-folder tools'],
  ['/email', '<task>', 'only use your email (connect it in Connect)'],
  ['/browser', '<task>', 'use the agent\'s own browser window for a website task'],
  ['/folders', '<path>, <path>', 'choose the folders the agent may use'],
  ['/stop', '', 'stop the running task'],
  ['/clear', '', 'clear this window'],
  ['/help', '', 'show these commands'],
];
const TOOLSETS = { web: ['web_search', 'open_page'], files: ['list_files', 'read_file', 'write_file', 'move_file'], email: ['list_emails', 'read_email', 'draft_email', 'send_email'], browser: ['browser_open', 'browser_read', 'browser_follow_link', 'browser_type', 'browser_click', 'browser_press_enter'] };

// '/loop 2h check X' -> {cmd: 'loop', args: '2h check X'}; plain text -> {cmd: null, args: text}
export function parseCommand(text) {
  const m = /^\/([a-z]+)\b\s*([\s\S]*)$/.exec(text.trim());
  return m ? { cmd: m[1], args: m[2].trim() } : { cmd: null, args: text.trim() };
}

export function agentTile(svc) {
  let taskId = null;
  let seen = 0;
  let timer = null;
  let destroyed = false;
  let state = { folders: [], loops: [], tasks: [] };

  const bar = h('div', { class: 'agent-bar' });
  const log = h('div', { class: 'msgs agent-log', role: 'log', 'aria-label': 'Agent steps', tabindex: '0' });
  log.dataset.keepScroll = '1';
  const hints = h('div', { class: 'agent-hints', hidden: true, role: 'listbox', 'aria-label': 'Commands' });
  const input = h('textarea', { class: 'composer-input', rows: '1', 'aria-label': 'Task for the agent', spellcheck: 'false',
    placeholder: 'Give the agent a task, or type / for commands' });
  const sendBtn = h('button', { class: 'btn primary', type: 'submit' }, 'run');
  const form = h('form', { class: 'composer', onsubmit: (e) => { e.preventDefault(); submit(); } }, input, sendBtn);
  const el = h('div', { class: 'agent' }, bar, log, hints, form);

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); submit(); }
    else if (e.key === 'Escape') { e.preventDefault(); hints.hidden = true; el.closest('.pane')?.focus(); }
    else if (e.key === 'Tab' && !hints.hidden) {
      const first = hints.querySelector('button');
      if (first) { e.preventDefault(); first.click(); }
    }
  });
  input.addEventListener('input', () => { input.rows = Math.min(6, Math.max(1, input.value.split('\n').length)); paintHints(); });

  function paintHints() {
    const v = input.value;
    if (!v.startsWith('/') || v.includes(' ')) { hints.hidden = true; return; }
    const matches = COMMANDS.filter(([c]) => c.startsWith(v));
    hints.replaceChildren(...matches.map(([c, a, d]) => h('button', { class: 'agent-hint', type: 'button', role: 'option',
      onclick: () => { input.value = c + ' '; hints.hidden = true; input.focus(); } },
    h('code', {}, c), a ? h('span', { class: 'dim' }, ' ' + a) : null, h('span', { class: 'agent-hint-d' }, d))));
    hints.hidden = !matches.length;
  }

  async function refreshState() {
    try { state = await api.agent.state(); } catch { return; }
    paintBar();
  }

  function paintBar() {
    const folders = state.folders.length ? state.folders.map((f) => f.split('/').slice(-2).join('/')).join(', ') : 'none yet';
    bar.replaceChildren(
      h('span', { class: 'agent-bar-item', title: state.folders.join('\n') || 'Share a folder so the agent can work with your files' },
        'Folders: ', h('strong', {}, folders)),
      h('button', { class: 'pane-btn wide', type: 'button', onclick: chooseFolders }, 'change'),
      ...state.loops.map((l) => h('span', { class: 'agent-bar-item' }, chip(`every ${fmtEvery(l.every)}`, 'warn'), ' ' + l.goal.slice(0, 40),
        h('button', { class: 'pane-btn', type: 'button', 'aria-label': 'Stop loop', title: 'Stop this loop', onclick: () => unloop(l.id) }, 'x'))));
  }

  const fmtEvery = (s) => (s % 86400 === 0 ? s / 86400 + 'd' : s % 3600 === 0 ? s / 3600 + 'h' : Math.round(s / 60) + 'm');

  async function chooseFolders(preset) {
    const value = typeof preset === 'string' ? preset : await svc.prompt('folders the agent may use (comma-separated paths)', {
      initial: state.folders.join(', ') || '~/Documents/zhunt-agent', hint: 'Specific folders only, never your whole disk. Reading is free; changes always ask you first.' });
    if (value == null) return;
    try {
      const res = await api.agent.setFolders(value.split(',').map((s) => s.trim()).filter(Boolean));
      note(`Shared folders: ${res.folders.join(', ') || 'none'}`);
      refreshState();
    } catch (err) { note(err.message || String(err), true); }
  }

  function note(text, isError = false) {
    log.querySelector('.chat-empty')?.remove();
    log.append(h('p', { class: 'agent-note' + (isError ? ' err' : '') }, text));
    log.scrollTop = log.scrollHeight;
  }

  function paintEmpty() {
    log.replaceChildren(h('div', { class: 'empty chat-empty' },
      h('p', { class: 'chat-greet' }, 'Your agent'),
      h('p', { class: 'empty-detail' }, 'Give it a task and it works through it with free models: searching the web, reading pages, and working with files in folders you share. It always asks before changing anything. Type / for commands.'),
      h('div', { class: 'suggest' }, [
        'Find three free alternatives to Notion and compare them, with sources',
        '/email Triage my inbox: what needs a reply today? Draft short replies for those.',
        '/goal Write a one-page summary of today\'s AI news to news.md',
        '/loop 1h check the Groq status page and tell me if anything is down',
      ].map((t) => h('button', { class: 'suggest-btn', type: 'button', onclick: () => { input.value = t; input.focus(); } }, t)))));
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.rows = 1;
    hints.hidden = true;
    const { cmd, args } = parseCommand(text);
    if (cmd === 'help') { note(COMMANDS.map(([c, a, d]) => `${c} ${a} — ${d}`).join('\n')); return; }
    if (cmd === 'clear') { stopPolling(); taskId = null; paintEmpty(); return; }
    if (cmd === 'stop') { if (taskId) { await api.agent.stop(taskId).catch(() => {}); } else note('No task is running.'); return; }
    if (cmd === 'folders') { chooseFolders(args); return; }
    if (cmd === 'loops') { note(state.loops.length ? state.loops.map((l) => `${l.id}: every ${fmtEvery(l.every)} — ${l.goal}`).join('\n') : 'No loops running.'); return; }
    if (cmd === 'unloop') { unloop(args); return; }
    if (cmd === 'loop') {
      const m = /^(\S+)\s+([\s\S]+)$/.exec(args);
      if (!m) { note('Use: /loop 2h <task>', true); return; }
      try {
        const loop = await api.agent.loop({ every: m[1], goal: m[2] });
        note(`Loop ${loop.id} started: every ${fmtEvery(loop.every)} while zhunt runs. Stop it with /unloop ${loop.id}.`);
        refreshState();
      } catch (err) { note(err.message || String(err), true); }
      return;
    }
    let body = { goal: args };
    if (cmd === 'goal') body = { goal: args, mode: 'goal' };
    else if (cmd in TOOLSETS) body = { goal: args, tools: TOOLSETS[cmd] };
    else if (cmd) { note(`Unknown command /${cmd}. Type /help.`, true); return; }
    if (!body.goal) { note('Tell the agent what to do after the command.', true); return; }
    try {
      const task = await api.agent.start(body);
      openTask(task);
    } catch (err) { note(err.message || String(err), true); }
  }

  async function unloop(id) {
    try { await api.agent.unloop(id); note(`Loop ${id} stopped.`); refreshState(); } catch (err) { note(err.message || String(err), true); }
  }

  function openTask(task) {
    stopPolling();
    log.querySelector('.chat-empty')?.remove();
    taskId = task.id;
    seen = 0;
    render(task);
    poll();
  }

  function render(task) {
    for (const e of task.events) addEvent(e);
    seen = task.events.length ? task.events[task.events.length - 1].seq + 1 : seen;
    const running = task.state === 'running' || task.state === 'waiting';
    sendBtn.textContent = running ? 'busy' : 'run';
    if (!running) { stopPolling(); refreshState(); }
  }

  async function poll() {
    if (destroyed || !taskId) return;
    try {
      const task = await api.agent.task(taskId, seen);
      render(task);
      if (task.state === 'running' || task.state === 'waiting') timer = setTimeout(poll, 900);
    } catch (err) {
      if (err.status !== 401) timer = setTimeout(poll, 3000);
    }
  }

  function stopPolling() { clearTimeout(timer); timer = null; }

  function argsLine(args) {
    const parts = Object.entries(args || {}).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => `${k}: ${String(v).length > 80 ? String(v).slice(0, 80) + '…' : v}`);
    return parts.join(' · ');
  }

  function addEvent(e) {
    let node = null;
    if (e.kind === 'goal') {
      node = h('article', { class: 'msg user' }, h('div', { class: 'msg-role' }, e.mode === 'goal' ? 'you · goal' : 'you'), h('div', { class: 'msg-text' }, e.text));
    } else if (e.kind === 'thought') {
      node = h('p', { class: 'agent-thought' }, e.text);
    } else if (e.kind === 'tool') {
      const head = h('summary', {}, h('span', { class: 'agent-tool ' + (e.ok === false || e.error ? 'bad' : 'good') }, e.name), ' ', h('span', { class: 'dim' }, argsLine(e.args)),
        e.ms != null ? h('span', { class: 'meta-dim' }, ` · ${e.ms} ms`) : null);
      node = h('details', { class: 'agent-step' }, head, h('pre', { class: 'agent-result' }, e.error || e.result || ''));
    } else if (e.kind === 'approval') {
      const approve = h('button', { class: 'act primary', type: 'button' }, 'Approve');
      const decline = h('button', { class: 'act', type: 'button' }, 'Decline');
      const decide = async (yes) => {
        approve.disabled = decline.disabled = true;
        try { await api.agent.decide(taskId, e.call_id, yes); } catch (err) { svc.toast(err.message || String(err), true); approve.disabled = decline.disabled = false; }
      };
      approve.addEventListener('click', () => decide(true));
      decline.addEventListener('click', () => decide(false));
      const long = e.args?.content ?? e.args?.body;
      const preview = long ? h('pre', { class: 'agent-result' }, String(long).slice(0, 1600)) : null;
      node = h('div', { class: 'card tone-accent agent-approval', 'data-call': e.call_id },
        h('h3', { class: 'card-title' }, e.name === 'send_email' ? 'The agent wants to SEND this email'
          : e.name === 'browser_click' ? `The agent wants to click [${e.args?.index}] in its browser`
            : e.name === 'browser_press_enter' ? `The agent wants to press Enter in [${e.args?.index}] (submits)` : `The agent wants to ${e.name.replace('_', ' ')}`),
        h('p', { class: 'card-detail' }, argsLine({ ...e.args, content: undefined, body: undefined })), preview,
        h('div', { class: 'acts' }, approve, decline));
    } else if (e.kind === 'decision') {
      const card = log.querySelector(`.agent-approval[data-call="${e.call_id}"]`);
      card?.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      card?.append(h('p', { class: 'quiet' }, e.approved ? 'Approved.' : 'Declined.'));
      return;
    } else if (e.kind === 'answer') {
      const text = h('div', { class: 'msg-text' });
      renderMarkdown(e.text, text);
      node = h('article', { class: 'msg assistant' }, h('div', { class: 'msg-role' }, 'agent'), text,
        h('div', { class: 'msg-meta' }, e.model ? [h('span', { class: 'meta-label' }, 'last step on '), modelLabel(e.model)] : null, e.limit ? h('span', { class: 'meta-dim' }, ' · stopped at the step limit') : null));
    } else if (e.kind === 'error') {
      node = errorBox('The agent stopped', e.text);
    } else if (e.kind === 'stopped') {
      node = h('p', { class: 'agent-note' }, 'Stopped.');
    }
    if (node) { log.append(node); log.scrollTop = log.scrollHeight; }
  }

  paintEmpty();
  refreshState();

  return {
    el,
    getState: () => ({}),
    focusPrimary: () => input.focus(),
    destroy() { destroyed = true; stopPolling(); },
  };
}
