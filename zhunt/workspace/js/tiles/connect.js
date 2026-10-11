// Connect window: get a free key from each provider and paste it here. The key goes only to the
// local zhunt daemon (saved owner-only in ~/.zhunt/env) and is never shown again.

import { h } from '../dom.js';
import { api, ApiError } from '../api.js';
import { chip, errorBox, swapIfChanged } from './util.js';

export function connectTile(svc) {
  let snap = svc.store.get('status');
  const list = h('div', { class: 'connect-list' });
  list.dataset.keepScroll = '1';
  const intro = h('p', { class: 'connect-intro' },
    'Each provider gives a free API key. Open its page, create a key, then paste it here. ',
    'Keys stay on this computer, in a file only you can read.');
  const el = h('div', { class: 'connect' }, intro, list);
  const open = new Set(); // providers whose paste box is showing
  let images = null; // /images/status
  const loadImages = () => api.images.status().then((s) => { images = s; paint(); }, () => {});
  loadImages();

  // Cloudflare Workers AI needs two values (account id + token), so it has its own row.
  function picturesRow() {
    const actions = [h('button', { class: 'act primary', type: 'button', onclick: () => window.open(images.key_url, '_blank', 'noopener') }, images.connected ? 'Manage token' : 'Get free token')];
    if (!open.has('images')) actions.push(h('button', { class: 'act', type: 'button', onclick: () => { open.add('images'); paint(); focusInput('images'); } }, images.connected ? 'Replace' : 'Connect'));
    if (images.connected) actions.push(h('button', { class: 'act ghost', type: 'button', onclick: async () => { await api.images.disconnect(); svc.toast('AI pictures disconnected'); loadImages(); } }, 'Disconnect'));
    let form = null;
    if (open.has('images')) {
      const account = h('input', { class: 'connect-input', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Cloudflare account ID', placeholder: 'account ID (32 letters and digits)' });
      const token = h('input', { class: 'connect-input', type: 'password', autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Cloudflare API token', placeholder: 'API token' });
      const note = h('p', { class: 'quiet connect-note', role: 'status' });
      form = h('form', { class: 'connect-form', 'data-for': 'images' }, account, token,
        h('button', { class: 'act primary', type: 'submit' }, 'Save'),
        h('button', { class: 'act ghost', type: 'button', onclick: () => { open.delete('images'); paint(); } }, 'Cancel'), note);
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        try {
          await api.images.connect(account.value.trim(), token.value.trim());
          token.value = '';
          open.delete('images');
          svc.toast('AI pictures connected');
          loadImages();
        } catch (err) { note.textContent = err.message || String(err); }
      });
    }
    return h('article', { class: 'connect-row' + (images.connected ? ' is-set' : ''), 'data-platform': 'images' },
      h('div', { class: 'connect-top' }, h('strong', {}, images.provider), images.connected ? chip('connected', 'ok') : chip('not connected', '')),
      h('p', { class: 'quiet' }, images.note),
      h('div', { class: 'acts' }, actions), form);
  }

  function paint() {
    if (snap.error && !snap.data) { swapIfChanged(list, [errorBox('Cannot reach zhunt', snap.error.message)]); return; }
    const providers = snap.data?.providers || [];
    const premium = (p) => (p.models || []).length && p.models.every((m) => !m.free);
    const free = providers.filter((p) => !premium(p));
    const paid = providers.filter(premium);
    const connected = free.filter((p) => p.key_set).length;
    const trainers = free.filter((p) => (p.models || []).some((m) => m.may_train_on_prompts)).map((p) => p.name.replace(/ \(.*\)$/, ''));
    const rows = [
      h('h3', { class: 'connect-h' }, `Free models · ${connected} of ${free.length} connected`),
      trainers.length ? trainingSwitch(!!snap.data.policy?.allow_training, trainers) : null,
      ...free.map(row),
    ].filter(Boolean);
    if (images) rows.push(h('h3', { class: 'connect-h' }, 'AI pictures (free)'), picturesRow());
    if (paid.length) rows.push(h('h3', { class: 'connect-h' }, 'Premium (Pro, billed by the provider)'), ...paid.map(row));
    swapIfChanged(list, rows);
  }

  // The privacy gate, offered where people set things up: off unless they choose it.
  function trainingSwitch(on, names) {
    const box = h('input', { type: 'checkbox', checked: on, 'aria-describedby': 'connect-train-note' });
    box.addEventListener('change', async () => {
      box.disabled = true;
      try {
        await api.setTraining(box.checked);
        svc.toast(box.checked ? 'providers that may train are on' : 'providers that may train are off');
        await svc.store.refresh('status');
      } catch (err) { box.checked = !box.checked; svc.toast(err.message || String(err), true); } finally { box.disabled = false; }
    });
    return h('label', { class: 'connect-train' }, box,
      h('span', {}, h('strong', {}, 'Also use free providers that may train on your prompts'),
        h('span', { class: 'quiet', id: 'connect-train-note' }, ` (${names.join(', ')}). Off by default. Turn it on for more free capacity, including ones that need no key.`)));
  }

  function row(p) {
    const ready = (p.models || []).filter((m) => m.allowed).length;
    const status = p.keyless ? chip('no key needed', 'ok') : p.key_set ? chip('connected', 'ok') : chip('not connected', '');
    const actions = [];
    if (p.keyless) {
      const blocked = (p.models || []).every((m) => m.blocked_reason === 'may train on prompts');
      return h('article', { class: 'connect-row is-set', 'data-platform': p.platform },
        h('div', { class: 'connect-top' }, h('strong', {}, p.name), status,
          h('span', { class: 'quiet' }, blocked ? 'off: turn on the switch above to use it' : `${ready} model${ready === 1 ? '' : 's'} ready`)),
        p.free_note ? h('p', { class: 'quiet' }, p.free_note) : null);
    }
    if (p.key_url) actions.push(h('button', { class: 'act primary', type: 'button', onclick: () => window.open(p.key_url, '_blank', 'noopener') }, p.key_set ? 'Manage key' : 'Get free key'));
    if (!open.has(p.platform)) {
      actions.push(h('button', { class: 'act', type: 'button', onclick: () => { open.add(p.platform); paint(); focusInput(p.platform); } }, p.key_set ? 'Replace key' : 'Paste key'));
    }
    if (p.key_set) actions.push(h('button', { class: 'act ghost', type: 'button', 'aria-label': 'Disconnect ' + p.name, onclick: () => remove(p) }, 'Disconnect'));
    const form = open.has(p.platform) ? pasteForm(p) : null;
    return h('article', { class: 'connect-row' + (p.key_set ? ' is-set' : ''), 'data-platform': p.platform },
      h('div', { class: 'connect-top' }, h('strong', {}, p.name), status,
        p.key_set ? h('span', { class: 'quiet' }, `${ready} model${ready === 1 ? '' : 's'} ready`) : null),
      p.free_note ? h('p', { class: 'quiet' }, p.free_note) : null,
      h('div', { class: 'acts' }, actions),
      form);
  }

  function pasteForm(p) {
    const input = h('input', {
      class: 'connect-input', type: 'password', autocomplete: 'off', spellcheck: 'false',
      'aria-label': p.name + ' API key', placeholder: 'paste your ' + p.name.replace(' (premium)', '') + ' key',
    });
    const save = h('button', { class: 'act primary', type: 'submit' }, 'Save');
    const cancel = h('button', { class: 'act ghost', type: 'button', onclick: () => { open.delete(p.platform); paint(); } }, 'Cancel');
    const note = h('p', { class: 'quiet connect-note', role: 'status' });
    const form = h('form', { class: 'connect-form', 'data-for': p.platform }, input, save, cancel, note);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const key = input.value.trim();
      if (!key) { note.textContent = 'Paste the key first.'; return; }
      save.disabled = true;
      try {
        await api.keys.add(p.platform, key);
        input.value = '';
        open.delete(p.platform);
        svc.toast(`${p.name} connected`);
        await svc.store.refresh('status');
      } catch (err) {
        note.textContent = err instanceof ApiError ? err.message : String(err);
      } finally {
        save.disabled = false;
      }
    });
    return form;
  }

  function focusInput(platform) {
    requestAnimationFrame(() => list.querySelector(`form[data-for="${platform}"] input`)?.focus());
  }

  async function remove(p) {
    try {
      await api.keys.remove(p.platform);
      svc.toast(`${p.name} disconnected`);
      await svc.store.refresh('status');
    } catch (err) { svc.toast(err.message || String(err), true); }
  }

  const unsub = svc.store.subscribe('status', (s) => { snap = s; paint(); });
  paint();

  return {
    el,
    getState: () => ({}),
    focusPrimary: () => (list.querySelector('.connect-row:not(.is-set) button') || el.closest('.pane'))?.focus(),
    destroy() { unsub(); },
  };
}
