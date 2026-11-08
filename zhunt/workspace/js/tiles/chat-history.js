// History and Projects panel for the chat tile: saved chats (searchable, grouped by project) and
// projects (a name plus optional instructions that become the system prompt of its chats).
// Saved chats live in ~/.zhunt/chats on this computer; deleting one here deletes the file.

import { h } from '../dom.js';
import { api } from '../api.js';

export const newId = () => {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
};

const when = (seconds) => {
  const d = new Date(seconds * 1000);
  const today = new Date();
  return d.toDateString() === today.toDateString() ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString([], { day: 'numeric', month: 'short' });
};

// svc: toast. cb: { current() -> chat id or null, open(id), deleted(id), projects(list) }
export function createHistory(svc, cb) {
  let projects = [];
  let chats = [];
  let limit = 1000;
  let filter = '';
  let editing = null; // project being edited: {id, name, instructions, isNew}
  let timer = 0;
  let token = 0;

  const search = h('input', { class: 'ctl', type: 'search', placeholder: 'search chats', 'aria-label': 'Search saved chats', spellcheck: 'false' });
  const projectFilter = h('select', { class: 'ctl', 'aria-label': 'Show chats from' });
  const list = h('div', { class: 'hist-list', role: 'list' });
  const projectBox = h('div', { class: 'hist-projects' });
  const closeBtn = h('button', { class: 'pane-btn', type: 'button', onclick: () => close() }, 'close');
  const el = h('section', { class: 'chat-history', hidden: true, 'aria-label': 'Saved chats and projects' },
    h('div', { class: 'row' }, search, projectFilter, closeBtn), list, projectBox);

  const fail = (err) => svc.toast(err.message || String(err), true);

  async function load() {
    const mine = ++token;
    try {
      const [c, p] = await Promise.all([api.chats.list({ q: search.value.trim(), project: filter }), api.projects.list()]);
      if (mine !== token) return;
      chats = c.chats; limit = c.limit || limit; projects = p.projects;
      cb.projects(projects);
      paint();
    } catch (err) { if (mine === token) fail(err); }
  }

  function paintFilter() {
    const value = filter;
    projectFilter.replaceChildren(h('option', { value: '' }, 'all chats'), h('option', { value: 'none' }, 'no project'),
      ...projects.map((p) => h('option', { value: p.id }, p.name)));
    projectFilter.value = [...projectFilter.options].some((o) => o.value === value) ? value : '';
    filter = projectFilter.value;
  }

  function paint() {
    paintFilter();
    const names = new Map(projects.map((p) => [p.id, p.name]));
    list.replaceChildren(...[...(chats.length ? chats.map((c) => h('div', { class: 'hist-item' + (c.id === cb.current() ? ' current' : ''), role: 'listitem' },
      h('button', { class: 'hist-open', type: 'button', title: c.snippet ? c.snippet : 'Open this chat', onclick: () => { cb.open(c.id); close(); } },
        h('span', { class: 'hist-title' }, c.title),
        h('span', { class: 'hist-sub' }, [names.get(c.project) || '', `${c.count} msgs`, when(c.updated)].filter(Boolean).join(' · ')),
        c.snippet && c.snippet !== c.title ? h('span', { class: 'hist-snippet' }, c.snippet) : null),
      h('button', { class: 'pane-btn', type: 'button', 'aria-label': 'Delete chat ' + c.title, title: 'Delete this saved chat', onclick: () => remove(c) }, '×')))
      : [h('p', { class: 'dim hist-empty' }, search.value.trim() ? 'No saved chat matches.' : 'No saved chats yet. Chats are saved here as you go (use "saved" in the chat header to stop saving).')]),
    chats.length >= limit ? h('p', { class: 'dim' }, `You have reached the ${limit}-chat limit: delete some to save new ones.`) : null].filter(Boolean));
    paintProjects();
  }

  function paintProjects() {
    const field = (label, control) => h('label', { class: 'hist-field' }, h('span', {}, label), control);
    if (editing) {
      const name = h('input', { class: 'ctl', maxlength: '60', value: editing.name, 'aria-label': 'Project name' });
      const instructions = h('textarea', { class: 'ctl', rows: '4', maxlength: '4000', 'aria-label': 'Project instructions', placeholder: 'Optional. Sent as the system prompt in every chat of this project, e.g. "Answer like a local guide."' });
      instructions.value = editing.instructions;
      projectBox.replaceChildren(h('h3', {}, editing.isNew ? 'New project' : 'Edit project'), field('Name', name), field('Instructions', instructions),
        h('div', { class: 'row' },
          h('button', { class: 'btn primary', type: 'button', onclick: async () => {
            try { await api.projects.put(editing.id, { name: name.value, instructions: instructions.value }); editing = null; await load(); } catch (err) { fail(err); }
          } }, 'save project'),
          h('button', { class: 'btn', type: 'button', onclick: () => { editing = null; paintProjects(); } }, 'cancel'),
          editing.isNew ? null : h('button', { class: 'btn danger', type: 'button', onclick: async () => {
            if (!confirm(`Delete the project "${editing.name}"? Its chats are kept and become "no project".`)) return;
            try { await api.projects.remove(editing.id); editing = null; await load(); } catch (err) { fail(err); }
          } }, 'delete project')));
      return;
    }
    projectBox.replaceChildren(h('h3', {}, 'Projects'),
      ...projects.map((p) => h('div', { class: 'hist-proj' }, h('span', {}, p.name), h('button', { class: 'pane-btn', type: 'button', onclick: () => { editing = { ...p, isNew: false }; paintProjects(); } }, 'edit'))),
      h('button', { class: 'btn', type: 'button', onclick: () => { editing = { id: newId(), name: '', instructions: '', isNew: true }; paintProjects(); } }, 'new project'));
  }

  async function remove(chat) {
    if (!confirm(`Delete "${chat.title}"? This removes the saved copy from this computer.`)) return;
    try { await api.chats.remove(chat.id); cb.deleted(chat.id); await load(); } catch (err) { if (err.status === 404) await load(); else fail(err); }
  }

  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  projectFilter.addEventListener('change', () => { filter = projectFilter.value; load(); });
  el.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } });

  function open() { el.hidden = false; load(); search.focus(); }
  function close() { el.hidden = true; }
  return { el, open, close, toggle: () => (el.hidden ? open() : close()), refreshProjects: load };
}
