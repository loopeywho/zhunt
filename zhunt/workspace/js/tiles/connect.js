// Connect window: get a free key from each provider and paste it here. The key goes only to the
// local zhunt daemon (saved owner-only in ~/.zhunt/env) and is never shown again.

import { h } from '../dom.js';
import { api, ApiError } from '../api.js';
import { chip, errorBox, swapIfChanged } from './util.js';

// providers whose keys zhunt can recognise on a page, so the agent can save them (key_prefix.py)
const AUTO_KEYS = new Set(['groq', 'google', 'cerebras']);

export function connectTile(svc) {
  let snap = svc.store.get('status');
  const list = h('div', { class: 'connect-list' });
  list.dataset.keepScroll = '1';
  const intro = h('p', { class: 'connect-intro' },
    'zhunt works with no keys. One free key makes it fast: get it, then paste it below. ',
    'Keys stay on this computer, in a file only you can read.');
  const el = h('div', { class: 'connect' }, intro, list);
  const open = new Set(); // providers whose paste box is showing
  let images = null; // /images/status
  let mail = null; // /email/status
  const loadMail = () => api.email.status().then((s) => { mail = s; paint(); }, () => {});
  loadMail();

  // Email for the agent: provider preset (or own servers), address and an app password.
  function emailRow() {
    const actions = [];
    if (!open.has('email')) actions.push(h('button', { class: 'act primary', type: 'button', onclick: () => { open.add('email'); paint(); focusInput('email'); } }, mail.connected ? 'Change' : 'Connect email'));
    if (mail.connected) actions.push(h('button', { class: 'act ghost', type: 'button', onclick: async () => { await api.email.disconnect(); svc.toast('email disconnected'); loadMail(); } }, 'Disconnect'));
    let form = null;
    if (open.has('email')) {
      const presets = mail.presets || {};
      const provider = h('select', { class: 'connect-input', 'aria-label': 'Email provider' },
        ...Object.entries(presets).map(([k, p]) => h('option', { value: k }, p.name)), h('option', { value: 'other' }, 'Other (my own servers)'));
      const address = h('input', { class: 'connect-input', type: 'email', autocomplete: 'off', 'aria-label': 'Email address', placeholder: 'you@example.com' });
      const password = h('input', { class: 'connect-input', type: 'password', autocomplete: 'off', 'aria-label': 'App password', placeholder: 'app password (not your normal password)' });
      const imap = h('input', { class: 'connect-input', 'aria-label': 'IMAP server', placeholder: 'imap.example.com:993', hidden: true });
      const smtp = h('input', { class: 'connect-input', 'aria-label': 'SMTP server', placeholder: 'smtp.example.com:465', hidden: true });
      const help = h('p', { class: 'quiet connect-note-help' });
      const note = h('p', { class: 'quiet connect-note', role: 'status' });
      const paintHelp = () => {
        const p = presets[provider.value];
        imap.hidden = smtp.hidden = !!p;
        help.replaceChildren(...(p ? [p.note + ' ', h('a', { href: p.help, target: '_blank', rel: 'noopener' }, 'Make an app password')] : ['Enter your provider\'s IMAP and SMTP servers (host:port).']));
      };
      provider.addEventListener('change', paintHelp);
      paintHelp();
      const save = h('button', { class: 'act primary', type: 'submit' }, 'Connect');
      form = h('form', { class: 'connect-form', 'data-for': 'email' }, provider, address, password, imap, smtp, help, save,
        h('button', { class: 'act ghost', type: 'button', onclick: () => { open.delete('email'); paint(); } }, 'Cancel'), note);
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        save.disabled = true;
        note.textContent = 'Checking the login...';
        try {
          await api.email.connect({ provider: provider.value, address: address.value, password: password.value, imap: imap.value, smtp: smtp.value });
          password.value = '';
          open.delete('email');
          svc.toast('email connected');
          loadMail();
        } catch (err) { note.textContent = err.message || String(err); } finally { save.disabled = false; }
      });
    }
    return h('article', { class: 'connect-row' + (mail.connected ? ' is-set' : ''), 'data-platform': 'email' },
      h('div', { class: 'connect-top' }, h('strong', {}, 'Email for the agent'), mail.connected ? chip('connected', 'ok') : chip('not connected', ''),
        mail.connected ? h('span', { class: 'quiet' }, mail.address) : null),
      h('p', { class: 'quiet' }, 'Lets the agent sort your inbox and draft replies. It never marks mail read, and saving drafts or sending always asks you first.'),
      h('div', { class: 'acts' }, actions), form);
  }
  const loadImages = () => api.images.status().then((s) => { images = s; paint(); }, () => {});
  loadImages();
  let catalog = null; // /catalog
  const loadCatalog = () => api.catalog.status().then((c) => { catalog = c; paint(); }, () => {});
  loadCatalog();

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
    const keyed = free.filter((p) => !p.keyless);
    // one recommended key first (Groq: fast, no card); everything else is more free capacity
    const first = keyed.find((p) => p.platform === 'groq') || keyed[0];
    const more = free.filter((p) => p !== first);
    const connected = more.filter((p) => p.key_set && !p.keyless).length;
    const trainers = free.filter((p) => (p.models || []).some((m) => m.may_train_on_prompts)).map((p) => p.name.replace(/ \(.*\)$/, ''));
    const rows = [
      keyed.some((p) => p.key_set) ? null : noKeysNote(free, first),
      clipCard,
      getKeysRow(keyed),
      anyBox(keyed),
      first ? h('h3', { class: 'connect-h' }, 'Recommended') : null,
      first ? row(first, true) : null,
      h('h3', { class: 'connect-h' }, `More free capacity · ${connected} of ${more.filter((p) => !p.keyless).length} connected`),
      trainers.length ? trainingSwitch(!!snap.data.policy?.allow_training, trainers) : null,
      ...more.map((p) => row(p)),
    ].filter(Boolean);
    if (mail) rows.push(h('h3', { class: 'connect-h' }, 'Email (agent)'), emailRow());
    if (images) rows.push(h('h3', { class: 'connect-h' }, 'AI pictures (free)'), picturesRow());
    if (paid.length) rows.push(h('h3', { class: 'connect-h' }, 'Premium (Pro, billed by the provider)'), ...paid.map((p) => row(p)));
    if (catalog?.updates) rows.push(h('h3', { class: 'connect-h' }, 'Free-model list'), catalogRow());
    swapIfChanged(list, rows);
  }

  // The agent fetches keys in its own browser: the user signs in, it creates each key and saves it
  // straight from the page (the key never goes through a model).
  let getKeysEl = null, getKeysFor = '';
  function getKeysRow(keyed) {
    const missing = keyed.filter((p) => !p.key_set && AUTO_KEYS.has(p.platform));
    const sig = missing.map((p) => p.platform).join();
    if (!missing.length) return null;
    if (getKeysEl && sig === getKeysFor) return getKeysEl;
    getKeysFor = sig;
    const btn = h('button', { class: 'act primary', type: 'button' }, 'Get keys for me');
    const note = h('p', { class: 'quiet connect-note', role: 'status' });
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await api.agent.getKeys();
        note.textContent = 'Started in the Agent window. Sign in where its browser asks; it creates the keys and asks before each click.';
        svc.openWindow?.('agent');
        window.dispatchEvent(new Event('zhunt:agent-task'));
      } catch (err) { note.textContent = err.message || String(err); } finally { btn.disabled = false; }
    });
    getKeysEl = h('article', { class: 'connect-row', 'data-platform': 'auto' },
      h('div', { class: 'connect-top' }, h('strong', {}, 'Let the agent get them')),
      h('p', { class: 'quiet' }, `For ${missing.map((p) => p.name.replace(/ \(.*\)$/, '')).join(', ')}: it opens each key page in its own browser window, you sign in, and it creates and saves the key. Or type /keys in Agent.`),
      h('div', { class: 'acts' }, btn), note);
    return getKeysEl;
  }

  // A key copied to the clipboard (say, from a provider's page) is offered for saving when this
  // window gets focus. Only the prefix is checked; nothing is saved without a click. This only
  // runs once the browser already allows clipboard reads: asking on focus would pop up a
  // permission prompt nobody asked for (the paste box's "From clipboard" asks on a click).
  let clipCard = null, clipSeen = '';
  async function clipboardAllowed() {
    try { return (await navigator.permissions.query({ name: 'clipboard-read' })).state === 'granted'; } catch { return false; }
  }
  async function checkClipboard() {
    if (!navigator.clipboard?.readText || !document.hasFocus() || !(await clipboardAllowed())) return;
    let text = '';
    try { text = (await navigator.clipboard.readText()).trim(); } catch { return; } // permission refused
    if (!text || text === clipSeen || text.length > 300 || /\s/.test(text)) return;
    clipSeen = text;
    let found;
    try { found = await api.keys.detect(text); } catch { return; }
    if (!found.platform || found.key_set) return;
    const save = h('button', { class: 'act primary', type: 'button' }, 'Save it');
    const ignore = h('button', { class: 'act ghost', type: 'button', onclick: () => { clipCard = null; paint(); } }, 'Ignore');
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const r = await api.keys.add(found.platform, text);
        clipCard = null;
        svc.toast(`${r.name || r.platform} connected`);
        await svc.store.refresh('status');
      } catch (err) { svc.toast(err.message || String(err), true); save.disabled = false; }
    });
    clipCard = h('article', { class: 'connect-row tone-accent', 'data-platform': 'clipboard' },
      h('div', { class: 'connect-top' }, h('strong', {}, `A ${found.name} key is on your clipboard`)),
      h('p', { class: 'quiet' }, `Ending ...${text.slice(-4)}. Save it so zhunt can use ${found.name}?`),
      h('div', { class: 'acts' }, save, ignore));
    paint();
  }
  const onFocus = () => { checkClipboard(); };
  window.addEventListener('focus', onFocus);
  el.addEventListener('pointerenter', onFocus);

  // Plain words for a fresh install: zhunt already answers through the keyless providers.
  function noKeysNote(free, first) {
    const keyless = free.some((p) => p.keyless && (p.models || []).some((m) => m.allowed));
    const add = first ? ` Add a free ${first.name.replace(/ \(.*\)$/, '')} key for speed.` : '';
    return h('p', { class: 'connect-status', role: 'status' },
      keyless ? 'Working with no keys (slow, shared limits).' + add : 'No free model is ready yet.' + add);
  }

  // One box for any key: the daemon tells the provider from the prefix and asks only when it cannot.
  // Built once (and again only if the provider list changes) so a status poll never wipes a paste.
  let anyForm = null, anyFor = '';
  function anyBox(keyed) {
    const sig = keyed.map((p) => p.platform).join();
    if (!anyForm || sig !== anyFor) { anyForm = pasteAny(keyed); anyFor = sig; }
    return anyForm;
  }

  function pasteAny(keyed) {
    let anyChoice = false; // the prefix was unknown: show the provider picker
    const input = h('input', {
      class: 'connect-input', type: 'password', autocomplete: 'off', spellcheck: 'false',
      'aria-label': 'Paste any API key', placeholder: 'paste any API key',
    });
    const pick = h('select', { class: 'connect-input', 'aria-label': 'Which provider is this key for?', hidden: true },
      h('option', { value: '' }, 'Which provider is this key for?'),
      ...providersForPick().map((p) => h('option', { value: p.platform }, p.name)));
    const save = h('button', { class: 'act primary', type: 'submit' }, 'Save');
    const note = h('p', { class: 'quiet connect-note', role: 'status' });
    const fromClip = navigator.clipboard?.readText ? h('button', { class: 'act ghost', type: 'button', onclick: async () => {
      try { input.value = (await navigator.clipboard.readText()).trim(); input.dispatchEvent(new Event('input')); }
      catch { note.textContent = 'The browser did not allow reading the clipboard. Paste the key instead.'; }
    } }, 'From clipboard') : null;
    const form = h('form', { class: 'connect-form connect-any', 'data-for': 'any' },
      h('label', { class: 'connect-any-label' }, h('strong', {}, 'Paste any key'),
        h('span', { class: 'quiet' }, ' zhunt tells which provider it is for.')),
      input, pick, save, fromClip, note);
    // a different key gets detected afresh
    input.addEventListener('input', () => { anyChoice = false; pick.hidden = true; pick.value = ''; note.textContent = ''; });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const key = input.value.trim();
      if (!key) { note.textContent = 'Paste the key first.'; return; }
      if (anyChoice && !pick.value) { note.textContent = 'Choose the provider first.'; return; }
      save.disabled = true;
      try {
        const r = await (anyChoice ? api.keys.add(pick.value, key) : api.keys.addAny(key));
        input.value = '';
        anyChoice = false;
        pick.hidden = true;
        pick.value = '';
        note.textContent = '';
        svc.toast(`${r.name || r.platform} connected`);
        await svc.store.refresh('status');
      } catch (err) {
        if (err instanceof ApiError && err.status === 422) {
          anyChoice = true;
          pick.hidden = false;
          note.textContent = 'zhunt cannot tell which provider this key is for. Choose it, then Save.';
          pick.focus();
        } else {
          note.textContent = err instanceof ApiError ? err.message : String(err);
        }
      } finally {
        save.disabled = false;
      }
    });
    return form;

    function providersForPick() {
      const premiumOnly = (snap.data?.providers || []).filter((p) => (p.models || []).length && p.models.every((m) => !m.free));
      return [...keyed, ...premiumOnly];
    }
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

  // The catalog feed. Nothing is fetched unless the user flips the switch (Pro) or presses Check now.
  function catalogRow() {
    const a = catalog.active, u = catalog.updates;
    const day = (ts) => (ts ? new Date(ts * 1000).toISOString().slice(0, 10) : '');
    const where = a.source === 'feed' ? `signed ${a.channel} list of ${day(a.issued_at)}` : 'shipped with zhunt';
    const summary = h('p', { class: 'quiet' }, `Version ${a.version}, ${where}: ${a.models} models from ${a.providers} providers.`,
      a.note ? ` ${a.note}.` : '', catalog.last_message ? ` Last check: ${catalog.last_message}.` : '');
    const check = h('button', { class: 'act', type: 'button' }, 'Check now');
    check.addEventListener('click', async () => {
      check.disabled = true;
      try {
        const r = await api.catalog.update();
        catalog = r;
        svc.toast(r.message, r.status === 'error');
        await svc.store.refresh('status');
      } catch (err) { svc.toast(err.message || String(err), true); } finally { check.disabled = false; paint(); }
    });
    const host = u.url.replace(/^https?:\/\//, '').split('/')[0];
    let control;
    if (u.available) {
      const box = h('input', { type: 'checkbox', checked: u.switched_on, 'aria-describedby': 'connect-catalog-note' });
      box.addEventListener('change', async () => {
        box.disabled = true;
        try {
          const r = await api.catalog.setUpdates(box.checked);
          catalog = r;
          svc.toast(box.checked ? (r.message || 'catalog updates on') : 'catalog updates off', r.status === 'error');
          await svc.store.refresh('status');
        } catch (err) { box.checked = !box.checked; svc.toast(err.message || String(err), true); } finally { box.disabled = false; paint(); }
      });
      control = h('label', { class: 'connect-train' }, box,
        h('span', {}, h('strong', {}, 'Keep the free-model list up to date (Pro)'),
          h('span', { class: 'quiet', id: 'connect-catalog-note' },
            ` Every ${u.every_hours} hours zhunt downloads one public, signed file from ${host}. Nothing about you is sent. Off until you turn it on.`)));
    } else {
      control = h('p', { class: 'quiet', id: 'connect-catalog-note' },
        `zhunt Pro keeps this list current automatically. "Check now" downloads this month's public, signed list from ${host} once; nothing about you is sent.`);
    }
    return h('article', { class: 'connect-row is-set', 'data-platform': 'catalog' }, control, summary, h('div', { class: 'acts' }, [check]));
  }

  function row(p, recommended = false) {
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
    const getLabel = p.key_set ? 'Manage key' : recommended ? `Get a free ${p.name} key (about 1 minute)` : 'Get free key';
    if (p.key_url) actions.push(h('button', { class: 'act primary', type: 'button', onclick: () => window.open(p.key_url, '_blank', 'noopener') }, getLabel));
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
    destroy() { unsub(); window.removeEventListener('focus', onFocus); },
  };
}
