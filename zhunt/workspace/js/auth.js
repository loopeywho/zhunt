// Full-screen notice shown when the master key is missing or rejected. There is deliberately no
// input field: the key only ever arrives via `zhunt open`, so it is never typed or pasted here.

import { h } from './dom.js';

export function showAuthScreen(reason) {
  document.getElementById('app')?.setAttribute('inert', '');
  document.getElementById('overlay-root')?.replaceChildren();
  const rejected = reason === 'rejected';
  const screen = h('main', { class: 'auth', role: 'alert' },
    h('div', { class: 'auth-card' },
      h('h1', {}, rejected ? 'The daemon rejected this key' : 'This workspace needs its key'),
      h('p', {}, rejected
        ? 'The key stored for this tab no longer matches the running zhunt daemon (it may have been restarted or rotated).'
        : 'No master key was found for this tab. The workspace never asks you to type it.'),
      h('p', {}, 'Open it from a terminal instead:'),
      h('pre', { class: 'auth-cmd' }, h('code', {}, 'zhunt open')),
      h('p', { class: 'dim' }, 'That opens this page with the key attached to the address. The page keeps it for this tab only and removes it from the address bar immediately.'),
      h('button', { class: 'btn', type: 'button', onclick: () => location.reload() }, 'retry')));
  document.body.append(screen);
  screen.querySelector('button').focus();
}
