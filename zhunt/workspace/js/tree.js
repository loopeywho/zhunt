// Pure binary split-tree operations. No DOM here, so it can be unit-tested in node.
//
//   tile  : { type: 'tile',  id, kind, state }
//   split : { type: 'split', dir: 'row' | 'col', ratio, a, b }
//
// 'row' puts a|b side by side (vertical divider); 'col' stacks a over b.
// Operations mutate the tree in place and return the (possibly new) root.

export const KINDS = ['chat', 'compare', 'routing', 'providers', 'logs'];
export const MAX_TILES = 12;
export const MAX_DEPTH = 8;
export const MIN_RATIO = 0.08;
export const MAX_RATIO = 0.92;

let counter = 0;
export function newId() {
  counter += 1;
  return 't' + counter;
}

export function tile(kind, state = {}) {
  return { type: 'tile', id: newId(), kind, state };
}

export function split(dir, a, b, ratio = 0.5) {
  return { type: 'split', dir, ratio, a, b };
}

export const isTile = (n) => !!n && n.type === 'tile';

export function leaves(node, out = []) {
  if (isTile(node)) out.push(node);
  else { leaves(node.a, out); leaves(node.b, out); }
  return out;
}

export function findTile(root, id) {
  return leaves(root).find((t) => t.id === id) || null;
}

export function parentOf(root, node) {
  if (root === node || isTile(root)) return null;
  if (root.a === node || root.b === node) return root;
  return parentOf(root.a, node) || parentOf(root.b, node);
}

function replace(root, target, replacement) {
  if (root === target) return replacement;
  const p = parentOf(root, target);
  if (p) { if (p.a === target) p.a = replacement; else p.b = replacement; }
  return root;
}

export function depthOf(root, node, d = 0) {
  if (root === node) return d;
  if (isTile(root)) return -1;
  const x = depthOf(root.a, node, d + 1);
  return x >= 0 ? x : depthOf(root.b, node, d + 1);
}

// Split tile `id`; the new tile goes second (right / below). Returns {root, added} or null if refused.
export function splitTile(root, id, dir, kind, state = {}) {
  const target = findTile(root, id);
  if (!target) return null;
  if (leaves(root).length >= MAX_TILES) return null;
  if (depthOf(root, target) >= MAX_DEPTH) return null;
  const added = tile(kind, state);
  const node = split(dir, target, added, 0.5);
  return { root: replace(root, target, node), added };
}

// Close tile `id`. Returns {root, focus} where focus is the id that should get focus, or null if
// it is the last tile.
export function closeTile(root, id) {
  const target = findTile(root, id);
  if (!target || isTile(root)) return null;
  const p = parentOf(root, target);
  const sibling = p.a === target ? p.b : p.a;
  const newRoot = replace(root, p, sibling);
  return { root: newRoot, focus: leaves(sibling)[0].id };
}

// Exchange the positions of two tiles.
export function swapTiles(root, idA, idB) {
  const a = findTile(root, idA);
  const b = findTile(root, idB);
  if (!a || !b || a === b) return root;
  const pa = parentOf(root, a);
  const pb = parentOf(root, b);
  const aSide = pa.a === a ? 'a' : 'b';
  const bSide = pb.a === b ? 'a' : 'b';
  pa[aSide] = b;
  pb[bSide] = a;
  return root;
}

export const clampRatio = (r) => Math.min(MAX_RATIO, Math.max(MIN_RATIO, r));

// Lay the tree out in a W x H box with `gap` between siblings.
// Returns { rects: Map(id -> {x,y,w,h}), dividers: [{node, dir, rect, parent}] }.
export function computeLayout(root, W, H, gap) {
  const rects = new Map();
  const dividers = [];
  (function walk(node, r) {
    if (isTile(node)) { rects.set(node.id, r); return; }
    if (node.dir === 'row') {
      const avail = Math.max(0, r.w - gap);
      const wa = avail * node.ratio;
      walk(node.a, { x: r.x, y: r.y, w: wa, h: r.h });
      dividers.push({ node, dir: 'row', rect: { x: r.x + wa, y: r.y, w: gap, h: r.h }, parent: r });
      walk(node.b, { x: r.x + wa + gap, y: r.y, w: avail - wa, h: r.h });
    } else {
      const avail = Math.max(0, r.h - gap);
      const ha = avail * node.ratio;
      walk(node.a, { x: r.x, y: r.y, w: r.w, h: ha });
      dividers.push({ node, dir: 'col', rect: { x: r.x, y: r.y + ha, w: r.w, h: gap }, parent: r });
      walk(node.b, { x: r.x, y: r.y + ha + gap, w: r.w, h: avail - ha });
    }
  })(root, { x: 0, y: 0, w: W, h: H });
  return { rects, dividers };
}

