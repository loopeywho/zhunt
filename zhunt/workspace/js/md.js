// A deliberately small markdown renderer that builds DOM nodes and never parses HTML.
// Model output is untrusted: every character ends up in a text node, so "<img onerror=...>"
// is displayed as literal text. Links are limited to http(s) and carry rel=noopener.

const INLINE_SRC = /(`+)([\s\S]*?[^`])\1(?!`)|\*\*([^\s*](?:[\s\S]*?[^\s*])?)\*\*|\*([^\s*](?:[^*\n]*?[^\s*])?)\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/;

function renderInline(text, parent, depth = 0) {
  let last = 0;
  // a fresh regex per call: the recursive calls below must not share lastIndex
  const re = new RegExp(INLINE_SRC.source, 'g');
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.append(text.slice(last, m.index));
    if (m[2] !== undefined) {
      const c = document.createElement('code');
      c.textContent = m[2].trim();
      parent.append(c);
    } else if (m[3] !== undefined && depth < 3) {
      const b = document.createElement('strong');
      renderInline(m[3], b, depth + 1);
      parent.append(b);
    } else if (m[4] !== undefined && depth < 3) {
      const i = document.createElement('em');
      renderInline(m[4], i, depth + 1);
      parent.append(i);
    } else if (m[5] !== undefined) {
      const a = document.createElement('a');
      a.textContent = m[5];
      a.href = m[6];
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      parent.append(a);
    } else {
      parent.append(m[0]);
    }
    last = re.lastIndex;
  }
  if (last < text.length) parent.append(text.slice(last));
}

function inlineLines(lines, parent) {
  lines.forEach((ln, i) => {
    if (i > 0) parent.append(document.createElement('br'));
    renderInline(ln, parent);
  });
}

// Replace the children of `target` with the rendered markdown.
export function renderMarkdown(src, target) {
  const frag = document.createDocumentFragment();
  const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  const isBlank = (s) => /^\s*$/.test(s);
  const fence = /^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$/;
  const bullet = /^\s{0,3}[-*+]\s+(.*)$/;
  const ordered = /^\s{0,3}\d{1,9}[.)]\s+(.*)$/;

  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) { i += 1; continue; }

    let m = fence.exec(line);
    if (m) {
      const body = [];
      i += 1;
      // an unclosed fence (mid-stream) just runs to the end
      while (i < lines.length && !(lines[i].trim().startsWith(m[1]) && /^\s*[`~]+\s*$/.test(lines[i]))) { body.push(lines[i]); i += 1; }
      i += 1;
      const pre = document.createElement('pre');
      if (m[2]) pre.dataset.lang = m[2];
      const code = document.createElement('code');
      code.textContent = body.join('\n');
      pre.append(code);
      frag.append(pre);
      continue;
    }

    m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (m) {
      const hd = document.createElement('p');
      hd.className = 'md-h md-h' + m[1].length;
      renderInline(m[2], hd);
      frag.append(hd);
      i += 1;
      continue;
    }

    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      frag.append(document.createElement('hr'));
      i += 1;
      continue;
    }

    if (/^\s{0,3}>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) { body.push(lines[i].replace(/^\s{0,3}>\s?/, '')); i += 1; }
      const q = document.createElement('blockquote');
      inlineLines(body, q);
      frag.append(q);
      continue;
    }

    if (bullet.test(line) || ordered.test(line)) {
      const isOrdered = ordered.test(line) && !bullet.test(line);
      const list = document.createElement(isOrdered ? 'ol' : 'ul');
      const re = isOrdered ? ordered : bullet;
      while (i < lines.length && re.test(lines[i])) {
        const li = document.createElement('li');
        renderInline(re.exec(lines[i])[1], li);
        list.append(li);
        i += 1;
      }
      frag.append(list);
      continue;
    }

    const para = [];
    while (i < lines.length && !isBlank(lines[i]) && !fence.test(lines[i]) && !/^(#{1,6})\s/.test(lines[i])
      && !bullet.test(lines[i]) && !ordered.test(lines[i]) && !/^\s{0,3}>/.test(lines[i])) {
      para.push(lines[i]);
      i += 1;
    }
    const p = document.createElement('p');
    inlineLines(para, p);
    frag.append(p);
  }
  target.replaceChildren(frag);
}
