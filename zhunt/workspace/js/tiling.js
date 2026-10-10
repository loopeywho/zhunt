// The tiling manager: owns the split tree, the pane elements and the dividers.
//
// Panes are absolutely positioned from the computed layout and never re-parented, so splitting,
// closing, swapping or zooming keeps every tile's scroll position, input text and streams alive.

import { h, clamp } from './dom.js';
import * as T from './tree.js';

const NARROW_PX = 640;   // below this only the focused tile is shown (like a zoomed tile)
const MIN_TILE_PX = 110; // dragging a divider cannot squeeze a tile below this

export function createWorkspace(stage, { makeTile, onChange, onFocus, toast }) {
  let root = null;
  let focusedId = null;
  let zoomId = null;
  const entries = new Map();   // id -> {kind, inst, pane, idxEl, kindEl, node}
  const dividerEls = new Map(); // split node -> element
  let animTimer = null;
  let gap = 6;

  const ws = {
    get root() { return root; },
    get focusedId() { return focusedId; },
    get zoomId() { return zoomId; },
    get tileCount() { return entries.size; },
    get narrow() { return stage.clientWidth < NARROW_PX; },
  };

  // ---- structure ---------------------------------------------------------------------------

  function buildPane(node, inst) {
    const idxEl = h('span', { class: 'pane-idx', 'aria-hidden': 'true' });
    const kindEl = h('span', { class: 'pane-kind' }, node.kind);
    const btnZoom = h('button', {
      class: 'pane-btn', type: 'button', 'aria-label': 'Zoom tile', title: 'Zoom tile (leader z)',
      onclick: () => { setFocus(node.id); ws.toggleZoom(); },
    }, 'z');
    const btnClose = h('button', {
      class: 'pane-btn', type: 'button', 'aria-label': 'Close tile', title: 'Close tile (leader x)',
      onclick: () => { setFocus(node.id); ws.closeFocused(); },
    }, 'x');
    const extra = h('div', { class: 'pane-extra' }, inst.header || null);
    const head = h('header', { class: 'pane-head', role: 'toolbar', 'aria-label': 'Tile controls' }, idxEl, kindEl, extra, btnZoom, btnClose);
    const body = h('div', { class: 'pane-body' }, inst.el);
    const pane = h('section', { class: 'pane no-anim', role: 'region', tabindex: '0', dataset: { id: node.id, kind: node.kind } }, head, body);
    pane.addEventListener('focusin', () => setFocus(node.id));
    pane.addEventListener('pointerdown', () => { if (focusedId !== node.id) setFocus(node.id); }, true);
    stage.append(pane);
    return { idxEl, kindEl, pane, btnClose };
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
    const { rects, dividers } = T.computeLayout(root, W, H, gap);
    const leaves = T.leaves(root);
    const full = zoomId && rects.has(zoomId) ? zoomId : (ws.narrow ? focusedId : null);
    leaves.forEach((node, i) => {
      const e = entries.get(node.id);
      if (!e) return;
      const r = rects.get(node.id);
      const isFull = full === node.id;
      const rr = isFull ? { x: 0, y: 0, w: W, h: H } : r;
      const s = e.pane.style;
      s.left = Math.round(rr.x) + 'px';
      s.top = Math.round(rr.y) + 'px';
      s.width = Math.max(0, Math.round(rr.w)) + 'px';
      s.height = Math.max(0, Math.round(rr.h)) + 'px';
      s.zIndex = isFull ? '3' : '1';
      e.pane.classList.toggle('hidden-by-zoom', !!full && !isFull);
      e.pane.classList.toggle('is-focused', node.id === focusedId);
      e.pane.classList.toggle('is-zoomed', isFull);
      e.idxEl.textContent = String(i + 1);
      e.kindEl.textContent = node.kind;
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
      onFocus?.(ws);
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

  ws.setRoot = (newRoot, focusIndex = 0) => {
    root = newRoot;
    zoomId = null;
    sync();
    const leaves = T.leaves(root);
    focusedId = (leaves[clamp(focusIndex, 0, leaves.length - 1)] || leaves[0]).id;
    layout(false);
    setFocus(focusedId, { dom: true });
    onChange?.();
  };

  ws.split = (dir, kind) => {
    const current = T.findTile(root, focusedId);
    const res = T.splitTile(root, focusedId, dir, kind || (current.kind === 'chat' ? 'routing' : 'chat'));
    if (!res) { toast?.('cannot split further (limit ' + T.MAX_TILES + ' tiles)'); return; }
    root = res.root;
    zoomId = null;
    sync();
    focusedId = res.added.id;
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
    if (!T.resizeTile(root, focusedId, axis, delta)) { toast?.('no divider on that axis'); return; }
    layout(false);
    onChange?.();
  };

  ws.equalize = () => { T.equalize(root); changed(true); };

  ws.serialize = () => ({
    tree: T.toJSON(root, (id) => entries.get(id)?.inst.getState?.() || {}),
    focus: Math.max(0, T.leaves(root).findIndex((n) => n.id === focusedId)),
  });

  ws.layoutNow = () => layout(false);
  ws.entries = entries;

  // Keep scroll/focus when the window resizes; no animation while the user drags the window edge.
  new ResizeObserver(() => layout(false)).observe(stage);

  return ws;
}
