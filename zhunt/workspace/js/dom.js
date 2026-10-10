// Tiny DOM helpers. Everything goes through createElement / textContent: no innerHTML anywhere.

// h('div', {class: 'x', onclick: fn, 'aria-label': 'y'}, 'text', childEl, ...)
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export function uid(prefix = 'id') {
  return prefix + '-' + Math.random().toString(36).slice(2, 8);
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined || !isFinite(ms)) return '-';
  return ms < 1000 ? Math.round(ms) + 'ms' : (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + 's';
}

export function fmtNum(n) {
  if (n === null || n === undefined || !isFinite(n)) return '-';
  return n >= 10000 ? (n / 1000).toFixed(1) + 'k' : String(n);
}

export function fmtClock(tsSeconds) {
  if (!tsSeconds) return '-';
  const d = new Date(tsSeconds * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm' + String(s % 60).padStart(2, '0') + 's';
  return Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') + 'm';
}

// "groq/openai/gpt-oss-120b" -> {platform: "groq", name: "openai/gpt-oss-120b"}
export function splitModel(id) {
  const s = String(id || '');
  const i = s.indexOf('/');
  return i < 0 ? { platform: '', name: s } : { platform: s.slice(0, i), name: s.slice(i + 1) };
}

export const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

// Roving-tabindex tablist: tabs = [{id, label}], onSelect(id). Returns the element and a setter.
export function tablist(label, tabs, selected, onSelect) {
  const list = h('div', { class: 'tabs', role: 'tablist', 'aria-label': label });
  const buttons = new Map();
  function paint(id) {
    for (const [tid, b] of buttons) {
      const on = tid === id;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
  }
  function select(id, focus) {
    paint(id);
    if (focus) buttons.get(id).focus();
    onSelect(id);
  }
  for (const t of tabs) {
    const b = h('button', {
      class: 'tab', role: 'tab', type: 'button', id: uid('tab'), 'aria-selected': 'false', tabindex: '-1',
      onclick: () => select(t.id, false),
    }, t.label);
    b.addEventListener('keydown', (e) => {
      const ids = tabs.map((x) => x.id);
      let i = ids.indexOf(t.id);
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') i = (i + 1) % ids.length;
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') i = (i + ids.length - 1) % ids.length;
      else if (e.key === 'Home') i = 0;
      else if (e.key === 'End') i = ids.length - 1;
      else return;
      e.preventDefault();
      e.stopPropagation();
      select(ids[i], true);
    });
    buttons.set(t.id, b);
    list.append(b);
  }
  paint(selected);
  return { el: list, set: (id) => select(id, false) };
}
