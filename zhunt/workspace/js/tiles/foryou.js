// "For you" window (after Hark Pro's proactive cards): what needs attention right now, each with
// one-tap actions. Derived from /status and /decisions; it never asks for or shows a key value.

import { h, splitModel } from '../dom.js';
import { groupRequests } from './routing.js';
import { swapIfChanged } from './util.js';
import { api } from '../api.js';

const DISMISS_STORE = 'zhunt.foryou.dismissed';
const shortModel = (id) => splitModel(id).name.split('/').pop();

function lsGet(key, fallback) { try { return JSON.parse(localStorage.getItem(key) || 'null') ?? fallback; } catch { return fallback; } }
function lsSet(key, val) { try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* storage blocked: lasts until reload */ } }

// Cards, most useful first. Each: {id, tone, title, detail, code?, actions: [{label, run}]}
export function buildCards(status, decisions, { openWindow, openPlan, plan = null, now = Date.now() }) {
  const cards = [];
  if (status.error && !status.data) {
    cards.push({
      id: 'offline', tone: 'err', title: 'zhunt is not answering',
      detail: 'The local daemon is unreachable. Start it again in a terminal:', code: 'zhunt serve', actions: [],
    });
    return cards;
  }
  const s = status.data;
  if (!s) return cards;
  const providers = s.providers || [];
  for (const p of providers.filter((x) => !x.key_set)) {
    const n = (p.models || []).length;
    if (n && p.models.every((m) => !m.free)) {
      // a premium-only provider: only worth a card for Pro users
      if (plan && plan.plan !== 'free') {
        cards.push({
          id: 'premium-key-' + p.platform, tone: 'accent', title: 'Connect premium models',
          detail: `Pro can step up to ${n} premium models when every free model is busy. Add your paid ${p.name || p.platform} key, then turn it on with a monthly cap:`,
          code: `zhunt keys add ${p.platform} && zhunt premium on --cap 5`, actions: [],
        });
      }
      continue;
    }
    cards.push({
      id: 'key-' + p.platform, tone: 'accent', title: `Add your free ${p.name || p.platform} key`,
      detail: `${n} free model${n === 1 ? '' : 's'} ready to use once zhunt has a key. Run this in a terminal (the key is typed there, never here):`,
      code: `zhunt keys add ${p.platform}`, actions: [],
    });
  }
  // premium models only count while premium is allowed
  const models = providers.flatMap((p) => p.models || []).filter((m) => m.free || s.policy?.allow_paid);
  const cooling = models.filter((m) => m.allowed && m.cooldown_remaining > 0)
    .sort((a, b) => a.cooldown_remaining - b.cooldown_remaining);
  if (cooling.length) {
    const left = Math.max(0, cooling[0].cooldown_remaining - (now - status.at) / 1000);
    cards.push({
      id: 'cooling', tone: 'warn',
      title: cooling.length === 1 ? `${shortModel(cooling[0].id)} is resting` : `${cooling.length} models are resting`,
      // whole minutes, so the card does not change (and repaint) on every poll
      detail: `Free limit reached; the first is back within ${Math.max(1, Math.ceil(left / 60))} min. zhunt is answering with the others meanwhile.`,
      actions: [{ label: 'See models', run: () => openWindow('providers') }],
    });
  }
  const low = models.filter((m) => m.allowed && !(m.cooldown_remaining > 0) && m.headroom !== null && m.headroom !== undefined && m.headroom < 0.2);
  if (low.length) {
    cards.push({
      id: 'low', tone: 'warn',
      title: low.length === 1 ? `${shortModel(low[0].id)} is almost out of free quota` : `${low.length} models are almost out of free quota`,
      detail: 'zhunt will move to the next free model on its own when they run dry.',
      actions: [{ label: 'See models', run: () => openWindow('providers') }],
    });
  }
  const hourAgo = now / 1000 - 3600;
  const recent = groupRequests((decisions.data || []).filter((d) => d.ts > hourAgo));
  const rescued = recent.filter((g) => g.attempts.length > 1 && g.attempts.some((a) => a.success));
  if (rescued.length) {
    cards.push({
      id: 'rescued-' + Math.floor(now / 3600000), tone: 'ok',
      title: `zhunt rescued ${rescued.length} request${rescued.length === 1 ? '' : 's'} this hour`,
      detail: 'A model hit a limit or failed, and the request was retried on another model before you saw an error.',
      actions: [{ label: 'Show why', run: () => openWindow('routing') }],
    });
  }
  const spend = s.spend;
  if (spend && spend.cap > 0 && spend.spent >= spend.cap * 0.8) {
    const reached = spend.spent >= spend.cap;
    cards.push({
      id: 'cap-' + spend.month + (reached ? '-reached' : ''), tone: reached ? 'err' : 'warn',
      title: reached ? 'Premium cap reached for this month' : 'Premium spending is near this month\'s cap',
      detail: `$${spend.spent.toFixed(2)} of your $${spend.cap.toFixed(2)} cap used. ${reached ? 'zhunt is back to free models only until next month.' : 'At the cap zhunt goes back to free models only.'} To change the cap, start zhunt with:`,
      code: 'ZHUNT_MONTHLY_CAP=10', actions: [],
    });
  }
  const unanswered = recent.filter((g) => !g.attempts.some((a) => a.success));
  if (unanswered.length && plan && plan.plan === 'free') {
    cards.push({
      id: 'upsell-' + Math.floor(now / 86400000), tone: 'info',
      title: `${unanswered.length} request${unanswered.length === 1 ? '' : 's'} found no free model this hour`,
      detail: 'Every free model was busy or out of quota. zhunt Pro steps up to a premium model (GPT, Claude, Gemini) in that case, under a monthly cap you set.',
      actions: [{ label: 'See Pro', run: () => openPlan?.() }],
    });
  }
  const privacy = models.filter((m) => m.blocked_reason === 'may train on prompts');
  if (privacy.length && !s.policy?.allow_training) {
    cards.push({
      id: 'privacy', tone: 'info',
      title: `${privacy.length} model${privacy.length === 1 ? ' is' : 's are'} skipped for privacy`,
      detail: 'Their providers may train on prompts. To allow them, start zhunt with this set:',
      code: 'ZHUNT_ALLOW_TRAINING=1', actions: [],
    });
  }
  if (!cards.length) {
    const usable = models.filter((m) => m.allowed && !(m.cooldown_remaining > 0)).length;
    cards.push({
      id: 'all-good', tone: 'ok', title: 'All set',
      detail: `${usable} free model${usable === 1 ? '' : 's'} ready. Nothing needs you right now.`, actions: [],
    });
  }
  return cards;
}

