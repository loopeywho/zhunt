import { h, splitModel } from '../dom.js';

export const TIERS = ['chat', 'coding', 'long-context', 'reasoning'];

// A tier badge. Colour comes from theme variables; the text carries the meaning (not colour alone).
export function tierChip(tier) {
  const t = String(tier || '?');
  const cls = TIERS.includes(t) ? 'tier-' + t : 'tier-unknown';
  return h('span', { class: 'chip ' + cls }, t);
}

export function chip(text, cls = '', title) {
  return h('span', { class: ('chip ' + cls).trim(), title }, text);
}

// "groq/openai/gpt-oss-120b" with the platform dimmed.
export function modelLabel(id) {
  const { platform, name } = splitModel(id);
  return h('span', { class: 'model' }, platform ? h('span', { class: 'model-plat' }, platform + '/') : null, name);
}

export function emptyState(title, detail) {
  return h('div', { class: 'empty' }, h('p', { class: 'empty-title' }, title), detail ? h('p', { class: 'empty-detail' }, detail) : null);
}

// Inline error box. `actions` is an optional array of nodes.
export function errorBox(title, detail, actions) {
  return h('div', { class: 'errbox', role: 'alert' },
    h('p', { class: 'errbox-title' }, title),
    detail ? h('p', { class: 'errbox-detail' }, detail) : null,
    actions && actions.length ? h('div', { class: 'errbox-actions' }, actions) : null);
}

// Headroom meter, 0..1. null = unknown.
export function headroomBar(headroom) {
  if (headroom === null || headroom === undefined) {
    return h('span', { class: 'bar bar-unknown', title: 'headroom unknown' }, h('span', { class: 'bar-label' }, 'n/a'));
  }
  const pct = Math.round(Math.max(0, Math.min(1, headroom)) * 100);
  const level = pct > 50 ? 'ok' : pct > 20 ? 'warn' : 'err';
  const fill = h('span', { class: 'bar-fill ' + level });
  fill.style.width = pct + '%';
  return h('span', {
    class: 'bar', role: 'meter', 'aria-label': 'Quota headroom', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct),
  }, fill, h('span', { class: 'bar-label' }, pct + '%'));
}

// Shared header control: a small toggle button for live-refresh tiles.
export function liveToggle(initiallyPaused, onToggle) {
  let paused = initiallyPaused;
  const b = h('button', { class: 'pane-btn wide', type: 'button' });
  const paint = () => {
    b.textContent = paused ? 'paused' : 'live';
    b.setAttribute('aria-pressed', String(!paused));
    b.setAttribute('aria-label', paused ? 'Resume live updates' : 'Pause live updates');
    b.classList.toggle('is-live', !paused);
  };
  b.addEventListener('click', () => { paused = !paused; paint(); onToggle(paused); });
  paint();
  return b;
}
