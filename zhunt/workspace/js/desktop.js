// Desktop chrome around the windows: the top bar (clock, readiness, Floating/Tiled switch, new
// window, theme menu) and, in floating mode or on a narrow screen, a dock listing every window.
// The wallpaper is the theme's --wallpaper, or a sky that follows the local time when a theme has none.

import { h } from './dom.js';
import { api } from './api.js';
import { KINDS } from './tree.js';
import { KIND_LABELS } from './tiles/index.js';

export function skyFor(hour) {
  if (hour >= 5 && hour < 8) return 'dawn';
  if (hour >= 8 && hour < 17) return 'day';
  if (hour >= 17 && hour < 20) return 'dusk';
  return 'night';
}

const SHORT = { chat: 'Chat', foryou: 'For you', panels: 'Panels', compare: 'Compare', routing: 'Routing', providers: 'Providers', logs: 'Logs', connect: 'Connect', team: 'Team' };

export function createDesktop({ appEl, topbar, dock, ws, store, themes, overlays, toast, openPalette }) {
  const clock = h('span', { class: 'top-clock' });
  const pulse = h('span', { class: 'top-pulse', role: 'status' });
  const modeBtns = ['floating', 'tiled'].map((m) => h('button', {
    class: 'seg-btn', type: 'button', role: 'radio', 'aria-checked': 'false', dataset: { mode: m },
    title: m === 'floating' ? 'Floating windows you can drag and overlap (Leader g)' : 'Tiled windows that fill the screen (Leader g)',
    onclick: () => { ws.setMode(m); refresh(); },
  }, m === 'floating' ? 'Floating' : 'Tiled'));
  const seg = h('div', { class: 'seg', role: 'radiogroup', 'aria-label': 'Window style' }, modeBtns);
  const btn = (label, title, onclick) => h('button', { class: 'top-btn', type: 'button', title, onclick }, label);
  const planBtn = h('button', { class: 'top-btn plan-btn', type: 'button', onclick: () => planSheet() }, 'Free');
  let plan = { plan: 'free', features: [], feature_labels: {}, upgrade_url: '' };
  topbar.replaceChildren(
    h('span', { class: 'top-mark' }, 'zhunt'), clock, h('span', { class: 'top-spacer' }), pulse, planBtn, seg,
    btn('+ Window', 'Open a new window (Leader n)', () => newWindowMenu()),
    btn('Theme', 'Choose a theme (Leader t)', () => themeMenu()),
    btn('⌘K', 'Command palette (Ctrl+K)', () => openPalette()));

  function tick() {
    const d = new Date();
    appEl.dataset.sky = skyFor(d.getHours());
    clock.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  tick();
  setInterval(tick, 30000);

  store.subscribe('status', (snap) => {
    if (!snap.data) {
      pulse.textContent = snap.error ? 'offline' : '';
      pulse.className = 'top-pulse' + (snap.error ? ' bad' : '');
      return;
    }
    const models = (snap.data.providers || []).flatMap((p) => p.models || []).filter((m) => m.free || snap.data.policy?.allow_paid);
    const usable = models.filter((m) => m.allowed && !(m.cooldown_remaining > 0)).length;
    pulse.textContent = `${usable} free model${usable === 1 ? '' : 's'} ready`;
    pulse.className = 'top-pulse' + (snap.error ? ' bad' : usable ? ' ok' : ' warn');
  });

  async function loadPlan() {
    try { plan = await api.plan(); } catch { return; }
    const pro = plan.plan !== 'free';
    planBtn.textContent = pro ? 'Pro' : 'Upgrade';
    planBtn.classList.toggle('is-pro', pro);
    planBtn.title = pro ? 'Your zhunt Pro plan' : 'See what zhunt Pro adds';
  }
  loadPlan();

  // What Pro adds, and how to get or check it. Purchases happen on the website, never in here.
  async function planSheet() {
    await loadPlan();
    const pro = plan.plan !== 'free';
    const labels = plan.feature_labels || {};
    const until = plan.expires_at ? new Date(plan.expires_at * 1000).toLocaleDateString() : null;
    const items = [
      ...Object.keys(labels).map((f) => ({ title: labels[f], group: plan.features.includes(f) ? 'included' : 'pro', hint: plan.features.includes(f) ? 'on' : '' })),
      pro
        ? { title: `Pro${plan.email ? ' for ' + plan.email : ''}${until ? ', renews by ' + until : ''}${plan.in_grace ? ' (expired: run zhunt licence renew)' : ''}`, group: 'plan' }
        : { title: 'Get zhunt Pro ($7/month, 7-day free trial)', group: 'upgrade', hint: 'opens the website', open: true },
      ...(pro ? [] : [{ title: 'Already bought it? Run: zhunt licence activate', group: 'upgrade' }]),
      ...(plan.problem ? [{ title: 'Stored licence not in effect: ' + plan.problem, group: 'plan' }] : []),
    ];
    const picked = await overlays.palette(items, { placeholder: pro ? 'your plan' : 'zhunt Pro' });
    if (picked?.open && plan.upgrade_url) window.open(plan.upgrade_url, '_blank', 'noopener');
  }

  async function newWindowMenu() {
    const picked = await overlays.palette(KINDS.map((k) => ({ title: KIND_LABELS[k], group: 'window', kind: k })), { placeholder: 'open which window?' });
    // one-of-a-kind windows come forward instead of opening twice; chat and compare can repeat
    if (picked) { if (['chat', 'compare'].includes(picked.kind)) ws.splitAuto(picked.kind); else ws.openKind(picked.kind); }
  }

  // A personal wallpaper (AI-generated) sits over whatever the theme provides, until cleared.
  let customWallpaper = false;
  function applyWallpaper(image) {
    customWallpaper = !!image;
    appEl.style.background = image ? `url("${image}") center / cover no-repeat` : '';
  }
  api.wallpaper.get().then((r) => applyWallpaper(r.image), () => {});

  async function generateWallpaper() {
    const theme = themes.current;
    const description = await overlays.prompt('describe your wallpaper (AI)', {
      initial: `Calm desktop wallpaper in ${theme.replace(/-/g, ' ')} colours: a minimal landscape, soft light, no text, no people`,
      hint: 'Made with your free Cloudflare connection (Connect window > AI pictures)',
    });
    if (!description) return;
    toast('generating your wallpaper...');
    try {
      const { image } = await api.images.generate(description);
      await api.wallpaper.put(image);
      applyWallpaper(image);
      toast('wallpaper set');
    } catch (err) {
      if (err.status === 409) { toast('AI wallpapers need a free Cloudflare token: open Connect models > AI pictures', true); ws.openKind('connect'); }
      else toast(err.message || String(err), true);
    }
  }

  async function themeMenu() {
    await themes.refresh();
    const current = themes.current;
    const items = themes.list.map((t) => ({
      title: t.name, group: t.builtin ? 'theme' : 'your theme', hint: t.name === current ? 'current' : '', name: t.name,
    }));
    items.push({ title: 'Generate a wallpaper with AI...', group: 'wallpaper', action: 'generate' });
    if (customWallpaper) items.push({ title: "Use the theme's wallpaper again", group: 'wallpaper', action: 'clear' });
    const picked = await overlays.palette(items, { placeholder: 'choose a theme or a wallpaper' });
    if (!picked) return;
    if (picked.action === 'generate') return generateWallpaper();
    if (picked.action === 'clear') { await api.wallpaper.remove().catch(() => {}); applyWallpaper(null); toast("theme wallpaper restored"); return; }
    await themes.apply(picked.name);
    toast(`theme ${picked.name}`);
  }

  function refresh() {
    const mode = ws.mode;
    for (const b of modeBtns) b.setAttribute('aria-checked', String(b.dataset.mode === mode));
    // floating: the dock lists and restores windows; on a narrow screen (one window at a time, either
    // mode) it is the only touch-friendly way to switch windows
    const showDock = mode === 'floating' || ws.narrow;
    dock.hidden = !showDock;
    appEl.classList.toggle('has-dock', showDock); // windows stay clear of the dock
    if (!showDock) return;
    dock.replaceChildren(...ws.windows().map((w, i) => h('button', {
      class: 'dock-btn' + (w.min ? ' is-min' : '') + (w.focused && !w.min ? ' is-focused' : ''), type: 'button',
      title: (w.min ? 'Restore ' : 'Show ') + KIND_LABELS[w.kind] + ` (Alt+${i + 1})`,
      onclick: () => { ws.setFocus(w.id, { dom: true }); refresh(); },
    }, SHORT[w.kind] || w.kind)),
    h('button', { class: 'dock-btn dock-add', type: 'button', 'aria-label': 'Open a new window', onclick: () => newWindowMenu() }, '+'));
  }

  return { refresh, newWindowMenu, themeMenu, planSheet };
}