export function foryouTile(svc) {
  const dismissed = new Set(lsGet(DISMISS_STORE, []));
  let status = svc.store.get('status');
  let decisions = svc.store.get('decisions');
  let plan = null;
  api.plan().then((p) => { plan = p; paint(); }, () => {});
  const feed = h('div', { class: 'feed', 'aria-live': 'polite' });
  feed.dataset.keepScroll = '1';
  const el = h('div', { class: 'foryou' }, feed);

  function paint() {
    const cards = buildCards(status, decisions, { openWindow: svc.openWindow, openPlan: svc.openPlan, plan }).filter((c) => !dismissed.has(c.id));
    swapIfChanged(feed, cards.length ? cards.map(cardEl) : [h('p', { class: 'quiet' }, 'Nothing new.')]);
  }

  function cardEl(c) {
    const actions = c.actions.map((a) => h('button', { class: 'act', type: 'button', onclick: a.run }, a.label));
    if (c.code) actions.unshift(h('button', { class: 'act primary', type: 'button', onclick: () => copy(c.code) }, 'Copy'));
    if (c.id !== 'offline') {
      actions.push(h('button', { class: 'act ghost', type: 'button', 'aria-label': 'Dismiss: ' + c.title, onclick: () => dismiss(c.id) }, 'Dismiss'));
    }
    return h('article', { class: 'card tone-' + c.tone },
      h('h3', { class: 'card-title' }, c.title),
      h('p', { class: 'card-detail' }, c.detail),
      c.code ? h('code', { class: 'card-code' }, c.code) : null,
      actions.length ? h('div', { class: 'acts' }, actions) : null);
  }

  function dismiss(id) {
    dismissed.add(id);
    lsSet(DISMISS_STORE, [...dismissed].slice(-50));
    paint();
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); svc.toast('copied: ' + text); } catch { svc.toast('copy failed; select the text instead', true); }
  }

  const unsubS = svc.store.subscribe('status', (snap) => { status = snap; paint(); });
  const unsubD = svc.store.subscribe('decisions', (snap) => { decisions = snap; paint(); });
  paint();

  return {
    el,
    getState: () => ({}),
    focusPrimary: () => (feed.querySelector('button') || el.closest('.pane'))?.focus(),
    destroy() { unsubS(); unsubD(); },
  };
}
