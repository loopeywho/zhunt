// Themes are CSS files of custom properties. dark/light ship as static files (so the page is themed
// before any API call); the API can add user themes and override built-ins. The fetched CSS is
// applied through a constructed stylesheet, which stays outside the document's <style> rules.

import { api } from './api.js';

export const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,39}$/;
const STORE_KEY = 'zhunt.theme';
const BUILTIN = [{ name: 'dark', builtin: true }, { name: 'light', builtin: true }];

export function createThemes({ toast, onChange }) {
  const root = document.documentElement;
  let list = BUILTIN;
  let sheet = null;
  let fallbackStyle = null;
  let pending = null; // theme being fetched; rapid 'next' presses must advance from it

  function setSheet(css) {
    if (css === null) {
      if (sheet) document.adoptedStyleSheets = document.adoptedStyleSheets.filter((s) => s !== sheet);
      sheet = null;
      fallbackStyle?.remove();
      fallbackStyle = null;
      return;
    }
    try {
      if (!sheet) sheet = new CSSStyleSheet();
      sheet.replaceSync(css);
      if (!document.adoptedStyleSheets.includes(sheet)) document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    } catch {
      if (!fallbackStyle) { fallbackStyle = document.createElement('style'); document.head.append(fallbackStyle); }
      fallbackStyle.textContent = css;
    }
  }

  const themes = {
    get current() { return root.dataset.theme || 'dark'; },
    get list() { return list; },

    async refresh() {
      try {
        const res = await api.themes.list();
        const names = (res.themes || []).filter((t) => NAME_RE.test(t.name));
        for (const b of BUILTIN) if (!names.some((t) => t.name === b.name)) names.unshift(b);
        list = names;
      } catch { list = BUILTIN; }
      return list;
    },

    async apply(name, { persist = true } = {}) {
      if (!NAME_RE.test(name)) return false;
      pending = name;
      const isBuiltin = BUILTIN.some((b) => b.name === name);
      let css = null;
      try { css = await api.themes.css(name); } catch (err) {
        if (!isBuiltin) {
          toast?.(`theme "${name}" is not available (${err.message}); using dark`);
          return themes.apply('dark', { persist: false });
        }
      }
      if (pending !== name) return false; // a newer request superseded this one
      pending = null;
      setSheet(css);
      root.dataset.theme = name;
      if (persist) { try { localStorage.setItem(STORE_KEY, name); } catch { /* storage blocked */ } }
      onChange?.(name);
      return true;
    },

    next() {
      const names = list.map((t) => t.name);
      const i = names.indexOf(pending || themes.current);
      return themes.apply(names[(i + 1) % names.length]);
    },
  };
  return themes;
}
