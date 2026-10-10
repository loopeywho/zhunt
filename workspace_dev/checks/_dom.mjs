// Minimal DOM stub for node checks of pure-logic modules (md.js etc.). Not a real DOM.
class Node_ {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.className = ''; this.attrs = {}; }
  append(...kids) { for (const k of kids) this.children.push(typeof k === 'string' ? { text: k } : k); }
  replaceChildren(...kids) { this.children = []; if (kids.length) this.append(...kids); }
  setAttribute(k, v) { this.attrs[k] = v; }
  get textContent() { return this.children.map((c) => (c.text !== undefined ? c.text : c.textContent)).join(''); }
  set textContent(v) { this.children = [{ text: String(v) }]; }
}
export function installDom() {
  globalThis.document = {
    createElement: (t) => new Node_(t),
    createDocumentFragment: () => new Node_('#frag'),
    createTextNode: (s) => ({ text: String(s) }),
  };
  return globalThis.document;
}
export const countNodes = (n) => 1 + (n.children || []).reduce((a, c) => a + countNodes(c), 0);
export { Node_ };
