// Engineering is an ordinary Zhunt window: the existing floating/tiled managers own it.
import { h } from '../dom.js';
import { api } from '../api.js';

export function engineeringTile(svc, state, ctx) {
  let selectedTask = state.task || '', selectedSession = state.session || '';
  let snapshot = { projects: [], tasks: [], sessions: [] }, stopped = false, polling = false;
  let generation = 0, offset = 0, terminal = null, fit = null, inputQueue = Promise.resolve();
  const call = (path = '', method = 'GET', json) => api.engineering(path, method, json);
  const error = e => svc.toast(e.message, true);
  const run = fn => async () => { try { await fn(); } catch (e) { error(e); } };
  const button = (label, fn) => h('button', { class: 'ctl', type: 'button', onclick: run(fn) }, label);
  const project = h('select', { class: 'ctl', 'aria-label': 'Engineering project' });
  const tasks = h('select', { class: 'ctl', 'aria-label': 'Engineering task' });
  const sessions = h('select', { class: 'ctl', 'aria-label': 'Terminal session' });
  const directory = h('input', { class: 'ctl', placeholder: '/path/to/project', 'aria-label': 'Project directory' });
  const name = h('input', { class: 'ctl', placeholder: 'Task name', maxlength: 100, 'aria-label': 'Task name' });
  const isolated = h('input', { type: 'checkbox' });
  const kind = h('select', { class: 'ctl', 'aria-label': 'Terminal type' },
    [['shell', 'Shell'], ['codex-zhunt', 'Codex · Zhunt routing'], ['hermes-zhunt', 'Hermes · Zhunt routing'], ['codex', 'Codex · own account'], ['claude', 'Claude Code · own account']].map(([id, label]) => h('option', { value: id }, label)));
  const policy = h('p', { class: 'dim' });
  const status = h('span', { role: 'status' });
  const host = h('div', { class: 'engineering-terminal' });
  const diff = h('pre', { class: 'engineering-diff', hidden: true });
  const stop = button('Stop session', async () => {
    if (!selectedSession || !confirm('Stop this terminal and its processes?')) return;
    await call(`/sessions/${selectedSession}/stop`, 'POST'); await refresh();
  });
  const remove = button('Remove exited session', async () => {
    if (!selectedSession) return;
    await call(`/sessions/${selectedSession}`, 'DELETE'); selectedSession = ''; attach(); await refresh();
  });
  const launch = button('Launch', async () => {
    if (!selectedTask) throw new Error('Create or choose a task first.');
    if (['codex', 'claude'].includes(kind.value) && !confirm('Launch using this CLI’s own account and billing? Zhunt routing and spending controls will not apply.')) return;
    const result = await call('/sessions', 'POST', { task: selectedTask, kind: kind.value });
    selectedSession = result.id; ctx.changed(); attach(); await refresh(); terminal?.focus();
  });
  const el = h('div', { class: 'engineering' },
    h('div', { class: 'engineering-row' }, directory, button('Add project', async () => {
      const added = await call('/projects', 'POST', { path: directory.value });
      await refresh(); project.value = added.id; selectedTask = ''; selectedSession = ''; attach(); paint(); directory.value = '';
    })),
    h('div', { class: 'engineering-row' }, project, tasks),
    h('div', { class: 'engineering-row' }, name, h('label', {}, isolated, ' Isolated Git worktree'), button('Create task', async () => {
      const task = await call('/tasks', 'POST', { project: project.value, name: name.value, isolated: isolated.checked });
      selectedTask = task.id; selectedSession = ''; ctx.changed(); attach(); await refresh(); name.value = '';
    })),
    h('div', { class: 'engineering-row' }, kind, launch, button('Git changes', async () => {
      if (!selectedTask) return;
      const changes = await call(`/tasks/${selectedTask}/changes`);
      diff.textContent = `Working tree\n${changes.status || 'Clean'}\n\nUnstaged\n${changes.diff}\n\nStaged\n${changes.staged}`;
      diff.hidden = false; host.hidden = true;
    }), button('Terminal', () => { diff.hidden = true; host.hidden = false; resize(); })),
    policy, h('div', { class: 'engineering-row' }, sessions, stop, remove, status), host, diff,
    h('p', { class: 'dim' }, 'Closing this window detaches. Sessions run until stopped or the daemon exits. Project/task definitions are saved; processes and output are not restored after a daemon restart.'));
  function explain() {
    policy.textContent = kind.value.endsWith('-zhunt')
      ? 'Uses the existing Zhunt router, free quotas, failover and configured privacy/paid policy.'
      : kind.value === 'shell' ? 'Local shell in the task folder. Commands run with your normal user permissions.'
      : 'Uses the CLI’s own account, model and billing. Zhunt routing and spending caps do not apply.';
  }
  kind.onchange = explain; explain();
  function options(select, rows, selected, placeholder) {
    const signature = JSON.stringify(rows);
    if (select.dataset.options !== signature) {
      select.replaceChildren(h('option', { value: '' }, placeholder), ...rows.map(r => h('option', { value: r.id }, r.name)));
      select.dataset.options = signature;
    }
    select.value = selected;
  }
  function paint() {
    let p = project.value || snapshot.tasks.find(t => t.id === selectedTask)?.project || snapshot.projects[0]?.id || '';
    options(project, snapshot.projects, p, 'Choose project');
    const available = snapshot.tasks.filter(t => t.project === p);
    if (!available.some(t => t.id === selectedTask)) selectedTask = available[0]?.id || '';
    options(tasks, available, selectedTask, 'Choose task');
    const live = snapshot.sessions.filter(s => s.task === selectedTask);
    if (!live.some(s => s.id === selectedSession)) { selectedSession = live[0]?.id || ''; attach(); }
    options(sessions, live.map(s => ({ id: s.id, name: `${s.kind} · ${s.running ? 'running' : `exited ${s.exit_code}`} · ${s.id.slice(0, 6)}` })), selectedSession, 'Choose terminal');
    const session = live.find(s => s.id === selectedSession);
    stop.disabled = !session?.running; remove.disabled = !session || session.running;
    launch.disabled = !selectedTask || snapshot.supported === false || (kind.value !== 'shell' && !snapshot.agents?.[kind.value.replace('-zhunt', '')]);
    status.textContent = `${snapshot.sessions.filter(s => s.running).length} running`;
  }
  project.onchange = () => { selectedTask = ''; selectedSession = ''; attach(); paint(); ctx.changed(); };
  tasks.onchange = () => { selectedTask = tasks.value; selectedSession = ''; attach(); paint(); ctx.changed(); };
  sessions.onchange = () => { selectedSession = sessions.value; attach(); ctx.changed(); };
  kind.addEventListener('change', paint);
  async function refresh() { snapshot = await call(); if (!stopped) paint(); }
  function resize() {
    if (!terminal || !host.isConnected || host.clientWidth < 20 || host.clientHeight < 20 || host.hidden) return;
    fit.fit(); const id = selectedSession;
    call(`/sessions/${id}/resize`, 'POST', { cols: terminal.cols, rows: terminal.rows }).catch(error);
  }
  function attach() {
    generation++; offset = 0; terminal?.dispose(); terminal = null; host.replaceChildren();
    diff.hidden = true; host.hidden = false;
    if (!selectedSession || stopped) { host.append(h('p', { class: 'dim' }, 'Launch a terminal for this task. Open another Engineering window for side-by-side sessions.')); return; }
    const id = selectedSession;
    // xterm creates sizing/theme style elements. Authorize only those elements,
    // keeping the workspace's strict CSP (no unsafe-inline or remote scripts).
    const terminalDocument = new Proxy(document, { get(target, property) {
      if (property === 'createElement') return (...args) => {
        const node = target.createElement(...args);
        if (args[0] === 'style') node.nonce = snapshot.style_nonce;
        return node;
      };
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    } });
    terminal = new window.Terminal({ documentOverride: terminalDocument, fontSize: 13, scrollback: 5000, theme: { background: '#101715', foreground: '#dce8d5' } });
    fit = new window.FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(host);
    terminal.onData(data => {
      inputQueue = inputQueue.then(() => call(`/sessions/${id}/input`, 'POST', { data })).catch(error);
    });
    terminal.onBell(() => svc.toast('A terminal needs attention.'));
    requestAnimationFrame(resize);
  }
  const observer = new ResizeObserver(resize); observer.observe(host);
  async function poll() {
    if (polling || stopped) return;
    polling = true;
    try {
      if (selectedSession && terminal) {
        const g = generation, term = terminal;
        const data = await call(`/sessions/${selectedSession}?since=${offset}`);
        if (g === generation && !stopped) {
          if (data.reset) term.reset();
          if (data.data) await new Promise(resolve => {
            const timeout = setTimeout(resolve, 1000);
            term.write(Uint8Array.from(atob(data.data), c => c.charCodeAt(0)), () => { clearTimeout(timeout); resolve(); });
          });
          if (g !== generation || stopped) return;
          offset = data.offset;
          status.textContent = data.running ? 'Running (may be waiting for input)' : `Exited ${data.exit_code}`;
          stop.disabled = !data.running; remove.disabled = data.running;
        }
      }
    } catch (e) { status.textContent = e.message; }
    finally { polling = false; }
  }
  const timer = setInterval(poll, 200);
  const refreshTimer = setInterval(() => refresh().catch(error), 5000);
  refresh().then(() => { if (selectedSession && !terminal) attach(); }).catch(error);
  return { el, header: button('Refresh', refresh), getState: () => ({ task: selectedTask, session: selectedSession }),
    focusPrimary: () => terminal?.focus(), destroy: () => { stopped = true; generation++; clearInterval(timer); clearInterval(refreshTimer); observer.disconnect(); terminal?.dispose(); } };
}
