// Modal overlays: the command palette (also used as a picker), a one-line prompt, and the help sheet.

import { h, uid, clear } from './dom.js';

// Subsequence match with bonuses for word starts and runs. Returns a score or -1.
export function fuzzy(query, text) {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 0;
  let score = 0; let ti = 0; let run = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti);
    if (found < 0) return -1;
    const boundary = found === 0 || /[\s:/_-]/.test(t[found - 1]);
    run = found === ti ? run + 1 : 0;
    score += 1 + run * 2 + (boundary ? 4 : 0) - Math.min(found - ti, 6) * 0.2;
    ti = found + 1;
  }
  return score - t.length * 0.01;
}

export function createOverlays(root) {
  let current = null; // {close(result)}
  let returnFocus = null;

  // primary: the element that should hold focus while the overlay is open (the input, or the dialog itself).
  function mount(dialog, label, primary = dialog) {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const backdrop = h('div', { class: 'backdrop' });
    const wrap = h('div', { class: 'overlay' }, backdrop, dialog);
    dialog.setAttribute('role', 'dialog');
    dialog.tabIndex = -1;
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', label);
    root.append(wrap);
    document.getElementById('app')?.setAttribute('inert', '');
    const close = () => {
      wrap.remove();
      document.getElementById('app')?.removeAttribute('inert');
      current = null;
      const target = returnFocus && document.contains(returnFocus) ? returnFocus : document.querySelector('.pane.is-focused');
      target?.focus({ preventScroll: true });
    };
    backdrop.addEventListener('mousedown', () => current?.cancel());
    // Escape works wherever focus sits inside the overlay (not only on the input), except while an IME is composing.
    wrap.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || e.isComposing || e.keyCode === 229) return;
      e.preventDefault();
      e.stopPropagation();
      current?.cancel();
    }, true);
    // Clicking a non-focusable part of the dialog (label, list gap, help text) must not drop focus to <body>.
    wrap.addEventListener('mousedown', (e) => {
      if (e.target === backdrop) return;
      if (e.target instanceof Element && e.target.closest('input, textarea, select, button, a')) return;
      e.preventDefault();
      primary.focus({ preventScroll: true });
    });
    return close;
  }

  function closeCurrent() { if (current) current.cancel(); }

  // items: [{title, group, keys, hint, run}]. Resolves when something is picked (after closing).
  function palette(items, { placeholder = 'type a command' } = {}) {
    closeCurrent();
    return new Promise((resolve) => {
      const listId = uid('list');
      const input = h('input', {
        class: 'pal-input', type: 'text', role: 'combobox', 'aria-expanded': 'true', 'aria-controls': listId,
        'aria-autocomplete': 'list', 'aria-label': placeholder, placeholder, spellcheck: 'false', autocomplete: 'off',
      });
      const list = h('ul', { class: 'pal-list', id: listId, role: 'listbox', 'aria-label': 'Results' });
      const count = h('div', { class: 'pal-foot' });
      const dialog = h('div', { class: 'dialog palette' }, input, list, count);
      let shown = []; let sel = 0;

      const render = () => {
        const q = input.value.trim();
        shown = items
          .map((it) => ({ it, s: fuzzy(q, (it.group ? it.group + ' ' : '') + it.title) }))
          .filter((x) => x.s >= 0)
          .sort((a, b) => (q ? b.s - a.s : 0))
          .map((x) => x.it);
        sel = Math.min(sel, Math.max(0, shown.length - 1));
        clear(list);
        shown.forEach((it, i) => {
          const li = h('li', { class: 'pal-item' + (i === sel ? ' sel' : ''), role: 'option', id: listId + '-' + i, 'aria-selected': String(i === sel) },
            it.group ? h('span', { class: 'pal-group' }, it.group) : null,
            h('span', { class: 'pal-title' }, it.title),
            it.hint ? h('span', { class: 'pal-hint' }, it.hint) : null,
            it.keys ? h('kbd', {}, it.keys) : null);
          li.addEventListener('mousemove', () => { if (sel !== i) { sel = i; paintSel(); } });
          li.addEventListener('click', () => choose(i));
          list.append(li);
        });
        if (!shown.length) list.append(h('li', { class: 'pal-empty', role: 'presentation' }, 'nothing matches'));
        count.textContent = `${shown.length} of ${items.length}   up/down move  Enter run  Esc close`;
        input.setAttribute('aria-activedescendant', shown.length ? listId + '-' + sel : '');
      };
      const paintSel = () => {
        [...list.children].forEach((li, i) => {
          li.classList.toggle('sel', i === sel);
          li.setAttribute('aria-selected', String(i === sel));
        });
        input.setAttribute('aria-activedescendant', shown.length ? listId + '-' + sel : '');
        list.children[sel]?.scrollIntoView({ block: 'nearest' });
      };
      const finish = (picked) => { closeFn(); current = null; resolve(picked); };
      const choose = (i) => { const it = shown[i]; if (it) finish(it); };

      input.addEventListener('input', () => { sel = 0; render(); });
      input.addEventListener('keydown', (e) => {
        if (e.isComposing || e.keyCode === 229) return; // IME candidate selection must not run/select an item
        if (e.key === 'ArrowDown' || (e.ctrlKey && e.key === 'n')) { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); paintSel(); }
        else if (e.key === 'ArrowUp' || (e.ctrlKey && e.key === 'p')) { e.preventDefault(); sel = Math.max(0, sel - 1); paintSel(); }
        else if (e.key === 'Enter') { e.preventDefault(); choose(sel); }
        else if (e.key === 'Tab') { e.preventDefault(); }
      });
      const closeFn = mount(dialog, placeholder, input);
      current = { cancel: () => finish(null), kind: 'palette' };
      render();
      input.focus();
    });
  }

  // One-line text prompt. validate(text) returns an error string or ''. Resolves to the text or null.
  function prompt(label, { initial = '', validate, hint = '' } = {}) {
    closeCurrent();
    return new Promise((resolve) => {
      const input = h('input', { class: 'pal-input', type: 'text', 'aria-label': label, value: initial, spellcheck: 'false', autocomplete: 'off' });
      const note = h('div', { class: 'pal-foot', role: 'status' }, hint);
      const dialog = h('div', { class: 'dialog palette' }, h('div', { class: 'pal-label' }, label), input, note);
      const finish = (v) => { closeFn(); current = null; resolve(v); };
      const check = () => { const msg = validate ? validate(input.value.trim()) : ''; note.textContent = msg || hint; note.classList.toggle('bad', !!msg); return !msg; };
      input.addEventListener('input', check);
      input.addEventListener('keydown', (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === 'Enter') { e.preventDefault(); if (input.value.trim() && check()) finish(input.value.trim()); }
        else if (e.key === 'Tab') e.preventDefault();
      });
      const closeFn = mount(dialog, label, input);
      current = { cancel: () => finish(null), kind: 'prompt' };
      input.focus();
      input.select();
    });
  }

  // Read-only sheet; groups = [{title, rows: [[keys, description], ...]}]
  function help(groups) {
    closeCurrent();
    const body = h('div', { class: 'help-body' }, groups.map((g) => h('section', { class: 'help-group' },
      h('h3', {}, g.title),
      h('dl', {}, g.rows.map(([k, d]) => [h('dt', {}, h('kbd', {}, k)), h('dd', {}, d)])))));
    const dialog = h('div', { class: 'dialog help', tabindex: '-1' }, h('div', { class: 'pal-label' }, 'Keyboard reference  (Esc closes)'), body);
    const finish = () => { closeFn(); current = null; };
    dialog.addEventListener('keydown', (e) => { if (e.key === '?' || e.key === 'q') { e.preventDefault(); finish(); } });
    const closeFn = mount(dialog, 'Keyboard reference');
    current = { cancel: finish, kind: 'help' };
    dialog.focus();
  }

  return { palette, prompt, help, close: closeCurrent, get isOpen() { return !!current; }, get kind() { return current?.kind; } };
}
