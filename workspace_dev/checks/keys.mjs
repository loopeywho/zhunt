// Run: node workspace_dev/checks/keys.mjs   (KEYS_JS=/path/to/old/keys.js to run against another copy)
// Drives createKeys() with a stub document and synthetic key events.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const target = process.env.KEYS_JS || path.join(here, '../../zhunt/workspace/js/keys.js');

class HTMLElement_ {
  constructor(tag = 'DIV', { editable = false, pane = false } = {}) {
    this.tagName = tag; this.type = 'text'; this.isContentEditable = editable;
    this.classList = { contains: (c) => pane && c === 'pane' };
  }
}
const handlers = {};
globalThis.HTMLElement = HTMLElement_;
const body = new HTMLElement_('BODY');
globalThis.document = {
  body, activeElement: body,
  addEventListener: (t, fn) => { handlers[t] = fn; },
};
const { createKeys } = await import(pathToFileURL(target).href);

const log = [];
const ws = new Proxy({}, { get: (_, name) => (...a) => log.push(name + (a.length ? ':' + a.join(',') : '')) });
const state = { open: false, closed: 0 };
const overlays = { get isOpen() { return state.open; }, kind: 'palette', close() { state.closed += 1; state.open = false; } };
const statusbar = { setLeader() {}, setMode() {} };
const cmds = [
  { id: 'a', leader: ['h'], run: () => log.push('leader-h') },
  { id: 'b', leader: ['H'], run: () => log.push('leader-H') },
];
createKeys({
  ws, getCommands: () => cmds, overlays, statusbar, toast: (t) => log.push('toast:' + t),
  openPalette: () => log.push('palette'), openHelp: () => log.push('help'),
});

function press(init, target = body) {
  const e = {
    key: '', code: '', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, isComposing: false, keyCode: 0,
    target, defaultPrevented: false, stopped: false,
    preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; },
    ...init,
  };
  log.length = 0;
  handlers.keydown(e);
  return { e, log: [...log] };
}
let fail = 0;
const check = (name, ok, extra = '') => { if (!ok) fail += 1; console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + extra)); };

const textarea = new HTMLElement_('TEXTAREA');

// F8: Alt chords must not hijack text editing
let r = press({ key: 'ArrowLeft', code: 'ArrowLeft', altKey: true }, textarea);
check('F8 Alt+Arrow in textarea is left to the browser', !r.e.defaultPrevented && r.log.length === 0, JSON.stringify(r.log));
r = press({ key: '˙', code: 'KeyH', altKey: true }, textarea);
check('F8 Alt+h in textarea is left to the browser', !r.e.defaultPrevented && r.log.length === 0, JSON.stringify(r.log));
r = press({ key: '¡', code: 'Digit1', altKey: true }, textarea);
check('F8 Alt+1 in textarea is left to the browser', !r.e.defaultPrevented && r.log.length === 0, JSON.stringify(r.log));
r = press({ key: 'ArrowLeft', code: 'ArrowLeft', altKey: true }, body);
check('F8 Alt+Arrow outside a field still moves focus', r.log.join() === 'focusDir:left', JSON.stringify(r.log));
r = press({ key: '2', code: 'Digit2', altKey: true }, body);
check('F8 Alt+2 outside a field focuses tile 2', r.log.join() === 'focusIndex:1', JSON.stringify(r.log));

// F12: Caps Lock must not turn h into the swap binding
r = press({ key: 'H', code: 'KeyH', shiftKey: false }, body);
check('F12 CapsLock+h (key "H", no shift) moves focus left', r.log.join() === 'focusDir:left', JSON.stringify(r.log));
r = press({ key: 'H', code: 'KeyH', shiftKey: true }, body);
check('F12 Shift+h swaps left', r.log.join() === 'swap:left', JSON.stringify(r.log));
r = press({ key: 'h', code: 'KeyH', shiftKey: true }, body); // CapsLock + Shift reports lowercase
check('F12 CapsLock+Shift+h swaps left', r.log.join() === 'swap:left', JSON.stringify(r.log));
r = press({ key: 'Z', code: 'KeyZ' }, body);
check('F12 CapsLock+z still zooms', r.log.join() === 'toggleZoom', JSON.stringify(r.log));
r = press({ key: 'p', code: 'Space', ctrlKey: true, code: 'Space', key: ' ' }, body); // open the leader
r = press({ key: 'H', code: 'KeyH', shiftKey: false }, body);
check('F12 leader + CapsLock-h runs the "h" binding', r.log.join() === 'leader-h', JSON.stringify(r.log));
press({ key: ' ', code: 'Space', ctrlKey: true }, body);
r = press({ key: 'h', code: 'KeyH', shiftKey: true }, body);
check('F12 leader + Shift+h runs the "H" binding', r.log.join() === 'leader-H', JSON.stringify(r.log));

// F13: Cmd+K opens the palette; leader does not swallow Ctrl/Meta chords
r = press({ key: 'k', code: 'KeyK', metaKey: true }, body);
check('F13 Cmd+K opens the palette', r.log.join() === 'palette' && r.e.defaultPrevented, JSON.stringify(r.log));
press({ key: ' ', code: 'Space', ctrlKey: true }, body); // leader on
r = press({ key: 'r', code: 'KeyR', metaKey: true }, body);
check('F13 leader active: Cmd+R is not swallowed', !r.e.defaultPrevented && !r.e.stopped && r.log.length === 0, JSON.stringify(r.log));
r = press({ key: 'w', code: 'KeyW', ctrlKey: true }, body);
check('F13 leader active: Ctrl+W is not swallowed', !r.e.defaultPrevented && !r.e.stopped && r.log.length === 0, JSON.stringify(r.log));
r = press({ key: 'x', code: 'KeyX' }, body);
check('F13 leader still swallows plain keys', r.e.defaultPrevented, JSON.stringify(r.log));

// F6 (belt and braces): Escape closes an open overlay even when focus is on <body>
state.open = true;
r = press({ key: 'Escape', code: 'Escape' }, body);
check('F6 Escape with an overlay open and focus on body closes it', state.closed === 1, String(state.closed));

// F7: IME composition keystrokes are ignored
r = press({ key: 'h', code: 'KeyH', keyCode: 229 }, body);
check('F7 keyCode 229 is ignored', r.log.length === 0 && !r.e.defaultPrevented, JSON.stringify(r.log));

process.exit(fail ? 1 : 0);