// Nearest tile in direction dir ('left'|'right'|'up'|'down'), judged on a unit layout. null if none.
export function neighbor(root, id, dir) {
  const { rects } = computeLayout(root, 1000, 1000, 0);
  const r = rects.get(id);
  if (!r) return null;
  const eps = 0.5;
  let best = null;
  for (const [oid, o] of rects) {
    if (oid === id) continue;
    let dist; let overlap;
    if (dir === 'left' || dir === 'right') {
      dist = dir === 'left' ? r.x - (o.x + o.w) : o.x - (r.x + r.w);
      overlap = Math.min(r.y + r.h, o.y + o.h) - Math.max(r.y, o.y);
    } else {
      dist = dir === 'up' ? r.y - (o.y + o.h) : o.y - (r.y + r.h);
      overlap = Math.min(r.x + r.w, o.x + o.w) - Math.max(r.x, o.x);
    }
    if (dist < -eps || overlap <= eps) continue;
    if (!best || dist < best.dist - eps || (Math.abs(dist - best.dist) <= eps && overlap > best.overlap)) {
      best = { id: oid, dist, overlap };
    }
  }
  return best ? best.id : null;
}

// Grow (delta > 0) or shrink the tile along an axis ('x' | 'y') by moving the nearest matching divider.
export function resizeTile(root, id, axis, delta) {
  const want = axis === 'x' ? 'row' : 'col';
  let node = findTile(root, id);
  while (node) {
    const p = parentOf(root, node);
    if (!p) return false;
    if (p.dir === want) {
      p.ratio = clampRatio(p.ratio + (p.a === node ? delta : -delta));
      return true;
    }
    node = p;
  }
  return false;
}

// Reset every ratio so that tiles in a same-direction chain get equal shares.
export function equalize(node) {
  if (isTile(node)) return;
  const count = (n, dir) => (isTile(n) || n.dir !== dir ? 1 : count(n.a, dir) + count(n.b, dir));
  const ca = count(node.a, node.dir);
  const cb = count(node.b, node.dir);
  node.ratio = ca / (ca + cb);
  equalize(node.a);
  equalize(node.b);
}

// Build a clean tree from untrusted JSON (a saved layout). Fresh ids, known kinds only.
// Returns null when nothing usable is found.
export function sanitize(raw) {
  let budget = MAX_TILES;
  function walk(n, depth) {
    if (!n || typeof n !== 'object' || depth > MAX_DEPTH) return null;
    if (n.type === 'tile') {
      if (budget <= 0 || !KINDS.includes(n.kind)) return null;
      budget -= 1;
      let state = {};
      if (n.state && typeof n.state === 'object' && !Array.isArray(n.state)) {
        try {
          const s = JSON.stringify(n.state);
          if (s.length <= 4096) state = JSON.parse(s);
        } catch { /* ignore */ }
      }
      return tile(n.kind, state);
    }
    if (n.type === 'split' && (n.dir === 'row' || n.dir === 'col')) {
      const a = walk(n.a, depth + 1);
      const b = walk(n.b, depth + 1);
      if (a && b) {
        const ratio = typeof n.ratio === 'number' && isFinite(n.ratio) ? clampRatio(n.ratio) : 0.5;
        return split(n.dir, a, b, ratio);
      }
      return a || b; // collapse a half-broken split
    }
    return null;
  }
  return walk(raw, 0);
}

// Plain-JSON copy of the tree for saving. `stateOf(id)` supplies live tile state.
export function toJSON(node, stateOf) {
  if (isTile(node)) {
    return { type: 'tile', id: node.id, kind: node.kind, state: stateOf ? stateOf(node.id) : node.state };
  }
  return {
    type: 'split',
    dir: node.dir,
    ratio: Math.round(node.ratio * 1000) / 1000,
    a: toJSON(node.a, stateOf),
    b: toJSON(node.b, stateOf),
  };
}
