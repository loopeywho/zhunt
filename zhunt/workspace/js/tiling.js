// The window manager: owns the split tree, the pane elements and the dividers.
//
// Panes are absolutely positioned and never re-parented, so splitting, closing, swapping, zooming
// or switching between the two modes keeps every window's scroll position, input text and streams
// alive. Two modes over the same windows:
//   tiled    - positions come from the split tree (Omarchy / Hyprland style)
//   floating - each window keeps its own rectangle (node.win, fractions of the desktop), can be
//              dragged by its header, resized from its corner, raised and minimised (Hark style)

import { h, clamp } from './dom.js';
import * as T from './tree.js';

const NARROW_PX = 640;   // below this only the focused tile is shown (like a zoomed tile)
const MIN_TILE_PX = 110; // dragging a divider cannot squeeze a tile below this
const LABELS = { foryou: 'for you', connect: 'connect models' }; // window titles that differ from the kind id
const MIN_WIN_W = 240;   // floating windows cannot be resized smaller than this
const MIN_WIN_H = 150;

// Where a new floating window of each kind opens (fractions of the desktop): chat in the middle,
// the Hark-style feed and panels on the left, detail views on the right.
const SLOTS = {
  chat: { x: 0.27, y: 0.02, w: 0.46, h: 0.96 },
  foryou: { x: 0.01, y: 0.02, w: 0.25, h: 0.57 },
  panels: { x: 0.01, y: 0.61, w: 0.25, h: 0.37 },
  routing: { x: 0.74, y: 0.02, w: 0.25, h: 0.47 },
  providers: { x: 0.74, y: 0.51, w: 0.25, h: 0.47 },
  compare: { x: 0.18, y: 0.06, w: 0.64, h: 0.82 },
  logs: { x: 0.14, y: 0.48, w: 0.72, h: 0.48 },
  connect: { x: 0.3, y: 0.06, w: 0.4, h: 0.86 },
};

