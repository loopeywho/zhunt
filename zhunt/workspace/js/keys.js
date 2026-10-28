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

  // The letter a key stands for, independent of Caps Lock (which flips e.key's case). Falls back to the
  // physical key (e.code KeyA..KeyZ) when the layout produces a non-Latin character. Null for non-letters.
  const letterOf = (e) => {
    if (/^[a-zA-Z]$/.test(e.key)) return e.key.toLowerCase();
    const m = /^Key([A-Z])$/.exec(e.code || '');
    return m ? m[1].toLowerCase() : null;
  };

  // Letters are normalised from the letter plus shiftKey, so Shift+j is "J" and Caps Lock + j is still "j".
  const token = (e) => {
    if (e.key.startsWith('Arrow') && e.shiftKey) return 'Shift+' + e.key;
    const l = letterOf(e);
    if (l) return e.shiftKey ? l.toUpperCase() : l;
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
    if (e.ctrlKey || e.metaKey) return; // Cmd+R, Ctrl+W ... still belong to the browser
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
    if (e.isComposing || e.keyCode === 229) return;
    // Terminal programs own their editing/interrupt shortcuts, including Ctrl+K.
    // Focus the window header to use Zhunt's keyboard commands.
    if (e.target instanceof HTMLElement && e.target.closest('.engineering-terminal')) return;
    const ctrlOnly = e.ctrlKey && !e.metaKey && !e.altKey;

    // Ctrl+K or Cmd+K (Ctrl+Space can collide with IME / input-source switching; Ctrl+\ stays as the fallback)
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (overlays.isOpen && overlays.kind === 'palette') overlays.close(); else { setLeader(false); openPalette(); }
      return;
    }
    if (overlays.isOpen) {
      // focus can still end up on <body> (e.g. after a click outside the dialog): Escape must close regardless
      if (e.key === 'Escape') { e.preventDefault(); overlays.close(); }
      return;
    }

    if (ctrlOnly && (e.code === 'Space' || e.key === ' ' || e.key === '\\')) {
      e.preventDefault();
      setLeader(!leader);
      return;
    }
    if (leader) { onLeaderKey(e); return; }

    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      const dir = DIR_BY_CODE[e.code];
      // In a text field Option/Alt+Arrow/letter/digit is word movement or a typed character: leave it alone.
      // (Alt+Enter has no text meaning, so it keeps zooming; chat no longer sends on it.)
      const editing = isEditable(e.target);
      if (dir && !editing) { e.preventDefault(); if (e.shiftKey) ws.swap(dir); else ws.focusDir(dir); return; }
      if (e.code === 'Enter') { e.preventDefault(); ws.toggleZoom(); return; }
      const digit = /^Digit([1-9])$/.exec(e.code);
      if (digit && !editing) { e.preventDefault(); ws.focusIndex(Number(digit[1]) - 1); return; }
      return;
    }

    // Normal mode: focus is on a pane itself (or nothing), never inside a field or on a button.
    const t = e.target;
    const atRest = t === document.body || (t instanceof HTMLElement && t.classList.contains('pane'));
    if (!atRest || e.ctrlKey || e.metaKey) return;
    const l = letterOf(e);
    const dir = DIR_BY_KEY[l || e.key];
    if (dir) { e.preventDefault(); if (l && e.shiftKey) ws.swap(dir); else ws.focusDir(dir); return; }
    if ((l === 'i' && !e.shiftKey) || e.key === 'Enter') { e.preventDefault(); ws.focusPrimary(); return; }
    if (l === 'z' && !e.shiftKey) { e.preventDefault(); ws.toggleZoom(); return; }
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
