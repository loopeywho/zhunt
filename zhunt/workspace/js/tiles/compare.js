// Compare tile: one prompt, 1-4 models picked from /status, results side by side.

import { h, uid, fmtMs, fmtNum } from '../dom.js';
import { renderMarkdown } from '../md.js';
import { api, ApiError } from '../api.js';
import { modelLabel, errorBox, emptyState, chip } from './util.js';

const MAX_MODELS = 4;

export function compareTile(svc, state, ctx) {
  let selected = Array.isArray(state.models) ? state.models.filter((m) => typeof m === 'string').slice(0, MAX_MODELS) : [];
  let status = null;
  let statusError = null;
  let busy = false;
  let controller = null;
  let seeded = selected.length > 0;
  const boxes = new Map(); // model id -> {cb, item, why}; declared before subscribe(), which paints synchronously
  let pickerSig = '';

  const prompt = h('textarea', {
    class: 'composer-input', rows: '3', spellcheck: 'false', 'aria-label': 'Prompt to compare',
    placeholder: 'one prompt, several models  (Ctrl+Enter runs)',
  });
  const runBtn = h('button', { class: 'btn primary', type: 'button', onclick: run }, 'run');
  const picker = h('details', { class: 'picker' });
  const summary = h('summary', {});
  picker.append(summary);
  const pickerBody = h('div', { class: 'picker-body' });
  picker.append(pickerBody);
  const results = h('div', { class: 'cmp-results', 'aria-live': 'polite' });
  results.dataset.keepScroll = '1';
  results.append(emptyState('Pick up to four models and run a prompt.', 'Compare bypasses routing, but keys, quota and the privacy policy still apply.'));

  const top = h('div', { class: 'cmp-top' }, picker, h('div', { class: 'composer' }, prompt, runBtn));
  const el = h('div', { class: 'compare' }, top, results);

  prompt.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); }
    else if (e.key === 'Escape') { e.preventDefault(); if (busy) controller?.abort(); else el.closest('.pane')?.focus(); }
  });

  const unsub = svc.store.subscribe('status', (snap) => {
    status = snap.data;
    statusError = snap.error;
    if (status && !seeded) {
      seeded = true;
      selected = allModels().filter((m) => m.allowed).slice(0, 2).map((m) => m.id);
    }
    if (status) {
      const known = new Set(allModels().map((m) => m.id));
      selected = selected.filter((id) => known.has(id));
    }
    paintPicker();
  });

  function allModels() {
    return (status?.providers || []).flatMap((p) => (p.models || []).map((m) => ({ ...m, platform: p.platform, providerName: p.name })));
  }

  // The checkbox list is rebuilt only when the model catalog itself changes. Everything else (selection,
  // disabled state, "max 4" notes) is updated in place, so a status poll or a toggle never drops keyboard focus.

  function buildPicker() {
    const focusedModel = pickerBody.contains(document.activeElement) ? document.activeElement.dataset.model : null;
    pickerBody.replaceChildren();
    boxes.clear();
    for (const p of status.providers || []) {
      const group = h('fieldset', { class: 'picker-group' }, h('legend', {}, p.name || p.platform));
      for (const m of p.models || []) {
        const id = uid('m');
        const cb = h('input', { type: 'checkbox', id });
        cb.dataset.model = m.id;
        cb.addEventListener('change', () => {
          selected = cb.checked ? [...selected, m.id] : selected.filter((x) => x !== m.id);
          paintPicker();
          ctx.changed();
        });
        const item = h('label', { class: 'picker-item', for: id }, cb, modelLabel(m.id));
        boxes.set(m.id, { cb, item, why: null, m });
        group.append(item);
      }
      pickerBody.append(group);
    }
    if (!(status.providers || []).length) pickerBody.append(h('p', { class: 'dim' }, 'no providers in the catalog'));
    if (focusedModel) boxes.get(focusedModel)?.cb.focus();
  }

  function paintPicker() {
    summary.textContent = `models ${selected.length}/${MAX_MODELS}` + (selected.length ? '  ' + selected.join(', ') : '');
    if (statusError && !status) {
      pickerSig = '';
      boxes.clear();
      pickerBody.replaceChildren(errorBox('Cannot load models', statusError.message));
      return;
    }
    if (!status) { pickerSig = ''; boxes.clear(); pickerBody.replaceChildren(h('p', { class: 'dim' }, 'loading models...')); return; }
    const sig = JSON.stringify((status.providers || []).map((p) => [p.name || p.platform, (p.models || []).map((m) => m.id)]));
    if (sig !== pickerSig) { pickerSig = sig; buildPicker(); }
    const full = selected.length >= MAX_MODELS;
    const current = new Map(allModels().map((x) => [x.id, x]));
    for (const [id, entry] of boxes) {
      const { cb, item } = entry;
      const m = current.get(id) || entry.m;
      const on = selected.includes(m.id);
      const disabled = !m.allowed || (!on && full);
      cb.checked = on;
      cb.disabled = disabled;
      item.classList.toggle('off', disabled);
      const why = !m.allowed ? (m.blocked_reason || 'blocked') : (!on && full ? 'max ' + MAX_MODELS : '');
      if (why && !entry.why) { entry.why = h('span', { class: 'picker-why' }, why); item.append(entry.why); }
      else if (why) entry.why.textContent = why;
      else if (entry.why) { entry.why.remove(); entry.why = null; }
    }
  }

  async function run() {
    const content = prompt.value.trim();
    if (busy) { controller?.abort(); return; }
    if (!content) { prompt.focus(); return; }
    if (!selected.length) { results.replaceChildren(errorBox('No models selected', 'Open the model list above and tick one to four models.')); picker.open = true; return; }
    busy = true;
    runBtn.textContent = 'stop';
    runBtn.classList.add('danger');
    controller = new AbortController();
    const asked = [...selected];
    results.replaceChildren(h('div', { class: 'cmp-grid' }, asked.map((m) => card(m, null))));
    try {
      const res = await api.compare([{ role: 'user', content }], asked, controller.signal);
      const byModel = new Map((res.results || []).map((r) => [r.model, r]));
      results.replaceChildren(h('div', { class: 'cmp-grid' }, asked.map((m) => card(m, byModel.get(m) || { model: m, ok: false, error: 'no result returned' }))));
    } catch (err) {
      if (err.name === 'AbortError') results.replaceChildren(emptyState('Stopped.'));
      else if (err instanceof ApiError && err.status === 401) results.replaceChildren();
      else results.replaceChildren(errorBox(
        err.status === 404 ? 'Unknown model' : err.status === 400 ? 'Request rejected' : 'Compare failed', err.message));
    } finally {
      busy = false;
      controller = null;
      runBtn.textContent = 'run';
      runBtn.classList.remove('danger');
    }
  }

  function card(model, r) {
    const head = h('header', { class: 'cmp-card-head' }, modelLabel(model));
    const body = h('div', { class: 'cmp-card-body' });
    const foot = h('footer', { class: 'cmp-card-foot' });
    const art = h('article', { class: 'cmp-card', 'aria-busy': r ? 'false' : 'true' }, head, body, foot);
    if (!r) {
      body.append(h('p', { class: 'dim pulse' }, 'waiting...'));
      return art;
    }
    if (r.ok) {
      head.append(chip('ok', 'ok'));
      const md = h('div', { class: 'md' });
      renderMarkdown(r.text || '', md);
      body.append(md);
    } else {
      head.append(chip('failed', 'err'));
      art.classList.add('failed');
      body.append(h('p', { class: 'cmp-error', role: 'alert' }, r.error || 'unknown error'));
    }
    foot.append(
      h('span', {}, 'latency ', h('b', {}, fmtMs(r.latency_ms))),
      h('span', {}, 'in ', h('b', {}, fmtNum(r.input_tokens))),
      h('span', {}, 'out ', h('b', {}, fmtNum(r.output_tokens))),
    );
    return art;
  }

  paintPicker();
  return {
    el,
    header: h('span', { class: 'dim' }, 'compare'),
    getState: () => ({ models: selected }),
    focusPrimary: () => prompt.focus(),
    destroy() { unsub(); controller?.abort(); },
  };
}