export function createWorkspace(stage, { makeTile, onChange, onFocus, toast }) {
  let root = null;
  let focusedId = null;
  let zoomId = null;
  const entries = new Map();   // id -> {kind, inst, pane, idxEl, kindEl, node}
  const dividerEls = new Map(); // split node -> element
  let animTimer = null;
  let gap = 6;
  let mode = 'tiled';
  let zOrder = []; // floating stacking order, bottom to top

  const ws = {
    get root() { return root; },
    get focusedId() { return focusedId; },
    get zoomId() { return zoomId; },
    get tileCount() { return entries.size; },
    get narrow() { return stage.clientWidth < NARROW_PX; },
    get mode() { return mode; },
  };
  const floating = () => mode === 'floating' && !ws.narrow;

  // ---- structure ---------------------------------------------------------------------------

  function buildPane(node, inst) {
    const idxEl = h('span', { class: 'pane-idx', 'aria-hidden': 'true' });
    const kindEl = h('span', { class: 'pane-kind' }, LABELS[node.kind] || node.kind);
    const btnZoom = h('button', {
      class: 'pane-btn', type: 'button', 'aria-label': 'Zoom tile', title: 'Zoom tile (leader z)',
      onclick: () => { setFocus(node.id); ws.toggleZoom(); },
    }, 'z');
    const btnClose = h('button', {
      class: 'pane-btn', type: 'button', 'aria-label': 'Close tile', title: 'Close tile (leader x)',
      onclick: () => { setFocus(node.id); ws.closeFocused(); },
    }, 'x');
    const btnMin = h('button', {
      class: 'pane-btn pane-min', type: 'button', 'aria-label': 'Minimise window', title: 'Minimise to the dock (leader m)',
      onclick: () => ws.minimize(node.id),
    }, '_');
    const extra = h('div', { class: 'pane-extra' }, inst.header || null);
    const head = h('header', { class: 'pane-head', role: 'toolbar', 'aria-label': 'Tile controls' }, idxEl, kindEl, extra, btnMin, btnZoom, btnClose);
    const body = h('div', { class: 'pane-body' }, inst.el);
    const grip = h('div', { class: 'pane-grip', 'aria-hidden': 'true', title: 'Resize' });
    const pane = h('section', { class: 'pane no-anim', role: 'region', tabindex: '0', dataset: { id: node.id, kind: node.kind } }, head, body, grip);
    pane.addEventListener('focusin', () => setFocus(node.id));
    pane.addEventListener('pointerdown', () => { if (focusedId !== node.id) setFocus(node.id); else if (floating()) raise(node.id); }, true);
    // floating: drag by the header (not its controls), resize from the corner, double-click maximises
    dragOn(head, node.id, 'move');
    dragOn(grip, node.id, 'size');
    head.addEventListener('dblclick', (e) => {
      if (!floating() || e.target.closest('button, select, input, textarea, a')) return;
      setFocus(node.id);
      ws.toggleZoom();
    });
    stage.append(pane);
    return { idxEl, kindEl, pane, btnClose };
  }

  function dragOn(handle, id, kind) {
    let start = null;
    handle.addEventListener('pointerdown', (e) => {
      if (!floating() || zoomId || e.button !== 0) return;
      if (kind === 'move' && e.target.closest('button, select, input, textarea, a')) return;
      const e0 = entries.get(id);
      if (!e0?.node.win) return;
      e.preventDefault();
      try { handle.setPointerCapture(e.pointerId); } catch { /* synthetic pointer */ }
      start = { x: e.clientX, y: e.clientY, win: { ...e0.node.win }, W: stage.clientWidth, H: stage.clientHeight };
      stage.classList.add('dragging');
    });
    handle.addEventListener('pointermove', (e) => {
      if (!start) return;
      const e0 = entries.get(id);
      if (!e0) return;
      const { W, H, win } = start;
      const dx = (e.clientX - start.x) / W;
      const dy = (e.clientY - start.y) / H;
      const w = e0.node.win;
      if (kind === 'move') {
        w.x = clamp(win.x + dx, 0, 1 - win.w);
        w.y = clamp(win.y + dy, 0, 1 - win.h);
      } else {
        w.w = clamp(win.w + dx, Math.min(1, MIN_WIN_W / W), 1 - win.x);
        w.h = clamp(win.h + dy, Math.min(1, MIN_WIN_H / H), 1 - win.y);
      }
      place(e0, rectOf(w, W, H));
    });
    const end = () => {
      if (!start) return;
      start = null;
      stage.classList.remove('dragging');
      onChange?.();
    };
    handle.addEventListener('pointerup', end);
    handle.addEventListener('pointercancel', end);
  }

  const rectOf = (w, W, H) => ({ x: w.x * W, y: w.y * H, w: w.w * W, h: w.h * H });

  function place(e, rr) {
    const s = e.pane.style;
    s.left = Math.round(rr.x) + 'px';
    s.top = Math.round(rr.y) + 'px';
    s.width = Math.max(0, Math.round(rr.w)) + 'px';
    s.height = Math.max(0, Math.round(rr.h)) + 'px';
  }

  // Give every window without a floating rectangle one: its kind's slot, nudged off any window
  // already sitting there.
  function ensureWins() {
    const taken = [];
    for (const node of T.leaves(root)) if (node.win) taken.push(node.win);
    for (const node of T.leaves(root)) {
      if (node.win) continue;
      const slot = SLOTS[node.kind] || SLOTS.compare;
      const w = { ...slot, min: false };
      let n = 0;
      while (n < 10 && taken.some((t) => Math.abs(t.x - w.x) < 0.01 && Math.abs(t.y - w.y) < 0.01)) {
        w.x = clamp(w.x + 0.03, 0, 1 - w.w);
        w.y = clamp(w.y + 0.04, 0, 1 - w.h);
        n += 1;
      }
      node.win = w;
      taken.push(w);
    }
  }

  function raise(id) {
    zOrder = [...zOrder.filter((x) => x !== id && entries.has(x)), id];
    for (const [i, x] of zOrder.entries()) {
      const e = entries.get(x);
      if (e && !e.pane.classList.contains('is-zoomed')) e.pane.style.zIndex = String(10 + i);
    }
  }

  function sync() {
    const live = T.leaves(root);
    const ids = new Set(live.map((n) => n.id));
    for (const [id, e] of [...entries]) {
      if (!ids.has(id) || e.kind !== e.node.kind) {
        try { e.inst.destroy?.(); } catch (err) { console.error(err); }
        e.pane.remove();
        entries.delete(id);
      }
    }
    for (const node of live) {
      if (entries.has(node.id)) { entries.get(node.id).node = node; continue; }
      const inst = makeTile(node.kind, node.state || {}, { id: node.id, changed: () => onChange?.() });
      const parts = buildPane(node, inst);
      entries.set(node.id, { kind: node.kind, node, inst, ...parts });
    }
  }

  // ---- geometry ----------------------------------------------------------------------------

  function readGap() {
    const g = parseFloat(getComputedStyle(stage).getPropertyValue('--gap'));
    gap = isFinite(g) ? g : 6;
  }

  function layout(animate = false) {
    if (!root) return;
    readGap();
    const W = stage.clientWidth;
    const H = stage.clientHeight;
    if (animate && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
      stage.classList.add('anim');
      clearTimeout(animTimer);
      animTimer = setTimeout(() => stage.classList.remove('anim'), 220);
    }
    const float = floating();
    if (float) ensureWins();
    const { rects, dividers: allDividers } = T.computeLayout(root, W, H, gap);
    const dividers = float ? [] : allDividers;
    const leaves = T.leaves(root);
    const full = zoomId && rects.has(zoomId) ? zoomId : (ws.narrow ? focusedId : null);
    stage.dataset.mode = float ? 'floating' : 'tiled';
    leaves.forEach((node, i) => {
      const e = entries.get(node.id);
      if (!e) return;
      const r = float ? rectOf(node.win, W, H) : rects.get(node.id);
      const isFull = full === node.id;
      place(e, isFull ? { x: 0, y: 0, w: W, h: H } : r);
      const zi = zOrder.indexOf(node.id);
      e.pane.style.zIndex = isFull ? '900' : float ? String(10 + Math.max(0, zi)) : '1';
      e.pane.classList.toggle('hidden-by-zoom', !!full && !isFull);
      e.pane.classList.toggle('is-min', float && !isFull && !!node.win?.min);
      e.pane.classList.toggle('is-focused', node.id === focusedId);
      e.pane.classList.toggle('is-zoomed', isFull);
      e.idxEl.textContent = String(i + 1);
      e.kindEl.textContent = LABELS[node.kind] || node.kind;
      e.pane.dataset.kind = node.kind;
      e.pane.setAttribute('aria-label', `Tile ${i + 1} of ${leaves.length}: ${node.kind}${isFull ? ' (zoomed)' : ''}`);
      e.btnClose.disabled = leaves.length === 1;
      if (e.pane.classList.contains('no-anim')) requestAnimationFrame(() => e.pane.classList.remove('no-anim'));
    });
    const seen = new Set();
    for (const d of dividers) {
      let el = dividerEls.get(d.node);
      if (!el) { el = makeDivider(); dividerEls.set(d.node, el); stage.append(el); }
      seen.add(d.node);
      el._info = d;
      const s = el.style;
      s.left = Math.round(d.rect.x) + 'px';
      s.top = Math.round(d.rect.y) + 'px';
      s.width = Math.round(d.rect.w) + 'px';
      s.height = Math.round(d.rect.h) + 'px';
      el.hidden = !!full;
      el.dataset.dir = d.dir;
      el.setAttribute('aria-orientation', d.dir === 'row' ? 'vertical' : 'horizontal');
      el.setAttribute('aria-valuenow', String(Math.round(d.node.ratio * 100)));
    }
    for (const [node, el] of [...dividerEls]) {
      if (!seen.has(node)) { el.remove(); dividerEls.delete(node); }
    }
    stage.dataset.zoomed = full ? 'true' : 'false';
    onFocus?.(ws);
  }

  function makeDivider() {
    const el = h('div', {
      class: 'divider', role: 'separator', tabindex: '0', 'aria-label': 'Resize tiles (arrow keys)',
      'aria-valuemin': '8', 'aria-valuemax': '92',
    });
    let dragging = false;
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      dragging = true;
      try { el.setPointerCapture(e.pointerId); } catch { /* synthetic pointer: drag still works while over the divider */ }
      stage.classList.add('resizing');
      el.classList.add('active');
    });
    el.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      const d = el._info;
      const box = stage.getBoundingClientRect();
      const horizontal = d.dir === 'row';
      const total = (horizontal ? d.parent.w : d.parent.h) - gap;
      if (total <= 0) return;
      const pos = horizontal ? e.clientX - box.left - d.parent.x : e.clientY - box.top - d.parent.y;
      const minR = Math.min(0.45, MIN_TILE_PX / total);
      d.node.ratio = clamp((pos - gap / 2) / total, Math.max(T.MIN_RATIO, minR), 1 - Math.max(T.MIN_RATIO, minR));
      layout(false);
    });
    const end = () => {
      if (!dragging) return;
      dragging = false;
      stage.classList.remove('resizing');
      el.classList.remove('active');
      onChange?.();
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('dblclick', () => { el._info.node.ratio = 0.5; layout(true); onChange?.(); });
    el.addEventListener('keydown', (e) => {
      const d = el._info;
      const keys = d.dir === 'row' ? { ArrowLeft: -1, ArrowRight: 1 } : { ArrowUp: -1, ArrowDown: 1 };
      if (!(e.key in keys)) return;
      e.preventDefault();
      e.stopPropagation();
      d.node.ratio = T.clampRatio(d.node.ratio + keys[e.key] * (e.shiftKey ? 0.1 : 0.02));
      layout(false);
      onChange?.();
    });
    return el;
  }

  // ---- focus -------------------------------------------------------------------------------

  function setFocus(id, { dom = false } = {}) {
    if (!entries.has(id)) return;
    const changed = focusedId !== id;
    focusedId = id;
    if (changed) {
      for (const [eid, e] of entries) e.pane.classList.toggle('is-focused', eid === id);
      if (ws.narrow) layout(false); // one window at a time: show the newly focused one
      else onFocus?.(ws);
    }
    if (floating()) {
      const node = entries.get(id).node;
      if (node.win?.min) { node.win.min = false; layout(false); onChange?.(); }
      raise(id);
    }
    if (dom) entries.get(id).pane.focus({ preventScroll: true });
  }
  ws.setFocus = (id, opts) => setFocus(id, opts);

  ws.focusIndex = (i) => {
    const leaf = T.leaves(root)[i];
    if (!leaf) return;
    if (zoomId) zoomId = leaf.id;
    setFocus(leaf.id, { dom: true });
    layout(true);
  };

  ws.focusDir = (dir) => {
    const id = T.neighbor(root, focusedId, dir);
    if (!id) { toast?.('no tile ' + dir); return false; }
    if (zoomId) zoomId = id;
    setFocus(id, { dom: true });
    layout(true);
    return true;
  };

  ws.focusPrimary = () => {
    const e = entries.get(focusedId);
    if (e?.inst.focusPrimary) e.inst.focusPrimary(); else e?.pane.focus();
  };

  // ---- actions -----------------------------------------------------------------------------

  function changed(animate = true) {
    sync();
    layout(animate);
    onChange?.();
  }

  ws.setRoot = (newRoot, focusIndex = 0, newMode = mode) => {
    root = newRoot;
    zoomId = null;
    mode = newMode === 'floating' ? 'floating' : 'tiled';
    sync();
    const leaves = T.leaves(root);
    focusedId = (leaves[clamp(focusIndex, 0, leaves.length - 1)] || leaves[0]).id;
    zOrder = [...leaves.map((n) => n.id).filter((id) => id !== focusedId), focusedId];
    layout(false);
    setFocus(focusedId, { dom: true });
    onChange?.();
  };

  ws.split = (dir, kind) => {
    const current = T.findTile(root, focusedId);
    const res = T.splitTile(root, focusedId, dir, kind || (current.kind === 'chat' ? 'routing' : 'chat'));
    if (!res) {
      toast?.(T.leaves(root).length >= T.MAX_TILES
        ? 'cannot split further (limit ' + T.MAX_TILES + ' tiles)'
        : 'cannot split this tile further (nesting depth limit ' + T.MAX_DEPTH + ')');
      return;
    }
    root = res.root;
    zoomId = null;
    sync();
    focusedId = res.added.id;
    if (floating()) raise(res.added.id);
    layout(true);
    setFocus(focusedId, { dom: true });
    onChange?.();
  };

  ws.closeFocused = () => {
    const res = T.closeTile(root, focusedId);
    if (!res) { toast?.('cannot close the last tile'); return; }
    root = res.root;
    if (zoomId && !T.findTile(root, zoomId)) zoomId = null;
    sync();
    layout(true);
    setFocus(res.focus, { dom: true });
    onChange?.();
  };

  ws.swap = (dir) => {
    if (floating()) { nudge({ left: [-0.04, 0], right: [0.04, 0], up: [0, -0.04], down: [0, 0.04] }[dir]); return; }
    const other = T.neighbor(root, focusedId, dir);
    if (!other) { toast?.('nothing to swap with ' + dir); return; }
    root = T.swapTiles(root, focusedId, other);
    layout(true);
    onChange?.();
  };

  ws.toggleZoom = () => {
    zoomId = zoomId ? null : focusedId;
    layout(true);
    toast?.(zoomId ? 'zoomed' : 'unzoomed');
  };

  ws.setKind = (kind) => {
    const node = T.findTile(root, focusedId);
    if (!node || node.kind === kind) return;
    node.kind = kind;
    node.state = {};
    sync();
    layout(false);
    setFocus(focusedId, { dom: true });
    onChange?.();
  };

  ws.resize = (axis, delta) => {
    if (floating()) { const w = T.findTile(root, focusedId)?.win; if (!w) return; if (axis === 'x') w.w = clamp(w.w + delta, 0.1, 1 - w.x); else w.h = clamp(w.h + delta, 0.1, 1 - w.y); layout(false); onChange?.(); return; }
    if (!T.resizeTile(root, focusedId, axis, delta)) { toast?.('no divider on that axis'); return; }
    layout(false);
    onChange?.();
  };

  ws.equalize = () => {
    if (floating()) { for (const n of T.leaves(root)) delete n.win; changed(true); return; } // back to the default arrangement
    T.equalize(root); changed(true);
  };

  function nudge([dx, dy]) {
    const w = T.findTile(root, focusedId)?.win;
    if (!w) return;
    w.x = clamp(w.x + dx, 0, 1 - w.w);
    w.y = clamp(w.y + dy, 0, 1 - w.h);
    layout(true);
    onChange?.();
  }

  ws.setMode = (next) => {
    const m = next === 'floating' ? 'floating' : 'tiled';
    if (m === mode) return;
    mode = m;
    zoomId = null;
    if (m === 'floating') zOrder = [...T.leaves(root).map((n) => n.id).filter((id) => id !== focusedId), focusedId];
    layout(true);
    onChange?.();
  };

  // Minimise to the dock (floating only); focus moves to the top visible window.
  ws.minimize = (id = focusedId) => {
    if (!floating()) { toast?.('minimise works in floating mode'); return; }
    const node = T.findTile(root, id);
    if (!node?.win) return;
    node.win.min = true;
    if (zoomId === id) zoomId = null;
    const next = [...zOrder].reverse().find((x) => x !== id && !T.findTile(root, x)?.win?.min);
    layout(true);
    onChange?.();
    if (next) setFocus(next, { dom: true }); else stage.focus?.();
  };

  // Bring a window of this kind forward (restoring it if minimised), or open one.
  ws.openKind = (kind) => {
    const existing = T.leaves(root).find((n) => n.kind === kind);
    if (existing) {
      setFocus(existing.id, { dom: true });
      if (zoomId && zoomId !== existing.id) zoomId = existing.id;
      layout(true);
      return;
    }
    ws.split('row', kind);
  };

  ws.windows = () => T.leaves(root).map((n) => ({ id: n.id, kind: n.kind, min: !!(floating() && n.win?.min), focused: n.id === focusedId }));

  ws.serialize = () => ({
    mode,
    tree: T.toJSON(root, (id) => entries.get(id)?.inst.getState?.() || {}),
    focus: Math.max(0, T.leaves(root).findIndex((n) => n.id === focusedId)),
  });

  ws.layoutNow = () => layout(false);
  ws.entries = entries;

  // Keep scroll/focus when the window resizes; no animation while the user drags the window edge.
  new ResizeObserver(() => layout(false)).observe(stage);

  return ws;
}
