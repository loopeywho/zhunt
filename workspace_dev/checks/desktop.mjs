// Run: node workspace_dev/checks/desktop.mjs
// The For you cards and the time-of-day sky are pure functions of /status, /decisions and the clock.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const dir = path.join(here, '../../zhunt/workspace/js');
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.location = { hash: '' };
const { buildCards } = await import(pathToFileURL(path.join(dir, 'tiles/foryou.js')).href);
const { skyFor } = await import(pathToFileURL(path.join(dir, 'desktop.js')).href);

let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };
const now = 1_760_000_000_000;
const model = (id, over = {}) => ({ id, free: true, allowed: true, cooldown_remaining: 0, headroom: 0.9, blocked_reason: null, ...over });
const status = (providers, policy = {}) => ({ data: { policy, providers }, error: null, at: now });
const ids = (cards) => cards.map((c) => c.id.replace(/-\d+$/, ''));
const opts = { openWindow() {}, now };

{
  const cards = buildCards({ data: null, error: new Error('down'), at: 0 }, { data: [] }, opts);
  check('offline daemon shows only the restart card', ids(cards).join() === 'offline' && cards[0].code === 'zhunt serve');
}
{
  const cards = buildCards(status([{ platform: 'groq', name: 'Groq', key_set: true, models: [model('groq/a')] }]), { data: [] }, opts);
  check('nothing to do shows "All set"', ids(cards).join() === 'all-good');
}
{
  const s = status([
    { platform: 'groq', name: 'Groq', key_set: false, models: [model('groq/a', { allowed: false, blocked_reason: 'no key' })] },
    { platform: 'google', name: 'Google', key_set: true, models: [
      model('google/b', { cooldown_remaining: 125 }),
      model('google/c', { headroom: 0.1 }),
      model('google/d', { allowed: false, blocked_reason: 'may train on prompts' }),
    ] },
  ]);
  const ds = { data: [
    { ts: now / 1000 - 10, session: 's', attempt: 2, success: true, model: 'google/c' },
    { ts: now / 1000 - 11, session: 's', attempt: 1, success: false, model: 'google/b' },
  ] };
  const cards = buildCards(s, ds, opts);
  check('cards come in priority order', ids(cards).join() === 'key-groq,cooling,low,rescued,privacy', ids(cards).join());
  check('missing key card gives the terminal command, never asks for the key', cards[0].code === 'zhunt keys add groq');
  check('cooldown is rounded up to whole minutes (stable between polls)', /within 3 min/.test(cards[1].detail), cards[1].detail);
}
{
  const s = status([{ platform: 'google', name: 'Google', key_set: true, models: [model('google/d', { allowed: false, blocked_reason: 'may train on prompts' })] }], { allow_training: true });
  check('privacy card hidden once training is allowed', !ids(buildCards(s, { data: [] }, opts)).includes('privacy'));
}
{
  const old = { data: [
    { ts: now / 1000 - 7200, session: 's', attempt: 2, success: true, model: 'x' },
    { ts: now / 1000 - 7201, session: 's', attempt: 1, success: false, model: 'y' },
  ] };
  const s = status([{ platform: 'groq', name: 'Groq', key_set: true, models: [model('groq/a')] }]);
  check('failovers older than an hour are not reported', !ids(buildCards(s, old, opts)).includes('rescued'));
}
{
  const T = await import(pathToFileURL(path.join(dir, 'tree.js')).href);
  const { parseDoc } = await import(pathToFileURL(path.join(dir, 'layouts.js')).href);
  const raw = { type: 'split', dir: 'row', ratio: 0.5,
    a: { type: 'tile', kind: 'chat', state: {}, win: { x: 0.9, y: -1, w: 0.5, h: 0.02, min: true } },
    b: { type: 'tile', kind: 'foryou', state: {}, win: { x: 'evil' } } };
  const tree = T.sanitize(raw);
  const w = tree.a.win;
  check('a saved window is pulled back inside the desktop', w.x === 0.5 && w.y === 0 && w.h === 0.1 && w.min === true, JSON.stringify(w));
  check('a malformed window rectangle is dropped', tree.b.win === undefined);
  check('window rectangles survive a save', JSON.stringify(T.toJSON(tree).a.win) === JSON.stringify({ x: 0.5, y: 0, w: 0.5, h: 0.1, min: true }));
  const doc = { version: 1, kind: 'zhunt-layout', tree: raw };
  check('layouts saved before floating windows open tiled', parseDoc(doc).mode === 'tiled');
  check('a floating layout reopens floating', parseDoc({ ...doc, mode: 'floating' }).mode === 'floating');
}
{
  const s = status([{ platform: 'groq', name: 'Groq', key_set: true, models: [model('groq/a')] }]);
  s.data.spend = { month: '2026-10', spent: 4.2, cap: 5 };
  const near = buildCards(s, { data: [] }, opts);
  check('premium spend near the cap warns', near[0].id === 'cap-2026-10' && /\$4\.20 of your \$5\.00/.test(near[0].detail), near[0].detail);
  s.data.spend.spent = 5;
  check('premium cap reached is an error card', buildCards(s, { data: [] }, opts)[0].tone === 'err');
  const failed = { data: [{ ts: now / 1000 - 5, session: 'x', attempt: 1, success: false, model: 'groq/a' }] };
  const s2 = status([{ platform: 'groq', name: 'Groq', key_set: true, models: [model('groq/a')] }]);
  check('free users see Pro after requests found no model', ids(buildCards(s2, failed, { ...opts, plan: { plan: 'free' } })).includes('upsell'));
  check('Pro users are not upsold', !ids(buildCards(s2, failed, { ...opts, plan: { plan: 'pro' } })).includes('upsell'));
}
{
  const premium = { platform: 'anthropic', name: 'Anthropic (premium)', key_set: false, models: [model('anthropic/x', { free: false, allowed: false, blocked_reason: 'pro only' })] };
  const premium2 = { platform: 'openai', name: 'OpenAI (premium)', key_set: false, models: [model('openai/y', { free: false, allowed: false, blocked_reason: 'pro only' })] };
  const free = { platform: 'groq', name: 'Groq', key_set: true, models: [model('groq/a')] };
  const freeUser = ids(buildCards(status([free, premium, premium2]), { data: [] }, { ...opts, plan: { plan: 'free' } }));
  const proUser = buildCards(status([free, premium, premium2]), { data: [] }, { ...opts, plan: { plan: 'pro' } });
  const proWithKey = ids(buildCards(status([free, { ...premium, key_set: true }, premium2]), { data: [] }, { ...opts, plan: { plan: 'pro' } }));
  check('Free users get no card for a premium-only provider', !freeUser.some((i) => i.startsWith('key-') || i.startsWith('premium-key')), freeUser.join());
  check('Pro users get one card naming every premium provider', proUser[0].id === 'premium-keys' && /Anthropic, OpenAI/.test(proUser[0].detail) && /zhunt premium on/.test(proUser[0].code) && proUser.filter((c) => c.id.startsWith('premium')).length === 1, proUser[0].detail);
  check('the card goes once any premium key is added', !proWithKey.includes('premium-keys'));
}
check('sky follows the hour', [4, 6, 12, 18, 22].map(skyFor).join() === 'night,dawn,day,dusk,night');

if (fail) { console.log(fail + ' failed'); process.exit(1); }
console.log('PASS all desktop checks');
