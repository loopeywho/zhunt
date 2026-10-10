// Global keyboard handling: leader key, palette shortcut, direct Alt chords, and vim-style
// "normal mode" keys that apply only while a tile (not a text field) has focus.

const MOD_KEYS = new Set(['Shift', 'Control', 'Alt', 'Meta', 'CapsLock', 'AltGraph']);
const LEADER_TIMEOUT_MS = 6000;
const DIR_BY_CODE = { KeyH: 'left', KeyJ: 'down', KeyK: 'up', KeyL: 'right', ArrowLeft: 'left', ArrowDown: 'down', ArrowUp: 'up', ArrowRight: 'right' };
const DIR_BY_KEY = { h: 'left', j: 'down', k: 'up', l: 'right', ArrowLeft: 'left', ArrowDown: 'down', ArrowUp: 'up', ArrowRight: 'right' };

export function isEditable(el) {
  if (!el || !(el instanceof HTMLElement)) return false;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') return !['checkbox', 'radio', 'button', 'submit'].includes(el.type);
  return false;
}

export function createKeys({ ws, getCommands, overlays, openPalette, openHelp, statusbar, toast }) {
  let leader = false;
  let timer = null;

  // Letters are normalised so Shift+j is "J" even if a synthetic event reports key "j" + shiftKey.
  const token = (e) => {
    if (e.key.startsWith('Arrow') && e.shiftKey) return 'Shift+' + e.key;
    if (e.shiftKey && /^[a-z]$/.test(e.key)) return e.key.toUpperCase();
    return e.key;
  };

  function setLeader(on) {
    leader = on;
    clearTimeout(timer);
    if (on) timer = setTimeout(() => setLeader(false), LEADER_TIMEOUT_MS);
    statusbar.setLeader(on, on ? getCommands().filter((c) => c.leader) : []);
    updateMode();
  }

  function updateMode() {
    statusbar.setMode(leader ? 'leader' : isEditable(document.activeElement) ? 'insert' : 'normal');
  }

  function run(cmd) {
    try {
      const r = cmd.run();
      if (r && typeof r.catch === 'function') r.catch((err) => toast(err.message || String(err), true));
    } catch (err) { toast(err.message || String(err), true); }
  }

  function onLeaderKey(e) {
    if (MOD_KEYS.has(e.key)) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === 'Escape' || e.key === ' ') { setLeader(false); return; }
    const t = token(e);
    const cmd = getCommands().find((c) => c.leader && c.leader.includes(t));
    if (!cmd) { toast(`leader: no binding for "${e.key}"  (? lists them)`); setLeader(false); return; }
    if (cmd.sticky) setLeader(true); else setLeader(false);
    run(cmd);
  }

  function onKeyDown(e) {
    if (e.isComposing) return;
    const ctrlOnly = e.ctrlKey && !e.metaKey && !e.altKey;

    if (ctrlOnly && !e.shiftKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (overlays.isOpen && overlays.kind === 'palette') overlays.close(); else { setLeader(false); openPalette(); }
      return;
    }
    if (overlays.isOpen) return;

    if (ctrlOnly && (e.code === 'Space' || e.key === ' ' || e.key === '\\')) {
      e.preventDefault();
      setLeader(!leader);
      return;
    }
    if (leader) { onLeaderKey(e); return; }

    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const dir = DIR_BY_CODE[e.code];
      if (dir) { e.preventDefault(); if (e.shiftKey) ws.swap(dir); else ws.focusDir(dir); return; }
      if (e.code === 'Enter') { e.preventDefault(); ws.toggleZoom(); return; }
      const digit = /^Digit([1-9])$/.exec(e.code);
      if (digit) { e.preventDefault(); ws.focusIndex(Number(digit[1]) - 1); return; }
      return;
    }

    // Normal mode: focus is on a pane itself (or nothing), never inside a field or on a button.
    const t = e.target;
    const atRest = t === document.body || (t instanceof HTMLElement && t.classList.contains('pane'));
    if (!atRest || e.ctrlKey || e.metaKey) return;
    const dir = DIR_BY_KEY[e.key];
    if (dir) { e.preventDefault(); ws.focusDir(dir); return; }
    const swapDir = DIR_BY_KEY[e.key.toLowerCase()];
    if (swapDir && e.key.length === 1 && e.shiftKey) { e.preventDefault(); ws.swap(swapDir); return; }
    if (e.key === 'i' || e.key === 'Enter') { e.preventDefault(); ws.focusPrimary(); return; }
    if (e.key === 'z') { e.preventDefault(); ws.toggleZoom(); return; }
    if (e.key === '?') { e.preventDefault(); openHelp(); return; }
    if (e.key === ':') { e.preventDefault(); openPalette(); }
  }

  document.addEventListener('keydown', onKeyDown);
  document.addEventListener('focusin', updateMode);
  document.addEventListener('focusout', () => setTimeout(updateMode, 0));
  // clicking elsewhere ends a pending leader
  document.addEventListener('pointerdown', () => { if (leader) setLeader(false); });

  return { setLeader, updateMode, get leaderActive() { return leader; } };
}
